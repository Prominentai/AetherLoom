"""RunningHub task state and restart recovery, independent of Qt widgets."""

import json
import os
import tempfile
import threading
import time


TERMINAL_STATUSES = frozenset({'SUCCESS', 'FAILED', 'CANCELED'})
ACTIVE_STATUSES = frozenset({
    'QUEUED', 'RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'POLL_TIMEOUT',
    'CANCEL_FAILED', 'WAITING_FOR_KEY',
})


def normalize_base_url(value):
    from api_calls.call_rh import site_base_url
    return site_base_url(value or 'www.runninghub.cn')


class TaskStore:
    """Atomically update a task map; never serialize API keys or Qt objects."""

    FIELDS = frozenset({'webapp_id', 'base_url', 'output_dir', 'status', 'decode_token'})

    def __init__(self, path):
        self.path = os.path.abspath(os.fspath(path))
        self.lock = threading.RLock()

    def _read_unlocked(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, 'r', encoding='utf-8') as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError('RunningHub task file must contain an object')
        result = {}
        for task_id, value in data.items():
            if isinstance(value, (str, int)):
                value = {'webapp_id': str(value)}
            if not isinstance(value, dict) or not value.get('webapp_id'):
                continue
            result[str(task_id)] = {key: str(item) for key, item in value.items()
                                    if key in self.FIELDS and item is not None}
        return result

    def read(self):
        with self.lock:
            return self._read_unlocked()

    def _write_unlocked(self, data):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.running-tasks-', suffix='.tmp', dir=folder)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as destination:
                json.dump(data, destination, ensure_ascii=False, indent=2)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def put(self, task_id, context):
        with self.lock:
            data = self._read_unlocked()
            previous = data.get(str(task_id))
            entry = dict(previous or {})
            entry.update({key: str(value) for key, value in context.items()
                          if key in self.FIELDS and value is not None})
            if not entry.get('webapp_id'):
                raise ValueError('A persisted RunningHub task needs a webapp_id')
            if entry == previous:
                return
            data[str(task_id)] = entry
            self._write_unlocked(data)

    def remove(self, task_id):
        with self.lock:
            data = self._read_unlocked()
            if str(task_id) in data:
                data.pop(str(task_id))
                self._write_unlocked(data)


