"""Per-task JSON documents with a bounded writer, atomic updates and private secrets.

UI models cache these documents; painting/paging never reads the filesystem.
Only a worker preparing a paid POST waits for its own write barrier. Ordinary
session documents are disposable; the existing taskId index selects downloads
that may survive shutdown, without replaying any queued submissions.
"""
from collections import OrderedDict
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

KINDS = frozenset({'applications', 'workflows', 'batches'})
SECRET_KEYS = frozenset({'apikey', 'apikeys', 'acceptedapikey', 'password', 'decodepassword',
                         'authorization', 'accesstoken', 'secret', 'clientsecret'})
MAX_READ_BYTES = 64 * 1024 * 1024
_INIT_LOCK = threading.Lock()

if os.name == 'nt':
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    _DPAPI_PROTECT = ctypes.windll.crypt32.CryptProtectData
    _DPAPI_PROTECT.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    _DPAPI_PROTECT.restype = wintypes.BOOL
    _DPAPI_UNPROTECT = ctypes.windll.crypt32.CryptUnprotectData
    _DPAPI_UNPROTECT.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    _DPAPI_UNPROTECT.restype = wintypes.BOOL


class _Patch(dict):
    """A cold-cache update is merged by the writer, never by a GUI callback."""
    def __init__(self, values, defaults):
        super().__init__(values)
        self.defaults = defaults


