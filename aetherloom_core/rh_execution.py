"""One widget-independent execution path for RH App pages and canvases.

Submission owns only uploads and the single POST. TaskLifecycle owns polling,
downloads, retries and restart recovery for every accepted task. Observers run
synchronously before a final state is committed, allowing a canvas to durably
save result references before temporary task/verification records are removed.
"""
from __future__ import annotations

import copy
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

import requests
from PyQt5 import QtCore

from aetherloom_core.rh_tasks import (TERMINAL_STATUSES, TaskStore, normalize_base_url,
                                      normalize_api_keys, api_key_id, is_download_recovery, output_group)
from aetherloom_core.rh_submission_queue import get_submission_queue, SubmissionCancelled


FINAL = TERMINAL_STATUSES | {'UNKNOWN'}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif', '.avif', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus'}


def submission_diagnostic(error, response=None):
    """Keep actionable failure metadata without request bodies or credentials."""
    from api_calls.call_rh import RunningHubResponseError
    detail = {'exception_type': type(error).__name__}
    http_status = getattr(getattr(error, 'response', None), 'status_code', None)
    if isinstance(http_status, int):
        detail['http_status'] = http_status
    if isinstance(response, dict):
        try:
            detail['response_code'] = int(response.get('code'))
        except (ValueError, TypeError, OverflowError):
            pass
    if isinstance(error, requests.Timeout):
        reason = '提交请求超时，未能确认服务端是否创建任务'
    elif isinstance(error, requests.ConnectionError):
        reason = '提交连接中断，未能确认服务端是否收到请求'
    elif isinstance(error, requests.HTTPError):
        reason = '提交接口返回 HTTP {}'.format(http_status if isinstance(http_status, int) else '错误')
    elif isinstance(error, RunningHubResponseError):
        reason = '提交接口未返回可确认的 taskId'
        if 'response_code' in detail:
            reason += '（code={}）'.format(detail['response_code'])
    else:
        reason = '提交处理异常（{}）'.format(type(error).__name__)
    detail['reason'] = reason
    return detail


class MissingDecodePassword(RuntimeError):
    waiting_for_secret = True


class MissingDecodeConfiguration(RuntimeError):
    waiting_for_secret = True
    recovery_message = '旧任务缺少发起时的解码设置，请补齐该任务设置后继续；不会使用当前 App 设置'


def frozen_decode_settings(value, *, legacy_missing=False):
    """Capture defaults once; output processing never consults App preferences."""
    settings = copy.deepcopy(value) if isinstance(value, dict) else {}
    if legacy_missing:
        settings.update(enabled=True, settings_missing=True)
        return settings
    settings.setdefault('enabled', False)
    settings.setdefault('mode', 'grc')
    settings.setdefault('grid_cols', 32)
    settings.setdefault('delete_original', True)
    settings.setdefault('password', '')
    if settings.get('password'):
        settings['password_required'] = True
    return settings


class SubmissionKeysRejected(RuntimeError):
    """Every credential was definitively rejected before generation acceptance."""


def public_snapshot(snapshot):
    """Copy only execution inputs suitable for models/files, removing secrets."""
    result = {key: copy.deepcopy(value) for key, value in snapshot.items()
              if key in {'webapp_id', 'app_name', 'base_url', 'nodes', 'output_dir',
                         'retry_max', 'retry_delay', 'retry_concurrency', 'origin', 'run_id'}}
    decode = snapshot.get('decode_settings') or {}
    result['decode_settings'] = {key: copy.deepcopy(value) for key, value in decode.items()
                                  if key in {'enabled', 'mode', 'grid_cols', 'delete_original',
                                             'password_required', 'settings_missing'}}
    if decode.get('password'):
        result['decode_settings']['password_required'] = True
    # Node definitions can carry arbitrary vendor metadata. These are not
    # credentials for execution and must never accidentally export a key.
    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()
                    if str(key).lower().replace('_', '') not in
                    {'apikey', 'apikeys', 'acceptedapikey', 'accesstoken', 'authorization', 'password', 'secret'}}
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value
    return scrub(result)


def result_for_path(path, task_id):
    path = os.path.abspath(os.fspath(path))
    suffix = Path(path).suffix.lower()
    mime = mimetypes.guess_type(path)[0] or ''
    if suffix in IMAGE_EXTENSIONS or mime.startswith('image/'):
        kind = 'image'
    elif suffix in VIDEO_EXTENSIONS or mime.startswith('video/'):
        kind = 'video'
    elif suffix in AUDIO_EXTENSIONS or mime.startswith('audio/'):
        kind = 'audio'
    elif suffix in {'.txt', '.json', '.csv', '.md', '.html', '.log'} or mime.startswith('text/'):
        kind = 'text'
    else:
        kind = 'file'
    result = dict(kind=kind, type=kind, path=path, task_id=str(task_id), name=Path(path).name)
    if kind == 'text':
        try:
            # Huge outputs stay file references; graph text ports may choose
            # them explicitly instead of loading unbounded data in the UI.
            if os.path.getsize(path) <= 2 * 1024 * 1024:
                result['text'] = Path(path).read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            pass
    return result


