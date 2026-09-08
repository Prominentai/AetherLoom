"""RunningHub task state and restart recovery, independent of Qt widgets."""

import json
import copy
import hashlib
import os
import tempfile
import threading
import time
from collections import OrderedDict


TERMINAL_STATUSES = frozenset({'SUCCESS', 'FAILED', 'CANCELED', 'INTERRUPTED'})
ACTIVE_STATUSES = frozenset({
    'QUEUED', 'RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'POLL_TIMEOUT',
    'CANCELING', 'CANCEL_FAILED', 'WAITING_FOR_KEY', 'WAITING_FOR_SECRET',
})


def output_group(record):
    """One display grouping for task JSON projections and App output cards."""
    status = str(record.get('status') or '').upper()
    if record.get('task_id') or status in TERMINAL_STATUSES | {
        'UNKNOWN', 'PAUSED', 'SKIPPED', 'CANCELING', 'CANCEL_FAILED',
    }:
        return 'active'
    # Admission grants retry permission, not a place in the results section.
    # Keep each POST attempt here too, avoiding group churn during retries.
    if status in {'PENDING', 'PREPARING', 'LOCAL_WAIT', 'SUBMITTING', 'RETRYING', 'WAITING_FOR_KEY'}:
        return 'waiting'
    if 'submission_admitted' in record:
        return 'active' if record['submission_admitted'] else 'waiting'
    return 'active'


def is_download_recovery(value):
    """Only explicitly generated, unfinished local results survive a session."""
    if not isinstance(value, dict) or value.get('cancel_requested'):
        return False
    status = str(value.get('status') or '').upper()
    if status in TERMINAL_STATUSES | {'UNKNOWN'}:
        return False
    return bool(value.get('cloud_success') or
                status in {'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET'})


def normalize_base_url(value):
    from api_calls.call_rh import site_base_url
    return site_base_url(value or 'www.runninghub.cn')


def normalize_api_keys(value):
    """Copy ordered, distinct credential strings; never stringify containers."""
    values = value if isinstance(value, (list, tuple)) else [value]
    return list(dict.fromkeys(item.strip() for item in values if isinstance(item, str) and item.strip()))


def api_key_id(key):
    value = str(key or '').strip()
    return hashlib.sha256(value.encode('utf-8')).hexdigest() if value else ''