def public_document(value):
    """Remove credentials recursively, including named vendor input fields."""
    if isinstance(value, dict):
        result = {str(key): public_document(item) for key, item in value.items()
                  if str(key).lower().replace('_', '').replace('-', '') not in SECRET_KEYS}
        field = str(value.get('fieldName') or '').lower().replace('_', '').replace('-', '')
        if field in SECRET_KEYS and 'fieldValue' in result:
            result['fieldValue'] = '[已隐藏敏感参数]'
        return result
    if isinstance(value, (tuple, list)):
        return [public_document(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError('任务文档仅支持 JSON 数据')


def get_task_documents(owner):
    with _INIT_LOCK:
        repository = getattr(owner, '_task_documents', None)
        if repository is None:
            repository = TaskDocuments(Path(owner._rh_task_lifecycle.store.path).parent / 'tasks')
            owner._task_documents = repository
        return repository


def _protect(password):
    data = str(password).encode('utf-8')
    if os.name != 'nt':
        # Other platforms use a private 0600 file, never a public task document.
        return b'PRIVATE1\0' + data
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    if not _DPAPI_PROTECT(ctypes.byref(source), 'AetherLoom task decode', None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return b'DPAPI1\0' + ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def _unprotect(data):
    if data.startswith(b'PRIVATE1\0') and os.name != 'nt':
        return data[9:].decode('utf-8')
    if not data.startswith(b'DPAPI1\0') or os.name != 'nt':
        return ''
    data = data[7:]
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    if not _DPAPI_UNPROTECT(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        return ''
    try:
        return ctypes.string_at(target.data, target.size).decode('utf-8')
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def _atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.task-', suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers and antivirus scanners can briefly deny DELETE
        # sharing. Retry the atomic rename on this background writer instead
        # of rejecting an otherwise valid task before its paid POST. A real
        # permissions/disk failure still reaches the write barrier promptly.
        for delay in (0.01, 0.025, 0.05, 0.1, 0.2, 0.3, None):
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if os.name != 'nt' or getattr(error, 'winerror', None) not in {5, 32, 33} or delay is None:
                    raise
                time.sleep(delay)
    finally:
        if temporary and os.path.isfile(temporary):
            os.unlink(temporary)


class TaskDocuments:
    CACHE_SIZE = 256

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.session_id = uuid.uuid4().hex
        self._condition = threading.Condition(threading.RLock())
        self._io_lock = threading.RLock()
        self._cache = OrderedDict()
        self._pending = OrderedDict()
        self._secrets = {}
        self._versions = {}
        self._written = {}
        self._errors = {}
        self._owned = set()
        self._allowed = None
        self._cleanup = None
        self._cleanup_active = False
        self._inflight = None
        self._inflight_document = None
        self._thread = None
        self._safe_directories = {}

    @staticmethod
    def _key(kind, identity):
        if kind not in KINDS:
            raise ValueError('未知任务文档类型')
        identity = str(identity)
        if not identity:
            raise ValueError('任务文档缺少标识')
        leaf = (identity if re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', identity)
                else 'id-' + hashlib.sha256(identity.encode('utf-8')).hexdigest())
        return kind, leaf

    def reference(self, kind, identity):
        kind, leaf = self._key(kind, identity)
        return kind + '/' + leaf + '.json'

    def _path(self, key):
        return self.root / key[0] / (key[1] + '.json')

    def _secret_path(self, key):
        return self.root / '.private' / key[0] / (key[1] + '.bin')

    def _validate_target(self, path):
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        stat = directory.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self._safe_directories.get(directory) == identity:
            return
        if not directory.resolve().is_relative_to(self.root):
            raise OSError('任务文件路径超出独立任务目录')
        self._safe_directories[directory] = identity

    def _remember(self, key, document):
        self._cache[key] = document
        self._cache.move_to_end(key)
        while len(self._cache) > self.CACHE_SIZE:
            self._cache.popitem(last=False)

    def _load(self, key):
        pending = self._pending.get(key)
        if pending is not None:
            return self._materialize(key, pending[1])
        if self._inflight == key:
            return self._materialize(key, self._inflight_document)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._materialize(key, self._cache[key])
        document = self._read_disk(key)
        if document is not None:
            self._remember(key, document)
        return document

    def _read_disk(self, key):
        try:
            path = self._path(key)
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError('任务文档不能是外部链接')
            with open(path, 'rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > MAX_READ_BYTES:
                    raise ValueError('任务文档超过读取限制')
                data = stream.read(size + 1)
            document = json.loads(data)
            if not isinstance(document, dict) or document.get('schema_version') != 1:
                raise ValueError('任务文档格式或版本无效')
            document = public_document(document)
            return document
        except FileNotFoundError:
            return None

    def _materialize(self, key, value):
        if not isinstance(value, _Patch):
            return value
        document = self._read_disk(key) or dict(value.defaults)
        document.update(value)
        return document

    def get(self, kind, identity):
        with self._condition:
            return copy.deepcopy(self._load(self._key(kind, identity)))

    @staticmethod
    def _download(document):
        state = document.get('state') if isinstance(document.get('state'), dict) else document
        status = str(state.get('status') or document.get('status') or '').upper()
        if state.get('cancel_requested') or document.get('cancel_requested') or status in {
                'SUCCESS', 'FAILED', 'CANCELED', 'INTERRUPTED', 'UNKNOWN'}:
            return False
        return bool(state.get('cloud_success') or document.get('cloud_success') or
                    status in {'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET'})

    def _related(self, document):
        origin = document.get('origin') or {}
        result = set()
        for field, kind in (('workflow_group_id', 'batches'), ('workflow_job_id', 'workflows')):
            if origin.get(field):
                result.add(self._key(kind, origin[field]))
        return result

    def _enqueue(self, key, document):
        if self._allowed is not None and key not in self._allowed:
            if key[0] == 'applications' and self._download(document):
                self._allowed.add(key)
                self._allowed.update(self._related(document))
            else:
                return False
        version = self._versions.get(key, 0) + 1
        self._versions[key] = version
        self._pending[key] = (version, document)
        self._errors.pop(key, None)
        self._owned.add(key)
        self._remember(key, document)
        self._start_writer()
        self._condition.notify_all()
        return True

    def put(self, kind, identity, document, *, private_password=None):
        key = self._key(kind, identity)
        public = public_document(document)
        public.setdefault('schema_version', 1)
        public.setdefault('session_id', self.session_id)
        public.setdefault('id', str(identity))
        public.setdefault('kind', kind)
        public['task_document'] = self.reference(kind, identity)
        with self._condition:
            if private_password is not None:
                public['secret_ref'] = '.private/' + key[0] + '/' + key[1] + '.bin'
                self._secrets[key] = str(private_password)
            if not self._enqueue(key, public) and private_password is not None:
                self._secrets.pop(key, None)
        return public['task_document']

    def patch(self, kind, identity, changes):
        key = self._key(kind, identity)
        with self._condition:
            pending = self._pending.get(key)
            previous = (pending[1] if pending else self._inflight_document if self._inflight == key
                        else self._cache.get(key))
            if isinstance(previous, _Patch):
                document = _Patch(previous, previous.defaults)
            elif previous is not None:
                document = dict(previous)
            else:
                document = _Patch({}, dict(schema_version=1, session_id=self.session_id,
                                            id=str(identity), kind=kind,
                                            task_document=self.reference(kind, identity)))
            document.update(public_document(changes))
            self._enqueue(key, document)
        return self.reference(kind, identity)

    def set_secret(self, kind, identity, password):
        key = self._key(kind, identity)
        with self._condition:
            document = dict(self._load(key) or dict(schema_version=1, session_id=self.session_id,
                                                   kind=kind, id=str(identity),
                                                   task_document=self.reference(kind, identity)))
            document['secret_ref'] = '.private/' + key[0] + '/' + key[1] + '.bin'
            self._secrets[key] = str(password)
            if not self._enqueue(key, document):
                self._secrets.pop(key, None)

    def secret(self, kind, identity):
        key = self._key(kind, identity)
        with self._condition:
            if key in self._secrets:
                return self._secrets[key]
        try:
            path = self._secret_path(key)
            if path.is_symlink() or path.parent.is_symlink():
                return ''
            with open(path, 'rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                return _unprotect(stream.read(size + 1)) if size <= MAX_READ_BYTES else ''
        except (OSError, ValueError, UnicodeError):
            return ''

    def _start_writer(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._write_loop, name='task-json-writer', daemon=True)
            self._thread.start()

    def _write_loop(self):
        while True:
            with self._condition:
                if self._cleanup is not None:
                    cleanup = self._cleanup
                    self._cleanup = None
                    self._cleanup_active = True
                    entry = None
                elif self._pending:
                    key, (version, document) = self._pending.popitem(last=False)
                    self._inflight = key
                    self._inflight_document = document
                    password = self._secrets.get(key)
                    entry = key, version, document, password
                    cleanup = None
                else:
                    self._thread = None
                    self._condition.notify_all()
                    return
            if cleanup is not None:
                try:
                    self._remove_stale(*cleanup)
                except OSError:
                    # Cleanup failure must not strand unrelated pending writes.
                    pass
                finally:
                    with self._condition:
                        self._cleanup_active = False
                        self._condition.notify_all()
                continue
            key, version, document, password = entry
            error = None
            try:
                document = self._materialize(key, document)
                with self._condition:
                    self._inflight_document = document
                    if self._versions.get(key) == version:
                        self._remember(key, document)
                data = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
                with self._io_lock:
                    self._validate_target(self._path(key))
                    if password is not None:
                        self._validate_target(self._secret_path(key))
                        _atomic_bytes(self._secret_path(key), _protect(password))
                    _atomic_bytes(self._path(key), data)
            except Exception as failure:
                error = OSError('任务 JSON 未能保存：' + str(failure))
            with self._condition:
                if error is None:
                    self._written[key] = max(self._written.get(key, 0), version)
                    if password is not None and self._secrets.get(key) == password:
                        self._secrets.pop(key, None)
                elif version == self._versions.get(key):
                    self._errors[key] = error
                self._inflight = None
                self._inflight_document = None
                self._condition.notify_all()

    def is_flushed(self, kind, identity):
        key = self._key(kind, identity)
        with self._condition:
            if key in self._errors:
                raise self._errors[key]
            if key in self._pending:
                self._pending.move_to_end(key, last=False)
            return self._written.get(key, 0) >= self._versions.get(key, 1)

    def flush(self, kind=None, identity=None, timeout=10):
        end = time.monotonic() + timeout
        key = self._key(kind, identity) if kind is not None else None
        with self._condition:
            if key in self._pending:
                self._pending.move_to_end(key, last=False)
            while True:
                error = self._errors.get(key) if key else next(iter(self._errors.values()), None)
                if error:
                    raise error
                if key is not None:
                    if self._allowed is not None and key not in self._allowed:
                        return
                    if self._written.get(key, 0) >= self._versions.get(key, 1):
                        return
                elif not self._pending and self._inflight is None and self._cleanup is None and not self._cleanup_active:
                    return
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise OSError('等待任务 JSON 保存超时')
                self._condition.wait(remaining)

    def cleanup(self, retain_app_ids=(), *, closing=False):
        with self._condition:
            retained = {self._key('applications', identity) for identity in retain_app_ids}
            for key in tuple(retained):
                try:
                    document = self._load(key)
                    if document:
                        retained.update(self._related(document))
                except (OSError, ValueError):
                    pass
            if closing:
                self._allowed = retained
                for key in list(self._pending):
                    if key not in retained:
                        self._pending.pop(key, None)
                        self._secrets.pop(key, None)
                        self._errors.pop(key, None)
                self._errors = {key: error for key, error in self._errors.items() if key in retained}
            self._cleanup = (retained, bool(closing))
            self._start_writer()
            self._condition.notify_all()

    def _remove_stale(self, retained, closing):
        # Enumerate only fixed task subdirectories, never paths supplied in JSON.
        for kind in KINDS:
            for directory, suffix in ((self.root / kind, '.json'),
                                      (self.root / '.private' / kind, '.bin')):
                if (directory.is_symlink() or not directory.is_dir()
                        or not directory.resolve().is_relative_to(self.root)):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(suffix):
                            continue
                        key = kind, entry.name[:-len(suffix)]
                        with self._io_lock:
                            with self._condition:
                                allowed = self._allowed if self._allowed is not None else retained
                                if key in allowed or (not closing and key in self._owned):
                                    continue
                            try:
                                os.unlink(entry.path)
                            except FileNotFoundError:
                                pass
                            except OSError:
                                # Disposable session records are never replayed;
                                # a sharing lock can be retried at the next start.
                                pass

    def close(self, retain_app_ids=()):
        self.cleanup(retain_app_ids, closing=True)