class RhExecutionService(QtCore.QObject):
    changed = QtCore.pyqtSignal(str, dict)
    is_download_recovery = staticmethod(is_download_recovery)

    def __init__(self, owner):
        super().__init__(owner if isinstance(owner, QtCore.QObject) else None)
        self.owner = owner
        self.lifecycle = owner._rh_task_lifecycle
        self.queue = get_submission_queue(owner)
        self._condition = threading.Condition(threading.RLock())
        self._notifications = threading.RLock()
        self._records = {}
        self._snapshots = {}
        self._task_runs = {}
        self._cancel_requests = set()
        self._pause_requests = set()
        self._cancel_workers = set()
        self._post_inflight = set()
        self._subscribers = []
        self._workers = {}
        self._pending_runs = {}
        self._dispatcher = None
        self._dispatching = False
        self._closed = False
        from aetherloom_core.task_documents import get_task_documents
        self.documents = get_task_documents(owner)
        self.task_documents = self.documents
        self._unsubscribe_dispatch = self.queue.subscribe_dispatch(self._dispatch_submissions)
        self.restore_password = None
        owner._rh_execution_service = self
        self._progress_timer = QtCore.QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self._sync_progress)
        self._progress_timer.start()
        # This does not submit or poll. It makes persisted tasks observable
        # even when their App page has never been constructed.
        try:
            persisted_tasks = list(self.lifecycle.store.retain_download_retries().items())
            self.documents.cleanup([str(value.get('run_id') or 'recovered-' + task_id)
                                    for task_id, value in persisted_tasks])
            self._restore_submission_gates(persisted_tasks)
            for task_id, persisted in persisted_tasks:
                with self.lifecycle.lock:
                    context = self.lifecycle.context(task_id, persisted=persisted, refresh_key=True)
                self.adopt_task(task_id, context)
        except (OSError, ValueError):
            pass  # Existing recovery keeps retrying an unavailable task file.

    def _restore_submission_gates(self, records):
        """Rebuild accepted-but-not-started FIFO barriers across client restarts."""
        ordered = []
        legacy = []
        for task_id, context in records:
            try:
                order = int(context['submission_order'])
                ordered.append((order, task_id, context))
            except (KeyError, TypeError, ValueError, OverflowError):
                legacy.append((task_id, context))
        # Old indexes preserve insertion order but have no reserved order field.
        # Place those earlier records ahead of numbered/new submissions. Negative
        # sequence values are valid queue positions and never sent to RH.
        first_order = min((entry[0] for entry in ordered), default=0)
        ordered.extend((first_order - len(legacy) + index, task_id, context)
                       for index, (task_id, context) in enumerate(legacy))
        started_statuses = {'RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET'} | TERMINAL_STATUSES
        with self.queue.condition:
            used = set(self.queue._pending_orders) | set(self.queue._awaiting_start.values())
            for order, task_id, context in sorted(ordered, key=lambda item: item[0]):
                self.queue._next_order = max(self.queue._next_order, order + 1)
                if task_id in self.queue._awaiting_start:
                    context['submission_order'] = self.queue._awaiting_start[task_id]
                    continue
                if (str(context.get('started', False)).lower() in {'true', '1'}
                        or context.get('status') in started_statuses):
                    context['submission_order'] = order
                    continue
                while order in used:
                    order += 1
                used.add(order)
                context['submission_order'] = order
                self.queue._next_order = max(self.queue._next_order, order + 1)
                self.queue._pending_orders.add(order)
                self.queue._awaiting_start[str(task_id)] = order
                self.queue._admitted_orders.add(order)
            self.queue.condition.notify_all()

    def subscribe(self, callback):
        with self._condition:
            self._subscribers.append(callback)
        def unsubscribe():
            with self._condition:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    @staticmethod
    def _document_state(record):
        """Mutable projection; immutable submission inputs are written once."""
        state_fields = ('status', 'progress', 'node_progress', 'message', 'warning', 'submission_error', 'cancel_requested',
                        'cloud_success', 'created_at', 'updated_at')
        return dict(task_id=record.get('task_id'),
                    state={key: copy.deepcopy(record[key]) for key in state_fields if key in record},
                    queue=dict(submission_order=record.get('submission_order'),
                               submission_admitted=bool(record.get('submission_admitted')),
                               group=output_group(record)),
                    results=copy.deepcopy(record.get('results') or []),
                    output_files=copy.deepcopy(record.get('output_files') or []),
                    input_files=copy.deepcopy(record.get('input_files') or []))

    def _create_document(self, record, snapshot, *, legacy=False):
        run_id = record['run_id']
        public = public_snapshot(snapshot)
        document = dict(schema_version=1, kind='application', id=run_id, run_id=run_id,
                        webapp_id=record['webapp_id'], app_name=record.get('app_name', ''),
                        request=public, decode_settings=public['decode_settings'],
                        origin=copy.deepcopy(record.get('origin') or {}),
                        post=dict(endpoint=normalize_base_url(public.get('base_url')) + '/task/openapi/ai-app/run',
                                  phase='legacy_unknown' if legacy else 'pending', attempt=0,
                                  body=None if legacy else dict(webappId=record['webapp_id'],
                                                               nodeInfoList=copy.deepcopy(public.get('nodes') or []))))
        document.update(self._document_state(record))
        self.documents.put('applications', run_id, document,
                           private_password=(snapshot.get('decode_settings') or {}).get('password') or None)

    def get(self, run_id):
        with self._condition:
            return copy.deepcopy(self._records.get(str(run_id)))

    def records(self):
        with self._condition:
            return copy.deepcopy(list(self._records.values()))

    def records_for_app(self, webapp_id):
        with self._condition:
            return copy.deepcopy([record for record in self._records.values()
                                  if record['webapp_id'] == str(webapp_id)])

    def record_headers(self, webapp_id=None):
        """Cheap queue/card index: never copy parameters or media result lists."""
        fields = ('run_id', 'task_id', 'webapp_id', 'app_name', 'status', 'message',
                  'progress', 'created_at', 'updated_at', 'cancel_requested', 'submission_admitted', 'cloud_success',
                  'task_document', 'submission_order')
        with self._condition:
            records = self._records.values()
            if webapp_id is not None:
                records = (record for record in records if record['webapp_id'] == str(webapp_id))
            return [dict({key: record[key] for key in fields if key in record},
                         origin=copy.deepcopy(record.get('origin') or {})) for record in records]

    def statuses(self, run_ids):
        """Read selected scheduler state without copying snapshots/results."""
        with self._condition:
            return {run_id: {'status': self._records[run_id]['status'],
                             'cancel_requested': bool(self._records[run_id].get('cancel_requested'))}
                    for run_id in run_ids if run_id in self._records}

    def restore_record(self, saved):
        """Hydrate one durably finished canvas item without any cloud request.

        The canvas stores individual batch items, not a node's aggregate. Their
        run/task identity and ordered results restore exactly one App card each.
        Synchronous subscribers are intentionally not called: the source is an
        already committed canvas snapshot, including the crash window between
        committing SUCCESS there and removing its temporary task index entry.
        """
        if not isinstance(saved, dict):
            raise ValueError('已保存任务格式无效')
        run_id = str(saved.get('run_id') or '').strip()
        status = str(saved.get('status') or '').upper()
        webapp_id = str(saved.get('webapp_id') or '').strip()
        if not run_id or not webapp_id or status not in FINAL:
            raise ValueError('只能恢复具有运行标识和应用标识的已结束任务')
        raw_id = saved.get('task_id')
        task_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else None
        task_id = task_id or None
        if status == 'SUCCESS' and not task_id:
            raise ValueError('已成功的 App 任务缺少 taskId')
        snapshot = public_snapshot(saved.get('snapshot') or {})
        origin = TaskStore.clean_context({'origin': saved.get('origin') or {}}).get('origin', {})
        results = []
        for value in saved.get('results') or []:
            if not isinstance(value, dict):
                raise ValueError('已保存任务的结果格式无效')
            # Keep useful provenance; exclude any arbitrary imported credentials.
            result = {key: copy.deepcopy(item) for key, item in value.items()
                      if key in {'path', 'text', 'type', 'kind', 'name', 'task_id', 'generation',
                                 'index', 'batch_index', 'repeat_index', 'mime', 'size'}}
            if result.get('path'):
                result['path'] = os.path.abspath(os.fspath(result['path']))
            result.setdefault('kind', result.get('type', 'file'))
            result.setdefault('type', result['kind'])
            result['task_id'] = task_id
            results.append(result)
        output_files = [os.path.abspath(os.fspath(value)) for value in saved.get('output_files', []) if value]
        output_files = list(dict.fromkeys([*output_files, *(r['path'] for r in results if r.get('path'))]))
        timestamp = saved.get('updated_at', saved.get('created_at', time.time()))
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError, OverflowError):
            timestamp = time.time()
        record = dict(run_id=run_id, task_id=task_id, webapp_id=webapp_id,
            app_name=str(saved.get('app_name') or snapshot.get('app_name') or webapp_id),
            status=status, submission_admitted=bool(saved.get('submission_admitted', task_id or status in FINAL)),
            progress=100 if status == 'SUCCESS' else saved.get('progress', 0),
            message=str(saved.get('message') or ('已恢复完成结果' if status == 'SUCCESS' else status)),
            results=results, output_files=output_files,
            input_files=copy.deepcopy(saved.get('input_files') or []),
            origin=origin, snapshot=snapshot, created_at=saved.get('created_at', timestamp), updated_at=timestamp)
        record['task_document'] = self.documents.reference('applications', run_id)
        record['submission_order'] = saved.get('submission_order')
        with self._notifications:
            with self._condition:
                existing = self._records.get(run_id)
                if existing and existing.get('task_id') and task_id and existing['task_id'] != task_id:
                    raise ValueError('已保存运行标识与现有 taskId 不一致')
                if (existing and existing['status'] in FINAL
                        and (existing['status'] == 'SUCCESS' or status != 'SUCCESS')):
                    return copy.deepcopy(existing)
                self._records[run_id] = record
                if task_id:
                    self._task_runs[task_id] = run_id
                self._condition.notify_all()
            if self.documents.get('applications', run_id) is None:
                self._create_document(record, snapshot, legacy=True)
            else:
                self.documents.patch('applications', run_id, self._document_state(record))
            if task_id:
                with self.lifecycle.lock:
                    self.owner._rh_task_to_wid[task_id] = webapp_id
                    self.owner._rh_status_entries[task_id] = status
                    self.owner._rh_running_tasks.setdefault(webapp_id, set()).discard(task_id)
                    self.owner._rh_app_active_count[webapp_id] = len(self.owner._rh_running_tasks[webapp_id])
                    if status == 'SUCCESS':
                        self.owner._rh_downloaded_tasks.add(task_id)
                    pending = self.lifecycle._pending_downloads.pop(task_id, None)
                    if pending is not None:
                        self.owner._rh_recovering_tasks.discard(task_id)
                if status in TERMINAL_STATUSES:
                    self.lifecycle.store.remove(task_id)
                    self.queue.task_status(task_id, status)
            # Loading several canvases must preserve completion chronology,
            # regardless of directory enumeration or card-opening order.
            with self._condition:
                latest = max((r for r in self._records.values()
                              if r['webapp_id'] == webapp_id and r['status'] in FINAL),
                             key=lambda r: r.get('updated_at', 0))
            with self.lifecycle.lock:
                self.owner._rh_app_last_result[webapp_id] = latest['status']
            self.changed.emit(run_id, copy.deepcopy(record))
            return copy.deepcopy(record)

    def _publish(self, run_id, **changes):
        with self._notifications:
            with self._condition:
                previous = self._records.get(run_id)
                if previous is None:
                    return None
                if previous['status'] in FINAL and changes.get('status', previous['status']) != previous['status']:
                    late_download = (previous['status'] == 'INTERRUPTED' and changes.get('cloud_success') is True and
                                     changes.get('status') in {'DOWNLOADING', 'DOWNLOAD_FAILED'})
                    if not late_download:
                        return copy.deepcopy(previous)
                if 'node_progress' in changes and previous['status'] != 'RUNNING':
                    return copy.deepcopy(previous)
                record = copy.deepcopy(previous)
                record.update(copy.deepcopy(changes))
                if record == previous:
                    return record
                record['updated_at'] = time.time()
                callbacks = tuple(self._subscribers)
            # Do not swallow an observer's disk error: recovery must retain
            # accepted tasks until their result references are durable.
            self.documents.patch('applications', run_id, self._document_state(record))
            if (record.get('task_id') and record['status'] in FINAL and
                    QtCore.QThread.currentThread() != self.thread()):
                self.documents.flush('applications', run_id)
            for callback in callbacks:
                callback(copy.deepcopy(record))
            with self._condition:
                self._records[run_id] = record
                self._condition.notify_all()
            if record['status'] in FINAL and previous['status'] != record['status']:
                # Pre-submission failures have no TASK_STATUS event. Keep App
                # summaries accurate even if no output-card page is open.
                with self.lifecycle.lock:
                    self.owner._rh_app_last_result[record['webapp_id']] = record['status']
            self.changed.emit(run_id, copy.deepcopy(record))
            return record

    def submit(self, snapshot):
        if self._closed or getattr(self.owner, '_closing', False):
            raise RuntimeError('客户端正在关闭，无法提交新任务')
        snapshot = copy.deepcopy(snapshot)
        webapp_id = str(snapshot.get('webapp_id') or '').strip()
        if not webapp_id or not isinstance(snapshot.get('nodes', []), list):
            raise ValueError('应用标识或参数无效')
        snapshot['webapp_id'] = webapp_id
        snapshot['base_url'] = normalize_base_url(snapshot.get('base_url'))
        snapshot['output_dir'] = os.path.abspath(os.fspath(snapshot['output_dir']))
        snapshot['decode_settings'] = frozen_decode_settings(snapshot.get('decode_settings'))
        run_id = str(snapshot.get('run_id') or uuid.uuid4().hex)
        snapshot['run_id'] = run_id
        with self._condition:
            if self._closed:
                raise RuntimeError('客户端正在关闭，无法提交新任务')
            if run_id in self._records:
                raise ValueError('运行标识已存在，不能重复提交')
            self._snapshots[run_id] = snapshot
            self._records[run_id] = dict(
                run_id=run_id, task_id=None, webapp_id=webapp_id,
                app_name=str(snapshot.get('app_name') or webapp_id), status='LOCAL_WAIT',
                submission_admitted=False,
                progress=0, message='', results=[], output_files=[],
                input_files=[os.path.abspath(node['fieldValue']) for node in snapshot.get('nodes', [])
                             if isinstance(node, dict) and isinstance(node.get('fieldValue'), str)
                             and os.path.isfile(node['fieldValue'])],
                origin=copy.deepcopy(snapshot.get('origin') or {}), snapshot=public_snapshot(snapshot),
                created_at=time.time(), updated_at=time.time())
            order = self.queue.reserve_orders(1)[0]
            self._records[run_id]['submission_order'] = order
            self._records[run_id]['task_document'] = self.documents.reference('applications', run_id)
            # Register before the durable publication. Later callers may finish
            # their callbacks first but cannot pass this not-yet-ready head.
            self._pending_runs[run_id] = [order, False]
        try:
            self._create_document(self.get(run_id), snapshot)
            self._publish(run_id, status='LOCAL_WAIT', message='等待提交')
            with self._condition:
                pending = self._pending_runs.get(run_id)
                if pending is not None:
                    pending[1] = True
                self._condition.notify_all()
            self._dispatch_submissions()
        except Exception:
            self.queue.release_order(order)
            with self._condition:
                self._pending_runs.pop(run_id, None)
                self._records.pop(run_id, None)
                self._snapshots.pop(run_id, None)
                self._condition.notify_all()
            raise
        return run_id

    def _dispatch_submissions(self):
        """Launch only eligible work; accepted QUEUED slots own no thread."""
        with self._condition:
            if self._dispatching or self._closed:
                return
            self._dispatching = True
            try:
                while self._pending_runs and len(self._workers) < self.queue.admission_limit:
                    run_id = next(iter(self._pending_runs))
                    order, ready = self._pending_runs[run_id]
                    if not ready or not self.queue.can_dispatch(order):
                        break
                    self._pending_runs.pop(run_id)
                    worker = threading.Thread(target=self._dispatch_one, args=(run_id, order),
                                              name='rh-submit-' + run_id[:16], daemon=True)
                    self._workers[run_id] = worker
                    try:
                        worker.start()
                    except Exception:
                        self._workers.pop(run_id, None)
                        self._pending_runs = dict(sorted(
                            {run_id: [order, ready], **self._pending_runs}.items(), key=lambda item: item[1][0]))
                        raise
            finally:
                self._dispatching = False
                self._condition.notify_all()

    def _dispatch_one(self, run_id, order):
        try:
            self._submit_worker(run_id, order)
        except Exception:
            # One persistence observer must not strand independent queued work.
            # Accepted IDs retain the lifecycle's recovery context.
            self.queue.release_order(order)
            with self._condition:
                self._workers.pop(run_id, None)
                self._snapshots.pop(run_id, None)
                self._condition.notify_all()
        finally:
            self._dispatch_submissions()

    def _stopped(self, run_id):
        return (self._closed or getattr(self.owner, '_closing', False)
                or self.lifecycle.stop_event.is_set() or run_id in self._cancel_requests
                or run_id in self._pause_requests)

    def _submit_worker(self, run_id, order):
        snapshot = self._snapshots[run_id]
        api = self.lifecycle._api()
        task_id = None
        post_attempted = False
        post_document = {}
        post_count = 0
        submission_response = None
        try:
            from api_calls.call_rh import (accepted_task_id, submission_response_kind,
                validate_response, RunningHubResponseError, RunningHubAPIError)
            api_keys = normalize_api_keys(snapshot.get('api_keys')) or normalize_api_keys(snapshot.get('api_key'))
            if not api_keys:
                raise ValueError('请先配置当前 RunningHub 站点的 API Key')
            api_key = ''
            original_nodes = copy.deepcopy(snapshot.get('nodes') or [])
            if any(not isinstance(node, dict) for node in original_nodes):
                raise ValueError('应用参数包含无效节点')
            # Upload IDs are account-scoped. Cache each credential separately,
            # preserving successful uploads across FIFO retry rounds.
            uploads, copied_inputs = {}, set()

            def nodes_for_key(key):
                nodes = copy.deepcopy(original_nodes)
                key_uploads = uploads.setdefault(api_key_id(key), {})
                for node in nodes:
                    if self._stopped(run_id):
                        raise SubmissionCancelled()
                    value = node.get('fieldValue')
                    upload = (str(node.get('fieldType') or '').upper() in {'IMAGE', 'VIDEO', 'AUDIO', 'UPLOAD'}
                              or bool(node.get('_rh_upload')))
                    if not upload or not isinstance(value, str):
                        continue
                    if not os.path.isfile(value):
                        if os.path.isabs(value) or os.path.splitdrive(value)[0]:
                            raise ValueError('上传文件不存在：' + os.path.basename(value))
                        continue
                    source = os.path.normcase(os.path.abspath(value))
                    if source not in key_uploads:
                        self._publish(run_id, message='上传：' + os.path.basename(value))
                        response = api.upload_file(value, api_key=key, base_url=snapshot['base_url'], timeout=120)
                        validate_response(response, 'Upload file', api_key=key)
                        data = response.get('data')
                        token = (data.get('fileName') or data.get('filePath')) if isinstance(data, dict) else None
                        if not isinstance(token, str) or not token.strip():
                            raise ValueError('上传失败，未返回文件标识')
                        key_uploads[source] = token
                        input_dir = snapshot.get('input_dir')
                        if input_dir and source not in copied_inputs:
                            try:
                                os.makedirs(input_dir, exist_ok=True)
                                destination = os.path.join(input_dir, os.path.basename(value))
                                if os.path.normcase(os.path.abspath(destination)) != source:
                                    shutil.copy2(value, destination)
                                copied_inputs.add(source)
                            except OSError:
                                pass
                    node['fieldValue'] = key_uploads[source]
                return nodes

            def request():
                nonlocal post_attempted, api_key, post_document, post_count, submission_response
                busy_response, failures = None, []
                for key_index, key in enumerate(api_keys, 1):
                    if self._stopped(run_id):
                        raise SubmissionCancelled()
                    self._publish(run_id, status='SUBMITTING',
                                  message=f'尝试第 {key_index}/{len(api_keys)} 个 API Key')
                    try:
                        nodes = nodes_for_key(key)
                    except SubmissionCancelled:
                        raise
                    except Exception as error:
                        # Uploads never create generation tasks. A failed upload
                        # on one account may safely try the next account.
                        code = getattr(error, 'code', None)
                        if code in (415, 421):
                            busy_response = {'code': code, 'msg': 'Upload resources busy'}
                        failures.append(f'第 {key_index} 个 Key 上传失败' + (f' (code={code})' if isinstance(code, int) else ''))
                        continue
                    # The reviewable actual request must exist before HTTP can
                    # create a paid task. Keys/passwords remain private refs.
                    post_count += 1
                    post_document = dict(
                        endpoint=snapshot['base_url'] + '/task/openapi/ai-app/run',
                        phase='submitting', attempt=post_count,
                        body=dict(webappId=snapshot['webapp_id'], nodeInfoList=copy.deepcopy(nodes)),
                        credential_ref=dict(site=snapshot['base_url'], key_id=api_key_id(key)))
                    self.documents.patch('applications', run_id, {'post': post_document})
                    self.documents.flush('applications', run_id)
                    try:
                        with self._condition:
                            if self._stopped(run_id):
                                raise SubmissionCancelled()
                            self._post_inflight.add(run_id)
                            post_attempted = True
                        submission_response = None
                        response = api.run_task(snapshot['webapp_id'], key, nodes,
                                                base_url=snapshot['base_url'], timeout=30)
                    except SubmissionCancelled:
                        raise
                    except Exception as error:
                        code = getattr(error, 'code', None)
                        http_code = getattr(getattr(error, 'response', None), 'status_code', None)
                        response = getattr(error, 'payload', None)
                        if response is None:
                            try:
                                response = error.response.json()
                            except (AttributeError, TypeError, ValueError):
                                response = None
                        submission_response = response
                        if accepted_task_id(response):
                            pass
                        elif isinstance(error, RunningHubAPIError):
                            response = {'code': code}
                        elif http_code in (401, 403):
                            response = {'code': 802}
                        else:
                            # Timeout/transport failure may follow acceptance.
                            # Do not call another credential or retry this POST.
                            self.documents.patch('applications', run_id,
                                                 {'post': dict(post_document, phase='unknown')})
                            raise
                    outcome = submission_response_kind(response)
                    submission_response = response
                    if outcome == 'accepted':
                        api_key = key
                        snapshot['api_key'] = key
                        snapshot['accepted_api_key'] = key
                        return response
                    with self._condition:
                        self._post_inflight.discard(run_id)
                    if outcome == 'busy':
                        post_attempted = False
                        self.documents.patch('applications', run_id,
                                             {'post': dict(post_document, phase='rejected', response_code=response.get('code'))})
                        busy_response = {'code': int(response['code']), 'msg': 'All eligible API Keys are busy'}
                    elif outcome == 'rejected':
                        post_attempted = False
                        self.documents.patch('applications', run_id,
                                             {'post': dict(post_document, phase='rejected', response_code=response.get('code'))})
                        failures.append(f'第 {key_index} 个 Key 拒绝提交 (code={response.get("code")})')
                    else:
                        self.documents.patch('applications', run_id, {'post': dict(post_document, phase='unknown')})
                        raise RunningHubResponseError('服务端未返回有效 taskId，提交结果无法确认')
                if busy_response is not None:
                    return busy_response
                raise SubmissionKeysRejected('所有 API Key 均未能提交：' + '；'.join(failures))

            def positive(name, default):
                try:
                    return max(1, int(snapshot.get(name, default)))
                except (TypeError, ValueError, OverflowError):
                    return default

            response = self.queue.submit(request, dict(webapp_id=snapshot['webapp_id'],
                run_id=run_id, tid=None, card=None, origin=snapshot.get('origin') or {},
                _submission_order=order), max_retries=positive('retry_max', 100),
                delay=positive('retry_delay', 5), concurrency=positive('retry_concurrency', 25),
                cancelled=lambda: self._stopped(run_id),
                on_wait=lambda attempt, reason: self._publish(run_id, status='LOCAL_WAIT',
                    message=f'等待队首重试 {attempt}/{positive("retry_max", 100)}'),
                on_submit=lambda: self._publish(run_id, status='SUBMITTING', submission_admitted=True, message='提交任务'))
            task_id = accepted_task_id(response)
            if not task_id:
                validate_response(response, 'Submit task')
                raise RunningHubResponseError('服务端未返回有效 taskId，无法确认提交结果')
            decode = copy.deepcopy(snapshot.get('decode_settings') or {})
            context = dict(webapp_id=snapshot['webapp_id'], app_name=snapshot.get('app_name', ''),
                run_id=run_id, base_url=snapshot['base_url'], api_key=api_key, key_id=api_key_id(api_key),
                output_dir=snapshot['output_dir'], decode_settings=decode,
                origin=copy.deepcopy(snapshot.get('origin') or {}), submission_order=order,
                task_document=self.documents.reference('applications', run_id))
            response_data = response.get('data') if isinstance(response, dict) else None
            returned_status = str(response_data.get('taskStatus') or '').upper() if isinstance(response_data, dict) else ''
            returned_status = 'CANCELED' if returned_status == 'CANCELLED' else returned_status
            if returned_status == 'SUCCESS':
                context.update(cloud_success=True, status='DOWNLOADING', started=True)
            # Acceptance is authoritative even if updating the large task
            # document fails. The compact recovery index retains the taskId.
            initial_status = ('DOWNLOADING' if returned_status == 'SUCCESS'
                              else returned_status if returned_status in {'RUNNING', 'FAILED', 'CANCELED'} else 'QUEUED')
            if decode.get('enabled'):
                context['decode_token'] = uuid.uuid4().hex
            with self.lifecycle.lock:
                self.owner._rh_live_task_ids.add(task_id)
                self.owner._rh_task_contexts[task_id] = context
            self.lifecycle.store.put(task_id, dict(context, status=initial_status))
            accepted_state = self._document_state(self.get(run_id))['state']
            accepted_state.update(status=initial_status, cloud_success=bool(context.get('cloud_success')))
            self.documents.patch('applications', run_id,
                                 dict(task_id=task_id, state=accepted_state,
                                      post=dict(post_document, phase='accepted', task_id=task_id)))
            if decode.get('password'):
                # A late SUCCESS response may follow session cleanup of a
                # pre-taskId submission. Its newly confirmed download retry
                # needs the exact original secret, which is still in memory.
                self.documents.set_secret('applications', run_id, decode['password'])
            if ((self._closed or getattr(self.owner, '_closing', False) or self.lifecycle.stop_event.is_set())
                    and not is_download_recovery(context)):
                # A response received after session shutdown never revives an
                # ordinary generation task or creates a restart record.
                self.adopt_task(task_id, context)
                self._publish(run_id, task_id=task_id, status='INTERRUPTED',
                              message='客户端会话已结束，任务不再自动跟踪')
                self.lifecycle._status(snapshot['webapp_id'], task_id, 'INTERRUPTED')
                return
            with self._condition:
                cancel_requested = run_id in self._cancel_requests
            if cancel_requested:
                context.update(cancel_requested=True, cancel_attempts=0, cancel_retry_at=0)
            # Protect against recovery racing the initial persistence. A
            # callback failing after a successful POST must not lose taskId.
            with self.lifecycle.lock:
                self.owner._rh_live_task_ids.add(task_id)
                self.owner._rh_task_contexts[task_id] = context
            self.adopt_task(task_id, context)
            initial_status = 'CANCELING' if cancel_requested else initial_status
            self.lifecycle.store.put(task_id, dict(context, status=initial_status))
            self._publish(run_id, task_id=task_id, status=initial_status,
                          cloud_success=bool(context.get('cloud_success')),
                          message='已提交，正在取消' if cancel_requested else '已提交，等待云端运行')
            self.lifecycle.emit(snapshot['webapp_id'], 'TASK_ADD:' + task_id)
            self.lifecycle._status(snapshot['webapp_id'], task_id, initial_status)
            if run_id in self._cancel_requests:
                if returned_status in {'SUCCESS', 'FAILED', 'CANCELED'}:
                    self.lifecycle._cancel_status_result(task_id, context, returned_status)
                else:
                    self._schedule_cancel(run_id, task_id, snapshot['webapp_id'], background=False)
            elif self._closed or getattr(self.owner, '_closing', False) or self.lifecycle.stop_event.is_set():
                if is_download_recovery(context):
                    self._publish(run_id, status='DOWNLOAD_FAILED', cloud_success=True, message='已生成结果，下次启动继续下载')
                else:
                    self._publish(run_id, status='INTERRUPTED', message='客户端会话已结束，任务不再自动跟踪')
                    self.lifecycle._status(snapshot['webapp_id'], task_id, 'INTERRUPTED')
        except SubmissionCancelled:
            self._publish(run_id, status='PAUSED' if self._closed and run_id not in self._cancel_requests else 'CANCELED',
                          message='提交已停止')
        except Exception as error:
            from api_calls.call_rh import RunningHubAPIError
            if task_id:
                # Retain the accepted task even if a subscriber or local write
                # failed; the one recovery path will retry its state/results.
                try:
                    self.lifecycle.store.put(task_id, dict(self.owner._rh_task_contexts.get(task_id, {}),
                                                          status='POLL_TIMEOUT'))
                    self._publish(run_id, task_id=task_id, status='POLL_TIMEOUT',
                                  message='任务已提交，等待恢复查询或保存：' + type(error).__name__)
                except Exception:
                    pass
            else:
                unknown = post_attempted and not isinstance(error, (RunningHubAPIError, SubmissionKeysRejected))
                status = 'UNKNOWN' if unknown else 'FAILED'
                diagnostic = submission_diagnostic(error, submission_response)
                message = ('提交结果未知：' + diagnostic['reason'] + '。未重复提交，请在 RunningHub 确认任务' if unknown else
                           str(error) if isinstance(error, (ValueError, RunningHubAPIError, SubmissionKeysRejected)) else
                           '运行失败：' + type(error).__name__)
                self._publish(run_id, status=status, message=message, submission_error=diagnostic)
        finally:
            self.queue.release_order(order)
            if task_id:
                with self.lifecycle.lock:
                    self.owner._rh_live_task_ids.discard(task_id)
            with self._condition:
                self._post_inflight.discard(run_id)
                self._workers.pop(run_id, None)
                self._snapshots.pop(run_id, None)
                self._condition.notify_all()
            self.lifecycle.wake_event.set()

    def adopt_task(self, task_id, context):
        """Reattach pure callbacks to a persisted task before recovery polls it."""
        task_id = str(task_id)
        context = dict(context)
        with self._condition:
            run_id = self._task_runs.get(task_id) or str(context.get('run_id') or 'recovered-' + task_id)
            known = run_id in self._records
        document = None if known else self.documents.get('applications', run_id)
        if document and (str(document.get('webapp_id') or '') != str(context['webapp_id']) or
                         document.get('task_id') not in (None, task_id)):
            raise ValueError('任务记录与已接收的 RunningHub 任务不匹配')
        saved_request = (document or {}).get('request') or {}
        if document and 'decode_settings' in saved_request:
            context['decode_settings'] = copy.deepcopy(saved_request['decode_settings'])
        context['task_document'] = self.documents.reference('applications', run_id)
        if is_download_recovery(context):
            # Preserve legacy download-phase evidence when a later missing-key
            # or local-processing status replaces its old status string.
            context['cloud_success'] = True
        # Legacy decode tokens do not reveal the settings used at submission.
        # Guessing from current preferences can corrupt or delete old outputs.
        context['decode_settings'] = frozen_decode_settings(
            context.get('decode_settings'),
            legacy_missing=bool(context.get('decode_token') and not context.get('decode_settings')))
        if (context['decode_settings'].get('password_required') and
                not context['decode_settings'].get('password')):
            context['decode_settings']['password'] = self.documents.secret('applications', run_id) or ''
        with self._condition:
            self._task_runs[task_id] = run_id
            if context.get('cancel_requested'):
                self._cancel_requests.add(run_id)
            if run_id not in self._records:
                snapshot = dict(copy.deepcopy(saved_request),
                    webapp_id=str(context['webapp_id']), app_name=context.get('app_name', ''),
                    base_url=context.get('base_url'), output_dir=context.get('output_dir'),
                    nodes=copy.deepcopy(saved_request.get('nodes') or []),
                    origin=copy.deepcopy(context.get('origin') or {}),
                    decode_settings=copy.deepcopy(context.get('decode_settings') or {}), run_id=run_id)
                self._records[run_id] = dict(run_id=run_id, task_id=task_id,
                    webapp_id=str(context['webapp_id']), app_name=context.get('app_name') or str(context['webapp_id']),
                    status=('CANCEL_FAILED' if context.get('status') == 'CANCEL_FAILED' else 'CANCELING')
                           if context.get('cancel_requested') else context.get('status', 'QUEUED'),
                    cancel_requested=bool(context.get('cancel_requested')), submission_admitted=True,
                    cloud_success=bool(context.get('cloud_success') or is_download_recovery(context)),
                    progress=0, message='恢复已生成结果的下载',
                    results=copy.deepcopy((document or {}).get('results') or []),
                    input_files=copy.deepcopy((document or {}).get('input_files') or []),
                    output_files=copy.deepcopy((document or {}).get('output_files') or []),
                    origin=snapshot['origin'], task_document=context['task_document'],
                    submission_order=context.get('submission_order'),
                    snapshot=public_snapshot(snapshot), created_at=time.time(), updated_at=time.time())
            elif not self._records[run_id].get('task_id'):
                self._records[run_id]['task_id'] = task_id
            context['run_id'] = run_id
        if not known and document is None:
            self._create_document(self.get(run_id), snapshot, legacy=True)
        elif not known:
            restored = self._document_state(self.get(run_id))
            post = copy.deepcopy(document.get('post') or {})
            if post.get('body') and post.get('phase') != 'pending':
                restored['post'] = dict(post, phase='accepted', task_id=task_id)
            self.documents.patch('applications', run_id, restored)
        if not known:
            # Migration adds only a stable document pointer; request bodies and
            # secret data never bloat the compact accepted-task recovery index.
            self.lifecycle.store.put(task_id, context)
        context['on_files_saved'] = lambda paths: self._track_paths(run_id, paths)
        context['on_downloaded'] = lambda paths: self._process_outputs(run_id, task_id, context, paths)
        with self.lifecycle.lock:
            live = self.owner._rh_task_contexts.get(task_id, {})
            if live.get('cancel_requested'):
                for key in ('cancel_requested', 'cancel_attempts', 'cancel_retry_at', 'cancel_acknowledged',
                            'cancel_generation', 'cancel_retry_available'):
                    if key in live:
                        context[key] = live[key]
            self.owner._rh_task_contexts[task_id] = context
        return context

    def lifecycle_event(self, webapp_id, event):
        if not event.startswith(('TASK_STATUS:', 'TASK_DOWNLOAD_NOTE:')):
            return
        parts = event.split(':', 2)
        if len(parts) != 3:
            return
        task_id, value = parts[1:]
        with self._condition:
            run_id = self._task_runs.get(task_id)
        if not run_id:
            return
        current = self.get(run_id)
        if current is None or current['webapp_id'] != str(webapp_id):
            return
        if event.startswith('TASK_DOWNLOAD_NOTE:'):
            self._publish(run_id, message=value[:500])
            return
        messages = {'QUEUED': '云端排队中', 'RUNNING': '运行中', 'DOWNLOADING': '下载和处理输出',
                    'DOWNLOAD_FAILED': '等待下载重试', 'SUCCESS': '任务完成', 'FAILED': '任务失败',
                    'CANCELED': '任务已取消', 'POLL_TIMEOUT': '等待重新查询状态',
                    'WAITING_FOR_KEY': '等待当前站点 API Key', 'WAITING_FOR_SECRET': '等待本地解码密码',
                    'INTERRUPTED': '客户端会话已结束', 'CANCELING': '正在取消，自动重试并确认状态',
                    'CANCEL_FAILED': '取消尚未确认，可再次取消；保留任务并继续查询'}
        changes = dict(status=value, message=messages.get(value, value))
        with self.lifecycle.lock:
            task_context = self.owner._rh_task_contexts.get(task_id, {})
            if task_context.get('cloud_success') or value in {'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET', 'SUCCESS'}:
                changes['cloud_success'] = True
        if run_id in self._cancel_requests:
            changes['cancel_requested'] = True
            if value == 'WAITING_FOR_KEY':
                changes['message'] = '等待任务原 API Key，补齐后自动继续取消'
        if value in {'DOWNLOADING', 'DOWNLOAD_FAILED', 'SUCCESS'}:
            changes['progress'] = 100
        record = self.get(run_id)
        if value == 'SUCCESS' and record.get('warning'):
            changes['message'] = '任务完成；' + record['warning']
        self._publish(run_id, **changes)

    def _track_paths(self, run_id, paths):
        record = self.get(run_id)
        if record:
            merged = list(dict.fromkeys([*record['output_files'], *(os.path.abspath(path) for path in paths)]))
            self._publish(run_id, output_files=merged)

    def _process_outputs(self, run_id, task_id, context, paths):
        from aetherloom_core.rh_outputs import OutputDownloadCancelled, GRC_DECODE_LOCK
        from aetherloom_core.rh_storage import (is_decoded_output, valid_decoded_output,
            decoded_output_path, remember_decoded_output)
        from aetherloom_core.services.decoding import grc
        cancelled = lambda: self.lifecycle._cancelled(task_id)
        decode = copy.deepcopy(context.get('decode_settings') or {})
        token = context.get('decode_token')
        needs_decode = bool(decode.get('enabled') and token and
                            any(Path(path).suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS for path in paths))
        if needs_decode and decode.get('settings_missing'):
            raise MissingDecodeConfiguration()
        if needs_decode and decode.get('mode') == 'sst' and decode.get('password_required') and not decode.get('password'):
            decode['password'] = self.documents.secret('applications', run_id) or ''
            if not decode.get('password'):
                raise MissingDecodePassword()
        output_dir = context['output_dir']
        results, warning = [], ''
        for saved_path in paths:
            if cancelled():
                raise OutputDownloadCancelled('输出处理已暂停')
            presented = saved_path
            decodable = Path(saved_path).suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
            if decode.get('enabled') and token and decodable and not is_decoded_output(saved_path, output_dir, task_id=task_id,
                                                token=token, cancelled=cancelled):
                restored = valid_decoded_output(saved_path, task_id, token, cancelled=cancelled)
                try:
                    if not restored:
                        os.makedirs(output_dir, exist_ok=True)
                        with tempfile.TemporaryDirectory(prefix='.decode-', dir=output_dir) as staging:
                            suffix = Path(saved_path).suffix
                            target = os.path.join(staging, 'result' + suffix)
                            if decode.get('mode', 'grc') == 'sst':
                                target = _decode_sstool(saved_path, target, str(decode.get('password') or ''))
                                success = bool(target)
                            else:
                                with GRC_DECODE_LOCK:
                                    if cancelled():
                                        raise OutputDownloadCancelled('输出处理已暂停')
                                    old_cols, old_rows = grc.grid_cols, grc.grid_rows
                                    try:
                                        grc.grid_cols = int(decode.get('grid_cols', 32))
                                        grc.grid_rows = grc.grid_cols + 2
                                        success = bool(grc.reverse_image_grid(saved_path, target) if suffix.lower() in IMAGE_EXTENSIONS
                                                       else grc.restore_video_cv2(saved_path, target))
                                    finally:
                                        grc.grid_cols, grc.grid_rows = old_cols, old_rows
                            if cancelled():
                                raise OutputDownloadCancelled('输出处理已暂停')
                            if (not success or not target or not os.path.isfile(target) or os.path.getsize(target) <= 0
                                    or Path(target).resolve().parent != Path(staging).resolve()):
                                raise ValueError('本地解码未生成完整文件')
                            restored = decoded_output_path(output_dir, saved_path, Path(target).suffix)
                            os.replace(target, restored)
                            self._track_paths(run_id, (restored,))
                            remember_decoded_output(saved_path, restored, task_id, token, cancelled=cancelled)
                    if cancelled():
                        raise OutputDownloadCancelled('输出处理已暂停')
                    presented = restored
                    self._track_paths(run_id, (restored,))
                    if decode.get('delete_original', True):
                        try:
                            os.remove(saved_path)
                        except OSError:
                            pass
                except OutputDownloadCancelled:
                    raise
                except Exception:
                    presented = saved_path
                    warning = '本地解码失败，已保留原始输出'
            results.append(result_for_path(presented, task_id))
        # A complete ordered set is published only after every file has been
        # downloaded and postprocessed. UI presentation waits for SUCCESS.
        self._publish(run_id, results=results, warning=warning)

    def _sync_progress(self):
        with self.lifecycle.lock:
            progress_entries = copy.deepcopy(getattr(self.owner, '_rh_progress_entries', {}))
        for record in self.records():
            value = progress_entries.get(record.get('task_id'))
            if record['status'] == 'RUNNING' and value:
                try:
                    percent = 100 if value.get('finished') else float(value.get('overall_percent') or 0)
                    self._publish(record['run_id'], progress=percent, node_progress=value)
                except Exception:
                    pass  # Optional visual progress must not stop recovery.

    def bind_card(self, run_id, card):
        record = self.get(run_id)
        if not record:
            return
        task_id = record.get('task_id')
        if task_id:
            with self.lifecycle.lock:
                context = self.owner._rh_task_contexts.get(task_id)
                if context is not None:
                    context['card'] = card
        with self.queue.condition:
            for entry in self.owner._rh_retry_queue:
                if entry.get('run_id') == run_id:
                    entry['card'] = card

    def cancel(self, run_id):
        record = self.get(run_id)
        if not record or record['status'] in FINAL:
            return
        with self._condition:
            self._cancel_requests.add(run_id)
            post_inflight = run_id in self._post_inflight
            pending = self._pending_runs.pop(run_id, None)
            if pending is not None:
                self._snapshots.pop(run_id, None)
            self._condition.notify_all()
        if not record.get('task_id'):
            self._publish(run_id, status='CANCELING' if post_inflight else 'CANCELED',
                          cancel_requested=True,
                          message='提交响应返回后自动取消' if post_inflight else '已取消本地等待任务')
        if pending is not None:
            # Canvas observers first stop dependent queued items, then this
            # cancellation can release the next shared FIFO position.
            self.queue.release_order(pending[0])
        self.queue.cancel_matching(lambda entry: entry.get('run_id') == run_id)
        self.queue.wake()
        if record.get('task_id'):
            self._schedule_cancel(run_id, record['task_id'], record['webapp_id'])

    def pause_unsubmitted(self, run_id):
        """Stop a removed canvas's local submission; an in-flight accepted ID survives."""
        with self._condition:
            record = self._records.get(run_id)
            if record is None or record.get('task_id') or record['status'] in FINAL:
                return
            self._pause_requests.add(run_id)
            pending = self._pending_runs.pop(run_id, None)
            if pending is not None:
                self._snapshots.pop(run_id, None)
            self._condition.notify_all()
        if pending is not None:
            self.queue.release_order(pending[0])
            self._publish(run_id, status='PAUSED', message='本地等待已暂停')
        # Unlike cancel_matching this does not mark a card cancelled: if POST
        # returns a taskId during the pause, shared recovery must keep following it.
        self.queue.wake()

    def _schedule_cancel(self, run_id, task_id, webapp_id, *, background=True):
        with self._condition:
            if run_id in self._cancel_workers:
                return
            current = self._records.get(run_id)
            if current is None or current['status'] in FINAL:
                return
            self._cancel_workers.add(run_id)
        try:
            # This only persists intent and wakes the bounded status workers;
            # no synchronous HTTP and no thread for each click/task.
            self.lifecycle.cancel_task(task_id, webapp_id)
        finally:
            with self._condition:
                self._cancel_workers.discard(run_id)

    def provide_decode_password(self, run_id, password):
        record = self.get(run_id)
        if not record or not record.get('task_id'):
            return
        with self.lifecycle.lock:
            context = self.owner._rh_task_contexts.get(record['task_id'], {})
            if context.get('cancel_requested'):
                return
            self.documents.set_secret('applications', run_id, str(password))
            settings = copy.deepcopy(context.get('decode_settings') or {})
            settings['password'] = str(password)
            settings['password_required'] = bool(password)
            context['decode_settings'] = settings
            self.owner._rh_task_contexts[record['task_id']] = context
            self.lifecycle.store.put(record['task_id'], context)
            self.lifecycle._download_retry_due.pop(record['task_id'], None)
        snapshot = copy.deepcopy(record['snapshot'])
        snapshot['decode_settings'] = public_snapshot({'decode_settings': settings})['decode_settings']
        self.documents.patch('applications', run_id,
                             {'request': snapshot, 'decode_settings': snapshot['decode_settings']})
        self._publish(run_id, snapshot=snapshot)
        self._publish(run_id, status='DOWNLOADING', message='重新处理已生成结果')
        self.lifecycle.wake_event.set()

    def provide_decode_settings(self, run_id, settings):
        """Explicitly repair a legacy task whose original settings were absent."""
        record = self.get(run_id)
        if not record or not record.get('task_id'):
            return
        with self.lifecycle.lock:
            context = self.owner._rh_task_contexts.get(record['task_id'], {})
            if context.get('cancel_requested'):
                return
            if not (context.get('decode_settings') or {}).get('settings_missing'):
                raise ValueError('该任务已有固定解码配置，不能跟随当前 App 设置变更')
            settings = frozen_decode_settings(settings)
            settings.pop('settings_missing', None)
            context['decode_settings'] = settings
            self.owner._rh_task_contexts[record['task_id']] = context
            self.documents.set_secret('applications', run_id, settings.get('password') or '')
            self.lifecycle.store.put(record['task_id'], context)
            self.lifecycle._download_retry_due.pop(record['task_id'], None)
        snapshot = copy.deepcopy(record['snapshot'])
        snapshot['decode_settings'] = public_snapshot({'decode_settings': settings})['decode_settings']
        self.documents.patch('applications', run_id,
                             {'request': snapshot, 'decode_settings': snapshot['decode_settings']})
        self._publish(run_id, snapshot=snapshot, status='DOWNLOADING', message='按补齐的任务设置继续处理已生成结果')
        self.lifecycle.wake_event.set()

    def wait(self, run_id, cancelled=None):
        while True:
            with self._condition:
                record = copy.deepcopy(self._records.get(run_id))
                if record is None or record['status'] in FINAL or self._closed or record['status'] == 'PAUSED':
                    return record
            if cancelled and cancelled():
                return self.get(run_id)
            with self._condition:
                self._condition.wait(.2)

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending_runs.items())
            self._pending_runs.clear()
            for run_id, _ in pending:
                self._snapshots.pop(run_id, None)
            self._condition.notify_all()
        self._progress_timer.stop()
        self._unsubscribe_dispatch()
        # Closing is a local session boundary, not an instruction to send cloud
        # cancellations. Only confirmed generation/output retries survive it.
        self.lifecycle.stop()
        self.lifecycle.store.retain_download_retries(closing=True)
        for header in self.record_headers():
            if header['status'] in FINAL or is_download_recovery(header):
                continue
            self._publish(header['run_id'], status='INTERRUPTED', message='客户端会话已结束')
            if header.get('task_id'):
                self.lifecycle._status(header['webapp_id'], header['task_id'], 'INTERRUPTED')
        self.queue.release_orders(entry[0] for _, entry in pending)
        self.queue.wake()
        retained = self.lifecycle.store.read()
        self.documents.close([str(context.get('run_id') or 'recovered-' + task_id)
                              for task_id, context in retained.items() if is_download_recovery(context)])
        with self._condition:
            self._condition.notify_all()