class TaskStore:
    """Atomically update a task map; never serialize API keys or Qt objects."""

    FIELDS = frozenset({'webapp_id', 'base_url', 'output_dir', 'status', 'decode_token',
                        'run_id', 'app_name', 'submission_order', 'key_id', 'task_document'})

    @classmethod
    def clean_context(cls, value):
        """Persist only recovery data, never runtime callbacks or credentials."""
        result = {key: str(item) for key, item in value.items()
                  if key in cls.FIELDS and item is not None}
        if 'started' in value:
            result['started'] = str(value['started']).lower() in {'true', '1'}
        for key in ('cancel_requested', 'cancel_acknowledged', 'cancel_retry_available', 'cloud_success'):
            if key in value:
                result[key] = str(value[key]).lower() in {'true', '1'}
        for key in ('cancel_attempts', 'cancel_retry_at', 'cancel_generation'):
            if key in value:
                try:
                    result[key] = max(0, int(value[key]))
                except (TypeError, ValueError, OverflowError):
                    pass
        origin = value.get('origin')
        if isinstance(origin, dict):
            result['origin'] = {key: item for key, item in origin.items()
                                if key in {'canvas_id', 'node_id', 'execution_id', 'round_id',
                                           'batch_index', 'repeat_index', 'canvas_batch_index',
                                           'canvas_name', 'node_name', 'node_title',
                                           'workflow_group_id', 'workflow_job_id',
                                           'workflow_group_document', 'workflow_job_document',
                                           'kind', 'app_submission_group_id',
                                           'app_submission_index', 'app_submission_count'}
                                and isinstance(item, (str, int, float, bool))}
        decode = value.get('decode_settings')
        if isinstance(decode, dict):
            result['decode_settings'] = {key: item for key, item in decode.items()
                                          if key in {'enabled', 'mode', 'grid_cols', 'delete_original',
                                                     'password_required', 'settings_missing'}
                                          and isinstance(item, (str, int, bool))}
            if decode.get('password'):
                result['decode_settings']['password_required'] = True
        return result

    def __init__(self, path):
        self.path = os.path.abspath(os.fspath(path))
        self.lock = threading.RLock()
        self._cached = None
        self._signature = None
        self._download_only = False

    def _file_signature(self):
        try:
            stat = os.stat(self.path)
            return stat.st_mtime_ns, stat.st_size, stat.st_ino
        except FileNotFoundError:
            return None

    def _read_unlocked(self):
        signature = self._file_signature()
        if self._cached is not None and signature == self._signature:
            return self._cached
        if signature is None:
            self._cached, self._signature = {}, None
            return self._cached
        with open(self.path, 'r', encoding='utf-8') as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError('RunningHub task file must contain an object')
        result = {}
        for task_id, value in data.items():
            if not str(task_id).strip():
                continue
            if isinstance(value, (str, int)):
                value = {'webapp_id': str(value)}
            if not isinstance(value, dict) or not value.get('webapp_id'):
                continue
            result[str(task_id)] = self.clean_context(value)
        self._cached, self._signature = result, signature
        return result

    def read(self):
        with self.lock:
            return copy.deepcopy(self._read_unlocked())

    def _write_unlocked(self, data):
        if not data:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            self._cached, self._signature = {}, None
            return
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.running-tasks-', suffix='.tmp', dir=folder)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as destination:
                json.dump(data, destination, ensure_ascii=False, indent=2)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            self._cached, self._signature = data, self._file_signature()
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def put(self, task_id, context):
        if isinstance(task_id, bool) or not isinstance(task_id, (str, int)) or not str(task_id).strip():
            raise ValueError('A persisted RunningHub task needs a returned task ID')
        task_id = str(task_id).strip()
        with self.lock:
            data = self._read_unlocked()
            previous = data.get(str(task_id))
            entry = dict(previous or {})
            entry.update(self.clean_context(context))
            if not entry.get('webapp_id'):
                raise ValueError('A persisted RunningHub task needs a webapp_id')
            if self._download_only and not is_download_recovery(entry):
                if previous is not None:
                    data = dict(data)
                    data.pop(task_id, None)
                    self._write_unlocked(data)
                return
            if entry == previous:
                return
            data = dict(data)
            data[str(task_id)] = entry
            self._write_unlocked(data)

    def remove(self, task_id):
        with self.lock:
            data = self._read_unlocked()
            if str(task_id) in data:
                data = dict(data)
                data.pop(str(task_id))
                self._write_unlocked(data)

    def retain_download_retries(self, *, closing=False):
        """One atomic session cleanup; late ordinary writes stay disabled on close."""
        with self.lock:
            if closing:
                self._download_only = True
            records = self._read_unlocked()
            retained = {task_id: value for task_id, value in records.items() if is_download_recovery(value)}
            if len(retained) != len(records):
                self._write_unlocked(retained)
            return copy.deepcopy(retained)


