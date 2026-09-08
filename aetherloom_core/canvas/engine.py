"""Dependency scheduler over RhExecutionService; never performs its own RH polling."""

import copy
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from PyQt5 import QtCore

from . import model


TERMINAL = frozenset({'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN', 'BLOCKED', 'SKIPPED', 'INTERRUPTED'})
ACTIVE = frozenset({'SUBMITTING', 'LOCAL_WAIT', 'QUEUED', 'RUNNING', 'DOWNLOADING',
                    'DOWNLOAD_FAILED', 'POLL_TIMEOUT', 'WAITING_FOR_KEY',
                    'WAITING_FOR_SECRET', 'CANCEL_FAILED', 'CANCELING'})
RUNTIME_FIELDS = ('results', 'result_signatures', 'fingerprint', 'status', 'progress', 'node_progress',
                  'message', 'generation', 'cached', 'stale', '_restored_missing_results',
                  '_restored_positions_ambiguous', 'activated')


class MissingHistoricalInput(ValueError):
    """Only this local branch is omitted; completed upstream tasks stay complete."""


class DetachedExecution(RuntimeError):
    """A deleted/replaced workflow no longer owns its old scheduling worker."""


class CanvasEngine(QtCore.QObject):
    changed = QtCore.pyqtSignal(dict)
    queue_changed = QtCore.pyqtSignal(str)

    def __init__(self, service, prepare_app, store, parent=None):
        super().__init__(parent)
        self.service, self.prepare_app, self.store = service, prepare_app, store
        self._condition = threading.Condition(threading.RLock())
        self._documents = {}
        self._persisted_documents = {}
        self._workflow_tokens = {}
        self._forgotten = set()
        self._worker_round = threading.local()
        self._halt_cancellations = set()
        self._active = {}
        self._closing = threading.Event()
        self._last_view_notification = {}
        self._view_canvas = None
        self._unsubscribe = service.subscribe(self._on_record)
        previous_password_lookup = getattr(service, 'restore_password', None)
        def restore_password(origin):
            if origin.get('canvas_id'):
                return self.store.password_for(origin)
            return previous_password_lookup(origin) if callable(previous_password_lookup) else ''
        service.restore_password = restore_password
        # Shared task recovery remains global, but constructing the editor must
        # not load any canvas snapshot or restore unrequested downstream work.

    def _document_locked(self, canvas_id):
        document = self._documents.get(canvas_id)
        worker_round = getattr(self._worker_round, 'value', None)
        if document is None or (worker_round is not None
                                and document.get('run', {}).get('id') != worker_round):
            raise DetachedExecution('画布配置已删除或重新打开，旧执行停止后续提交')
        return document

    def _worker_call(self, callback, round_id, *args):
        self._worker_round.value = round_id
        try:
            return callback(*args)
        finally:
            self._worker_round.value = None

    def _runtime_document(self, document):
        """Use committed configuration while a GUI edit is awaiting autosave."""
        baseline = self._persisted_documents.get(document['id'])
        if baseline is None:
            return copy.deepcopy(document)
        result = copy.deepcopy(baseline)
        result['run'] = copy.deepcopy(document.get('run', {}))
        runtime_nodes = {node['id']: node for node in document.get('nodes', [])}
        states = result['run'].get('nodes', {})
        for node in result.get('nodes', []):
            values = runtime_nodes.get(node['id']) or states.get(node['id'], {})
            for key in RUNTIME_FIELDS:
                if key in values:
                    node[key] = copy.deepcopy(values[key])
        return result

    def _publish_locked(self, document, *, save=True, force_notify=False):
        # Synchronous subscriber completion is a durability barrier. Errors must
        # reach the execution service so it retains the task recovery record.
        if save:
            if document['id'] in self._forgotten:
                return False
            if self.store.save_runtime(self._runtime_document(document)) is None:
                self.forget_deleted(document['id'])
                return False
        self.queue_changed.emit(document['id'])
        now = time.monotonic()
        if (self._view_canvas is None or self._view_canvas == document['id']) and (
                save or force_notify or document.get('run', {}).get('status') in TERMINAL
                or now - self._last_view_notification.get(document['id'], 0) >= 0.1):
            self._last_view_notification[document['id']] = now
            self.changed.emit(copy.deepcopy(document))
        self._condition.notify_all()
        return True

    def update_document(self, document):
        with self._condition:
            previous = self._documents.get(document['id'])
            updated = copy.deepcopy(document)
            if previous:
                updated['run'] = copy.deepcopy(previous.get('run', {}))
                previous_nodes = {n['id']: n for n in previous['nodes']}
                snapshot_nodes = {n['id']: n for n in previous.get('run', {}).get('snapshot', {}).get('nodes', [])}
                for node in updated['nodes']:
                    before = previous_nodes.get(node['id'])
                    if before:
                        for key in RUNTIME_FIELDS:
                            if key in before:
                                node[key] = copy.deepcopy(before[key])
                    frozen = snapshot_nodes.get(node['id'])
                    if frozen and any(node.get(key) != frozen.get(key) for key in
                                      ('params', 'app', 'decode_settings')):
                        node['stale'] = True
                if updated.get('edges') != previous.get('edges'):
                    for node in updated['nodes']:
                        node['stale'] = True
            self._documents[updated['id']] = updated
            return copy.deepcopy(updated)

    def attach(self, document):
        """Restore editor/results; recover_pending starts the trusted local round."""
        document = self.store.normalize_session(copy.deepcopy(document))
        canvas_id = document['id']
        token = self.store.workflow_token(canvas_id)
        with self._condition:
            previous = self._documents.get(canvas_id)
            replaced = previous is not None and (
                self._workflow_tokens.get(canvas_id) != token
                or (previous.get('run') or {}).get('id') != (document.get('run') or {}).get('id'))
        if replaced:
            self.forget_deleted(canvas_id)
        self._restore_terminal_records(document)
        with self._condition:
            if canvas_id in self._active and not replaced:
                return self.update_document(document)
            self._documents[canvas_id] = copy.deepcopy(document)
            self._persisted_documents[canvas_id] = copy.deepcopy(document)
            self._workflow_tokens[canvas_id] = token
            self._forgotten.discard(canvas_id)
            current = self._document_locked(canvas_id)
            run = current.get('run') or {}
            if run and run.get('status') not in TERMINAL and not run.get('session_ended'):
                run['status'] = 'PAUSED'
            records = []
            for state in run.get('nodes', {}).values():
                for item in state.get('items', []):
                    record = self.service.get(item.get('run_id')) if item.get('run_id') else None
                    if record:
                        records.append(record)
                    elif item.get('status') == 'SUBMITTING' and not item.get('task_id'):
                        item.update(status='UNKNOWN', message='提交结果未知；请先核对 RunningHub 任务，避免重复计费')
                        state.update(status='UNKNOWN', message=item['message'])
                    elif (not item.get('task_id') and item.get('status') in ('LOCAL_WAIT', 'PAUSED')
                          and not record):
                        item['status'] = 'PENDING'
            self._publish_locked(current, save=bool(run))
        for record in records:
            self._on_record(record)
        with self._condition:
            return copy.deepcopy(self._document_locked(canvas_id))

    def document(self, canvas_id):
        with self._condition:
            if canvas_id in self._documents:
                return copy.deepcopy(self._document_locked(canvas_id))
        # Explicit callers may reopen an evicted, finished canvas on demand.
        return self.attach(self.store.load(canvas_id))

    def set_view_canvas(self, canvas_id):
        """Only the selected editor needs the complete graph notification."""
        self._view_canvas = str(canvas_id) if canvas_id is not None else None

    def queue_state(self, canvas_id):
        """Small scheduler projection, without graph inputs/results/snapshots."""
        with self._condition:
            run = self._document_locked(canvas_id).get('run') or {}
            value = {key: run.get(key) for key in ('id', 'status', 'message', 'has_failure')}
            value['nodes'] = {node_id: dict(
                {key: state[key] for key in ('status', 'progress', 'activated', 'cached', 'message',
                                            '_halt_message') if key in state},
                items=[{key: item[key] for key in ('run_id', 'task_id', 'status', 'task_document') if key in item}
                       for item in state.get('items', [])]) for node_id, state in run.get('nodes', {}).items()}
            for node_id, state in run.get('nodes', {}).items():
                if state.get('cached'):
                    value['nodes'][node_id]['reused_app_tasks'] = [
                        {key: item[key] for key in ('run_id', 'task_id', 'task_document') if item.get(key)}
                        for item in run.get('cache', {}).get(node_id, {}).get('items', [])]
                    value['nodes'][node_id]['result_references'] = [
                        {key: result[key] for key in ('path', 'url', 'type', 'kind', 'index', 'task_id', 'generation')
                         if key in result} for result in state.get('results', [])]
            return {'id': canvas_id, 'run': value}

    def release_document(self, canvas_id):
        """Evict a durable idle graph without deleting or detaching its files."""
        with self._condition:
            if canvas_id in self._active:
                return False
            document = self._documents.get(canvas_id)
            if document is None:
                return True
            run = document.get('run') or {}
            if run and (run.get('status') not in TERMINAL or any(
                    item.get('status') not in TERMINAL for state in run.get('nodes', {}).values()
                    for item in state.get('items', []))):
                return False
            if canvas_id not in self._workflow_tokens:
                return False  # No confirmed saved configuration to reload.
            baseline = self._persisted_documents.get(canvas_id) or {}
            def settings(value):
                return ({key: value.get(key) for key in ('name', 'edges', 'view', 'batch_count')},
                        [{key: item for key, item in node.items() if key not in model.RUNTIME_FIELDS}
                         for node in value.get('nodes', [])])
            if settings(document) != settings(baseline):
                return False  # Never discard an edit waiting for its autosave.
            ids = {item.get('run_id') for state in run.get('nodes', {}).values() for item in state.get('items', [])}
            self._halt_cancellations.difference_update(ids)
            self._documents.pop(canvas_id, None)
            self._persisted_documents.pop(canvas_id, None)
            self._workflow_tokens.pop(canvas_id, None)
            self._last_view_notification.pop(canvas_id, None)
            return True

    def save_document(self, document, *, explicit=False):
        with self._condition:
            canvas_id = document['id']
            if canvas_id in self._forgotten and not explicit:
                raise FileNotFoundError('画布文件已删除，自动保存已停止')
            self.update_document(document)
            current = self._document_locked(canvas_id)
            previous_token = self._workflow_tokens.get(canvas_id)
            missing = self.store.workflow_token(canvas_id) is None
            if explicit and missing:
                previous_token = None
            path = self.store.save_pair(current, expected_token=previous_token,
                recreate=explicit or previous_token is None and canvas_id not in self._forgotten)
            self._persisted_documents[canvas_id] = copy.deepcopy(current)
            self._workflow_tokens[canvas_id] = self.store.workflow_token(canvas_id)
            self._forgotten.discard(canvas_id)
            return path

    def _restore_terminal_records(self, document):
        restore = getattr(self.service, 'restore_record', None)
        if not callable(restore):
            return
        run = document.get('run') or {}
        nodes = {node['id']: node for node in run.get('snapshot', {}).get('nodes', document.get('nodes', []))}
        restored_ids = set()
        states = [*run.get('nodes', {}).items(), *run.get('cache', {}).items()]
        for node_id, state in states:
            node = nodes.get(node_id) or {}
            app = node.get('app') or {}
            if node.get('kind') != 'app':
                continue
            for item in state.get('items', []):
                if (item.get('status') != 'SUCCESS'
                        or not item.get('run_id') or item['run_id'] in restored_ids):
                    continue
                restored_ids.add(item['run_id'])
                existing = self.service.get(item['run_id'])
                if existing and existing.get('status') in TERMINAL:
                    continue
                snapshot = copy.deepcopy(item.get('snapshot') or {})
                webapp_id = snapshot.get('webapp_id') or app.get('webapp_id')
                if not webapp_id:
                    continue
                origin = copy.deepcopy(item.get('origin') or snapshot.get('origin')) or {
                          'canvas_id': document['id'], 'canvas_name': document.get('name', ''),
                          'node_id': node_id, 'node_title': node.get('title', 'App'),
                          'round_id': run['id'], 'execution_id': run['id'],
                          'canvas_batch_index': run.get('batch_index', 0),
                          'batch_index': item.get('batch_index', 0), 'repeat_index': item.get('repeat_index', 0)}
                restore(dict(run_id=item['run_id'], task_id=item.get('task_id'), status=item['status'],
                             task_document=item.get('task_document', ''),
                             webapp_id=webapp_id, app_name=snapshot.get('app_name') or app.get('name') or node.get('title', webapp_id),
                             origin=origin, snapshot=snapshot, results=copy.deepcopy(item.get('results', [])),
                             input_files=copy.deepcopy(item.get('input_files', [])),
                             output_files=copy.deepcopy(item.get('output_files', [])),
                             progress=item.get('progress', 0), message=item.get('message', ''),
                             created_at=item.get('created_at', run.get('created_at', 0)),
                             updated_at=item.get('updated_at', run.get('updated_at', run.get('created_at', 0)))))

    def recover_pending(self, selected=None):
        """Restore selected output references; only shared downloads can resume."""
        errors = []
        if selected is None:
            return errors
        canvas_id = selected.get('id') if isinstance(selected, dict) else str(selected)
        try:
            document = copy.deepcopy(selected) if isinstance(selected, dict) else self.store.load(canvas_id)
            restored = self.attach(document)
        except Exception as error:
            errors.append({'canvas_id': canvas_id, 'name': str(canvas_id), 'message': str(error)})
        return errors

    def is_running(self, canvas_id):
        with self._condition:
            return canvas_id in self._active

    def capture_prepared(self, document, target=None, prepare_app=None):
        """Freeze App options on the GUI thread when a workflow is enqueued."""
        order = model.validate_document(document)
        scope = model.ancestors(document, target) if target else set(order)
        captured = {}
        prepare = prepare_app or self.prepare_app
        for node in document['nodes']:
            if node['id'] not in scope or node.get('kind') != 'app':
                continue
            try:
                captured[node['id']] = copy.deepcopy(prepare(copy.deepcopy(node), model.canonical_fields(node)))
            except Exception as error:
                captured[node['id']] = {'_preparation_error': str(error)}
        return captured

    def start(self, document, target=None, force=False, resume=False, automatic=False, batch_count=None,
              execution_snapshot=None, prepared_snapshot=None, round_id=None, queue_origin=None):
        """Capture widget-derived credentials/options now, before any worker exists."""
        canvas_id = document['id']
        with self._condition:
            if canvas_id in self._active:
                raise ValueError('此画布已有一轮执行，请等待完成或停止')
            previous = copy.deepcopy(self._documents.get(canvas_id, document))
        current = copy.deepcopy(document)
        if previous.get('run'):
            current['run'] = previous['run']
        if resume:
            run = current.get('run') or {}
            if run.get('session_ended') or run.get('session_id') != self.store.session_id:
                raise ValueError('上次客户端会话已结束；请重新运行画布，已生成结果仅继续下载')
            if not run.get('snapshot'):
                raise ValueError('没有可继续的画布执行')
            if run.get('user_stopped') or automatic and run.get('status') in TERMINAL:
                return run['id']
            graph = copy.deepcopy(run['snapshot'])
            order = model.validate_document(graph)
            scope = set(run.get('scope') or order)
            round_id = run['id']
            force = bool(run.get('force', False))
            if queue_origin:
                run['workflow_queue'] = copy.deepcopy(queue_origin)
            run.setdefault('batch_count', 1)
            run.setdefault('batch_index', 0)
        else:
            # An accepted but unfinished cloud task may still be recovering while
            # the editor is paused. Do not replace its durable association.
            for state in current.get('run', {}).get('nodes', {}).values():
                if any(item.get('task_id') and item.get('status') not in TERMINAL
                       for item in state.get('items', [])):
                    raise ValueError('此画布仍有未完成任务，正在自动恢复，请等待完成或停止')
            source = execution_snapshot if execution_snapshot is not None else current
            if source.get('id') != canvas_id:
                raise ValueError('排队快照与画布标识不一致')
            order = model.validate_document(source)
            scope = model.ancestors(source, target) if target else set(order)
            count = model.normalize_batch_count(1 if target else
                source.get('batch_count', 1) if batch_count is None else batch_count)
            graph = {key: copy.deepcopy(source.get(key)) for key in ('version', 'id', 'name', 'nodes', 'edges', 'view')}
            graph['batch_count'] = source.get('batch_count', 1)
            round_id = str(round_id or uuid.uuid4().hex)
            run = {'id': round_id, 'status': 'RUNNING', 'created_at': time.time(),
                   'session_id': self.store.session_id,
                   'scope': [node_id for node_id in order if node_id in scope],
                   'force': bool(force), 'snapshot': graph, 'target': target,
                   'batch_count': count, 'batch_index': 0,
                   'nodes': {node_id: {'status': 'PENDING', 'activated': False, 'results': [], 'items': []}
                             for node_id in order if node_id in scope}}
            if queue_origin:
                run['workflow_queue'] = copy.deepcopy(queue_origin)
            node_ids = {node['id'] for node in graph['nodes']}
            run['cache'] = {key: copy.deepcopy(value) for key, value in previous.get('run', {}).get('cache', {}).items()
                            if key in node_ids}
            for node_id, state in previous.get('run', {}).get('nodes', {}).items():
                if node_id in node_ids and state.get('status') == 'SUCCESS' and not state.get('cached'):
                    cached_state = copy.deepcopy(state)
                    for item in cached_state.get('items', []):
                        item.setdefault('origin', copy.deepcopy((item.get('snapshot') or {}).get('origin')) or
                            {'canvas_id': canvas_id, 'node_id': node_id, 'round_id': previous['run']['id'],
                             'execution_id': previous['run']['id'], 'canvas_batch_index': previous['run'].get('batch_index', 0),
                             'batch_index': item.get('batch_index', 0), 'repeat_index': item.get('repeat_index', 0)})
                    run['cache'][node_id] = cached_state
            current['run'] = run
            for current_node in current['nodes']:
                if current_node['id'] in scope:
                    current_node.update(status='PENDING', activated=False, progress=0, message='等待依赖', stale=False, cached=False)
                    current_node.pop('_restored_missing_results', None)
                    current_node.pop('_restored_positions_ambiguous', None)
            for frozen_node in graph['nodes']:
                frozen_node.pop('_restored_missing_results', None)
                frozen_node.pop('_restored_positions_ambiguous', None)
        nodes = {node['id']: node for node in graph['nodes']}
        prepared = {}
        for node_id in order:
            if node_id not in scope or nodes[node_id]['kind'] != 'app':
                continue
            state = run['nodes'][node_id]
            if (resume and state.get('status') == 'SUCCESS'
                    and run.get('batch_index', 0) + 1 >= run.get('batch_count', 1)):
                continue
            # Deliberately executed in start(), hence on the GUI thread. This is
            # the only boundary permitted to read owner/UI configuration.
            try:
                prepare_node = copy.deepcopy(nodes[node_id])
                frozen_options = run.get('prepared', {}).get(node_id, {}) if resume else {}
                if frozen_options.get('base_url'):
                    # Old nodes may not store their site. Capture current keys
                    # for the round's frozen site before overlaying public
                    # settings; the currently selected site may have changed.
                    prepare_node.setdefault('app', {})['base_url'] = frozen_options['base_url']
                    wid = prepare_node['app'].get('webapp_id')
                    prepare_node['app']['url'] = str(frozen_options['base_url']).rstrip('/') + '/webapp/' + str(wid)
                    prepare_node['app'].pop('url_error', None)
                if prepared_snapshot is not None:
                    captured = copy.deepcopy(prepared_snapshot.get(node_id, {}))
                else:
                    captured = copy.deepcopy(self.prepare_app(
                        prepare_node, model.canonical_fields(nodes[node_id])))
                if resume and node_id in run.get('prepared', {}):
                    private_password = (captured.get('decode_settings') or {}).get('password')
                    captured.update(copy.deepcopy(run['prepared'][node_id]))
                    if private_password is not None:
                        captured.setdefault('decode_settings', {})['password'] = private_password
                prepared[node_id] = captured
                if not resume:
                    from aetherloom_core.rh_execution import public_snapshot
                    frozen = public_snapshot(captured)
                    if captured.get('input_dir'):
                        frozen['input_dir'] = str(captured['input_dir'])
                    # Private local password storage is separate from credentials
                    # and supports an immutable decoder configuration on restart.
                    password = (captured.get('decode_settings') or {}).get('password')
                    if password is not None:
                        frozen.setdefault('decode_settings', {})['password'] = password
                    run.setdefault('prepared', {})[node_id] = frozen
            except Exception as error:
                prepared[node_id] = copy.deepcopy(run.get('prepared', {}).get(node_id, {}))
                prepared[node_id]['_preparation_error'] = str(error)
        if resume:
            for node_id, state in run['nodes'].items():
                state.setdefault('activated', state.get('status') != 'PENDING' or bool(state.get('items')))
                if state.get('status') == 'SUCCESS':
                    state['_verify_resume'] = True
                    continue
                if any(item.get('status') == 'UNKNOWN' for item in state.get('items', [])):
                    state.update(status='UNKNOWN', message='存在提交结果未知的任务，请先核对服务端记录')
                elif state.get('status') not in TERMINAL:
                    state['status'] = 'PENDING'
            run['status'] = 'RUNNING'
        stop = threading.Event()
        with self._condition:
            if self._closing.is_set():
                raise ValueError('客户端正在关闭')
            if execution_snapshot is not None and (
                    canvas_id in self._forgotten or self.store.workflow_token(canvas_id) is None
                    or self.store.workflow_token(canvas_id) != self._workflow_tokens.get(canvas_id)):
                raise ValueError('画布 JSON 已被删除或外部修改，排队任务不会覆盖它')
            self._documents[canvas_id] = current
            self._active[canvas_id] = {'stop': stop, 'round_id': round_id}
            try:
                if not resume and execution_snapshot is None:
                    previous_token = self._workflow_tokens.get(canvas_id)
                    if self.store.workflow_token(canvas_id) is None:
                        previous_token = None
                    self.store.save_pair(current, expected_token=previous_token, recreate=True)
                    self._persisted_documents[canvas_id] = copy.deepcopy(current)
                    self._workflow_tokens[canvas_id] = self.store.workflow_token(canvas_id)
                    self._forgotten.discard(canvas_id)
                self._publish_locked(current)
            except Exception:
                self._active.pop(canvas_id, None)
                raise
        thread = threading.Thread(target=self._worker_call,
                                  args=(self._schedule, round_id, canvas_id, round_id, graph, scope, prepared, bool(force), stop),
                                  name='canvas-' + canvas_id[:8], daemon=True)
        thread.start()
        return round_id

    def _halt_branch(self, document, node_id, status, message):
        """Stop the failure branch immediately, before RH releases its queue gate."""
        run = document['run']
        run['has_failure'] = True
        descendants, pending = set(), [node_id]
        edges = run.get('snapshot', {}).get('edges', [])
        while pending:
            current = pending.pop()
            if current in descendants:
                continue
            descendants.add(current)
            pending.extend(edge['target'] for edge in edges if edge['source'] == current)
        for affected in descendants:
            state = run.get('nodes', {}).get(affected)
            if state is None:
                continue
            halt_status = state.get('_halt_status') or (status if affected == node_id else 'BLOCKED')
            halt_message = state.get('_halt_message') or (message if affected == node_id else '上游未成功，已停止本分支')
            state.update(_halt_status=halt_status, _halt_message=halt_message, status=halt_status, message=halt_message)
            for item in state.get('items', []):
                if item.get('status') == 'PENDING' and not item.get('task_id'):
                    item.update(status='BLOCKED', message='本分支已停止，未提交')
                elif item.get('status') in ACTIVE and item.get('run_id'):
                    item['cancel_requested'] = True
            node = next((n for n in document['nodes'] if n['id'] == affected), None)
            if node:
                for key in RUNTIME_FIELDS:
                    if key in state:
                        node[key] = copy.deepcopy(state[key])

    def _cancel_halted(self, document):
        with self._condition:
            ids = {item['run_id'] for state in document.get('run', {}).get('nodes', {}).values()
                   for item in state.get('items', []) if item.get('cancel_requested') and item.get('run_id')}
            pending = ids - self._halt_cancellations
            self._halt_cancellations.update(pending)
        for run_id in pending:
            self.service.cancel(run_id)

    def _set_state(self, canvas_id, node_id, **values):
        with self._condition:
            document = self._document_locked(canvas_id)
            state = document['run']['nodes'][node_id]
            if state.get('_halt_status') and values.get('status') not in {'FAILED', 'UNKNOWN', 'CANCELED', 'BLOCKED'}:
                return
            state.update(values)
            if values.get('status') in {'FAILED', 'UNKNOWN', 'CANCELED'}:
                self._halt_branch(document, node_id, values['status'], values.get('message', '任务未成功'))
            elif values.get('status') == 'SUCCESS' and not state.get('cached'):
                document['run'].setdefault('cache', {})[node_id] = copy.deepcopy(state)
            node = next((n for n in document['nodes'] if n['id'] == node_id), None)
            if node:
                for key in RUNTIME_FIELDS:
                    if key in values:
                        node[key] = copy.deepcopy(values[key])
            self._publish_locked(document, save=values.get('status') not in {'PREPARING', 'RUNNING'})
        self._cancel_halted(document)

    def _advance_batch(self, document, graph, order):
        """Commit the next complete DAG batch, retaining only latest node caches."""
        run = document['run']
        run['batch_index'] = int(run.get('batch_index', 0)) + 1
        for node in graph['nodes']:
            state = run['nodes'].get(node['id'])
            if state:
                for key in RUNTIME_FIELDS:
                    if key in state:
                        node[key] = copy.deepcopy(state[key])
        run['snapshot'] = copy.deepcopy(graph)
        run['nodes'] = {node_id: {'status': 'PENDING', 'activated': False, 'results': [], 'items': []}
                        for node_id in order}
        run['status'] = 'RUNNING'
        run['message'] = '正在运行第 {}/{} 批'.format(run['batch_index'] + 1, run['batch_count'])
        for node in document['nodes']:
            if node['id'] in run['nodes']:
                node.update(status='PENDING', activated=False, progress=0, cached=False, message='等待依赖')
        self._publish_locked(document)

    def _schedule(self, canvas_id, round_id, graph, scope, prepared, force, stop):
        nodes = {node['id']: node for node in graph['nodes']}
        order = [node_id for node_id in model.validate_document(graph) if node_id in scope]
        dependencies = {node_id: {edge['source'] for edge in model.incoming(graph, node_id)} for node_id in order}
        futures = {}
        try:
            # Content verification may read a large video; keep it off the GUI
            # thread and complete it before scheduling any dependent submission.
            with self._condition:
                verification = [(node_id, copy.deepcopy(state)) for node_id, state in
                                self._document_locked(canvas_id)['run']['nodes'].items()
                                if state.get('_verify_resume')]
            for node_id, state in verification:
                if self._closing.is_set() or stop.is_set():
                    break
                available, signatures, missing = model.available_results(state.get('results', []), state.get('result_signatures'))
                with self._condition:
                    if canvas_id not in self._documents:
                        return
                    self._document_locked(canvas_id)['run']['nodes'][node_id].pop('_verify_resume', None)
                if missing:
                    self._set_state(canvas_id, node_id, results=available,
                        result_signatures=signatures or [], fingerprint='', _restored_missing_results=True,
                        message='已忽略缺失的历史结果，已完成任务保持完成')
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='canvas-node') as pool:
                while not self._closing.is_set():
                    with self._condition:
                        if canvas_id not in self._documents:
                            return
                        states = self._document_locked(canvas_id)['run']['nodes']
                        states_copy = {node_id: {'status': state.get('status'),
                            'items': [{'task_id': item.get('task_id'), 'status': item.get('status')}
                                      for item in state.get('items', [])]} for node_id, state in states.items()}
                    for node_id, future in list(futures.items()):
                        if not future.done():
                            continue
                        try:
                            future.result()
                        except DetachedExecution:
                            return
                        except Exception as error:
                            failure_status = 'SKIPPED' if isinstance(error, MissingHistoricalInput) else 'FAILED'
                            if self._fail_unsubmitted_items(canvas_id, node_id, str(error), status=failure_status):
                                futures[node_id] = pool.submit(self._worker_call, self._wait_app, round_id, canvas_id, node_id, stop)
                                continue
                            self._set_state(canvas_id, node_id, status=failure_status, message=str(error))
                        del futures[node_id]
                    launched = False
                    for node_id in order:
                        state = states_copy[node_id]
                        if state.get('status') != 'PENDING' or node_id in futures:
                            continue
                        if stop.is_set():
                            self._set_state(canvas_id, node_id, status='CANCELED', message='已停止')
                            continue
                        parents = [states_copy[parent] for parent in dependencies[node_id]]
                        items = state.get('items') or []
                        submitted = bool(items) and all(item.get('task_id') or item.get('status') in TERMINAL for item in items)
                        if not submitted and any(parent.get('status') in TERMINAL - {'SUCCESS'} for parent in parents):
                            skipped = any(parent.get('status') == 'SKIPPED' for parent in parents)
                            message = '历史输入结果缺失，已跳过本分支' if skipped else '上游未成功，已停止后续提交'
                            if self._fail_unsubmitted_items(canvas_id, node_id, message, status='SKIPPED' if skipped else 'FAILED'):
                                futures[node_id] = pool.submit(self._worker_call, self._wait_app, round_id, canvas_id, node_id, stop)
                                launched = True
                            else:
                                self._set_state(canvas_id, node_id, status='SKIPPED' if skipped else 'BLOCKED', message=message)
                        elif submitted or all(parent.get('status') == 'SUCCESS' for parent in parents):
                            self._set_state(canvas_id, node_id, status='PREPARING', activated=True, progress=0, message='正在准备输入')
                            futures[node_id] = pool.submit(self._worker_call, self._execute_node, round_id, canvas_id, round_id, graph,
                                                           nodes[node_id], prepared.get(node_id), force, stop)
                            launched = True
                    with self._condition:
                        if canvas_id not in self._documents:
                            return
                        run = self._document_locked(canvas_id)['run']
                        if not futures and all(state.get('status') in TERMINAL for state in run['nodes'].values()):
                            statuses = {state['status'] for state in run['nodes'].values()}
                            if (statuses <= {'SUCCESS', 'SKIPPED'} and not run.get('has_failure') and not stop.is_set()
                                    and run.get('batch_index', 0) + 1 < run.get('batch_count', 1)):
                                self._advance_batch(self._document_locked(canvas_id), graph, order)
                                continue
                            run['status'] = ('INTERRUPTED' if 'INTERRUPTED' in statuses else
                                             'SUCCESS' if statuses <= {'SUCCESS', 'SKIPPED'} else
                                             'CANCELED' if stop.is_set() else 'FAILED')
                            if run.get('has_failure') and run.get('batch_index', 0) + 1 < run.get('batch_count', 1):
                                run['message'] = '本批存在失败，已停止后续画布批次；独立分支已处理完毕'
                            if 'SKIPPED' in statuses:
                                run['message'] = '部分历史结果缺失，已跳过对应分支'
                            self._publish_locked(self._document_locked(canvas_id))
                            break
                        if stop.is_set() and not futures:
                            run['status'] = 'PAUSED'
                            self._publish_locked(self._document_locked(canvas_id))
                            break
                        if not launched:
                            self._condition.wait(0.15)
        except Exception as error:
            with self._condition:
                document = self._documents.get(canvas_id)
                if document is None or document.get('run', {}).get('id') != round_id:
                    return
                document['run'].update(status='PAUSED', message=str(error))
                try:
                    self._publish_locked(document)
                except Exception:
                    pass
        finally:
            with self._condition:
                active = self._active.get(canvas_id)
                if active and active.get('round_id') == round_id:
                    self._active.pop(canvas_id, None)
                document = self._documents.get(canvas_id)
                if document is None or document.get('run', {}).get('id') != round_id:
                    self._condition.notify_all()
                    return
                if self._closing.is_set():
                    self.store.normalize_session(self._document_locked(canvas_id), ending=True)
                    self._publish_locked(self._document_locked(canvas_id))
                else:
                    # The last UI event must see is_running() == False so Run and
                    # Continue are re-enabled even if the final event was handled
                    # before the scheduler released its worker pool.
                    self._publish_locked(self._document_locked(canvas_id), save=False)
                self._condition.notify_all()

    def _fail_unsubmitted_items(self, canvas_id, node_id, message, *, status='FAILED'):
        """An invalid remaining input must not abandon already accepted siblings."""
        with self._condition:
            document = self._documents.get(canvas_id)
            if document is None:
                return False
            state = document['run']['nodes'][node_id]
            items = state.get('items') or []
            if not any(item.get('task_id') for item in items):
                return False
            for item in items:
                if not item.get('task_id') and item.get('status') not in TERMINAL:
                    item.update(status=status, message=message)
            if status == 'FAILED':
                self._halt_branch(document, node_id, status, message)
            else:
                remaining = {item.get('status') for item in items}
                phase = next((value for value in ('RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET',
                             'CANCEL_FAILED', 'WAITING_FOR_KEY', 'POLL_TIMEOUT', 'QUEUED', 'LOCAL_WAIT', 'SUBMITTING')
                              if value in remaining), 'PREPARING')
                state.update(status=phase, message='正在恢复已提交任务；' + message)
            self._publish_locked(document)
        self._cancel_halted(document)
        return True

    def _execute_node(self, canvas_id, round_id, graph, node, prepared, force, stop):
        node_id, kind = node['id'], node['kind']
        with self._condition:
            if self._document_locked(canvas_id)['run']['nodes'][node_id].get('_halt_status'):
                return
        if kind == 'app':
            with self._condition:
                existing = copy.deepcopy(self._document_locked(canvas_id)['run']['nodes'][node_id].get('items') or [])
            if existing and all(item.get('task_id') or item.get('status') in TERMINAL for item in existing):
                self._wait_app(canvas_id, node_id, stop)
                return
        edges = model.incoming(graph, node_id)
        ports = {port['key']: port['type'] for port in model.input_ports(node)}
        inputs = {}
        with self._condition:
            states = copy.deepcopy(self._document_locked(canvas_id)['run']['nodes'])
        for edge in edges:
            parent = states[edge['source']]
            if edge.get('mode') == 'index' and parent.get('_restored_positions_ambiguous'):
                raise MissingHistoricalInput('历史批次结果缺失，无法可靠定位所选序号，已跳过本分支')
            try:
                inputs[edge['input']] = model.select_results(parent.get('results', []), edge, ports[edge['input']])
            except ValueError as error:
                if parent.get('_restored_missing_results'):
                    raise MissingHistoricalInput('所需历史结果已缺失，已跳过本分支') from error
                raise
        try:
            batches = model.pair_inputs(inputs)
        except ValueError as error:
            if any(states[edge['source']].get('_restored_missing_results') for edge in edges):
                raise MissingHistoricalInput('历史结果缺失导致输入无法配对，已跳过本分支') from error
            raise
        try:
            digest = model.fingerprint(node, inputs, edges)
        except (OSError, ValueError) as error:
            if any(states[edge['source']].get('_restored_missing_results') for edge in edges):
                raise MissingHistoricalInput('历史输入已不可读取，已跳过本分支') from error
            raise
        with self._condition:
            cached = copy.deepcopy(self._document_locked(canvas_id)['run'].get('cache', {}).get(node_id) or node)
        if ((kind != 'app' or node.get('filter_repeats', False)) and not force
                and digest == cached.get('fingerprint')
                and model.results_valid(cached.get('results', []), cached.get('result_signatures'))):
            self._finish_node(canvas_id, node_id, cached['results'], digest, cached=True)
            return
        if kind != 'app':
            self._set_state(canvas_id, node_id, status='RUNNING', activated=True, message='正在执行')
        if kind in model.MEDIA:
            results = []
            for index, path in enumerate(node.get('params', {}).get('files') or []):
                if model.result_type({'path': path}) != kind:
                    raise ValueError('文件格式不属于此导入节点：' + str(path))
                result = {'path': path, 'type': kind, 'index': index}
                model.result_signature(result)
                results.append(model.normalize_result(result))
            if not results:
                raise ValueError('请先选择本地媒体文件')
        elif kind == 'text':
            values = node.get('params', {}).get('texts')
            if not isinstance(values, list):
                values = [node.get('params', {}).get('text', '')]
            results = [{'text': str(value), 'type': 'text', 'kind': 'text', 'index': index}
                       for index, value in enumerate(values)]
        elif kind in ('select', 'preview'):
            results = inputs.get('value') or []
            if not results:
                raise ValueError('请连接上游结果')
            if kind == 'select':
                params = node.get('params', {})
                if params.get('indices') and any(states[edge['source']].get('_restored_positions_ambiguous') for edge in edges):
                    raise MissingHistoricalInput('历史批次结果序号无法确定，已跳过本分支')
                try:
                    results = model.select_results(results, {'mode': 'index' if params.get('indices') else 'all',
                                                            'indices': params.get('indices', [])}, params.get('type', 'any'))
                except ValueError as error:
                    if any(states[edge['source']].get('_restored_missing_results') for edge in edges):
                        raise MissingHistoricalInput('所选历史结果已不可用，已跳过本分支') from error
                    raise
        else:
            self._execute_app(canvas_id, round_id, node, prepared, batches, digest, stop)
            return
        self._finish_node(canvas_id, node_id, results, digest)

    def _execute_app(self, canvas_id, round_id, node, prepared, batches, digest, stop):
        node_id = node['id']
        base_fields = prepared.get('nodes') or model.canonical_fields(node)
        task_count = len(batches)
        # Validate linked values before the first paid submission, but keep only
        # one prepared definition instead of a copy per input group.
        for batch in batches:
            for field in base_fields:
                key = model.parameter_key(field)
                if key in batch:
                    model.input_value(batch[key], field)
        with self._condition:
            document = self._document_locked(canvas_id)
            state = document['run']['nodes'][node_id]
            canvas_name, canvas_batch_index = document['name'], document['run'].get('batch_index', 0)
            workflow_origin = copy.deepcopy(document['run'].get('workflow_queue') or {})
            existing = state.get('items') or []
            if existing and len(existing) != task_count:
                raise ValueError('恢复输入数量与原执行不一致，请重新运行画布')
            if not existing:
                state['items'] = [{'run_id': uuid.uuid4().hex, 'task_id': '', 'status': 'PENDING',
                                   'batch_index': index, 'results': []}
                                  for index in range(task_count)]
            state['fingerprint'] = digest
            self._publish_locked(self._document_locked(canvas_id))
        for index in range(task_count):
            if self._closing.is_set() or stop.is_set():
                break
            with self._condition:
                state = self._document_locked(canvas_id)['run']['nodes'][node_id]
                if state.get('_halt_status') or any(item.get('status') in {'FAILED', 'UNKNOWN', 'CANCELED', 'BLOCKED'}
                                                   for item in state['items']):
                    break
                item = copy.deepcopy(state['items'][index])
            if item.get('status') == 'SUCCESS':
                continue
            if item.get('task_id') or item.get('status') in ACTIVE | {'UNKNOWN'}:
                # Resume waits for the one existing service/recovery task.
                continue
            batch_index = index
            snapshot = {key: copy.deepcopy(value) for key, value in prepared.items() if key != 'nodes'}
            snapshot['nodes'] = copy.deepcopy(base_fields)
            for field in snapshot['nodes']:
                key = model.parameter_key(field)
                if key in batches[batch_index]:
                    field['fieldValue'] = model.input_value(batches[batch_index][key], field)
            snapshot['run_id'] = item['run_id']
            snapshot['origin'] = {'kind': 'canvas', 'canvas_id': canvas_id, 'canvas_name': canvas_name,
                                  'node_id': node_id, 'node_title': node.get('title', 'App'),
                                  'round_id': round_id, 'execution_id': round_id,
                                  'canvas_batch_index': canvas_batch_index,
                                  'batch_index': batch_index}
            for key in ('workflow_group_id', 'workflow_job_id', 'workflow_group_document', 'workflow_job_document'):
                if workflow_origin.get(key):
                    snapshot['origin'][key] = workflow_origin[key]
            with self._condition:
                state = self._document_locked(canvas_id)['run']['nodes'][node_id]
                state['items'][index]['status'] = 'SUBMITTING'
                self._publish_locked(self._document_locked(canvas_id))
            try:
                if prepared.get('_preparation_error'):
                    raise ValueError(prepared['_preparation_error'])
                if self._closing.is_set() or stop.is_set():
                    # This item was persisted as SUBMITTING, but submit() has
                    # provably not been called. Do not leave a fictitious
                    # ambiguous POST or a cancellation which can never finish.
                    with self._condition:
                        document = self._document_locked(canvas_id)
                        document['run']['nodes'][node_id]['items'][index].update(
                            status='CANCELED' if stop.is_set() else 'PENDING',
                            message='已停止，未提交' if stop.is_set() else '客户端关闭，等待恢复')
                        self._publish_locked(document)
                    break
                actual_id = self.service.submit(snapshot)
                with self._condition:
                    halted = bool(self._document_locked(canvas_id)['run']['nodes'][node_id].get('_halt_status'))
                if halted:
                    self.service.cancel(actual_id)
                elif stop.is_set():
                    # A user stop racing registration still means cancellation.
                    # Client shutdown uses _closing and keeps accepted tasks.
                    self.service.cancel(actual_id)
                if actual_id != item['run_id']:
                    raise RuntimeError('共享执行服务未保留预分配的运行标识')
                # Upload latency must not reorder initial POSTs within one node.
                # Wait only for acceptance (or a definitive local outcome), so
                # generation can still overlap across independent branches.
                with self._condition:
                    while not self._closing.is_set() and not stop.is_set():
                        current_item = self._document_locked(canvas_id)['run']['nodes'][node_id]['items'][index]
                        if (self._document_locked(canvas_id)['run']['nodes'][node_id].get('_halt_status')
                                or current_item.get('task_id') or current_item.get('status') in TERMINAL
                                or current_item.get('status') == 'PAUSED'):
                            break
                        self._condition.wait(0.2)
            except DetachedExecution:
                return
            except Exception as error:
                # submit() validation failures occur before POST; ambiguous POST
                # outcomes are reported as UNKNOWN by the service itself.
                with self._condition:
                    state = self._document_locked(canvas_id)['run']['nodes'][node_id]
                    state['items'][index].update(status='FAILED', message=str(error))
                self._set_state(canvas_id, node_id, status='FAILED', message=str(error))
                break
        self._wait_app(canvas_id, node_id, stop)

    def _wait_app(self, canvas_id, node_id, stop):
        while not self._closing.is_set():
            with self._condition:
                document = self._document_locked(canvas_id)
                state = document['run']['nodes'][node_id]
                items = copy.deepcopy(state['items'])
                if all(item.get('status') in TERMINAL for item in items):
                    if state.get('_halt_status'):
                        return
                    if all(item['status'] == 'SUCCESS' for item in items):
                        # _on_record already committed ordered results at the
                        # service's durability barrier. A second save here could
                        # report failure after its task record was already cleared.
                        pass
                    else:
                        statuses = {i.get('status') for i in items}
                        status = ('SKIPPED' if statuses <= {'SUCCESS', 'SKIPPED'} else
                                  'UNKNOWN' if 'UNKNOWN' in statuses else
                                  'CANCELED' if stop.is_set() else 'FAILED')
                        message = next((i.get('message') for i in items if i.get('status') != 'SUCCESS' and i.get('message')), '任务未成功')
                        self._set_state(canvas_id, node_id, status=status, message=message)
                    return
                if stop.is_set():
                    for item in state['items']:
                        if item['status'] == 'PENDING':
                            item.update(status='CANCELED', message='已停止')
                    state['status'] = 'CANCELED' if all(i['status'] in TERMINAL for i in state['items']) else 'CANCEL_FAILED'
                    self._publish_locked(document)
                    return
                self._condition.wait(0.2)

    def _finish_node(self, canvas_id, node_id, results, digest, cached=False):
        results = [model.normalize_result(result) for result in results]
        if not cached:
            for result in results:
                result.pop('_restored_positions', None)
        signatures = [model.result_signature(result) for result in results]
        self._set_state(canvas_id, node_id, status='SUCCESS', progress=100, message='复用已有结果' if cached else '已完成',
                        results=results, result_signatures=signatures, fingerprint=digest, cached=cached)

    def _on_record(self, record):
        if record.get('status') == 'INTERRUPTED' and getattr(self.service, '_closed', False):
            # MainWindow closes the shared service first. Stop DAG activation
            # synchronously before its interruption publication wakes workers.
            self._closing.set()
        origin = record.get('origin') or {}
        canvas_id = origin.get('canvas_id')
        if not canvas_id or not origin.get('node_id') or not (origin.get('round_id') or origin.get('execution_id')):
            return
        handled = None
        with self._condition:
            current = self._documents.get(canvas_id)
            if current is not None and canvas_id not in self._forgotten:
                if self._merge_progress(current, record):
                    # The shared progress monitor already coalesces updates.
                    # Dropping the last value here can leave the canvas stale
                    # indefinitely when a node stops emitting progress events.
                    self._publish_locked(current, save=False, force_notify=True)
                    return
                merged = self._merged_record(current, record)
                if merged is None:
                    return
                document, durable = merged
                if durable:
                    if self.store.save_runtime(self._runtime_document(document)) is None:
                        self.forget_deleted(canvas_id)
                        return
                self._documents[canvas_id] = document
                self._publish_locked(document, save=False, force_notify=durable)
                handled = document
        if handled is not None:
            # This runs before the service completes its failure publication and
            # before TaskLifecycle releases the accepted task's FIFO start gate.
            self._cancel_halted(handled)
            return
        # Only a real completion may touch an unopened canvas, and only its
        # already-existing matching snapshot. No attach, UI state or scheduling.
        if record.get('status') in {'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN'}:
            updated = []
            def update(document):
                merged = self._merged_record(document, record)
                if merged is not None:
                    updated.append(merged[0])
                return merged[0] if merged is not None else None
            self.store.update_runtime_record(record, update)
            for document in updated:
                self._cancel_halted(document)

    @staticmethod
    def _node_progress(state):
        items = state.get('items') or []
        current = next((item for item in reversed(items) if item.get('status') == 'RUNNING' and item.get('node_progress')), {})
        detail = copy.deepcopy(current.get('node_progress') or {})
        for key in ('overall_percent', 'overall_reason', 'total'):
            detail.pop(key, None)  # Discard legacy total-progress metadata on restore.
        if len(items) > 1:
            detail['finished'] = all(item.get('status') == 'SUCCESS' for item in items)
        state['node_progress'] = detail

    def _merge_progress(self, document, record):
        """Progress changes no durability boundary and need no full graph copy."""
        origin, run = record.get('origin') or {}, document.get('run') or {}
        if (run.get('id') != (origin.get('round_id') or origin.get('execution_id'))
                or run.get('batch_index', 0) != origin.get('canvas_batch_index', 0)):
            return False
        node_id = origin.get('node_id')
        state = run.get('nodes', {}).get(node_id)
        if not state:
            return False
        item = next((value for value in state.get('items', []) if value.get('run_id') == record.get('run_id')), None)
        status = record.get('status')
        if (item is None or status in TERMINAL or status != item.get('status')
                or record.get('task_id') != item.get('task_id')):
            return False
        item.update(progress=record.get('progress', 0), message=record.get('message', ''),
                    node_progress=copy.deepcopy(record.get('node_progress') or {}),
                    updated_at=record.get('updated_at', item.get('updated_at', 0)))
        state['progress'] = sum(100 if value.get('status') == 'SUCCESS' else float(value.get('progress') or 0)
                                for value in state['items']) / max(1, len(state['items']))
        if not state.get('_halt_status'):
            state['message'] = record.get('message', '')
        self._node_progress(state)
        node = next((value for value in document['nodes'] if value['id'] == node_id), None)
        if node:
            node.update(progress=state['progress'], node_progress=copy.deepcopy(state['node_progress']), message=state.get('message', ''))
        return True

    def _merged_record(self, source_document, record):
        """Pure task-to-snapshot merge, also usable for targeted background writes."""
        origin = record.get('origin') or {}
        canvas_id, node_id = origin.get('canvas_id'), origin.get('node_id')
        round_id = origin.get('round_id') or origin.get('execution_id')
        if not canvas_id or not node_id or not round_id:
            return
        # Do not expose SUCCESS to the scheduler until its atomic save has
        # succeeded. A disk error leaves the previous in-memory state intact.
        document = copy.deepcopy(source_document)
        run = document.get('run') or {}
        if run.get('id') != round_id or node_id not in run.get('nodes', {}):
            return
        if origin.get('canvas_batch_index', 0) != run.get('batch_index', 0):
            return
        state = run['nodes'][node_id]
        item = next((i for i in state.get('items', []) if i['run_id'] == record.get('run_id')), None)
        if item is None:
            return
        previous_status, previous_task_id = item.get('status'), item.get('task_id')
        status = str(record.get('status') or 'RUNNING').upper()
        if previous_status in TERMINAL and status not in TERMINAL:
            from aetherloom_core.rh_tasks import is_download_recovery
            if previous_status != 'INTERRUPTED' or not is_download_recovery(record):
                return  # Durable completion outranks a stale generating-task index.
        item.update(task_id=record.get('task_id') or item.get('task_id', ''), status=status,
                    message=record.get('message', ''), progress=record.get('progress', 0),
                    node_progress=copy.deepcopy(record.get('node_progress') or {}))
        for key in ('input_files', 'output_files', 'snapshot', 'origin', 'created_at', 'updated_at',
                    'cloud_success', 'task_document'):
            if key in record:
                item[key] = copy.deepcopy(record[key])
        if status == 'SUCCESS' and previous_status != 'SUCCESS':
            results = [model.normalize_result(result) for result in record.get('results', [])]
            for index, result in enumerate(results):
                result.update(generation=round_id + ':' + str(record.get('run_id', '')),
                              task_id=item['task_id'], index=index,
                              batch_index=item.get('batch_index', 0), repeat_index=item.get('repeat_index', 0))
            results, unused, missing = model.available_results(results)
            signatures = []
            for result in results:
                try:
                    signatures.append(model.result_signature(result))
                except (OSError, ValueError):
                    missing = True
            if missing:
                results, unused, removed = model.available_results(results)
                item['_restored_missing_results'] = True
                signatures = []
            item.update(results=results, result_signatures=signatures)
        if status == 'SUCCESS':
            item['output_files'] = list(dict.fromkeys(
                [path for path in item.get('output_files', []) if isinstance(path, str) and os.path.isfile(path)]
                + [result['path'] for result in item.get('results', []) if result.get('path')]))
        # Finish a node here, including during client restart when no scheduler
        # is running. Persist final references before the service clears proof.
        if state.get('items') and all(i.get('status') == 'SUCCESS' for i in state['items']):
            if state.get('status') == 'SUCCESS':
                # A restored completed node already has globally ordered sparse
                # positions. Replaying its per-task cards must not reindex them.
                results, signatures, missing = model.available_results(
                    state.get('results', []), state.get('result_signatures'))
                signatures = signatures or []
            else:
                ordered = sorted(state['items'], key=lambda i: (i.get('batch_index', 0), i.get('repeat_index', 0)))
                results = copy.deepcopy([r for i in ordered for r in i.get('results', [])])
                signatures = [s for i in ordered for s in i.get('result_signatures', [])]
                if len(signatures) != len(results):
                    signatures = []
                # Item positions are local to one task. After partial restored
                # batches the original global count cannot be reconstructed.
                for result in results:
                    result.pop('_restored_positions', None)
                results, signatures, missing = model.available_results(results, signatures)
                signatures = signatures or []
                if any(i.get('_restored_missing_results') for i in ordered):
                    missing = True
                    state['_restored_positions_ambiguous'] = True
            missing |= bool(state.get('_restored_missing_results'))
            if missing:
                state.update(_restored_missing_results=True, fingerprint='')
                signatures = []
            state.update(status='SUCCESS', progress=100, results=results, result_signatures=signatures, message='已完成')
        elif all(i.get('status') in TERMINAL for i in state.get('items', [])):
            statuses = {i.get('status') for i in state['items']}
            state.update(status='SKIPPED' if statuses <= {'SUCCESS', 'SKIPPED'} else
                         'INTERRUPTED' if 'INTERRUPTED' in statuses else
                         'UNKNOWN' if any(i.get('status') == 'UNKNOWN' for i in state['items']) else
                         'CANCELED' if all(i.get('status') == 'CANCELED' for i in state['items']) else 'FAILED',
                         message=record.get('message', '任务未成功'))
        else:
            # Pending siblings contribute zero to the batch percentage.
            percentages = [100 if i.get('status') == 'SUCCESS' else float(i.get('progress') or 0)
                           for i in state.get('items', [])]
            active_statuses = {i.get('status') for i in state.get('items', [])}
            aggregate = next((value for value in ('RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET',
                             'CANCEL_FAILED', 'WAITING_FOR_KEY', 'POLL_TIMEOUT', 'QUEUED', 'LOCAL_WAIT', 'SUBMITTING')
                              if value in active_statuses), 'PREPARING')
            state.update(status=aggregate,
                         progress=sum(percentages) / max(1, len(percentages)), message=record.get('message', ''))
        self._node_progress(state)
        state['activated'] = True
        if status in {'FAILED', 'UNKNOWN', 'CANCELED'}:
            self._halt_branch(document, node_id, status, record.get('message') or '任务未成功')
        if state.get('_halt_status'):
            state['status'] = state['_halt_status']
            state['message'] = state.get('_halt_message', state.get('message', ''))
        elif state.get('status') == 'SUCCESS' and not state.get('cached'):
            run.setdefault('cache', {})[node_id] = copy.deepcopy(state)
        node = next((n for n in document['nodes'] if n['id'] == node_id), None)
        if node:
            for key in RUNTIME_FIELDS:
                if key in state:
                    node[key] = copy.deepcopy(state[key])
        if run.get('session_ended'):
            from aetherloom_core.rh_tasks import is_download_recovery
            if any(is_download_recovery(item) for value in run['nodes'].values() for item in value.get('items', [])):
                run.update(status='DOWNLOADING', message='仅恢复已生成结果的下载和处理')
            elif all(value.get('status') in {'SUCCESS', 'SKIPPED'} for value in run['nodes'].values()):
                run.update(status='SUCCESS', message='已生成结果处理完成')
            else:
                run.update(status='INTERRUPTED', message='客户端会话已结束；未执行部分不会自动继续')
        elif self._closing.is_set():
            self.store.normalize_session(document, ending=True)
        durable = (status != previous_status or item.get('task_id') != previous_task_id
                   or status in TERMINAL)
        return document, durable


    def freeze(self, canvas_id):
        """Stop new local activation before queue persistence or network work."""
        with self._condition:
            active = self._active.get(canvas_id)
            if active:
                active['stop'].set()
            document = self._documents.get(canvas_id)
            if document and document.get('run'):
                document['run']['user_stopped'] = True
            self._condition.notify_all()

    def stop(self, canvas_id):
        errors = []
        with self._condition:
            active = self._active.get(canvas_id)
            if active:
                active['stop'].set()
            document = self._documents.get(canvas_id)
            if not document:
                return
            if document.get('run'):
                document['run']['user_stopped'] = True
            run_ids = [item['run_id'] for state in document.get('run', {}).get('nodes', {}).values()
                       for item in state.get('items', [])
                       if item.get('run_id') and item.get('status') in ACTIVE]
            if document.get('run'):
                try:
                    self.store.save_runtime(self._runtime_document(document))
                except Exception as error:
                    errors.append('无法保存停止状态：' + str(error))
            self._condition.notify_all()
        # Service callbacks acquire the canvas lock; never call cancellation while
        # holding it because another service worker may be publishing a result.
        for run_id in run_ids:
            try:
                self.service.cancel(run_id)
            except Exception as error:
                errors.append('任务取消未确认：' + str(error))
        if errors:
            raise RuntimeError('；'.join(errors))

    def forget_deleted(self, canvas_id):
        """Forget deleted configuration without canceling accepted cloud tasks."""
        canvas_id = str(canvas_id)
        with self._condition:
            active = self._active.pop(canvas_id, None)
            if active:
                active['stop'].set()
            document = self._documents.pop(canvas_id, None)
            self._persisted_documents.pop(canvas_id, None)
            self._workflow_tokens.pop(canvas_id, None)
            self._forgotten.add(canvas_id)
            pending = [item.get('run_id') for state in (document or {}).get('run', {}).get('nodes', {}).values()
                       for item in state.get('items', []) if item.get('run_id') and not item.get('task_id')
                       and item.get('status') in ACTIVE]
            self._condition.notify_all()
        pause = getattr(self.service, 'pause_unsubmitted', None)
        if callable(pause):
            for run_id in pending:
                pause(run_id)

    def stop_all(self):
        with self._condition:
            canvas_ids = list(self._documents)
        errors = []
        for canvas_id in canvas_ids:
            try:
                self.stop(canvas_id)
            except Exception as error:
                errors.append({'canvas_id': canvas_id, 'message': str(error)})
        return errors

    def provide_password(self, canvas_id, node_id, password):
        """Supply missing local recovery material without changing generation inputs."""
        with self._condition:
            document = self._document_locked(canvas_id)
            state = document.get('run', {}).get('nodes', {}).get(node_id) or {}
            run_ids = [item['run_id'] for item in state.get('items', [])
                       if item.get('status') == 'WAITING_FOR_SECRET']
            if not run_ids:
                return 0
            for graph in (document, document.get('run', {}).get('snapshot') or {}):
                for node in graph.get('nodes', []):
                    if node['id'] == node_id:
                        node.setdefault('decode_settings', {})['password'] = str(password)
            self._publish_locked(document)
        for run_id in run_ids:
            self.service.provide_decode_password(run_id, str(password))
        return len(run_ids)

    def close(self):
        """End session work without cloud cancellation; retain only downloads."""
        self._closing.set()
        with self._condition:
            for document in list(self._documents.values()):
                if document.get('run'):
                    self.store.normalize_session(document, ending=True)
                    self._publish_locked(document)
            self._condition.notify_all()
        # Keep the synchronous subscriber until service shutdown so a task finishing
        # during window teardown still saves its canvas result before record cleanup.