def _decode_sstool(src_path, out_path, password=''):
    try:
        import numpy as _np
        from PIL import Image as _Img
        import moviepy.editor as _mpe
        import struct as _struct
    except Exception:
        return False

    WATERMARK_SKIP_W_RATIO = 0.40
    WATERMARK_SKIP_H_RATIO = 0.08
    TRY_K = (2, 6, 8)
    IMAGE_EXTS_LOCAL = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')
    VIDEO_EXTS_LOCAL = ('.mp4', '.mov', '.avi', '.webm', '.mkv', '.gif')

    def _load_image_array(path):
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext in IMAGE_EXTS_LOCAL:
            img = _Img.open(path).convert('RGB')
            try:
                arr = _np.array(img).astype(_np.uint8)
            finally:
                try:
                    img.close()
                except Exception:
                    pass
            return arr
        if ext in VIDEO_EXTS_LOCAL:
            clip = _mpe.VideoFileClip(path)
            try:
                frame = clip.get_frame(0)
            finally:
                try:
                    clip.close()
                except Exception:
                    pass
            return frame.astype(_np.uint8)
        raise ValueError('unsupported input type')

    def _extract_payload_with_k(arr, k):
        h, w, c = arr.shape
        skip_w = int(w * WATERMARK_SKIP_W_RATIO)
        skip_h = int(h * WATERMARK_SKIP_H_RATIO)
        mask2d = _np.ones((h, w), dtype=bool)
        if skip_w > 0 and skip_h > 0:
            mask2d[:skip_h, :skip_w] = False
        mask3d = _np.repeat(mask2d[:, :, None], c, axis=2)
        flat = arr.reshape(-1)
        idxs = _np.flatnonzero(mask3d.reshape(-1))
        vals = (flat[idxs] & ((1 << k) - 1)).astype(_np.uint8)
        ub = _np.unpackbits(vals, bitorder='big').reshape(-1, 8)[:, -k:]
        bits = ub.reshape(-1)
        if len(bits) < 32:
            raise ValueError('Insufficient image data')
        len_bits = bits[:32]
        length_bytes = _np.packbits(len_bits, bitorder='big').tobytes()
        header_len = _struct.unpack('>I', length_bytes)[0]
        total_bits = 32 + header_len * 8
        if header_len <= 0 or total_bits > len(bits):
            raise ValueError('Payload length invalid')
        payload_bits = bits[32:32 + header_len * 8]
        return _np.packbits(payload_bits, bitorder='big').tobytes()

    def _generate_key_stream(password_local, salt, length):
        import hashlib as _hashlib
        key_material = (password_local + salt.hex()).encode('utf-8')
        out = bytearray()
        counter = 0
        while len(out) < length:
            out.extend(_hashlib.sha256(key_material + str(counter).encode('utf-8')).digest())
            counter += 1
        return bytes(out[:length])

    def _parse_header(header, password_local):
        idx = 0
        if len(header) < 1:
            raise ValueError('Header corrupted')
        has_pwd = header[0] == 1
        idx += 1
        pwd_hash = b''
        salt = b''
        if has_pwd:
            if len(header) < idx + 32 + 16:
                raise ValueError('Header corrupted')
            pwd_hash = header[idx:idx + 32]
            idx += 32
            salt = header[idx:idx + 16]
            idx += 16
        if len(header) < idx + 1:
            raise ValueError('Header corrupted')
        ext_len = header[idx]
        idx += 1
        if len(header) < idx + ext_len + 4:
            raise ValueError('Header corrupted')
        ext = header[idx:idx + ext_len].decode('utf-8', errors='ignore')
        idx += ext_len
        data_len = _struct.unpack('>I', header[idx:idx + 4])[0]
        idx += 4
        data = header[idx:]
        if len(data) != data_len:
            raise ValueError('Data length mismatch')
        if not has_pwd:
            return data, ext
        if not password_local:
            raise ValueError('Password required')
        import hashlib as _hashlib
        check_hash = _hashlib.sha256((password_local + salt.hex()).encode('utf-8')).digest()
        if check_hash != pwd_hash:
            raise ValueError('Wrong password')
        ks = _generate_key_stream(password_local, salt, len(data))
        plain = bytes(a ^ b for a, b in zip(data, ks))
        return plain, ext

    def _decode_array(arr, password_local):
        for k in TRY_K:
            try:
                header = _extract_payload_with_k(arr, k)
                raw, ext = _parse_header(header, password_local)
                return raw, ext
            except Exception:
                continue
        raise RuntimeError('解析失败: 无法从图像提取载荷')

    def _save_payload(raw, ext, out_base):
        import re
        if not re.fullmatch(r'\.?[A-Za-z0-9]{1,16}(?:\.binpng)?', ext):
            raise ValueError('Invalid decoded extension')
        final_ext = ext
        if ext.endswith('.binpng'):
            tmp_png = out_base + '.binpng'
            with open(tmp_png, 'wb') as f:
                f.write(raw)
            try:
                img = _Img.open(tmp_png).convert('RGB')
                arr = _np.array(img).astype(_np.uint8)
            finally:
                try:
                    img.close()
                except Exception:
                    pass
                try:
                    os.unlink(tmp_png)
                except Exception:
                    pass
            mp4_bytes = arr.reshape(-1, 3).reshape(-1).tobytes().rstrip(b'\x00')
            final_path = out_base + '.mp4'
            with open(final_path, 'wb') as f:
                f.write(mp4_bytes)
            final_ext = 'mp4'
        else:
            if ext.startswith('.'):  # keep leading dot
                final_path = out_base + ext
            else:
                final_path = out_base + '.' + ext
            with open(final_path, 'wb') as f:
                f.write(raw)
        return final_path, final_ext

    try:
        arr = _load_image_array(src_path)
        raw, ext = _decode_array(arr, password or '')
        out_base, _ = os.path.splitext(out_path)
        final_path, _ = _save_payload(raw, ext, out_base)
        return final_path if final_path and os.path.exists(final_path) else ''
    except Exception:
        return ''
