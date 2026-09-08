"""Session-only owner-wide FIFO of immutable workflows, one DAG per job.

The queue owns ordering only. CanvasEngine remains the sole DAG executor and
RhExecutionService remains the sole submit/poll/download/cancel implementation.
"""
import copy
import threading
import time
import uuid
from collections import defaultdict, deque

from PyQt5 import QtCore

from . import model
from .engine import DetachedExecution
from .storage import _remove_secrets


FINAL = frozenset({'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN', 'SKIPPED', 'INTERRUPTED'})
WAITING = frozenset({'QUEUED'})
MAX_FINISHED_GROUPS = 50
MAX_FINISHED_NODES = 10000
MAX_FINISHED_JOBS = 2000


class WorkflowQueue(QtCore.QObject):
    changed = QtCore.pyqtSignal(list)
    error = QtCore.pyqtSignal(str)

    def __init__(self, engine, store=None, owner=None):
        super().__init__(owner if isinstance(owner, QtCore.QObject) else None)
        self.owner, self.engine = owner, engine
        self.store = store or engine.store
        self.service = engine.service
        self.task_documents = getattr(self.service, 'task_documents', None)
        if self.task_documents is None:
            from aetherloom_core.task_documents import get_task_documents
            self.task_documents = get_task_documents(owner if owner is not None else engine)
        if isinstance(owner, QtCore.QObject):
            engine.setParent(owner)
        engine.workflow_queue = self
        self.path = self.store.root / '.workflow_queue.json'
        self.secret_path = self.store.root / '.workflow_queue_secrets.json'
        self.journal_path = self.store.root / '.workflow_queue.events.jsonl'
        self._groups = []
        self._by_id, self._jobs = {}, {}
        self._canvas_groups = defaultdict(set)
        self._pending, self._draining = deque(), set()
        self._finished = deque()
        self._busy, self._canceling = defaultdict(int), defaultdict(int)
        self._cancelable = defaultdict(int)
        self._job_flags, self._group_status = {}, {}
        self._batch_document_states = {}
        self._parent_lock = threading.RLock()
        self._active_parent_documents = None
        self._late_parent_documents = None
        self._new, self._dirty, self._removed = set(), defaultdict(set), set()
        self._order = 0
        self._next_deleted_check = 0
        self._cancel_depth = 0
        self._selected_canvas = None
        self._release_candidates = set()
        self._active = None
        self._closed = self._dispatching = self._recovered = False
        self._load_error = ''
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._notify_timer = QtCore.QTimer(self)
        self._notify_timer.setSingleShot(True)
        self._notify_timer.setInterval(100)
        self._notify_timer.timeout.connect(lambda: self.changed.emit([]))
        engine.queue_changed.connect(self._engine_changed)
        signal = getattr(self.service, 'changed', None)
        if signal is not None:
            signal.connect(self._service_changed)
        self._unsubscribe_late = self.service.subscribe(self._retain_late_download_documents)
        try:
            self._read()
            self._reindex()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self._load_error = '工作流队列无法读取，已保留原记录：' + str(error)

    def _read(self):
        """Discard legacy queue files without reading or replaying any contents."""
        self.cleanup_legacy_queue()

    def cleanup_legacy_queue(self):
        errors = []
        # Fixed exact filenames, never paths from old queue JSON and never a
        # recursive removal. unlink of a symlink removes only the link itself.
        for path in (self.path, self.secret_path, self.journal_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(str(error))
        return errors

    @staticmethod
    def _job_canceling(job):
        return bool(job['status'] == 'CANCELING' or job.get('cancel_requested')
                    and job['status'] not in FINAL | {'CANCEL_FAILED'}
                    or job.get('failure_status') and job['status'] == 'WAITING_FOR_KEY')

    def _register(self, group):
        self._by_id[group['id']] = group
        group.setdefault('order', len(self._by_id))
        self._order = max(self._order, group['order'])
        self._canvas_groups[group['canvas_id']].add(group['id'])
        for job in group['jobs']:
            identity = (group['id'], job['id'])
            self._jobs[identity] = job
            if job['status'] == 'QUEUED':
                self._pending.append(identity)
            self._refresh_job(group, job)
        self._group_status[group['id']] = self._status(group)
        if self._group_status[group['id']] in FINAL:
            self._finished.append(group['id'])

    def _reindex(self):
        self._by_id.clear(); self._jobs.clear(); self._canvas_groups.clear()
        self._pending.clear(); self._busy.clear(); self._canceling.clear(); self._cancelable.clear()
        self._job_flags.clear(); self._group_status.clear()
        self._finished.clear()
        for group in self._groups:
            self._register(group)

    def _refresh_job(self, group, job):
        identity = (group['id'], job['id'])
        flags = (job['status'] not in FINAL, self._job_canceling(job),
                 job['status'] not in FINAL and not self._job_canceling(job))
        old = self._job_flags.get(identity, (False, False, False))
        canvas_id = group['canvas_id']
        self._busy[canvas_id] += int(flags[0]) - int(old[0])
        self._canceling[canvas_id] += int(flags[1]) - int(old[1])
        self._cancelable[canvas_id] += int(flags[2]) - int(old[2])
        self._job_flags[identity] = flags

    def _mark(self, group, job=None):
        if job is not None:
            self._dirty[group['id']].add(job['id'])
            self._refresh_job(group, job)
        else:
            self._dirty[group['id']]  # Group-only metadata change.
        previous = self._group_status.get(group['id'])
        self._group_status[group['id']] = self._status(group)
        if previous not in FINAL and self._group_status[group['id']] in FINAL:
            self._finished.append(group['id'])

    def _trim_finished(self):
        finished = [self._by_id[identity] for identity in self._finished if identity in self._by_id]
        nodes = jobs = 0
        keep = set()
        for group in reversed(finished):
            # Frozen execution input/credentials are not a history archive.
            group.pop('snapshot', None)
            group.pop('prepared', None)
            size = sum(len(job.get('nodes', [])) for job in group['jobs'])
            if keep and (len(keep) >= MAX_FINISHED_GROUPS or nodes + size > MAX_FINISHED_NODES
                         or jobs + len(group['jobs']) > MAX_FINISHED_JOBS):
                continue
            keep.add(group['id']); nodes += size; jobs += len(group['jobs'])
        remove = {group['id'] for group in finished if group['id'] not in keep}
        if not remove:
            return
        self._groups = [group for group in self._groups if group['id'] not in remove]
        for identity in remove:
            group = self._by_id.pop(identity)
            self._canvas_groups[group['canvas_id']].discard(identity)
            self._group_status.pop(identity, None)
            self._batch_document_states.pop(identity, None)
            for job in group['jobs']:
                self._jobs.pop((identity, job['id']), None)
                self._job_flags.pop((identity, job['id']), None)
            self._dirty.pop(identity, None)
        self._removed.update(remove)
        self._finished = deque(identity for identity in self._finished if identity not in remove)

    def _persist(self):
        """Publish task documents, never a queue which can resume next session."""
        if self._new or self._dirty or self._removed:
            for identity, job_ids in self._dirty.items():
                group = self._by_id.get(identity)
                if group is None:
                    continue
                batch_state = (self._group_status[identity], bool(group.get('cancel_requested')),
                               sum(job['status'] in FINAL for job in group['jobs']))
                if self._batch_document_states.get(identity) != batch_state:
                    self.task_documents.patch('batches', identity, {
                        'status': batch_state[0], 'cancel_requested': batch_state[1],
                        'completed_jobs': batch_state[2], 'updated_at': time.time()})
                    self._batch_document_states[identity] = batch_state
                for job in group['jobs']:
                    if job['id'] in job_ids:
                        self.task_documents.patch('workflows', job['id'], self._job_document_state(job))
            self._trim_finished()
            self._new.clear(); self._dirty.clear(); self._removed.clear()

    @staticmethod
    def _job_document_state(job):
        return {key: copy.deepcopy(value) for key, value in job.items() if key not in {'id', 'task_document'}}

    def _create_task_documents(self, group):
        """One immutable batch definition; each workflow gets a small JSON."""
        reference = self.task_documents.reference('batches', group['id'])
        group['task_document'] = reference
        for job in group['jobs']:
            job['task_document'] = self.task_documents.reference('workflows', job['id'])
        self.task_documents.put('batches', group['id'], self._batch_document(group))
        self._batch_document_states[group['id']] = ('QUEUED', False, 0)
        for job in group['jobs']:
            self.task_documents.put('workflows', job['id'], self._workflow_document(group, job))

    @staticmethod
    def _batch_document(group):
        return _remove_secrets({
            'canvas_id': group['canvas_id'], 'canvas_name': group['name'], 'target': group.get('target'),
            'target_title': group.get('target_title', ''), 'batch_count': group['batch_count'],
            'force': group['force'], 'created_at': group['created_at'], 'status': 'QUEUED',
            'scope_nodes': group['scope_nodes'],
            'workflows': [{'id': job['id'], 'index': job['index'], 'task_document': job['task_document']}
                          for job in group['jobs']],
            'frozen': {'graph': model.snapshot_result_references(group['snapshot']), 'prepared': group['prepared']},
        })

    def _workflow_document(self, group, job):
        reference = group['task_document']
        return {
            'canvas_id': group['canvas_id'], 'canvas_name': group['name'], 'group_id': group['id'],
            'batch_document': reference, 'batch_index': job['index'], 'created_at': group['created_at'],
            'input_reference': {'task_document': reference, 'pointer': '/frozen/graph'},
            'settings_reference': {'task_document': reference, 'pointer': '/frozen/prepared'},
            'scope_reference': {'task_document': reference, 'pointer': '/scope_nodes'},
            'task_state_source': 'application_task_document',
            'node_state_scope': 'session_projection',
            **self._job_document_state(job),
        }

    def _retain_late_download_documents(self, record):
        """A late accepted SUCCESS may outlive cleanup of this session's files."""
        if not (self._closed or getattr(self.service, '_closed', False)):
            return
        if not self.service.is_download_recovery(record):
            return
        origin = record.get('origin') or {}
        identity = (origin.get('workflow_group_id'), origin.get('workflow_job_id'))
        with self._parent_lock:
            parents = self._late_parent_documents or self._active_parent_documents
            if parents is None or parents[0] != identity:
                return
            unused, batch, workflow = parents
            nodes = workflow.setdefault('nodes', copy.deepcopy(batch.get('scope_nodes', [])))
            for node in nodes:
                node['state_scope'] = 'at_session_end'
                if node['id'] != origin.get('node_id'):
                    continue
                node.update(status=record['status'], progress=record.get('progress', 0), activated=True)
                references = node.setdefault('app_tasks', [])
                reference = {key: record[key] for key in ('run_id', 'task_id', 'task_document') if record.get(key)}
                for existing in references:
                    if existing.get('run_id') == record.get('run_id'):
                        existing.update(reference)
                        break
                else:
                    references.append(reference)
                for field, key in (('run_ids', 'run_id'), ('task_ids', 'task_id')):
                    if record.get(key) and record[key] not in node.setdefault(field, []):
                        node[field].append(record[key])
            # Only the currently active parent is retained in memory. No queue
            # metadata is read or replayed, and credentials are already stripped.
            self.task_documents.put('batches', identity[0], dict(batch, status='INTERRUPTED', session_ended=True))
            self.task_documents.put('workflows', identity[1], dict(workflow, status='INTERRUPTED',
                session_ended=True, download_recovery_only=True, node_state_scope='at_session_end'))

    @staticmethod
    def _status(group):
        statuses = {job['status'] for job in group['jobs']}
        for status in ('CANCEL_FAILED', 'CANCELING', 'WAITING_FOR_KEY', 'WAITING_FOR_SECRET', 'STARTING', 'RUNNING', 'PAUSED'):
            if status in statuses:
                return status
        if statuses <= FINAL:
            if 'INTERRUPTED' in statuses:
                return 'INTERRUPTED'
            if 'UNKNOWN' in statuses:
                return 'UNKNOWN'
            if 'FAILED' in statuses:
                return 'FAILED'
            return 'CANCELED' if 'CANCELED' in statuses else 'SUCCESS'
        return 'QUEUED'

    def _node_rows(self, group, job):
        if 'nodes' in job:
            return job['nodes']
        return group.get('scope_nodes', [])

    def _public_nodes(self, group, job, offset, limit):
        rows = self._node_rows(group, job)
        result = []
        for node in rows[offset:offset + limit]:
            value = dict(status='PENDING', progress=0, activated=False, cached=False, task_ids=[], run_ids=[])
            value.update(copy.deepcopy(node))
            result.append(value)
        return {'total': len(rows), 'nodes': result}

    def view_groups(self, offset=0, limit=100):
        offset, limit = max(0, int(offset)), min(250, max(0, int(limit)))
        values = []
        for group in self._groups[offset:offset + limit]:
            value = {key: copy.deepcopy(item) for key, item in group.items()
                     if key not in {'snapshot', 'prepared', 'scope_nodes', 'jobs'}}
            value.update(status=self._group_status[group['id']], job_count=len(group['jobs']))
            if len(group['jobs']) == 1:
                job = group['jobs'][0]
                value.update(single_job_id=job['id'], single_node_count=len(self._node_rows(group, job)),
                             cancel_requested=bool(group.get('cancel_requested') or job.get('cancel_requested')),
                             failure_message=job.get('failure_message', ''), message=job.get('message', ''))
            values.append(value)
        return {'total': len(self._groups), 'groups': values}

    def view_jobs(self, group_id, offset=0, limit=100):
        group = self._by_id.get(group_id)
        if group is None:
            return {'total': 0, 'jobs': []}
        offset, limit = max(0, int(offset)), min(250, max(0, int(limit)))
        return {'total': len(group['jobs']), 'jobs': [dict(
            {key: copy.deepcopy(value) for key, value in job.items() if key != 'nodes'},
            node_count=len(self._node_rows(group, job))) for job in group['jobs'][offset:offset + limit]]}

    def view_nodes(self, group_id, job_id, offset=0, limit=250):
        group, job = self._find(group_id, job_id)
        if group is None or job is None:
            return {'total': 0, 'nodes': []}
        return self._public_nodes(group, job, max(0, int(offset)), min(250, max(0, int(limit))))

    def snapshot(self):
        # Explicit compatibility/debug API; normal UI uses the bounded views.
        result = []
        for group in self._groups:
            value = {key: copy.deepcopy(item) for key, item in group.items()
                     if key not in {'snapshot', 'prepared', 'scope_nodes', 'jobs'}}
            value['status'] = self._group_status[group['id']]
            value['jobs'] = []
            for job in group['jobs']:
                item = copy.deepcopy(job)
                item['nodes'] = self._public_nodes(group, job, 0, len(self._node_rows(group, job)))['nodes']
                value['jobs'].append(item)
            result.append(value)
        return result

    groups = snapshot

    def _emit(self, persist=False):
        if persist or self._dirty:
            self._persist()
        if not self._notify_timer.isActive() and not self._closed:
            self._notify_timer.start()

    def _release_idle(self):
        page = getattr(self.owner, 'canvas_page', None)
        selected = (getattr(page, 'document', None) or {}).get('id') or self._selected_canvas
        release = getattr(self.engine, 'release_document', None)
        if not callable(release):
            return
        active_group = self._by_id.get(self._active[0]) if self._active else None
        active_canvas = active_group.get('canvas_id') if active_group else None
        for canvas_id in list(self._release_candidates):
            # The engine may finish before the queued final notification reaches
            # this coordinator. Retain its state until _dispatch consumes it.
            if canvas_id not in (selected, active_canvas) and release(canvas_id):
                self._release_candidates.discard(canvas_id)

    def _find(self, group_id, job_id=None):
        return self._by_id.get(group_id), self._jobs.get((group_id, job_id)) if job_id else None

    @staticmethod
    def _scope_nodes(document, target):
        order = model.validate_document(document)
        scope = model.ancestors(document, target) if target else set(order)
        by_id = {node['id']: node for node in document['nodes']}
        return [dict(id=node_id, title=by_id[node_id].get('title', 'App'),
                     webapp_id=(by_id[node_id].get('app') or {}).get('webapp_id', ''),
                     status='PENDING', progress=0, activated=False, cached=False,
                     task_ids=[], run_ids=[])
                for node_id in order if node_id in scope and by_id[node_id]['kind'] == 'app']

    def enqueue(self, document, target=None, force=False, batch_count=None, prepare_app=None):
        if self._closed:
            raise ValueError('客户端正在关闭')
        if self._load_error:
            raise ValueError(self._load_error)
        frozen = copy.deepcopy(document)
        frozen['run'] = {}
        model.validate_document(frozen)
        count = model.normalize_batch_count(1 if target else
            frozen.get('batch_count', 1) if batch_count is None else batch_count)
        nodes = self._scope_nodes(frozen, target)
        captured = self.engine.capture_prepared(frozen, target, prepare_app)
        # A deliberate Run may create a new workflow. Dispatch never may.
        self.engine.save_document(document, explicit=True)
        group_id = uuid.uuid4().hex
        group = dict(id=group_id, canvas_id=frozen['id'], name=frozen.get('name', '画布'),
                     order=self._order + 1,
                     target=target, target_title=next((node.get('title', '节点') for node in frozen['nodes']
                                                       if node['id'] == target), ''),
                     force=bool(force), batch_count=count, created_at=time.time(),
                     cancel_requested=False, snapshot=frozen, prepared=captured, scope_nodes=nodes,
                     jobs=[dict(id=uuid.uuid4().hex, index=index, status='QUEUED', run_id='',
                                cancel_requested=False, message='')
                           for index in range(count)])
        self._groups.append(group)
        self._register(group)
        self._new.add(group_id)
        try:
            self._create_task_documents(group)
            self._emit(persist=True)
        except Exception:
            self._groups.remove(group)
            self._new.discard(group_id)
            self._reindex()
            raise
        if not self._recovered:
            self.recover()
        self._release_candidates.add(frozen['id'])
        self._release_idle()
        self._wake()
        return group_id

    def is_busy(self, canvas_id=None):
        return bool(self._busy.get(canvas_id, 0)) if canvas_id is not None else any(self._busy.values())

    def is_canceling(self, canvas_id=None):
        return bool(self._canceling.get(canvas_id, 0)) if canvas_id is not None else any(self._canceling.values())

    def can_cancel(self, canvas_id=None):
        """New waiting jobs remain cancellable while an older task confirms."""
        return bool(self._cancelable.get(canvas_id, 0)) if canvas_id is not None else any(self._cancelable.values())

    def _hydrate(self, group):
        prepared = copy.deepcopy(group.get('prepared', {}))
        connection = getattr(self.owner, '_rh_connection_settings', None)
        for node_id, options in prepared.items():
            if connection is not None:
                credentials = connection.snapshot(options.get('base_url'))
                options.update(api_key=credentials['api_key'], api_keys=credentials['api_keys'])
            elif not options.get('api_key') and not options.get('api_keys'):
                # Headless owners/tests can supply a non-widget credential reader.
                nodes = {node['id']: node for node in group['snapshot']['nodes']}
                try:
                    fresh = self.engine.prepare_app(copy.deepcopy(nodes[node_id]), model.canonical_fields(nodes[node_id]))
                    for key in ('api_key', 'api_keys'):
                        if key in fresh:
                            options[key] = copy.deepcopy(fresh[key])
                except Exception as error:
                    options.setdefault('_preparation_error', str(error))
        return prepared

    def _document(self, canvas_id):
        if not self.store.path_for(canvas_id).is_file():
            raise FileNotFoundError('画布文件已删除')
        try:
            return self.engine.document(canvas_id)
        except (KeyError, RuntimeError):
            document = self.store.load(canvas_id)
            return self.engine.attach(document)

    def _queue_origin(self, group, job):
        return dict(group_id=group['id'], job_id=job['id'], index=job['index'], batch_count=group['batch_count'],
                    workflow_group_id=group['id'], workflow_job_id=job['id'],
                    workflow_group_document=group.get('task_document', ''),
                    workflow_job_document=job.get('task_document', ''))

    def recover(self):
        """Enable this session's dispatch, without recovering any prior queue."""
        self._recovered = True
        self._wake()
        return []

    def recover_selected(self, document):
        """Show saved results/download recovery; never schedule old DAG work."""
        errors = []
        try:
            if self._selected_canvas and self._selected_canvas != document['id']:
                self._release_candidates.add(self._selected_canvas)
            self._selected_canvas = document['id']
            try:
                self.engine.document(document['id'])
            except (KeyError, RuntimeError):
                self.engine.attach(document)
            self._release_idle()
        except Exception as error:
            errors.append({'canvas_id': document['id'], 'message': str(error)})
        return errors

    def _task_records(self, job):
        identities = {identity for node in job.get('nodes', []) for identity in node.get('run_ids', [])}
        return [record for identity in identities if (record := self.service.get(identity)) is not None]

    def _sync_job(self, group, job, document):
        run = document.get('run') or {}
        if run.get('id') != job.get('run_id'):
            return False
        before = {key: value for key, value in job.items() if key != 'nodes'}
        changed = False
        if 'nodes' not in job:
            job['nodes'] = self._public_nodes(group, job, 0, len(group.get('scope_nodes', [])))['nodes']
            changed = True
        for node in job.get('nodes', []):
            state = run.get('nodes', {}).get(node['id'], {})
            previous = dict(node)
            for key in ('status', 'progress', 'activated', 'cached', 'message', 'reused_app_tasks', 'result_references'):
                if key in state:
                    node[key] = copy.deepcopy(state[key])
            node['task_ids'] = [item['task_id'] for item in state.get('items', []) if item.get('task_id')]
            node['run_ids'] = [item['run_id'] for item in state.get('items', []) if item.get('run_id')]
            node['app_tasks'] = [{key: item[key] for key in ('run_id', 'task_id', 'task_document') if item.get(key)}
                                 for item in state.get('items', []) if item.get('run_id')]
            changed |= previous != node
        identities = {identity for node in job['nodes'] for identity in node.get('run_ids', [])}
        statuses = {record.get('status') for record in self.service.statuses(identities).values()}
        pending_records = any(status not in FINAL for status in statuses)
        running = self.engine.is_running(group['canvas_id'])
        failed_states = [state for state in run.get('nodes', {}).values()
                         if state.get('status') in {'FAILED', 'UNKNOWN'}]
        if (run.get('status') in {'FAILED', 'UNKNOWN'} or failed_states
                or run.get('has_failure') and not job.get('cancel_requested')):
            job['failure_status'] = ('UNKNOWN' if run.get('status') == 'UNKNOWN' or 'UNKNOWN' in statuses
                                     or any(state.get('status') == 'UNKNOWN' for state in failed_states) else 'FAILED')
            job['failure_message'] = next((state.get('_halt_message') or state.get('message')
                                           for state in failed_states if state.get('_halt_message') or state.get('message')),
                                          run.get('message') or '本批有任务未成功')
            self._cancel_later(group, job, '前一批未成功，已停止后续批次')
        failure = job.get('failure_status')
        if job.get('cancel_requested'):
            job['status'] = ('CANCEL_FAILED' if 'CANCEL_FAILED' in statuses else
                             'WAITING_FOR_KEY' if 'WAITING_FOR_KEY' in statuses else
                             'CANCELING' if pending_records or running else failure or 'CANCELED')
        elif not running and run.get('status') in FINAL and not pending_records:
            job['status'] = failure or run['status']
        elif failure and pending_records and (not running or statuses & {'CANCELING', 'CANCEL_FAILED'}):
            job['status'] = ('CANCEL_FAILED' if 'CANCEL_FAILED' in statuses else
                             'WAITING_FOR_KEY' if 'WAITING_FOR_KEY' in statuses else 'CANCELING')
        elif 'WAITING_FOR_KEY' in statuses or 'WAITING_FOR_SECRET' in statuses:
            job['status'] = 'WAITING_FOR_KEY' if 'WAITING_FOR_KEY' in statuses else 'WAITING_FOR_SECRET'
        else:
            job['status'] = 'RUNNING' if running else 'PAUSED'
        job['message'] = (job['failure_message'] + ('；正在确认其余任务已结束' if pending_records else '')
                          if failure else run.get('message', job.get('message', '')))
        if job['status'] in {'FAILED', 'UNKNOWN'}:
            self._cancel_later(group, job, '前一批未成功，已停止后续批次')
        if job['status'] in FINAL:
            job['finished_at'] = job.get('finished_at') or time.time()
        changed |= before != {key: value for key, value in job.items() if key != 'nodes'}
        if changed:
            self._mark(group, job)
        return changed

    def _cancel_later(self, group, current, message):
        for job in group['jobs']:
            if job['index'] > current['index'] and job['status'] == 'QUEUED':
                job.update(status='CANCELED', cancel_requested=True, message=message, finished_at=time.time())
                self._mark(group, job)

    @QtCore.pyqtSlot(str)
    def _engine_changed(self, canvas_id):
        if self._closed or not self._active:
            return
        group, job = self._find(*self._active)
        if group is None or canvas_id != group['canvas_id']:
            return
        try:
            document = self.engine.queue_state(canvas_id)
            previous = job['status']
            previous_ids = [node.get('task_ids') for node in job.get('nodes', [])]
            if self._sync_job(group, job, document):
                important = previous != job['status'] or previous_ids != [node.get('task_ids') for node in job['nodes']]
                self._emit(persist=important)
            self._wake()
        except DetachedExecution:
            # Queued Qt signals can outlive a completed canvas evicted while
            # opening/editing another one. They carry no new execution state.
            self._wake()
        except (OSError, ValueError, TypeError) as error:
            self._timer.stop()
            self.error.emit('工作流队列保存失败：' + str(error))

    @QtCore.pyqtSlot(str, dict)
    def _service_changed(self, unused, record):
        if not self._closed and self._active:
            origin = record.get('origin') or {}
            group, job = self._find(*self._active)
            if group and origin.get('canvas_id') == group['canvas_id']:
                self._wake()

    def _wake(self):
        if not self._closed and (not self._timer.isActive() or self._timer.interval() != 0):
            self._timer.start(0)

    def _documents_ready(self, group, job):
        return (self.task_documents.is_flushed('batches', group['id'])
                and self.task_documents.is_flushed('workflows', job['id']))

    def _launch_job(self, group, job):
        """Called only after immutable input and STARTING identity are on disk."""
        try:
            document = self._document(group['canvas_id'])
            prepared = self._hydrate(group)
            with self._parent_lock:
                self._active_parent_documents = ((group['id'], job['id']),
                    self._batch_document(group), self._workflow_document(group, job))
            job['engine_started'] = True
            self.engine.start(document, target=group.get('target'), force=group.get('force', False),
                batch_count=1, execution_snapshot=group['snapshot'], prepared_snapshot=prepared,
                round_id=job['run_id'], queue_origin=self._queue_origin(group, job))
            self._sync_job(group, job, self.engine.queue_state(group['canvas_id']))
            self._emit(persist=True)
        except Exception as error:
            job.update(status='FAILED', message=str(error), finished_at=time.time())
            self._mark(group, job)
            self._cancel_later(group, job, '前一批无法开始')
            self._active = None
            with self._parent_lock:
                self._active_parent_documents = None
            self._emit(persist=True)

    def _detach_deleted(self, canvas_id):
        self.engine.forget_deleted(canvas_id)
        for identity in list(self._canvas_groups.get(canvas_id, ())):
            group = self._by_id[identity]
            for job in group['jobs']:
                if job['status'] not in FINAL:
                    job.update(status='CANCELED', message='画布文件已删除；已提交的 App 任务仍由共享服务处理',
                               finished_at=time.time())
                    self._mark(group, job)
        if self._active:
            active_group, unused = self._find(*self._active)
            if active_group is None or active_group['canvas_id'] == canvas_id:
                self._active = None

    def _tick(self):
        if self._closed or not self._recovered or self._load_error or self._dispatching or self._cancel_depth:
            return
        self._timer.stop()
        self._dispatching = True
        waiting_documents = False
        try:
            deleted = set()
            if time.monotonic() >= self._next_deleted_check:
                deleted = {canvas_id for canvas_id, count in self._busy.items() if count
                           and not self.store.path_for(canvas_id).is_file()}
                self._next_deleted_check = time.monotonic() + 5
            for canvas_id in deleted:
                self._detach_deleted(canvas_id)
            if deleted:
                self._emit(persist=True)
            # A selected legacy round can be canceled while it is still a queued
            # resume item. Its accepted tasks must drain even before dispatch.
            drain = []
            for identity in list(self._draining):
                group, job = self._find(*identity)
                if group is None or job['status'] in FINAL:
                    self._draining.discard(identity)
                elif identity != self._active:
                    if self._sync_job(group, job, self.engine.queue_state(group['canvas_id'])):
                        self._emit(persist=True)
                    if job['status'] not in FINAL:
                        drain.append(identity)
            if self._active is None and drain:
                self._active = drain[0]
            if self._active:
                group, job = self._find(*self._active)
                if group is None:
                    self._active = None
                    with self._parent_lock:
                        self._active_parent_documents = None
                else:
                    if not self.store.path_for(group['canvas_id']).is_file():
                        self._detach_deleted(group['canvas_id'])
                        self._emit(persist=True)
                        return
                    if job['status'] == 'STARTING' and not job.get('engine_started'):
                        waiting_documents = True
                        if self._documents_ready(group, job):
                            self._launch_job(group, job)
                        return
                    document = self.engine.queue_state(group['canvas_id'])
                    changed = self._sync_job(group, job, document)
                    if job['status'] not in FINAL:
                        if changed:
                            self._emit(persist=True)
                        return
                    self._active = None
                    with self._parent_lock:
                        self._active_parent_documents = None
                    self._release_candidates.add(group['canvas_id'])
                    self._emit(persist=True)
                    self._release_idle()
            # Legacy direct users of this engine retain their running round.
            if self.engine._active:
                return
            if drain:
                self._active = drain[0]
                return
            candidate = None
            while self._pending:
                group, job = self._find(*self._pending[0])
                if group is not None and job['status'] == 'QUEUED':
                    candidate = (group, job)
                    break
                self._pending.popleft()
            if candidate is None:
                self._timer.stop()
                return
            group, job = candidate
            if not self.store.path_for(group['canvas_id']).is_file():
                self._detach_deleted(group['canvas_id'])
                self._emit(persist=True)
                return
            waiting_documents = True
            if not self._documents_ready(group, job):
                return
            job.update(status='STARTING', run_id=job.get('run_id') or uuid.uuid4().hex,
                       started_at=time.time(), engine_started=False)
            self._mark(group, job)
            self._active = (group['id'], job['id'])
            self._emit(persist=True)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self._timer.stop()
            self.error.emit('工作流队列暂未继续：' + str(error))
        finally:
            self._dispatching = False
            if not self._closed and self.is_busy() and not self._timer.isActive():
                self._timer.start(50 if waiting_documents else 1000)

    def _request_cancel(self, group, job):
        errors = []
        try:
            self.engine.stop(group['canvas_id'])
        except Exception as error:
            errors.append({'message': str(error)})
        for record in self._task_records(job):
            if record.get('status') not in FINAL | {'CANCELING'} and not (
                    record.get('status') == 'WAITING_FOR_KEY' and record.get('cancel_requested')):
                try:
                    self.service.cancel(record['run_id'])
                except Exception as error:
                    errors.append({'message': str(error)})
        return errors

    def _cancel_jobs(self, targets, *, groups=(), freeze_canvases=()):
        """Freeze the entire requested scope before any callback can dispatch."""
        errors, accepted = [], []
        self._cancel_depth += 1
        try:
            for group in groups:
                group['cancel_requested'] = True
                self._mark(group)
            for group, job in targets:
                if job['status'] in FINAL:
                    continue
                queued = job['status'] in {'QUEUED', 'STARTING'} and not job.get('engine_started')
                request_needed = not self._job_canceling(job)
                # Every ordinary pending job is frozen before even inspecting
                # an accepted legacy round, let alone calling service.cancel.
                job['cancel_requested'] = True
                job.update(status='CANCELED' if queued else 'CANCELING',
                           message='已取消排队' if queued else '正在确认取消')
                self._mark(group, job)
                if not queued:
                    if request_needed:
                        accepted.append((group, job))
                    self._draining.add((group['id'], job['id']))
            for canvas_id in set(freeze_canvases) | {group['canvas_id'] for group, unused in accepted}:
                self.engine.freeze(canvas_id)
            for group, job in accepted:
                if job.get('resume'):
                    try:
                        self._sync_job(group, job, self._document(group['canvas_id']))
                    except (OSError, ValueError, RuntimeError) as error:
                        errors.append({'canvas_id': group['canvas_id'], 'message': str(error)})
                if self._active is None:
                    self._active = (group['id'], job['id'])
            try:
                self._emit(persist=True)
            except Exception as error:
                errors.append({'message': str(error)})
            # A write error must not prevent canceling already accepted tasks.
            for group, job in accepted:
                errors.extend(self._request_cancel(group, job))
        finally:
            self._cancel_depth -= 1
        self._wake()
        return errors

    def cancel_job(self, group_id, job_id):
        group, job = self._find(group_id, job_id)
        return self._cancel_jobs([(group, job)]) if group is not None and job is not None else []

    def cancel_group(self, group_id):
        group, unused = self._find(group_id)
        return self._cancel_jobs([(group, job) for job in group['jobs']], groups=[group]) if group else []

    def cancel_canvas(self, canvas_id):
        groups = [self._by_id[identity] for identity in self._canvas_groups.get(canvas_id, ())]
        errors = self._cancel_jobs([(group, job) for group in groups for job in group['jobs']], groups=groups,
                                   freeze_canvases=[canvas_id])
        # A legacy direct round may not yet have a workflow queue entry.
        if not any(job.get('run_id') for group in groups for job in group['jobs'] if job['status'] not in FINAL):
            try:
                self.engine.stop(canvas_id)
            except Exception as error:
                errors.append({'canvas_id': canvas_id, 'message': str(error)})
        return errors

    def cancel_all(self):
        groups = list(self._groups)
        return self._cancel_jobs([(group, job) for group in groups for job in group['jobs']], groups=groups,
                                 freeze_canvases=list(self.engine._active))

    def clear_finished(self):
        removed = {group['id'] for group in self._groups if self._group_status[group['id']] in FINAL}
        self._groups = [group for group in self._groups if group['id'] not in removed]
        self._removed.update(removed)
        self._reindex()
        self._emit(persist=True)

    def close(self):
        """Discard local queued groups; closing never sends cloud cancellation."""
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._notify_timer.stop()
        with self._parent_lock:
            self._late_parent_documents = self._active_parent_documents
            self._active_parent_documents = None
            if self._late_parent_documents is not None:
                identity, batch, workflow = self._late_parent_documents
                group, job = self._find(*identity)
                if job is not None:
                    workflow.update(self._job_document_state(job))
        # Ordinary queued documents are removed by the shared repository. Only
        # a live job can be retained as the parent of a download retry; mark its
        # workflow ended without rewriting thousands of unstarted job files.
        for identity in self._draining | ({self._active} if self._active else set()):
            group, job = self._find(*identity)
            if group is None:
                continue
            try:
                nodes = copy.deepcopy(job.get('nodes') or [])
                for node in nodes:
                    node['state_scope'] = 'at_session_end'
                self.task_documents.patch('workflows', job['id'], {
                    'status': 'INTERRUPTED' if job['status'] not in FINAL else job['status'],
                    'session_ended': True, 'download_recovery_only': True,
                    'task_state_source': 'application_task_document',
                    'node_state_scope': 'at_session_end', 'nodes': nodes,
                    'message': '客户端会话已结束，仅已完成生成的结果下载可以继续',
                })
                self.task_documents.patch('batches', group['id'], {
                    'status': 'INTERRUPTED', 'session_ended': True,
                })
            except (OSError, ValueError, TypeError) as error:
                self.error.emit('工作流结束状态保存失败：' + str(error))
        self._active = None
        self._groups.clear()
        self._reindex()
        self._draining.clear()
        self._new.clear(); self._dirty.clear(); self._removed.clear()
        self._release_candidates.clear()
        self._batch_document_states.clear()
        self.cleanup_legacy_queue()


def ensure_workflow_queue(owner, engine=None, store=None):
    queue = getattr(owner, '_canvas_workflow_queue', None)
    if queue is None:
        if engine is None:
            page = getattr(owner, 'canvas_page', None)
            engine = getattr(page, 'engine', None)
        if engine is None:
            raise ValueError('画布执行服务尚未初始化')
        queue = WorkflowQueue(engine, store=store, owner=owner)
        owner._canvas_workflow_queue = queue
    return queue