def default_task_store():
    """Move the old task index only after the replacement is safely written."""
    from aetherloom_core.paths import current_dir
    from aetherloom_core.rh_storage import task_records_root
    store = TaskStore(task_records_root() / 'running_tasks.json')
    legacy = TaskStore(os.path.join(current_dir, 'running_tasks.json'))
    if os.path.isfile(legacy.path):
        records = legacy.read()
        records.update(store.read())
        with store.lock:
            store._write_unlocked(records)
        os.remove(legacy.path)
    return store


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
        self.wake_event = threading.Event()
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
        self.site_keyrings = {}
        self.recovered_task_ids = set()
        self._confirmed_cancellations = set()
        self._recovery_workers = {}
        self._download_workers = {}
        self._pending_downloads = OrderedDict()
        self._recovery_cursor = None
        self._poll_due = {}
        self._download_retry_attempts = {}
        self._download_retry_due = {}
        self._progress_due = {}
        self._progress_connected = set()
        self._progress_node_maps = OrderedDict()
        self._receipt_maintenance_due = 0
        self._receipt_maintenance_worker = None
        # Cancellation shares the bounded status pool. No per-task retry threads.
        self.cancel_delays = (1, 2, 4, 8)

    def set_credentials(self, defaults, site_keys):
        """Called with copied key strings/lists; credentials remain in memory only."""
        with self.lock:
            self.defaults = dict(defaults)
            self.defaults['base_url'] = normalize_base_url(defaults.get('base_url'))
            self.site_keyrings = {normalize_base_url(host): normalize_api_keys(keys) for host, keys in site_keys.items()}
            self.site_keys = {host: keys[0] if keys else '' for host, keys in self.site_keyrings.items()}

    def context(self, task_id, webapp_id=None, persisted=None, *, refresh_key=False):
        with self.lock:
            context = dict(self.defaults)
            context.pop('api_key', None)
            context.pop('api_keys', None)
            context.update(persisted or {})
            context.update(self.owner._rh_task_contexts.get(str(task_id), {}))
            if isinstance(context.get('decode_settings'), dict):
                context['decode_settings'] = copy.deepcopy(context['decode_settings'])
            if webapp_id is not None:
                context['webapp_id'] = str(webapp_id)
            context['base_url'] = normalize_base_url(context.get('base_url'))
            ring = self.site_keyrings.get(context['base_url'], [])
            bound_key = context.get('api_key') or ''
            key_id = context.get('key_id') or api_key_id(bound_key)
            if key_id:
                # A live accepted task keeps its immutable credential. A restart
                # has only the fingerprint and must locate that exact key.
                context['api_key'] = (bound_key if api_key_id(bound_key) == key_id else
                                      next((key for key in ring if api_key_id(key) == key_id), ''))
                context['key_id'] = key_id
            else:
                # Legacy records did not identify an account. A single configured
                # key is unambiguous; never guess after upgrading to multiple.
                context['api_key'] = ring[0] if len(ring) == 1 else ''
                if context['api_key']:
                    context['key_id'] = api_key_id(context['api_key'])
            return context

    def handle_event(self, webapp_id, event):
        webapp_id = str(webapp_id)
        if not isinstance(event, str):
            return
        event = self._cancel_event(event)
        service = getattr(self.owner, '_rh_execution_service', None)
        if service is not None:
            # Outside the lifecycle lock: a canvas subscriber can atomically
            # save its result before terminal task records are removed.
            service.lifecycle_event(webapp_id, event)
        submission_status = None
        with self.lock:
            if event.startswith('TASK_PROGRESS_NODES:'):
                _, task_id, payload = event.split(':', 2)
                monitor = getattr(self.owner, '_rh_progress_monitor', None)
                if monitor is not None and self.owner._rh_task_to_wid.get(task_id) == webapp_id:
                    try:
                        monitor.set_nodes(task_id, json.loads(payload))
                    except (ValueError, TypeError):
                        pass
                return
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
                if status in {'RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET', 'SUCCESS'}:
                    context['started'] = True
                # Persist first: a failed disk write must leave a retryable state.
                if status in TERMINAL_STATUSES:
                    self.store.remove(task_id)
                else:
                    self.store.put(task_id, dict(context, status=status))
                self.owner._rh_task_contexts[task_id] = context
                self.owner._rh_task_to_wid[task_id] = webapp_id
                self.owner._rh_status_entries[task_id] = status
                submission_status = (task_id, status)
                if status != 'RUNNING':
                    monitor = getattr(self.owner, '_rh_progress_monitor', None)
                    if monitor is not None:
                        monitor.stop_task(task_id)
                    self._progress_connected.discard(task_id)
                    self.owner._rh_progress_entries.pop(task_id, None)
                    if status in TERMINAL_STATUSES:
                        self._progress_due.pop(task_id, None)
                if status in TERMINAL_STATUSES:
                    self._receipt_maintenance_due = 0
                    self._poll_due.pop(task_id, None)
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
        # Submission workers check lifecycle cancellation while holding their
        # queue lock; never acquire that queue lock while holding this lock.
        submission_queue = getattr(self.owner, '_rh_submission_queue', None)
        if submission_status is not None and submission_queue is not None:
            submission_queue.task_status(*submission_status)

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
        event = self._cancel_event('TASK_STATUS:{}:{}'.format(task_id, status))
        service = getattr(self.owner, '_rh_execution_service', None)
        if service is not None:
            service.lifecycle_event(str(webapp_id), event)
        self.emit(str(webapp_id), event)

    def _cancel_event(self, event):
        """A late normal poll/Qt event cannot overwrite durable cancellation."""
        if event.startswith('TASK_STATUS:'):
            parts = event.split(':', 2)
            if len(parts) == 3:
                with self.lock:
                    context = self.owner._rh_task_contexts.get(parts[1], {})
                    if (context.get('cancel_requested') and parts[2] == 'CANCEL_FAILED' and
                            not context.get('cancel_retry_available') and
                            context.get('cancel_attempts', 0) <= len(self.cancel_delays)):
                        return 'TASK_STATUS:{}:CANCELING'.format(parts[1])
                    if context.get('cancel_requested') and parts[2] not in {
                            'CANCELED', 'INTERRUPTED', 'CANCELING', 'CANCEL_FAILED', 'WAITING_FOR_KEY'}:
                        state = 'CANCEL_FAILED' if context.get('cancel_attempts', 0) > len(self.cancel_delays) else 'CANCELING'
                        return 'TASK_STATUS:{}:{}'.format(parts[1], state)
        return event

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
                self._load_progress_nodes(task_id, webapp_id, api_key, base_url)
        except Exception:
            # Do not let an optional channel turn a RUNNING task into failure,
            # and do not log exceptions that might contain signed credentials.
            pass

    def _load_progress_nodes(self, task_id, webapp_id, api_key, base_url):
        # Optional metadata uses the existing worker. Share both successes and
        # permission failures by site/App/key, without repeated status polling.
        key = (normalize_base_url(base_url), str(webapp_id), api_key_id(api_key))
        with self.lock:
            cached = self._progress_node_maps.get(key)
            if cached is None:
                self._progress_node_maps[key] = (time.monotonic(), None)
                while len(self._progress_node_maps) > 64:
                    self._progress_node_maps.popitem(last=False)
        if cached is not None:
            nodes = cached[1]
            if nodes:
                self.emit(str(webapp_id), f'TASK_PROGRESS_NODES:{task_id}:' + json.dumps(nodes))
            return
        try:
            nodes = self._api().get_app_progress_nodes(webapp_id, api_key, base_url=base_url, timeout=4)
        except Exception:
            nodes = {}
        with self.lock:
            self._progress_node_maps[key] = (time.monotonic(), nodes)
            while len(self._progress_node_maps) > 64:
                self._progress_node_maps.popitem(last=False)
            recipients = [tid for tid, context in self.owner._rh_task_contexts.items()
                          if str(context.get('webapp_id')) == str(webapp_id)
                          and normalize_base_url(context.get('base_url')) == key[0]
                          and api_key_id(context.get('api_key', '')) == key[2]]
        if nodes:
            for tid in set(recipients) | {str(task_id)}:
                self.emit(str(webapp_id), f'TASK_PROGRESS_NODES:{tid}:' + json.dumps(nodes))

    def cancel_task(self, task_id, webapp_id=None):
        """Persist intent immediately; the bounded recovery pool confirms it.

        The return value means the request is recorded, not that cloud execution
        has stopped. Repeated clicks during one attempt series are idempotent.
        """
        task_id = str(task_id)
        records = self.store.read()
        with self.lock:
            if self.receipts_finished(task_id):
                return False
            context = self.context(task_id, webapp_id, records.get(task_id), refresh_key=True)
            if not context.get('webapp_id'):
                return False
            if context.get('cancel_requested'):
                retry_available = (context.get('cancel_retry_available') or
                    (context.get('status') == 'CANCEL_FAILED' and
                     context.get('cancel_attempts', 0) > len(self.cancel_delays)))
                if not retry_available:
                    return True
            context.update(cancel_requested=True, cancel_attempts=0, cancel_retry_at=0,
                           cancel_generation=context.get('cancel_generation', 0) + 1,
                           cancel_acknowledged=False, cancel_retry_available=False, status='CANCELING')
            # Write before exposing cancellation or scheduling a network request.
            self.store.put(task_id, context)
            self.owner._rh_task_contexts[task_id] = context
            self._download_retry_due.pop(task_id, None)
            self._poll_due.pop(task_id, None)
            if self._pending_downloads.pop(task_id, None) is not None:
                self.owner._rh_recovering_tasks.discard(task_id)
        self._status(context['webapp_id'], task_id,
                     'CANCELING' if context.get('api_key') else 'WAITING_FOR_KEY')
        self.wake_event.set()
        self.recover_once(background=True, respect_backoff=True)
        return True

    def _recovery_stopped(self, task_id):
        with self.lock:
            return (self.stop_event.is_set() or getattr(self.owner, '_closing', False)
                    or task_id in self._confirmed_cancellations
                    or self.owner._rh_status_entries.get(task_id) in TERMINAL_STATUSES)

    def _cancel_confirmed(self, task_id, context):
        if self._recovery_stopped(task_id):
            return False
        self._status(context['webapp_id'], task_id, 'CANCELED')
        with self.lock:
            self._confirmed_cancellations.add(task_id)
            self._pending_downloads.pop(task_id, None)
        return True

    def _cancel_status_result(self, task_id, context, remote_status):
        context = self.context(task_id, persisted=context, refresh_key=True)
        # The documented status vocabulary has no separate canceled value;
        # FAILED also confirms that generation has stopped after cancel intent.
        if remote_status in {'CANCELED', 'CANCELLED', 'FAILED', 'SUCCESS'}:
            confirmed = self._cancel_confirmed(task_id, context)
            if confirmed and remote_status == 'SUCCESS':
                self._note(context['webapp_id'], task_id, '云端已完成，本地后续处理已取消')
            return confirmed
        exhausted = context.get('cancel_attempts', 0) > len(self.cancel_delays)
        with self.lock:
            latest = self.owner._rh_task_contexts.get(task_id, {})
            if latest.get('cancel_generation', 0) != context.get('cancel_generation', 0):
                return False
            # Publish retry eligibility before any synchronous observer/Qt
            # callback sees CANCEL_FAILED. The UI status map can lag that event.
            context['cancel_retry_available'] = exhausted
            context['status'] = 'CANCEL_FAILED' if exhausted else 'CANCELING'
            self.store.put(task_id, context)
            self.owner._rh_task_contexts[task_id] = context
        self._status(context['webapp_id'], task_id, 'CANCEL_FAILED' if exhausted else 'CANCELING')
        with self.lock:
            still_exhausted = self.owner._rh_task_contexts.get(task_id, {}).get('cancel_retry_available')
        if exhausted and still_exhausted and context.get('cancel_acknowledged'):
            self._note(context['webapp_id'], task_id, '取消已受理，云端状态尚未确认；可再次取消')
        return False

    def _cancel_step(self, task_id, context):
        """At most one cancel and one confirmation query in this status slot."""
        if self._recovery_stopped(task_id):
            return
        context = self.context(task_id, persisted=context, refresh_key=True)
        if not context.get('api_key'):
            self._status(context['webapp_id'], task_id, 'WAITING_FOR_KEY')
            return
        attempts = context.get('cancel_attempts', 0)
        if attempts <= len(self.cancel_delays) and time.time() >= context.get('cancel_retry_at', 0):
            context['cancel_attempts'] = attempts + 1
            delay = self.cancel_delays[min(attempts, len(self.cancel_delays) - 1)] if self.cancel_delays else 0
            context['cancel_retry_at'] = time.time() + delay
            with self.lock:
                latest = self.owner._rh_task_contexts.get(task_id, {})
                if latest.get('cancel_generation', 0) != context.get('cancel_generation', 0):
                    return  # A manual retry superseded this waiting status pass.
                self.store.put(task_id, context)
                self.owner._rh_task_contexts[task_id] = context
            self._status(context['webapp_id'], task_id, 'CANCELING')
            try:
                reply = self._validate(self._api().cancel_task(
                    context['api_key'], task_id, base_url=context['base_url'], timeout=15))
                context['cancel_acknowledged'] = True
                with self.lock:
                    latest = self.owner._rh_task_contexts.get(task_id, {})
                    if latest.get('cancel_generation', 0) == context.get('cancel_generation', 0):
                        self.store.put(task_id, context)
                        self.owner._rh_task_contexts[task_id] = context
            except Exception:
                pass  # A timeout can still have canceled the job; query it.
        if self._recovery_stopped(task_id):
            return
        remote_status = ''
        try:
            reply = self._validate(self._api().get_status(
                context['api_key'], task_id, base_url=context['base_url'], timeout=15))
            value = reply.get('data')
            remote_status = value.strip().upper() if isinstance(value, str) else ''
        except Exception:
            # Even an acknowledged cancel plus 805/807 cannot prove a terminal
            # state: those errors can also mean unknown status or inaccessible ID.
            pass
        if not self._recovery_stopped(task_id):
            self._cancel_status_result(task_id, context, remote_status)

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
                    or context.get('cancel_requested', False)
                    or bool(getattr(context.get('card'), '_rh_cancelled', False)))

    def receipts_finished(self, task_id):
        """Stopping the client pauses work; only a confirmed outcome ends it."""
        with self.lock:
            return (task_id in self._confirmed_cancellations
                    or task_id in self.owner._rh_downloaded_tasks
                    or self.owner._rh_status_entries.get(task_id) in TERMINAL_STATUSES)

    def _note(self, webapp_id, task_id, text):
        service = getattr(self.owner, '_rh_execution_service', None)
        if service is not None:
            service.lifecycle_event(str(webapp_id), f'TASK_DOWNLOAD_NOTE:{task_id}:{text}')
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

    def _download_task(self, task_id, context, *, background=False):
        from aetherloom_core.rh_outputs import OutputDownloadCancelled, OutputDownloadError, cleanup_output_receipts
        webapp_id = context['webapp_id']
        output_records = None

        def track_paths(paths):
            callback = context.get('on_files_saved')
            if callable(callback):
                callback(paths)

        try:
            if self._cancelled(task_id):
                return
            # Refresh account lookup and callbacks, preserving the task's frozen
            # decoder configuration rather than reading current App settings.
            context = self.context(task_id, persisted=context, refresh_key=True)
            service = getattr(self.owner, '_rh_execution_service', None)
            if service is not None:
                context = service.adopt_task(task_id, context)
            if not context.get('api_key'):
                self._status(webapp_id, task_id, 'WAITING_FOR_KEY')
                return
            self._status(webapp_id, task_id, 'DOWNLOADING')
            with self.lock:
                already_downloaded = task_id in self.owner._rh_downloaded_tasks
            if not already_downloaded:
                # Signed output URLs are fetched here, not when entering the
                # pending queue. Long decodes never leave queued stale URLs.
                outputs = self._validate(self._api().get_outputs(
                    context['api_key'], task_id, base_url=context['base_url'], timeout=30))
                output_records = outputs.get('data')
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
                self._download_retry_attempts.pop(task_id, None)
                self._download_retry_due.pop(task_id, None)
            self._status(webapp_id, task_id, 'SUCCESS')
            with self.lock:
                self.owner._rh_downloaded_tasks.add(task_id)
            if not already_downloaded:
                if not cleanup_output_receipts(task_id, outputs.get('data'), context['output_dir']):
                    self._note(webapp_id, task_id, '输出已完成，部分校验记录暂未清理')
        except OutputDownloadCancelled as exc:
            track_paths(exc.completed_paths)
            if not self._cancelled(task_id):
                self._note(webapp_id, task_id, '下载已暂停，任务已保留')
        except Exception as exc:
            if isinstance(exc, OutputDownloadError):
                track_paths(exc.completed_paths)
            if self._cancelled(task_id):
                return
            if getattr(exc, 'waiting_for_secret', False):
                self._status(webapp_id, task_id, 'WAITING_FOR_SECRET')
                self._note(webapp_id, task_id, getattr(exc, 'recovery_message',
                           '请补充本次任务的本地解码密码后继续'))
                with self.lock:
                    self._download_retry_due[task_id] = time.monotonic() + 30
                return
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
        finally:
            if output_records and self.receipts_finished(task_id):
                cleanup_output_receipts(task_id, output_records, context['output_dir'])
            with self.lock:
                self.owner._rh_recovering_tasks.discard(task_id)
                self._download_workers.pop(task_id, None)
                if output_records:
                    self._receipt_maintenance_due = 0
                if self.owner._rh_task_contexts.get(task_id, {}).get('cancel_requested'):
                    self.wake_event.set()
            if background:
                self._start_downloads()

    def _start_downloads(self):
        """At most two download/decoder threads; waiting items own no thread."""
        while True:
            with self.lock:
                if self.stop_event.is_set() or getattr(self.owner, '_closing', False):
                    for task_id in self._pending_downloads:
                        self.owner._rh_recovering_tasks.discard(task_id)
                    self._pending_downloads.clear()
                    return
                if len(self._download_workers) >= 2 or not self._pending_downloads:
                    return
                task_id, context = self._pending_downloads.popitem(last=False)
                if self._cancelled(task_id):
                    self.owner._rh_recovering_tasks.discard(task_id)
                    continue
                worker = threading.Thread(target=self._download_task,
                    args=(task_id, context), kwargs={'background': True},
                    name='rh-download-' + task_id[:24], daemon=True)
                self._download_workers[task_id] = worker
            try:
                worker.start()
            except Exception:
                with self.lock:
                    self._download_workers.pop(task_id, None)
                    self.owner._rh_recovering_tasks.discard(task_id)
                    self._download_retry_due[task_id] = time.monotonic() + 30
                self._status(context['webapp_id'], task_id, 'DOWNLOAD_FAILED')

    def _recover_task(self, task_id, context, *, background_download=False):
        """One cloud status request; a successful task moves to the download pool."""
        webapp_id = context['webapp_id']
        deferred = False
        try:
            if self._recovery_stopped(task_id):
                return
            self.emit(webapp_id, 'TASK_ADD:' + task_id)
            context = self.context(task_id, persisted=context, refresh_key=True)
            if context.get('cancel_requested'):
                self._cancel_step(task_id, context)
                return
            if not context.get('api_key'):
                self._status(webapp_id, task_id, 'WAITING_FOR_KEY')
                return
            if is_download_recovery(context):
                remote_status = 'SUCCESS'  # Generation is already confirmed; only fetch fresh output URLs.
            else:
                reply = self._validate(self._api().get_status(
                    context['api_key'], task_id, base_url=context['base_url'], timeout=15))
                remote_status = reply.get('data')
            remote_status = remote_status.strip().upper() if isinstance(remote_status, str) else ''
            if remote_status == 'CANCELLED':
                remote_status = 'CANCELED'
            if self._recovery_stopped(task_id):
                return
            context = self.context(task_id, persisted=context, refresh_key=True)
            if context.get('cancel_requested'):
                self._cancel_status_result(task_id, context, remote_status)
                self.wake_event.set()
                return
            if remote_status == 'SUCCESS':
                context['cloud_success'] = True
                with self.lock:
                    self.store.put(task_id, context)
                    self.owner._rh_task_contexts[task_id] = context
                self._status(webapp_id, task_id, 'DOWNLOADING')
                if background_download:
                    with self.lock:
                        if not self._cancelled(task_id):
                            self._pending_downloads[task_id] = context
                            deferred = True
                    self._start_downloads()
                else:
                    self._download_task(task_id, context)
            elif remote_status in ('FAILED', 'CANCELED', 'QUEUED', 'RUNNING'):
                self._status(webapp_id, task_id, remote_status)
                self.poll_progress(task_id, webapp_id, context['api_key'], context['base_url'], remote_status)
            else:
                self._status(webapp_id, task_id, 'POLL_TIMEOUT')
        except Exception:
            if not self._cancelled(task_id):
                self._status(webapp_id, task_id, 'POLL_TIMEOUT')
        finally:
            with self.lock:
                self._recovery_workers.pop(task_id, None)
                if not deferred:
                    self.owner._rh_recovering_tasks.discard(task_id)
                if not self._recovery_stopped(task_id):
                    self._poll_due[task_id] = time.monotonic() + max(.01, self.interval)
            # Fill the freed slot with the next due task immediately. Completed
            # tasks have their own due time, so this never hot-polls two heads.
            self.wake_event.set()

    def recover_once(self, *, background=False, respect_backoff=False):
        """Manual recovery is immediate; cloud polls and downloads have two slots each.

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
            # Every admitted QUEUED slot can release a waiting submission.
            # Prioritize all of them in admission order, not only the first;
            # per-task due times and the bounded status pool still apply.
            submission_queue = getattr(self.owner, '_rh_submission_queue', None)
            if submission_queue is not None:
                with submission_queue.condition:
                    awaiting = dict(submission_queue._awaiting_start)
                if awaiting:
                    pending = (sorted((item for item in pending if item[0] in awaiting),
                                      key=lambda item: awaiting[item[0]]) +
                               [item for item in pending if item[0] not in awaiting])
        for task_id, record in pending:
            if self.stop_event.is_set() or getattr(self.owner, '_closing', False):
                break
            with self.lock:
                if (task_id in self.owner._rh_live_task_ids or
                        task_id in self.owner._rh_recovering_tasks or self._recovery_stopped(task_id)):
                    continue
                context = self.context(task_id, persisted=record, refresh_key=True)
                cancel_pending = context.get('cancel_requested')
                cancel_retry = (cancel_pending and context.get('api_key') and
                                context.get('cancel_attempts', 0) <= len(self.cancel_delays))
                if respect_backoff and not cancel_retry and self._poll_due.get(task_id, 0) > time.monotonic():
                    continue
                if (respect_backoff and cancel_pending and context.get('api_key') and
                        context.get('cancel_attempts', 0) <= len(self.cancel_delays) and
                        context.get('cancel_retry_at', 0) > time.time()):
                    continue
                if respect_backoff and not cancel_pending and self._download_retry_due.get(task_id, 0) > time.monotonic():
                    continue
                if background and len(self._recovery_workers) >= 2:
                    break
                self.owner._rh_recovering_tasks.add(task_id)
                if background:
                    self._recovery_cursor = task_id
                    # Reserve the poll slot before callbacks/worker creation;
                    # concurrent manual passes cannot overbook the pool.
                    self._recovery_workers[task_id] = None
                if task_id not in self.owner._rh_task_contexts:
                    self.recovered_task_ids.add(task_id)
                self.owner._rh_task_contexts[task_id] = context
            service = getattr(self.owner, '_rh_execution_service', None)
            if service is not None:
                try:
                    context = service.adopt_task(task_id, context)
                    with self.lock:
                        self.owner._rh_task_contexts[task_id] = context
                except Exception:
                    with self.lock:
                        self.owner._rh_recovering_tasks.discard(task_id)
                        self._recovery_workers.pop(task_id, None)
                    continue
            if background:
                worker = threading.Thread(target=self._recover_task, args=(task_id, context),
                                          kwargs={'background_download': True},
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

    def _prune_receipts(self):
        from pathlib import Path
        from aetherloom_core.rh_storage import prune_output_receipts

        def stopped():
            return self.stop_event.is_set() or getattr(self.owner, '_closing', False)

        def delete_if_idle(path, task_id, signature):
            with self.lock:
                active = self.owner._rh_live_task_ids | self.owner._rh_recovering_tasks
                if stopped() or task_id in active or (not task_id and active):
                    return
                status = self.owner._rh_status_entries.get(task_id, persisted_statuses.get(task_id))
                if not self.receipts_finished(task_id) and status not in TERMINAL_STATUSES:
                    if (task_id and (task_id in persisted_statuses or status in ACTIVE_STATUSES
                                     or task_id in self._download_retry_attempts)):
                        return  # Unresolved tasks resume polling/downloads after restart.
                    if not records_read:
                        return  # An unreadable task list cannot prove abandonment.
                try:
                    stat = path.stat(follow_symlinks=False)
                    if not path.is_symlink() and (stat.st_size, stat.st_mtime_ns) == signature:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

        try:
            persisted_statuses = {}
            records_read = False
            with self.lock:
                root = self.defaults.get('output_dir')
                directories = {value.get('output_dir') for value in self.owner._rh_task_contexts.values()
                               if value.get('output_dir')}
            try:
                records = self.store.read()
                records_read = True
                persisted_statuses = {task: value.get('status') for task, value in records.items()}
                directories.update(value.get('output_dir') for value in records.values()
                                   if value.get('output_dir'))
            except (OSError, ValueError, TypeError):
                pass  # Only confirmed terminal tasks can be cleaned in this case.

            def output_dirs():
                yield from directories
                if root:
                    yield root  # Includes outputs saved before per-app folders.
                    try:
                        with os.scandir(root) as entries:
                            for entry in entries:
                                if stopped():
                                    return
                                if not entry.name.startswith('.') and entry.is_dir(follow_symlinks=False):
                                    yield Path(entry.path)
                    except OSError:
                        pass

            prune_output_receipts(output_dirs(), delete_if_idle, cancelled=stopped)
        except Exception:
            # Maintenance must not interrupt execution or recovery.
            pass

    def _schedule_receipt_maintenance(self):
        if time.monotonic() < self._receipt_maintenance_due:
            return
        if self._receipt_maintenance_worker is not None and self._receipt_maintenance_worker.is_alive():
            return
        self._receipt_maintenance_due = time.monotonic() + 30 * 60
        self._receipt_maintenance_worker = threading.Thread(
            target=self._prune_receipts, name='rh-receipt-cleanup', daemon=True)
        self._receipt_maintenance_worker.start()

    def run(self):
        while not self.stop_event.is_set():
            self.wake_event.clear()
            try:
                self._schedule_receipt_maintenance()
                self.recover_once(background=True, respect_backoff=True)
            except Exception:
                # A temporarily unavailable task file must not kill recovery.
                pass
            delay = self.interval
            with self.lock:
                now = time.monotonic()
                for task_id, due in self._poll_due.items():
                    if due > now and task_id not in self.owner._rh_recovering_tasks:
                        delay = min(delay, max(.01, due - now))
                for task_id, context in self.owner._rh_task_contexts.items():
                    if (context.get('cancel_requested') and context.get('api_key') and
                            context.get('cancel_attempts', 0) <= len(self.cancel_delays) and
                            not self._recovery_stopped(task_id) and task_id not in self.owner._rh_recovering_tasks):
                        delay = min(delay, max(.05, context.get('cancel_retry_at', 0) - time.time()))
            self.wake_event.wait(delay)

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        with self.lock:
            for task_id in self._pending_downloads:
                self.owner._rh_recovering_tasks.discard(task_id)
            self._pending_downloads.clear()