class TaskLifecycle:
    """Coordinate explicit task events and background recovery without UI reads."""

    def __init__(self, owner, store, emit, *, api=None, downloader=None, interval=5):
        self.owner = owner
        self.store = store
        self.emit = emit
        self.api = api
        self.downloader = downloader
        self.interval = interval
        self.stop_event = threading.Event()
        if not hasattr(owner, '_rh_task_runtime_lock'):
            owner._rh_task_runtime_lock = threading.RLock()
        self.lock = owner._rh_task_runtime_lock
        for name, default in (
            ('_rh_task_contexts', {}), ('_rh_live_task_ids', set()),
            ('_rh_recovering_tasks', set()), ('_rh_downloaded_tasks', set()),
            ('_rh_running_tasks', {}), ('_rh_task_to_wid', {}),
            ('_rh_status_entries', {}), ('_rh_app_active_count', {}),
            ('_rh_app_last_result', {}),
            ('_rh_download_notes', {}),
            ('_rh_progress_entries', {}),
        ):
            if not hasattr(owner, name):
                setattr(owner, name, default)
        self.defaults = {}
        self.site_keys = {}
        self.recovered_task_ids = set()
        self._confirmed_cancellations = set()
        self._recovery_workers = {}
        self._recovery_cursor = None
        self._download_retry_attempts = {}
        self._download_retry_due = {}
        self._progress_due = {}
        self._progress_connected = set()

    def set_credentials(self, defaults, site_keys):
        """Called on the GUI thread with copied strings, including keys in memory only."""
        with self.lock:
            self.defaults = dict(defaults)
            self.defaults['base_url'] = normalize_base_url(defaults.get('base_url'))
            self.site_keys = {normalize_base_url(host): str(key or '') for host, key in site_keys.items()}

    def context(self, task_id, webapp_id=None, persisted=None, *, refresh_key=False):
        with self.lock:
            context = dict(self.defaults)
            context.pop('api_key', None)
            context.update(persisted or {})
            context.update(self.owner._rh_task_contexts.get(str(task_id), {}))
            if webapp_id is not None:
                context['webapp_id'] = str(webapp_id)
            context['base_url'] = normalize_base_url(context.get('base_url'))
            if refresh_key or str(task_id) in self.recovered_task_ids or not context.get('api_key'):
                context['api_key'] = self.site_keys.get(context['base_url'], '')
            return context

    def handle_event(self, webapp_id, event):
        webapp_id = str(webapp_id)
        if not isinstance(event, str):
            return
        with self.lock:
            if event.startswith('TASK_PROGRESS_SOURCE:'):
                parts = event.split(':', 2)
                if len(parts) != 3:
                    return
                _, task_id, url = parts
                if (self.owner._rh_task_to_wid.get(task_id) == webapp_id and
                        self.owner._rh_status_entries.get(task_id) == 'RUNNING' and not self._cancelled(task_id)):
                    monitor = getattr(self.owner, '_rh_progress_monitor', None)
                    if monitor is not None:
                        monitor.connect_task(task_id, url)
                return
            if event.startswith('TASK_DOWNLOAD_NOTE:'):
                parts = event.split(':', 2)
                if len(parts) != 3 or not parts[1]:
                    return
                _, task_id, note = parts
                known_app = self.owner._rh_task_to_wid.get(task_id)
                if known_app is not None and str(known_app) != webapp_id:
                    return
                if self.owner._rh_status_entries.get(task_id) in TERMINAL_STATUSES:
                    return
                self.owner._rh_download_notes[task_id] = note[:500]
                return
            if event.startswith('TASK_ADD:'):
                task_id = event.split(':', 1)[1]
                if not task_id:
                    return
                if self.owner._rh_status_entries.get(task_id) in TERMINAL_STATUSES:
                    return
                context = self.context(task_id, webapp_id)
                self.owner._rh_task_contexts[task_id] = context
                self.owner._rh_task_to_wid[task_id] = webapp_id
                self.owner._rh_running_tasks.setdefault(webapp_id, set()).add(task_id)
                status = self.owner._rh_status_entries.setdefault(task_id, 'QUEUED')
                self.store.put(task_id, dict(context, status=status))
            elif event.startswith('TASK_STATUS:'):
                parts = event.split(':', 2)
                if len(parts) != 3:
                    return
                _, task_id, status = parts
                if not task_id or status not in TERMINAL_STATUSES | ACTIVE_STATUSES:
                    return
                known_app = self.owner._rh_task_to_wid.get(task_id)
                if known_app is not None and str(known_app) != webapp_id:
                    return
                previous = self.owner._rh_status_entries.get(task_id)
                if previous in TERMINAL_STATUSES:
                    return
                context = self.context(task_id, webapp_id)
                # Persist first: a failed disk write must leave a retryable state.
                if status in TERMINAL_STATUSES:
                    self.store.remove(task_id)
                else:
                    self.store.put(task_id, dict(context, status=status))
                self.owner._rh_task_contexts[task_id] = context
                self.owner._rh_task_to_wid[task_id] = webapp_id
                self.owner._rh_status_entries[task_id] = status
                if status != 'RUNNING':
                    monitor = getattr(self.owner, '_rh_progress_monitor', None)
                    if monitor is not None:
                        monitor.stop_task(task_id)
                    self._progress_connected.discard(task_id)
                    self.owner._rh_progress_entries.pop(task_id, None)
                    if status in TERMINAL_STATUSES:
                        self._progress_due.pop(task_id, None)
                if status in TERMINAL_STATUSES:
                    self.owner._rh_download_notes.pop(task_id, None)
                    self._download_retry_attempts.pop(task_id, None)
                    self._download_retry_due.pop(task_id, None)
                    self.owner._rh_running_tasks.setdefault(webapp_id, set()).discard(task_id)
                    self.owner._rh_app_last_result[webapp_id] = status
                    if status == 'SUCCESS':
                        self.owner._rh_downloaded_tasks.add(task_id)
                else:
                    self.owner._rh_running_tasks.setdefault(webapp_id, set()).add(task_id)
                monitor = getattr(self.owner, '_rh_progress_monitor', None)
                if monitor is not None:
                    monitor.sync_card(task_id, status)
            elif event.startswith('TASK_REMOVE:'):
                # A legacy remove hint cannot discard a recoverable task.
                task_id = event.split(':', 1)[1]
                if self.owner._rh_status_entries.get(task_id) in TERMINAL_STATUSES:
                    self.owner._rh_running_tasks.setdefault(webapp_id, set()).discard(task_id)
            else:
                # Legacy unscoped events only affect the application's summary.
                if event in TERMINAL_STATUSES:
                    self.owner._rh_app_last_result[webapp_id] = event
                return
            self.owner._rh_app_active_count[webapp_id] = len(
                self.owner._rh_running_tasks.get(webapp_id, ()))

    def _api(self):
        if self.api is None:
            from api_calls import call_rh
            self.api = call_rh
        return self.api

    @staticmethod
    def _validate(payload):
        from api_calls.call_rh import validate_response
        return validate_response(payload, 'RunningHub task lifecycle')

    def _status(self, webapp_id, task_id, status):
        self.emit(str(webapp_id), 'TASK_STATUS:{}:{}'.format(task_id, status))

    def poll_progress(self, task_id, webapp_id, api_key, base_url, status):
        """Called by existing live/recovery polls; progress failure is non-fatal."""
        if status != 'RUNNING' or self._cancelled(task_id):
            return
        with self.lock:
            now = time.monotonic()
            if task_id in self._progress_connected or now < self._progress_due.get(task_id, 0):
                return
            self._progress_due[task_id] = now + 15
        try:
            url = self._api().get_progress_connection(api_key, task_id, base_url=base_url, timeout=8)
            if url and not self._cancelled(task_id):
                self.emit(str(webapp_id), f'TASK_PROGRESS_SOURCE:{task_id}:{url}')
        except Exception:
            # Do not let an optional channel turn a RUNNING task into failure,
            # and do not log exceptions that might contain signed credentials.
            pass

    def cancel_task(self, task_id, webapp_id=None):
        task_id = str(task_id)
        records = self.store.read()
        context = self.context(task_id, webapp_id, records.get(task_id), refresh_key=True)
        webapp_id = context.get('webapp_id', str(webapp_id or ''))
        try:
            if not context.get('api_key'):
                raise RuntimeError('No API key is available for this task site')
            reply = self._api().cancel_task(context['api_key'], task_id,
                                           base_url=context['base_url'], timeout=15)
            self._validate(reply)
        except Exception:
            self._status(webapp_id, task_id, 'CANCEL_FAILED')
            return False
        with self.lock:
            # Qt status delivery may be queued; stop a recovery without a card
            # immediately after the server has confirmed cancellation.
            self._confirmed_cancellations.add(task_id)
            card = context.get('card')
            if card is not None:
                card._rh_cancelled = True
        self._status(webapp_id, task_id, 'CANCELED')
        return True

    def has_active_app(self, webapp_id):
        with self.lock:
            webapp_id = str(webapp_id)
            if self.owner._rh_running_tasks.get(webapp_id):
                return True
            for card in list(getattr(self.owner, '_rh_running_cards', ()) or ()):
                if (str(getattr(card, '_webapp_id', '')) == webapp_id and
                        getattr(card, '_timer_start', None) and
                        not getattr(card, '_rh_cancelled', False)):
                    return True
            return False

    def _cancelled(self, task_id):
        with self.lock:
            context = self.owner._rh_task_contexts.get(task_id, {})
            return (self.stop_event.is_set() or getattr(self.owner, '_closing', False)
                    or task_id in self._confirmed_cancellations
                    or self.owner._rh_status_entries.get(task_id) in TERMINAL_STATUSES
                    or bool(getattr(context.get('card'), '_rh_cancelled', False)))

    def _note(self, webapp_id, task_id, text):
        self.emit(str(webapp_id), f'TASK_DOWNLOAD_NOTE:{task_id}:{text}')

    def _retry_note(self, webapp_id, task_id, details):
        if self._cancelled(task_id) or not isinstance(details, dict):
            return
        try:
            attempt = max(1, int(details.get('next_attempt', 1)))
            maximum = max(attempt, int(details.get('max_attempts', attempt)))
            delay = max(0.0, float(details.get('delay', 0)))
        except (TypeError, ValueError, OverflowError):
            return
        reason = str(details.get('reason') or '临时连接故障').replace('\n', ' ')[:160]
        self._note(webapp_id, task_id,
                   f'等待 {delay:g} 秒后进行第 {attempt}/{maximum} 次下载：{reason}')

    def _recover_task(self, task_id, context):
        from aetherloom_core.rh_outputs import OutputDownloadCancelled, OutputDownloadError
        webapp_id = context['webapp_id']
        phase = 'poll'

        def track_paths(paths):
            callback = context.get('on_files_saved')
            if callable(callback):
                callback(paths)

        try:
            if self._cancelled(task_id):
                return
            self.emit(webapp_id, 'TASK_ADD:' + task_id)
            if not context.get('api_key'):
                self._status(webapp_id, task_id, 'WAITING_FOR_KEY')
                return
            reply = self._validate(self._api().get_status(
                context['api_key'], task_id, base_url=context['base_url'], timeout=15))
            remote_status = reply.get('data')
            remote_status = remote_status.strip().upper() if isinstance(remote_status, str) else ''
            if remote_status == 'CANCELLED':
                remote_status = 'CANCELED'
            if self._cancelled(task_id):
                return
            if remote_status == 'SUCCESS':
                phase = 'download'
                self._status(webapp_id, task_id, 'DOWNLOADING')
                with self.lock:
                    already_downloaded = task_id in self.owner._rh_downloaded_tasks
                if not already_downloaded:
                    # Fetch on every recovery, including retries, to refresh
                    # short-lived signed URLs before entering the downloader.
                    outputs = self._validate(self._api().get_outputs(
                        context['api_key'], task_id, base_url=context['base_url'], timeout=30))
                    if self._cancelled(task_id):
                        return
                    if self.downloader is None:
                        from aetherloom_core.rh_outputs import download_outputs
                        self.downloader = download_outputs
                    paths = self.downloader(task_id, outputs.get('data'), context['output_dir'],
                        cancelled=lambda: self._cancelled(task_id),
                        on_retry=lambda details: self._retry_note(webapp_id, task_id, details),
                        **({'decoded_token': context['decode_token']} if context.get('decode_token') else {}))
                    track_paths(paths)
                    if self._cancelled(task_id):
                        return
                    callback = context.get('on_downloaded')
                    if callable(callback):
                        callback(paths)
                    if self._cancelled(task_id):
                        return
                with self.lock:
                    if self._cancelled(task_id):
                        return
                    self.owner._rh_downloaded_tasks.add(task_id)
                    self._download_retry_attempts.pop(task_id, None)
                    self._download_retry_due.pop(task_id, None)
                    self._status(webapp_id, task_id, 'SUCCESS')
            elif remote_status in ('FAILED', 'CANCELED', 'QUEUED', 'RUNNING'):
                self._status(webapp_id, task_id, remote_status)
                self.poll_progress(task_id, webapp_id, context['api_key'], context['base_url'], remote_status)
            else:
                self._status(webapp_id, task_id, 'POLL_TIMEOUT')
        except OutputDownloadCancelled as exc:
            track_paths(exc.completed_paths)
            if not self._cancelled(task_id):
                self._note(webapp_id, task_id, '下载已暂停，任务已保留')
        except Exception as exc:
            if isinstance(exc, OutputDownloadError):
                track_paths(exc.completed_paths)
            if self._cancelled(task_id):
                return
            if phase == 'download':
                with self.lock:
                    count = self._download_retry_attempts.get(task_id, 0) + 1
                    self._download_retry_attempts[task_id] = count
                    delay = min(300, 30 * 2 ** min(count - 1, 4))
                    self._download_retry_due[task_id] = time.monotonic() + delay
                self._status(webapp_id, task_id, 'DOWNLOAD_FAILED')
                # Downloader errors are sanitized at their boundary. Other
                # exceptions can contain credentials/URLs; expose only the type.
                detail = str(exc) if isinstance(exc, OutputDownloadError) else type(exc).__name__
                self._note(webapp_id, task_id,
                           f'输出下载失败，{delay} 秒后重试：{detail[:240]}')
            else:
                self._status(webapp_id, task_id, 'POLL_TIMEOUT')
        finally:
            with self.lock:
                self.owner._rh_recovering_tasks.discard(task_id)
                self._recovery_workers.pop(task_id, None)

    def recover_once(self, *, background=False, respect_backoff=False):
        """Manual recovery is immediate; the automatic loop uses two workers.

        Claims happen before thread creation, so repeated passes cannot create
        duplicate work or an unbounded queue while downloads are slow.
        """
        records = self.store.read()
        pending = list(records.items())
        if background:
            with self.lock:
                # Resume after the last claimed task. Starting at the first
                # record every pass lets two slow RUNNING polls starve all
                # later tasks even when both workers finish between passes.
                for index, (task_id, _) in enumerate(pending):
                    if task_id == self._recovery_cursor:
                        pending = pending[index + 1:] + pending[:index + 1]
                        break
        for task_id, record in pending:
            if self.stop_event.is_set() or getattr(self.owner, '_closing', False):
                break
            with self.lock:
                if (task_id in self.owner._rh_live_task_ids or
                        task_id in self.owner._rh_recovering_tasks or self._cancelled(task_id)):
                    continue
                if respect_backoff and self._download_retry_due.get(task_id, 0) > time.monotonic():
                    continue
                if background and len(self.owner._rh_recovering_tasks) >= 2:
                    continue
                self.owner._rh_recovering_tasks.add(task_id)
                if background:
                    self._recovery_cursor = task_id
                if task_id not in self.owner._rh_task_contexts:
                    self.recovered_task_ids.add(task_id)
                context = self.context(task_id, persisted=record, refresh_key=True)
                self.owner._rh_task_contexts[task_id] = context
            if background:
                worker = threading.Thread(target=self._recover_task, args=(task_id, context),
                                          name='rh-recover-' + task_id[:24], daemon=True)
                with self.lock:
                    self._recovery_workers[task_id] = worker
                try:
                    worker.start()
                except Exception:
                    with self.lock:
                        self._recovery_workers.pop(task_id, None)
                        self.owner._rh_recovering_tasks.discard(task_id)
                    raise
            else:
                self._recover_task(task_id, context)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.recover_once(background=True, respect_backoff=True)
            except Exception:
                # A temporarily unavailable task file must not kill recovery.
                pass
            if self.stop_event.wait(self.interval):
                break

    def stop(self):
        self.stop_event.set()
