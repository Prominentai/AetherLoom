"""FIFO NovelAI admission, concurrent accepted requests, and safe key rotation."""
import copy
import math
import time
import threading
import uuid
from pathlib import Path
from collections import OrderedDict

from PyQt5 import QtCore
from . import client, execution, storage, task_records
from .jobs import Job


TERMINAL = frozenset(('succeeded', 'failed', 'cancelled'))
WAITING = frozenset(('queued', 'preparing', 'ready', 'retry_wait'))
MAX_STAGED_AHEAD = 8


def _worker(context, job, operation):
    # Keep the event local: shutdown may clear this context as soon as done.
    shutdown_event = context['_shutdown']
    try:
        return operation(context, job)
    finally:
        context['_worker_done'] = True
        if shutdown_event.is_set():
            frozen = context.get('frozen')
            if frozen is not None:
                try:
                    frozen.close()
                except OSError:
                    pass
            context.clear()


class QueueService(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    progress = QtCore.pyqtSignal(str, object)
    preview = QtCore.pyqtSignal(str, object)
    completed = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str, object)
    _record_result = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks = []  # Public display metadata; callers must not mutate entries.
        self._by_id, self._contexts = {}, {}
        self._stage_job = self._run_job = None
        self._stage_id = self._run_id = None
        self._save_failed_ids = set()
        self._run_jobs = {}
        self._concurrency = 3
        self._retry_interval = 5
        self._retry_timer = QtCore.QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._schedule)
        self._rate_limited = False
        self._next_index = 1
        self._closed = False
        self._shutdown_event = threading.Event()
        self._suspend = 0
        self._pump_scheduled = False
        self._record_states, self._record_paths = {}, {}
        self._historical = OrderedDict()
        self._record_writer = task_records.RecordWriter(self._record_result.emit)
        self._record_result.connect(self._record_written)

    @property
    def busy(self):
        return bool(self._stage_job or self._run_jobs or
                    any(task['state'] in WAITING for task in self.tasks))

    @property
    def running_id(self):
        return self._run_id

    @property
    def active_job(self):
        return self._run_job

    @property
    def running_ids(self):
        return list(self._run_jobs)

    @property
    def active_count(self):
        return len(self._run_jobs)

    @property
    def concurrency(self):
        return self._concurrency

    @property
    def paid_concurrency(self):
        """Compatibility alias; every task uses the same bounded worker pool."""
        return self.concurrency

    @property
    def retry_interval(self):
        return self._retry_interval

    @property
    def rate_limited(self):
        return self._rate_limited

    @property
    def paused(self):
        return bool(self._save_failed_ids)

    def set_concurrency(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
            raise ValueError('同时请求数必须是 1 到 3 的整数。')
        if self._closed:
            raise RuntimeError('任务队列已经关闭。')
        if value != self._concurrency:
            self._concurrency = value
            self.changed.emit()
            self._schedule()
        return value

    def set_paid_concurrency(self, value):
        return self.set_concurrency(value)

    def set_retry_interval(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 300:
            raise ValueError('重试间隔必须是 1 到 300 秒的整数。')
        if self._closed:
            raise RuntimeError('任务队列已经关闭。')
        self._retry_interval = value
        for task in self.tasks:
            if task['state'] == 'retry_wait':
                task['next_retry'] = task.get('last_rejected', time.time()) + value
        self._retry_timer.stop()
        self.changed.emit()
        self._schedule()
        return value

    def resume_dispatch(self):
        """Release only the 429 hold; failed tasks are never retried."""
        if self._closed:
            raise RuntimeError('任务队列已经关闭。')
        if not self._rate_limited:
            return False
        self._rate_limited = False
        self.changed.emit()
        self._schedule()
        return True

    def _sync_active(self):
        self._run_id = next(iter(self._run_jobs), None)
        self._run_job = self._run_jobs.get(self._run_id)

    @property
    def has_unsaved(self):
        return any(bool(context.get('pending')) for context in self._contexts.values())

    def get_task(self, task_id):
        return self._by_id.get(task_id) or self._historical.get(task_id)

    def enqueue(self, snapshot, draft, token, output_dir, input_dir, data_dir, *, billing=None, account_evidence=None, batch=None, source=None):
        if self._closed:
            raise RuntimeError('任务队列已经关闭。')
        if not isinstance(snapshot, dict) or (draft is not None and not isinstance(draft, dict)):
            raise ValueError('绘图参数或遮罩数据格式无效。')
        values = [token] if isinstance(token, str) else token
        if not isinstance(values, (list, tuple)):
            raise ValueError('请先配置 NovelAI API Key。')
        tokens = tuple(dict.fromkeys(client._token(value) for value in values if value))
        if not tokens:
            raise ValueError('请先配置 NovelAI API Key。')
        token = tokens[0]
        snapshot, draft = copy.deepcopy(snapshot), copy.deepcopy(draft)
        sources = execution.capture_sources(snapshot, draft)
        identity = uuid.uuid4().hex
        action = str(snapshot.get('action', 'generate'))
        title = ' '.join(str(snapshot.get('prompt', '')).split())
        for secret in sorted(tokens, key=len, reverse=True):
            title = title.replace(secret, '[已隐藏]')
        title = title[:100]
        title = title or {'generate': '文生图', 'img2img': '图生图',
            'infill': '局部重绘', 'upscale': '超分辨率', 'augment': 'Director Tools'}.get(action, '图像任务')
        task = dict(id=identity, index=self._next_index, state='queued', title=title,
                    model=str(snapshot.get('model', '')), action=action, created=time.time(),
                    message='等待准备输入', progress=None, results=[], submitted=False, accepted=False,
                    attempts=0, key_index=1, key_count=len(tokens), next_retry=None,
                    billing='serial', billing_info={}, expected_results=(1 if action in ('upscale', 'augment') else snapshot.get('n_samples', 1)),
                    received_results=0, saved_results=0, result_unknown=False,
                    batch=task_records.clean(batch or {}, tokens),
                    source=task_records.clean(source or {'type': 'novelai_page'}, tokens))
        self._next_index += 1
        self.tasks.append(task)
        self._by_id[identity] = task
        self._contexts[identity] = dict(snapshot=snapshot, draft=draft, token=token,
            output_dir=str(output_dir), input_dir=str(input_dir), data_dir=str(data_dir),
            sources=sources, frozen=None, cancel_requested=False, billing='serial',
            tokens=tokens, key_index=0, rejected_keys=set(), round_seen=set(),
            accepted=False, rotating=False, request_state={},
            _shutdown=self._shutdown_event, _worker_done=False)
        state = task_records.RecordState(self._record_writer, data_dir, task, snapshot, draft, tokens)
        self._record_states[identity] = state
        self._record_paths[identity] = task['record_path'] = str(state.path)
        task['record_error'] = ''
        self._contexts[identity]['record'] = state
        self._persist(task)
        self.changed.emit()
        self._schedule()
        return identity

    def _persist(self, task, *, copy_value=None):
        state = self._record_states.get(task['id'])
        if state is None:
            return
        task['saved_results'] = len(task.get('results', []))
        state.update(task=task, copy_value=copy_value)

    def _record_written(self, identity, value):
        task = self._by_id.get(identity)
        state = self._record_states.get(identity)
        if task is None or value.get('deleted'):
            return
        version = value.get('version', 0)
        if version < task.get('_record_result_version', 0):
            return
        task['_record_result_version'] = version
        error = value.get('error', '')
        if error and state is not None and version < state.version:
            return
        task['record_error'] = self._safe_message(error, {'tokens': getattr(state, '_secrets', ())}) if error else ''
        if state is not None and not error:
            state.accept_mask(value['version'], value.get('mask'))
            if (value['version'] == state.version and task['state'] in TERMINAL
                    and identity not in self._contexts):
                self._record_states.pop(identity, None)
        self.changed.emit()

    def copy_task(self, task_id):
        state = self._record_states.get(task_id)
        if state is not None:
            return task_records.independent_copy(state.get_copy(), state.path)
        path = self._record_paths.get(task_id)
        if not path:
            raise ValueError('找不到此任务的参数记录。')
        document = task_records.read(path, previous_session=False)
        value = document.get('copy')
        if not isinstance(value, dict) or not isinstance(value.get('options'), dict):
            raise ValueError('此任务记录没有可复制的参数。')
        return task_records.independent_copy(value, path)

    def history_page(self, data_dir, *, limit=60, before=None):
        page = task_records.list_page(data_dir, limit=limit, before=before)
        tasks = []
        for document in page['records']:
            task = copy.deepcopy(document['task'])
            task.update(record_path=document['record_path'], record_error='', historical=True)
            identity = task.get('id')
            if not isinstance(identity, str):
                continue
            if identity in self._by_id:
                task = copy.deepcopy(self._by_id[identity])
            else:
                self._historical[identity] = task
                self._historical.move_to_end(identity)
                self._record_paths[identity] = document['record_path']
            tasks.append(task)
        while len(self._historical) > 120:
            identity, unused = self._historical.popitem(last=False)
            if identity not in self._by_id:
                self._record_paths.pop(identity, None)
        return {'tasks': tasks, 'next_cursor': page['next_cursor'], 'errors': page['errors']}

    def flush_records(self, timeout=5):
        return self._record_writer.flush(timeout)

    def update_account(self, token, evidence):
        # Kept for older callers. Account/fee estimates do not schedule tasks.
        return 0

    def _safe_message(self, value, context):
        # Replace every complete secret before the one bounded formatting pass.
        # Truncating after each key could expose part of a later or longer key.
        text = str(value)
        tokens = context.get('tokens', (context.get('token', ''),))
        for token in sorted((token for token in tokens if token), key=len, reverse=True):
            text = text.replace(token, '[凭据已隐藏]')
        return client._safe_message(text)

    def _arm_retry(self, deadline):
        delay = max(1, min(2 ** 31 - 1, math.ceil((deadline - time.time()) * 1000)))
        # One timer for the FIFO head, not one timer or thread per waiting task.
        if not self._retry_timer.isActive() or self._retry_timer.remainingTime() > delay:
            self._retry_timer.start(delay)

    def _schedule(self):
        if self._closed or self._suspend or self._pump_scheduled:
            return
        self._pump_scheduled = True
        QtCore.QTimer.singleShot(0, self._pump)

    def _pump(self):
        self._pump_scheduled = False
        if self._closed or self._suspend:
            return
        staged_count = sum(task['state'] in ('ready', 'retry_wait') for task in self.tasks)
        if (self._stage_job is None and not self.paused and not self.rate_limited
                and staged_count < MAX_STAGED_AHEAD):
            task = next((task for task in self.tasks if task['state'] == 'queued'), None)
            if task is not None:
                self._start_stage(task)
        while (not self._closed and not self.paused and not self.rate_limited
               and self.active_count < self._concurrency):
            # Only the head may submit until its generation POST is accepted.
            # Input preparation / Vibe encoding / a sent POST are not acceptance.
            # Accepted requests may keep streaming while the next head submits.
            if any(job._generation_request and (ctx := self._contexts.get(identity))
                   and not ctx.get('accepted') for identity, job in self._run_jobs.items()):
                break
            task = next((task for task in self.tasks
                         if task['state'] not in TERMINAL and task['id'] not in self._run_jobs), None)
            if task is None:
                break
            if task['state'] == 'retry_wait':
                deadline = task['next_retry']
                if deadline > time.time():
                    self._arm_retry(deadline)
                    break
                context = self._contexts[task['id']]
                context['round_seen'].clear()
                index = next(i for i in range(len(context['tokens'])) if i not in context['rejected_keys'])
                context['key_index'], context['token'] = index, context['tokens'][index]
                task.update(state='ready', next_retry=None, key_index=index + 1)
            if task['state'] != 'ready':
                break
            self._start_run(task, dispatched_at=time.time())

    def _start_stage(self, task):
        identity = task['id']
        context = self._contexts[identity]
        task.update(state='preparing', message='正在固化输入文件')
        self._persist(task)
        context['_worker_done'] = False
        job = self._stage_job = Job(lambda current: _worker(context, current, execution.freeze_inputs), self)
        self._stage_id = identity
        job.succeeded.connect(lambda value: self._stage_ready(identity, value))
        job.failed.connect(lambda error: self._stage_failed(identity, error))
        job.finished.connect(lambda: self._finished('stage', identity, job))
        self.changed.emit()
        job.start()

    def _stage_ready(self, identity, frozen):
        task, context = self._by_id.get(identity), self._contexts.get(identity)
        if context is None or task is None:
            frozen.close()
            return
        context['frozen'] = frozen
        if self._closed or context['cancel_requested']:
            message = ('已停止轮试，不再提交后续请求。' if task.get('attempts', 0) else
                       '已取消，未提交生成请求。')
            task.update(state='cancelled', message=message, progress=None, next_retry=None, finished=time.time())
            self._release(identity)
        else:
            task.update(state='ready', message='输入已固化，等待执行')
        self._persist(task)
        self.changed.emit()

    def _stage_failed(self, identity, error):
        self._failure(identity, error, stage=True)

    def _start_run(self, task, directory=None, *, dispatched_at=None):
        identity = task['id']
        context = self._contexts[identity]
        saving = directory is not None
        task.update(state='saving' if saving else 'running', progress=None,
                    message='正在重新保存已生成图片，不会调用生成接口。' if saving else '正在准备并生成图片')
        if not saving:
            context['dispatch_time'] = time.time() if dispatched_at is None else dispatched_at
            task.setdefault('started', context['dispatch_time'])
            task['dispatch_time'] = context['dispatch_time']
            task['attempts'] += 1
            task['accepted'] = context['accepted'] = False
            task['next_retry'] = None
        context['_worker_done'] = False
        self._persist(task)
        operation = (lambda ctx, current: execution.resave(ctx, directory, current)) if saving else execution.execute
        job = Job(lambda current: _worker(context, current, operation), self)
        job._generation_request = not saving
        job._paid_lane = not saving  # Legacy observers; no billing-dependent dispatch.
        self._run_jobs[identity] = job
        self._sync_active()
        job.progress.connect(lambda value: self._progress(identity, value))
        job.preview.connect(lambda value: self._preview(identity, value))
        job.succeeded.connect(lambda value: self._success(identity, value))
        job.failed.connect(lambda error: self._failure(identity, error))
        job.finished.connect(lambda: self._finished('run', identity, job))
        self.changed.emit()
        job.start()

    def _progress(self, identity, value):
        task, context = self._by_id.get(identity), self._contexts.get(identity)
        if self._closed or task is None or context is None:
            return
        if isinstance(value, dict):
            if value.get('phase') == 'request':
                task['request'] = task_records.clean(value.get('request'), context.get('tokens', ()))
                self.changed.emit()
                return
            phase = self._safe_message(value.get('phase', ''), context)[:40]
            if phase in ('submitted', 'encoding'):
                task['submitted'] = True
            if phase == 'accepted':
                task['accepted'] = context['accepted'] = True
                self._schedule()
            if phase == 'saving' and task['state'] == 'running':
                task['state'] = 'saving'
                task['received_results'] = value.get('received_results', task.get('expected_results', 0))
            message = self._safe_message(value.get('message', ''), context)
            if task['state'] != 'canceling' and message:
                task['message'] = message
            fraction = value.get('progress')
            safe = {'phase': phase, 'message': message}
            if isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
                try:
                    if math.isfinite(fraction):
                        safe['progress'] = max(0., min(1., fraction))
                except (OverflowError, ValueError):
                    pass
            task['progress'] = safe
            value = safe
            if phase in ('submitted', 'accepted', 'saving', 'encoding'):
                self._persist(task)
        else:
            value = {'message': self._safe_message(value, context)}
            task['progress'] = value
        if task['state'] == 'canceling':
            task['progress'] = None
        else:
            self.progress.emit(identity, value)
        self.changed.emit()

    def _preview(self, identity, value):
        task = self._by_id.get(identity)
        if task is not None and task['state'] == 'running' and not self._closed:
            self.preview.emit(identity, value)

    def _success(self, identity, value):
        if self._closed:
            self._release(identity, persist=False)
            return
        task, context = self._by_id.get(identity), self._contexts.get(identity)
        if task is None or context is None:
            return
        records = list(task['results']) + list(value.get('records', []))
        unique = {record.get('id', record.get('path')): record for record in records}
        task['results'] = list(unique.values())
        expected = task.get('expected_results', 1)
        try:
            complete = len(task['results']) == expected and all(
                Path(record['path']).is_file() and Path(record['path']).stat().st_size > 0
                for record in task['results'])
        except (OSError, KeyError, TypeError):
            complete = False
        if not complete:
            self._failure(identity, RuntimeError('任务结果尚未全部成功保存到本地，不能标记完成。'))
            return
        value = dict(value, warning=self._safe_message(value.get('warning') or '', context))
        message = value.get('warning') or f'已保存 {len(task["results"])} 张图片。'
        if context['cancel_requested']:
            message += ' 停止前已收到完整结果，图片已保留。'
        task.update(state='succeeded', message=self._safe_message(message, context),
                    progress={'progress': 1., 'message': message}, finished=time.time(),
                    result_unknown=False)
        self._save_failed_ids.discard(identity)
        task['received_results'] = expected
        state = self._record_states.get(identity)
        copy_value = state.get_copy() if state is not None else None
        if copy_value is not None and value.get('mask'):
            copy_value['mask'] = copy.deepcopy(value['mask'])
        self._persist(task, copy_value=copy_value)
        # Page completion handlers read the task before changed removes UI revisions.
        self.completed.emit(identity, value)
        self._release(identity)
        self.changed.emit()

    def _retry_rejected(self, task, context, error, message):
        """Only a proven rejection may authorize another generation POST."""
        kind = getattr(error, 'rejection_kind', None)
        if (kind not in ('concurrency', 'credential') or context.get('accepted')
                or context['cancel_requested'] or not isinstance(error, client.NovelAIError)):
            return False
        index = context['key_index']
        context['round_seen'].add(index)
        context['rotating'] = True
        task.update(submitted=False, accepted=False, progress=None)
        if kind == 'credential':
            context['rejected_keys'].add(index)
        remaining = [i for i in range(len(context['tokens']))
                     if i not in context['rejected_keys'] and i not in context['round_seen']]
        if remaining:
            index = remaining[0]
            context['key_index'], context['token'] = index, context['tokens'][index]
            task.update(state='ready', next_retry=None, key_index=index + 1,
                        message=f'{message}；正在轮试第 {index + 1}/{len(context["tokens"])} 个密钥。')
        elif len(context['rejected_keys']) < len(context['tokens']):
            now = time.time()
            task.update(state='retry_wait', last_rejected=now, next_retry=now + self._retry_interval,
                        message=f'{message}；本轮可用密钥均受并发限制，等待队首重试。')
        else:
            task.update(state='failed', next_retry=None, finished=time.time(),
                        message=f'所有密钥均明确拒绝此任务，已停止轮试。最后错误：{message}')
            self._persist(task)
            self.failed.emit(task['id'], RuntimeError(task['message']))
            self._release(task['id'])
        self._persist(task)
        self.changed.emit()
        return True

    def _failure(self, identity, error, *, stage=False):
        if self._closed:
            self._release(identity, persist=False)
            return
        task, context = self._by_id.get(identity), self._contexts.get(identity)
        if task is None or context is None:
            return
        message = self._safe_message(str(error), context)
        if not stage and self._retry_rejected(task, context, error, message):
            return
        if isinstance(error, storage.SaveError):
            # This branch takes precedence over cancellation: never discard received bytes.
            context.update(pending=error.pending, recovery_snapshot=error.snapshot,
                           mask=getattr(error, 'mask', None),
                           needs_composition=getattr(error, 'needs_composition', False))
            task['results'].extend(error.saved)
            task.update(state='save_failed', message=message, progress=None, result_unknown=False,
                        received_results=task.get('expected_results', 0))
            self._save_failed_ids.add(identity)
            error.args = (message,)
            error.__traceback__ = error.__cause__ = error.__context__ = None
            emitted = error
        else:
            limited = isinstance(error, client.NovelAIError) and getattr(error, 'status_code', None) == 429
            cancelled = context['cancel_requested'] or isinstance(error, (InterruptedError, client.Cancelled))
            limited = limited and not cancelled
            if limited:
                self._rate_limited = True
                message += ' 此任务未自动重试；新任务已暂停，请稍后手动继续队列。'
            elif cancelled:
                if stage or not task['submitted']:
                    message = ('已停止轮试，不再提交后续请求。' if task.get('attempts', 0) else
                               '已取消，未提交生成请求。')
                else:
                    message = '已停止本地等待；云端可能仍在执行并计费，未自动重新提交。'
            explicit_rejection = getattr(error, 'status_code', None) in (400, 401, 402, 403, 429)
            unknown = bool((task.get('submitted') or task.get('accepted') or context.get('accepted'))
                           and not explicit_rejection)
            task.update(state='cancelled' if cancelled else 'failed', message=message,
                        progress=None, next_retry=None, finished=time.time(), result_unknown=unknown)
            if limited:
                emitted = client.NovelAIError(message, status_code=429)
            else:
                emitted = InterruptedError(message) if cancelled else RuntimeError(message)
        self._persist(task)
        self.failed.emit(identity, emitted)
        if task['state'] in TERMINAL:
            self._release(identity)
        self.changed.emit()

    def _finished(self, kind, identity, job):
        # Job emits finished immediately before its thread returns. Wait without
        # blocking Qt so the next worker cannot overlap that thread's lifetime.
        if job.thread is not None and job.thread.is_alive():
            QtCore.QTimer.singleShot(1, lambda: self._finished(kind, identity, job))
            return
        if kind == 'stage' and self._stage_job is job:
            self._stage_job, self._stage_id = None, None
        elif kind == 'run' and self._run_jobs.get(identity) is job:
            self._run_jobs.pop(identity)
            self._sync_active()
            task, context = self._by_id.get(identity), self._contexts.get(identity)
            if (task is not None and context is not None and task['state'] == 'canceling'
                    and not context.get('pending')):
                task.update(state='cancelled', message='已取消，不再轮试或提交。',
                            progress=None, next_retry=None, finished=time.time())
                self._persist(task)
                self._release(identity)
        job.deleteLater()
        self.changed.emit()
        self._schedule()

    def _release(self, identity, *, persist=True):
        task = self._by_id.get(identity)
        if persist and task is not None and task['state'] in TERMINAL:
            self._persist(task)
        context = self._contexts.pop(identity, None)
        state = self._record_states.get(identity)
        if state is not None:
            state.release_secrets()
        if context is None:
            return
        frozen = context.get('frozen')
        if frozen is not None:
            try:
                frozen.close()
            except OSError:
                pass  # TemporaryDirectory retains its own eventual cleanup.
        context.clear()  # Includes token, embedded mask data, paths and frozen settings.

    def cancel(self, identity):
        task, context = self._by_id.get(identity), self._contexts.get(identity)
        if task is None or context is None or task['state'] in TERMINAL:
            return False
        if task['state'] in ('save_failed', 'saving'):
            return False  # Generated bytes must be saved, never silently discarded.
        context['cancel_requested'] = True
        if identity in self._run_jobs:
            task.update(state='canceling', message='正在停止本地等待；云端取消状态不可确认。', progress=None,
                        result_unknown=bool(task.get('submitted') or task.get('accepted') or context.get('accepted')))
            self._run_jobs[identity].cancel()
        elif identity == self._stage_id and self._stage_job is not None:
            task.update(state='canceling', message='正在停止准备，尚未提交生成请求。', progress=None)
            self._stage_job.cancel()
        else:
            message = ('已停止轮试，不再提交后续请求。' if task.get('attempts', 0) else
                       '已取消，未提交生成请求。')
            task.update(state='cancelled', message=message, progress=None, next_retry=None, finished=time.time())
            self._release(identity)
        self._persist(task)
        self.changed.emit()
        self._schedule()
        return True

    def cancel_all(self):
        self._suspend += 1
        try:
            for task in list(self.tasks):
                self.cancel(task['id'])
        finally:
            self._suspend -= 1
        self._schedule()

    def clear_finished(self):
        removable = {task['id'] for task in self.tasks if task['state'] in TERMINAL
                     and task['id'] not in self._run_jobs and task['id'] != self._stage_id}
        self.tasks[:] = [task for task in self.tasks if task['id'] not in removable]
        for identity in removable:
            self._by_id.pop(identity, None)
            self._release(identity)
            self._record_states.pop(identity, None)
            path = self._record_paths.pop(identity, None)
            if path:
                self._record_writer.delete(identity, path)
        self.changed.emit()

    def resave(self, identity, directory):
        if self._closed:
            raise RuntimeError('任务队列已经关闭。')
        task = self._by_id.get(identity)
        if task is None or task['state'] != 'save_failed':
            raise ValueError('此任务没有等待重新保存的图像。')
        if self._run_jobs:
            raise RuntimeError('请等待所有在途保存或请求线程结束。')
        if not str(directory or '').strip():
            raise ValueError('请选择保存目录。')
        self._start_run(task, str(directory))
        return True

    def shutdown(self):
        """Retain audit records, never a restartable cloud-submission queue."""
        self._closed = True
        self._retry_timer.stop()
        self._shutdown_event.set()
        self._suspend += 1
        try:
            for task in self.tasks:
                if task['state'] in TERMINAL:
                    continue
                context = self._contexts.get(task['id'])
                record = self._record_states.get(task['id'])
                saved_state = record.get_task() if record is not None else {}
                if saved_state.get('results'):
                    task['results'] = copy.deepcopy(saved_state['results'])
                    task['saved_results'] = len(task['results'])
                    task['received_results'] = saved_state.get('received_results', task.get('received_results', 0))
                if saved_state.get('state') == 'succeeded':
                    task.update(saved_state)
                    continue
                unknown = bool(task.get('submitted') or task.get('accepted') or
                               saved_state.get('submitted') or saved_state.get('accepted') or
                               context and context.get('accepted'))
                save_failed = (saved_state.get('state') == 'save_failed' or task['state'] == 'save_failed'
                               or bool(context and context.get('pending')))
                if save_failed:
                    unknown = False
                task.update(state='failed' if save_failed else 'cancelled', result_unknown=unknown, next_retry=None,
                            progress=None, finished=time.time(),
                            message=('客户端已关闭，图片未保存完整；尚未保存的部分已丢失，不会自动重新提交。' if save_failed else
                                     '客户端已关闭，云端结果可能已生成；不会自动重新提交。' if unknown else
                                     '客户端已关闭，已取消尚未提交的任务。'))
                self._persist(task)
                if context is not None:
                    context['cancel_requested'] = True
                job = self._run_jobs.get(task['id'])
                if job is not None:
                    job.cancel()
                elif task['id'] == self._stage_id and self._stage_job is not None:
                    self._stage_job.cancel()
        finally:
            self._suspend -= 1
        for identity, context in list(self._contexts.items()):
            if (identity != self._stage_id and identity not in self._run_jobs) or context.get('_worker_done'):
                self._release(identity, persist=False)
        self.flush_records(5)
        # Workers release temporary files even if the Qt event loop has stopped.
