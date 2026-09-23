"""Secret-free, atomic per-task records; one bounded/coalescing disk writer."""
import base64
import copy
import hashlib
import heapq
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict

from PIL import Image

SCHEMA_VERSION = 1
MAX_JSON = 12 * 1024 * 1024
MAX_ASSET = 32 * 1024 * 1024
_SECRET_KEYS = frozenset(('api_key', 'api_keys', 'token', 'tokens', 'authorization',
                         'access_token', 'refresh_token', 'password', 'cookie', 'headers'))
_BINARY_KEYS = frozenset(('image', 'mask', 'reference_image', 'reference_image_multiple',
                         'director_reference_images', 'png', 'paint_png', 'bytes'))
_NAME = re.compile(r'^(\d{20})-([a-f0-9]{32})\.json$')
_TASK_FIELDS = frozenset(('id', 'index', 'state', 'title', 'model', 'action', 'created', 'started',
    'dispatch_time', 'finished', 'message', 'submitted', 'accepted', 'attempts', 'key_index',
    'key_count', 'next_retry', 'last_rejected', 'results', 'result_unknown', 'expected_results',
    'received_results', 'saved_results', 'source', 'batch'))


def clean(value, secrets=(), *, binary=False):
    """Redact full credentials before serialization; media is only a digest."""
    if isinstance(value, dict):
        return {str(key): clean(item, secrets, binary=str(key).lower() in _BINARY_KEYS)
                for key, item in value.items() if str(key).lower() not in _SECRET_KEYS
                and not str(key).startswith('_')}
    if isinstance(value, (list, tuple)):
        return [clean(item, secrets, binary=binary) for item in value]
    if isinstance(value, bytes) or binary and isinstance(value, str):
        raw = value if isinstance(value, bytes) else value.encode('utf-8')
        return {'omitted': 'binary', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if isinstance(value, str):
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            value = value.replace(secret, '[凭据已隐藏]')
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def mask_copy(mask, secrets=()):
    if not isinstance(mask, dict):
        return None
    inline = {field: mask[field] for field in ('png', 'paint_png') if field in mask}
    result = clean({field: value for field, value in mask.items() if field not in inline}, secrets)
    result.update(inline)
    return result


def task_fields(task):
    return {key: copy.deepcopy(task[key]) for key in _TASK_FIELDS if key in task}


def request_metadata(endpoint, payload):
    return {'method': 'POST', 'endpoint': endpoint, 'body': clean(payload), 'prepared_at': time.time()}


def merge_resolved_characters(originals, positive, negative):
    """Map submitted captions onto active cards without shifting saved drafts.

    API caption arrays exclude disabled cards. Keep local names, enable states,
    and coordinates so copying a task cannot attach another card's settings.
    When importing captions without local originals, create active cards.
    """
    characters = copy.deepcopy(originals) if isinstance(originals, list) else []
    positive = positive if isinstance(positive, list) else []
    negative = negative if isinstance(negative, list) else []
    active = [ch for ch in characters if isinstance(ch, dict) and ch.get('enabled', True)]
    for index, caption in enumerate(positive[:32]):
        if not isinstance(caption, dict):
            continue
        if index < len(active):
            character = active[index]
        else:
            character = dict(enabled=True, use_coords=False, x=.5, y=.5)
            centers = caption.get('centers')
            center = centers[0] if isinstance(centers, list) and centers and isinstance(centers[0], dict) else {}
            for axis in ('x', 'y'):
                value = center.get(axis)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
                    character[axis] = value
            characters.append(character)
        text = caption.get('char_caption', caption.get('prompt', ''))
        if isinstance(text, str):
            character['prompt'] = text
        if index < len(negative) and isinstance(negative[index], dict):
            text = negative[index].get('char_caption', negative[index].get('negative_prompt', ''))
            if isinstance(text, str):
                character['negative_prompt'] = text
    return characters


def copy_from_request(original, mask, request):
    options = copy.deepcopy(original)
    body = request.get('body', {})
    params = body.get('parameters', {}) if isinstance(body, dict) else {}
    if params:
        if isinstance(params.get('seed'), int):
            options['seed'] = params['seed']
        for field in ('prompt', 'negative_prompt'):
            if isinstance(params.get(field), str):
                options[field] = params[field]
        options.update(quality_preset='none', uc_preset='none')
        # Resolved active captions no longer need chunks, but a disabled draft
        # may still reference them when the user enables that card later.
        if not any(isinstance(ch, dict) and not ch.get('enabled', True)
                   for ch in options.get('characters', [])):
            options['chunks'] = {}
        positive = params.get('v4_prompt', {}).get('caption', {}).get('char_captions', [])
        negative = params.get('v4_negative_prompt', {}).get('caption', {}).get('char_captions', [])
        options['characters'] = merge_resolved_characters(options.get('characters', []), positive, negative)
    elif request.get('resolved_tool'):
        resolved = request['resolved_tool']
        # Older records used prompt for Director before its editor was separate.
        options['tool_prompt'] = copy.deepcopy(resolved.get('tool_prompt', resolved.get('prompt', '')))
        options['chunks'] = {}
    return {'options': options, 'mask': copy.deepcopy(mask)}


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name('.' + uuid.uuid4().hex + '.part')
    try:
        with part.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def _asset_mask(mask, path):
    if not isinstance(mask, dict):
        return None
    result = copy.deepcopy(mask)
    folder = Path(path).parent / 'assets' / _NAME.fullmatch(Path(path).name)[2]
    for field, path_field, hash_field in (('png', 'path', 'sha256'), ('paint_png', 'paint_path', 'paint_sha256')):
        encoded = result.pop(field, None)
        if not encoded:
            continue
        if not isinstance(encoded, str) or len(encoded) > MAX_ASSET * 2:
            raise ValueError('任务绘制附件超过大小限制。')
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_ASSET:
            raise ValueError('任务绘制附件无效或过大。')
        digest = hashlib.sha256(raw).hexdigest()
        if result.get(hash_field) and result[hash_field] != digest:
            raise ValueError('任务绘制附件摘要不一致。')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'PNG' or image.width * image.height > 32_000_000:
                raise ValueError('任务绘制附件必须为有效的 PNG。')
            image.verify()
        asset = folder / (digest + '.png')
        if not asset.is_file() or asset.stat().st_size != len(raw):
            _atomic_bytes(asset, raw)
        result[path_field], result[hash_field] = str(asset), digest
    return result


class Acknowledgement:
    def __init__(self):
        self.event = threading.Event()
        self.error = None

    def wait(self, timeout=15):
        if not self.event.wait(timeout):
            raise OSError('任务记录保存超时，未提交生成请求。')
        if self.error:
            raise OSError('任务记录保存失败：' + self.error)


class RecordWriter:
    def __init__(self, callback):
        self.callback = callback
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._deleted = set()
        self._active = False
        self._thread = None

    def submit(self, identity, path, document, version):
        ack = Acknowledgement()
        with self._condition:
            if identity in self._deleted:
                ack.error = '任务记录已清除。'
                ack.event.set()
                return ack
            previous = self._pending.pop(identity, None)
            waiting = previous[4] if previous is not None else []
            self._pending[identity] = ('write', Path(path), document, version, waiting + [ack])
            self._ensure_thread()
            self._condition.notify()
        return ack

    def delete(self, identity, path):
        with self._condition:
            self._deleted.add(identity)
            previous = self._pending.pop(identity, None)
            if previous:
                for ack in previous[4]:
                    ack.error = '任务记录已清除。'
                    ack.event.set()
            self._pending[identity] = ('delete', Path(path), None, 0, [])
            self._ensure_thread()
            self._condition.notify()

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name='NovelAI task records', daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            with self._condition:
                if not self._pending:
                    self._active = False
                    self._condition.notify_all()
                    self._thread = None
                    return
                identity, operation = self._pending.popitem(last=False)
                self._active = True
            kind, path, document, version, waiting = operation
            error, mask = '', None
            try:
                if kind == 'delete':
                    self._delete(path)
                else:
                    mask = _asset_mask(document.get('copy', {}).get('mask'), path)
                    if document.get('copy'):
                        document['copy']['mask'] = mask
                    document = clean(document)
                    data = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
                    if len(data) > MAX_JSON:
                        raise ValueError('任务记录超过 12 MB，请减少输入参数。')
                    with self._condition:
                        deleted = identity in self._deleted
                    if not deleted:
                        _atomic_bytes(path, data)
                    else:
                        error = '任务记录已清除。'
            except Exception as exc:
                error = str(exc)
            for ack in waiting:
                ack.error = error
                ack.event.set()
            try:
                self.callback(identity, {'version': version, 'error': error, 'mask': mask, 'deleted': kind == 'delete'})
            except RuntimeError:
                pass
            with self._condition:
                self._active = False
                self._condition.notify_all()

    def _delete(self, path):
        # Only task JSON and its own managed PNG attachments may be removed.
        match = _NAME.fullmatch(path.name)
        if not match or path.parent.name != 'novelai' or path.parent.parent.name != 'task_records':
            raise ValueError('拒绝清理任务记录目录以外的路径。')
        path.unlink(missing_ok=True)
        folder = (path.parent / 'assets' / match[2]).resolve()
        expected = (path.parent / 'assets').resolve()
        if folder.parent != expected:
            raise ValueError('任务附件路径不在允许范围内。')
        if folder.exists():
            shutil.rmtree(folder)

    def flush(self, timeout=5):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active or self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


class RecordState:
    def __init__(self, writer, data_dir, task, options, mask, secrets=()):
        self.writer, self.identity = writer, task['id']
        self.path = Path(data_dir) / 'task_records' / 'novelai' / f'{time.time_ns():020d}-{self.identity}.json'
        self._lock = threading.RLock()
        self._secrets = tuple(secrets)
        self.version = 0
        self._local_complete = False
        self.document = {'schema_version': SCHEMA_VERSION, 'service': 'novelai',
                         'task': clean(task_fields(task), self._secrets),
                         'options': clean(options, self._secrets),
                         'copy': {'options': clean(options, self._secrets), 'mask': mask_copy(mask, self._secrets)},
                         'request': None, 'updated_at': time.time(), 'automatic_resume': False}

    def update(self, *, task=None, request=None, copy_value=None, wait=False, complete=False):
        with self._lock:
            if task is not None:
                values = clean(task_fields(task), self._secrets)
                if self._local_complete and not complete:
                    # Late GUI progress/cancel events cannot erase a worker's
                    # verified final file references during application shutdown.
                    for key in ('state', 'results', 'saved_results', 'received_results',
                                'finished', 'result_unknown', 'message'):
                        values.pop(key, None)
                self.document['task'].update(values)
            if complete:
                self._local_complete = True
            if request is not None:
                self.document['request'] = clean(request, self._secrets)
            if copy_value is not None:
                value = copy.deepcopy(copy_value)
                value['options'] = clean(value.get('options', {}), self._secrets)
                value['mask'] = mask_copy(value.get('mask'), self._secrets)
                self.document['copy'] = value
            self.document['updated_at'] = time.time()
            self.version += 1
            ack = self.writer.submit(self.identity, self.path, copy.deepcopy(self.document), self.version)
        if wait:
            ack.wait()
        return ack

    def get_copy(self):
        with self._lock:
            return copy.deepcopy(self.document['copy'])

    def get_task(self):
        with self._lock:
            return copy.deepcopy(self.document['task'])

    def release_secrets(self):
        with self._lock:
            self._secrets = ()

    def accept_mask(self, version, mask):
        with self._lock:
            if version == self.version and self.document.get('copy'):
                self.document['copy']['mask'] = copy.deepcopy(mask)


def independent_copy(value, record_path):
    """Copied drafts survive deletion of their source task's private attachments."""
    result = copy.deepcopy(value)
    mask = result.get('mask')
    match = _NAME.fullmatch(Path(record_path).name)
    if not isinstance(mask, dict) or not match:
        return result
    owned = (Path(record_path).parent / 'assets' / match[2]).resolve()
    for path_field, inline, hash_field in (('path', 'png', 'sha256'), ('paint_path', 'paint_png', 'paint_sha256')):
        if mask.get(inline) or not isinstance(mask.get(path_field), str):
            continue
        try:
            source = Path(mask[path_field]).resolve()
            if source.parent != owned:
                continue
            with source.open('rb') as stream:
                data = stream.read(MAX_ASSET + 1)
            if not data or len(data) > MAX_ASSET:
                raise ValueError('复制的任务绘制附件无效或超过大小限制。')
            digest = hashlib.sha256(data).hexdigest()
            if mask.get(hash_field) and mask[hash_field] != digest:
                raise ValueError('复制的任务绘制附件校验失败。')
            mask[inline] = base64.b64encode(data).decode('ascii')
            mask[hash_field] = digest
        except OSError:
            pass  # Preserve missing references for the editor's normal warning.
    return result


def read(path, *, previous_session=True):
    path = Path(path)
    if path.stat().st_size > MAX_JSON:
        raise ValueError('任务记录超过大小限制。')
    with path.open('r', encoding='utf-8') as stream:
        document = json.load(stream)
    match = _NAME.fullmatch(path.name)
    if (not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION
            or not isinstance(document.get('task'), dict) or not match
            or document['task'].get('id') != match[2]):
        raise ValueError('任务记录格式或版本不支持。')
    if previous_session:
        task = document['task']
        if task.get('state') == 'save_failed':
            task.update(state='failed', result_unknown=False, next_retry=None,
                        message='上次会话结束时图片未保存完整；尚未保存的部分已丢失，不会自动重新提交。')
        elif task.get('state') not in ('succeeded', 'failed', 'cancelled'):
            task.update(state='cancelled', result_unknown=bool(task.get('submitted') or task.get('accepted')),
                        next_retry=None, message='上次会话已结束；历史任务不会自动重新提交。')
    return document


def list_page(data_dir, *, limit=60, before=None):
    limit = max(1, min(120, int(limit)))
    directory = Path(data_dir) / 'task_records' / 'novelai'
    if not directory.is_dir():
        return {'records': [], 'next_cursor': None, 'errors': []}
    # Scan only names. A bounded heap retains this page, never every record body.
    with os.scandir(directory) as entries:
        names = heapq.nlargest(limit + 1, (entry.name for entry in entries
            if _NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            and (before is None or entry.name < str(before))))
    records, errors = [], []
    for name in names[:limit]:
        try:
            value = read(directory / name)
            value['record_path'] = str(directory / name)
            records.append(value)
        except (OSError, ValueError, TypeError) as error:
            errors.append({'path': str(directory / name), 'error': str(error)})
    return {'records': records, 'next_cursor': names[limit - 1] if len(names) > limit else None, 'errors': errors}
