"""Atomic local canvas persistence and portable, credential-free packages."""

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .model import (VERSION, validate_document, validate_node, field_type, app_fields,
                    workflow_document, initialize_runtime, RUNTIME_FIELDS)
from . import model as canvas_model


MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024 * 1024
MAX_PACKAGE_FILES = 20000
_CREDENTIALS = {'apikey', 'api_key', 'api_keys', 'site_keyrings', 'access_token', 'accesstoken', 'authorization',
                'refresh_token', 'refreshtoken', 'cookie', 'cookies', 'login_token',
                'secret', 'client_secret', 'clientsecret', 'token', 'session_token',
                'sessiontoken', 'credentials', 'bearer_token', 'bearertoken'}
_ID = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
_WINDOWS_DEVICES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + number for prefix in ('COM', 'LPT') for number in '123456789¹²³'}


def _unsafe_component(part):
    return (part.rstrip(' .') != part or part.split('.', 1)[0].upper() in _WINDOWS_DEVICES
            or any(ord(character) < 32 or character in '<>"|?*' for character in part))


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path):
    if Path(path).stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError('画布文件过大')
    with open(path, 'r', encoding='utf-8-sig') as source:
        return json.load(source)


def _remove_secrets(value, secrets=None, route=()):
    if isinstance(value, list):
        return [_remove_secrets(item, secrets, route + (index,)) for index, item in enumerate(value)]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if normalized in _CREDENTIALS:
            continue
        if normalized in ('password', 'decode_password', 'local_password'):
            if secrets is not None and item:
                secrets[json.dumps(route + (key,), separators=(',', ':'))] = str(item)
            if item:
                result['password_required'] = True
            continue
        result[key] = _remove_secrets(item, secrets, route + (key,))
    return result


def _restore_secrets(value, secrets):
    for encoded, secret in secrets.items():
        try:
            route = json.loads(encoded)
            target = value
            for key in route[:-1]:
                target = target[key]
            target[route[-1]] = secret
        except (ValueError, TypeError, KeyError, IndexError):
            continue


def _node_sets(document):
    yield document.get('nodes', [])
    graph = (document.get('run') or {}).get('snapshot') or {}
    if graph.get('nodes') is not document.get('nodes'):
        yield graph.get('nodes', [])


def _run_states(document):
    run = document.get('run') or {}
    for section in ('nodes', 'cache'):
        for node_id, state in (run.get(section) or {}).items():
            yield node_id, state, section == 'nodes'


def _visit_paths(document, transform, include_results=True):
    """Visit only file-bearing fields; never rewrite arbitrary prompt strings."""
    for nodes in _node_sets(document):
        for node in nodes:
            params = node.get('params') or {}
            if isinstance(params.get('files'), list):
                params['files'] = [transform(path, required=True) for path in params['files']]
            for field in app_fields(node):
                if field_type(field) not in ('image', 'video', 'audio', 'file'):
                    continue
                value = field.get('fieldValue')
                if isinstance(value, str) and value:
                    field['fieldValue'] = transform(value)
                key = '{}::{}'.format(field.get('nodeId', ''), field.get('fieldName', ''))
                if isinstance(params.get(key), str) and params[key]:
                    params[key] = transform(params[key])
            if include_results:
                for result in node.get('results') or []:
                    if result.get('path'):
                        result['path'] = transform(result['path'], required=True)
    if include_results:
        for prepared in (document.get('run') or {}).get('prepared', {}).values():
            for field in prepared.get('nodes') or []:
                if field_type(field) in ('image', 'video', 'audio', 'file') and isinstance(field.get('fieldValue'), str):
                    field['fieldValue'] = transform(field['fieldValue'])
            for key in ('input_dir', 'output_dir'):
                if isinstance(prepared.get(key), str) and prepared[key]:
                    prepared[key] = transform(prepared[key], required=True)
        for unused, state, unused_current in _run_states(document):
            for result in state.get('results') or []:
                if result.get('path'):
                    result['path'] = transform(result['path'], required=True)
            for item in state.get('items') or []:
                for key in ('input_files', 'output_files'):
                    if isinstance(item.get(key), list):
                        item[key] = [transform(path, required=True) for path in item[key]]
                for field in (item.get('snapshot') or {}).get('nodes') or []:
                    if field_type(field) in ('image', 'video', 'audio', 'file') and isinstance(field.get('fieldValue'), str):
                        field['fieldValue'] = transform(field['fieldValue'])
                for key in ('input_dir', 'output_dir'):
                    snapshot = item.get('snapshot') or {}
                    if isinstance(snapshot.get(key), str) and snapshot[key]:
                        snapshot[key] = transform(snapshot[key], required=True)
                for result in item.get('results') or []:
                    if result.get('path'):
                        result['path'] = transform(result['path'], required=True)


def _configuration_hash(document, base_dir):
    """Compare configuration independently of JSON formatting and relative roots."""
    public = _remove_secrets(workflow_document(document))
    def remove_markers(value):
        if isinstance(value, dict):
            return {key: remove_markers(item) for key, item in value.items()
                    if key not in ('password_required', 'local_files')}
        if isinstance(value, list):
            return [remove_markers(item) for item in value]
        return value
    public = remove_markers(public)
    local_files = set(document.get('local_files') or [])
    def absolute(value, required=False):
        if not isinstance(value, str) or not value or '://' in value:
            return value
        path = Path(value)
        if path.is_absolute():
            return os.path.normcase(str(path.resolve()))
        candidate = Path(base_dir) / path
        if required or value in local_files or value.startswith(('..\\', '../')) or candidate.exists():
            return os.path.normcase(str(candidate.resolve()))
        return value
    _visit_paths(public, absolute, include_results=False)
    encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _available_runtime_results(document):
    """Skip missing outputs without changing imports, manual parameters or inputs."""
    missing_node_ids = set()
    live_nodes = document.get('nodes', [])
    for nodes in _node_sets(document):
        for node in nodes:
            results, signatures, missing = canvas_model.available_results(
                node.get('results', []), node.get('result_signatures'))
            node['results'] = results
            if missing:
                node.update(_restored_missing_results=True, fingerprint='', result_signatures=[])
                # Frozen nodes may still show cache from an earlier run. Its
                # missing files invalidate only that old cache, never a newer
                # successful result on the live node/current run state.
                if nodes is live_nodes:
                    missing_node_ids.add(node['id'])
            elif 'result_signatures' in node and signatures is not None:
                node['result_signatures'] = signatures
    for node_id, state, current in _run_states(document):
        results, signatures, missing = canvas_model.available_results(
            state.get('results', []), state.get('result_signatures'))
        state['results'] = results
        for item in state.get('items') or []:
            available, unused, item_missing = canvas_model.available_results(item.get('results', []))
            item['results'] = available
            if item_missing:
                item['_restored_missing_results'] = True
            missing |= item_missing
            if isinstance(item.get('output_files'), list):
                item['output_files'] = [path for path in item['output_files']
                                        if isinstance(path, str) and os.path.isfile(path)]
        if missing:
            state.update(_restored_missing_results=True, fingerprint='', result_signatures=[])
            if current:
                missing_node_ids.add(node_id)
        elif 'result_signatures' in state and signatures is not None:
            state['result_signatures'] = signatures
    for node in live_nodes:
        if node['id'] in missing_node_ids:
            node.update(_restored_missing_results=True, fingerprint='', result_signatures=[])
    return document


class CanvasStore:
    def __init__(self, root=None):
        if root is None:
            from aetherloom_core.paths import current_dir
            root = Path(current_dir) / 'canvases'
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.secret_path = self.root / '.secrets.json'
        self.snapshot_root = self.root / 'snapshots'
        self.session_id = uuid.uuid4().hex

    def normalize_session(self, document, *, ending=False):
        """Saved output references survive; an old session's DAG never resumes."""
        from aetherloom_core.rh_tasks import is_download_recovery
        run = document.get('run') or {}
        if not run or (not ending and run.get('session_id') == self.session_id
                       and not run.get('session_ended')):
            return document
        terminal = {'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN', 'BLOCKED', 'SKIPPED', 'INTERRUPTED'}
        run['session_ended'] = True
        run.pop('workflow_queue', None)
        pending_download = False
        live_nodes = {node['id']: node for node in document.get('nodes', [])}
        for node_id, state in run.get('nodes', {}).items():
            items = state.get('items') or []
            downloading = False
            interrupted = False
            for item in items:
                if item.get('task_id') and is_download_recovery(item):
                    item['cloud_success'] = True
                    downloading = pending_download = True
                elif item.get('status') not in terminal:
                    item.update(status='INTERRUPTED', message='客户端会话已结束，未继续此任务')
                    item.pop('cancel_requested', None)
                    interrupted = True
            if downloading:
                state.update(status='DOWNLOADING', message='恢复已生成结果的下载和处理')
            elif state.get('status') not in terminal or interrupted:
                state.update(status='INTERRUPTED', message='客户端会话已结束，未继续执行',
                             fingerprint='', cached=False)
            completed = [item for item in items if item.get('status') == 'SUCCESS']
            if completed and state.get('status') != 'SUCCESS':
                completed.sort(key=lambda item: (item.get('batch_index', 0), item.get('repeat_index', 0)))
                state['results'] = [result for item in completed for result in item.get('results', [])]
                state['result_signatures'] = []
            node = live_nodes.get(node_id)
            if node and state.get('status') in {'DOWNLOADING', 'INTERRUPTED'}:
                node.update(status=state['status'] if state.get('activated') else 'IDLE',
                            message=state['message'], stale=True, fingerprint='', cached=False)
                if state.get('results'):
                    node['results'] = copy.deepcopy(state['results'])
        if pending_download:
            run.update(status='DOWNLOADING', message='仅恢复已生成结果；未运行的工作流部分已中断')
        elif run.get('status') not in terminal:
            run.update(status='INTERRUPTED', message='客户端会话已结束；未完成工作流不会自动继续')
        return document

    def path_for(self, canvas_id):
        if not _ID.fullmatch(str(canvas_id)):
            raise ValueError('无效的画布标识')
        return self.root / (str(canvas_id) + '.json')

    def runtime_path_for(self, canvas_id):
        return self.snapshot_root / self.path_for(canvas_id).name

    def list_runtime(self):
        if not self.snapshot_root.exists():
            return []
        return [path.stem for path in self.snapshot_root.glob('*.json') if _ID.fullmatch(path.stem)]

    def set_active(self, canvas_id):
        self.path_for(canvas_id)
        with self.lock:
            _atomic_json(self.root / '.session.json', {'active_canvas_id': str(canvas_id)})

    def get_active(self):
        with self.lock:
            try:
                canvas_id = _read_json(self.root / '.session.json').get('active_canvas_id', '')
                if self.path_for(canvas_id).exists():
                    return str(canvas_id)
            except (OSError, ValueError, AttributeError):
                pass
        return ''

    def list(self, lightweight=False):
        result = []
        if not self.root.exists():
            return result
        for path in self.root.glob('*.json'):
            if path.name.startswith('.'):
                continue
            try:
                if lightweight:
                    if _ID.fullmatch(path.stem):
                        result.append({'id': path.stem, 'name': path.stem, 'path': str(path),
                                       'modified': path.stat().st_mtime})
                    continue
                data = _read_json(path)
                if data.get('version') == VERSION and data.get('id'):
                    result.append({'id': data['id'], 'name': data.get('name', path.stem),
                                   'path': str(path), 'modified': path.stat().st_mtime})
            except (OSError, ValueError, AttributeError):
                continue
        return sorted(result, key=lambda item: item['modified'], reverse=True)

    def _drop_runtime_secrets(self, canvas_ids):
        try:
            current = self._secrets()
        except (ValueError, TypeError):
            return  # Do not overwrite a corrupt shared password file with empty data.
        changed = False
        for canvas_id in canvas_ids:
            changed |= current.pop('runtime:' + canvas_id, None) is not None
            if not self.path_for(canvas_id).exists():
                changed |= current.pop('workflow:' + canvas_id, None) is not None
                changed |= current.pop(canvas_id, None) is not None
            elif isinstance(current.get(canvas_id), dict):
                legacy = current[canvas_id]
                remaining = {key: value for key, value in legacy.items()
                             if not key.startswith(('["run",', '@node:'))}
                if remaining != legacy:
                    changed = True
                    if remaining:
                        current[canvas_id] = remaining
                    else:
                        current.pop(canvas_id, None)
        if changed:
            if current:
                _atomic_json(self.secret_path, current)
            else:
                try:
                    self.secret_path.unlink()
                except FileNotFoundError:
                    pass

    def discard_runtime(self, canvas_id):
        """Discard one runtime and its private data; never delete the workflow."""
        with self.lock:
            path = self.runtime_path_for(canvas_id)
            removed = path.exists()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._drop_runtime_secrets([str(canvas_id)])
            return removed

    delete_runtime = discard_runtime

    def _discard_unusable_runtime(self, canvas_id):
        """A locked disposable snapshot must not prevent its workflow opening."""
        try:
            self.discard_runtime(canvas_id)
        except OSError:
            # Windows may keep the file open. A later load/cleanup retries it;
            # normal save_runtime/save_pair writes still propagate disk failures.
            pass

    def prune_orphans(self):
        """Filename/existence-only cleanup; no runtime JSON payload is inspected."""
        with self.lock:
            orphaned = []
            if self.snapshot_root.exists():
                for path in self.snapshot_root.glob('*.json'):
                    if _ID.fullmatch(path.stem) and not self.path_for(path.stem).is_file():
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                        orphaned.append(path.stem)
            secret_ids = set(orphaned)
            try:
                for key in self._secrets():
                    canvas_id = key.split(':', 1)[1] if key.startswith(('runtime:', 'workflow:')) else key
                    if _ID.fullmatch(canvas_id) and not self.path_for(canvas_id).is_file():
                        secret_ids.add(canvas_id)
            except (ValueError, TypeError):
                pass
            if secret_ids:
                self._drop_runtime_secrets(secret_ids)
            return orphaned

    def _secrets(self):
        if not self.secret_path.exists():
            return {}
        data = _read_json(self.secret_path)
        if not isinstance(data, dict):
            raise ValueError('画布本地密码文件格式错误')
        return data

    def _write_document(self, document, path, secret_key, *, runtime=False):
        if document.get('version') != VERSION:
            raise ValueError('不支持的画布文件版本')
        path = Path(path)
        with self.lock:
            secrets = {}
            public = _remove_secrets(copy.deepcopy(document), secrets)
            if runtime:
                run = document.get('run') or {}
                for node in run.get('snapshot', {}).get('nodes', []):
                    password = ((run.get('prepared') or {}).get(node['id'], {}).get('decode_settings') or {}).get('password')
                    if password is None:
                        password = (node.get('decode_settings') or {}).get('password')
                    if password:
                        secrets['@node:{}:{}'.format(run.get('id', ''), node['id'])] = str(password)
            local_files = set()
            def relative(value, required=False):
                if not isinstance(value, str) or not value or '://' in value:
                    return value
                if not os.path.isabs(value):
                    if required:
                        local_files.add(value)
                    return value
                try:
                    relative_path = os.path.relpath(value, path.parent)
                except ValueError:
                    relative_path = value
                local_files.add(relative_path)
                return relative_path
            _visit_paths(public, relative)
            public['local_files'] = sorted(local_files)
            current = self._secrets()
            if secrets:
                current[secret_key] = secrets
            else:
                current.pop(secret_key, None)
            # Commit secrets first: a public password-required snapshot must never
            # overtake its private recovery material after a normal local save.
            if current:
                _atomic_json(self.secret_path, current)
            elif self.secret_path.exists():
                self.secret_path.unlink()
            _atomic_json(path, {'snapshot_version': 1, 'document': public} if runtime else public)
        return path

    def save(self, document):
        """Save only reusable node configuration, connections and viewport."""
        return self._write_document(workflow_document(document), self.path_for(document['id']),
                                    'workflow:' + document['id'])

    def save_runtime(self, document):
        """Update only a matching, existing workflow; a late task cannot revive it."""
        with self.lock:
            if not self.workflow_matches(document):
                self.discard_runtime(document['id'])
                return None
            references = canvas_model.snapshot_result_references(document)
            return self._write_document(references, self.runtime_path_for(document['id']),
                                        'runtime:' + document['id'], runtime=True)

    def workflow_token(self, canvas_id):
        with self.lock:
            path = self.path_for(canvas_id)
            try:
                document = self._read_document(path, local=False)
                if document['id'] != str(canvas_id):
                    return None
                return _configuration_hash(document, path.parent)
            except (FileNotFoundError, ValueError, TypeError, KeyError, AttributeError):
                return None

    def workflow_matches(self, document):
        actual = self.workflow_token(document['id'])
        if actual is None:
            return False
        try:
            return actual == _configuration_hash(document, self.root)
        except (ValueError, TypeError, KeyError, AttributeError):
            return False

    def save_pair(self, document, expected_token=None, recreate=True):
        """Explicit editor save: commit authority before its matching runtime."""
        with self.lock:
            path = self.path_for(document['id'])
            if not recreate and not path.is_file():
                raise FileNotFoundError(str(path))
            if expected_token is not None and self.workflow_token(document['id']) != expected_token:
                raise ValueError('画布 JSON 已被外部修改，请重新打开后保存')
            path = self.save(document)
            if document.get('run') or self.runtime_path_for(document['id']).exists():
                self.save_runtime(document)
            return path

    def _read_document(self, path, *, runtime=False, local=False):
        data = _read_json(path)
        if runtime:
            if not isinstance(data, dict) or data.get('snapshot_version') != 1:
                raise ValueError('不支持的画布运行快照版本')
            data = data.get('document')
        if not isinstance(data, dict) or data.get('version') != VERSION:
            raise ValueError('不支持的画布文件版本')
        data['batch_count'] = canvas_model.normalize_batch_count(data.get('batch_count', 1))
        self.path_for(data.get('id', ''))
        if not isinstance(data.get('nodes'), list) or not isinstance(data.get('edges'), list):
            raise ValueError('画布节点或连线格式错误')
        for node in data['nodes']:
            validate_node(node)
        local_files = set(data.get('local_files') or [])
        def absolute(value, required=False):
            if not isinstance(value, str) or not value or '://' in value:
                return value
            value_path = Path(value)
            if value_path.is_absolute():
                return str(value_path)
            candidate = (path.parent / value_path).resolve()
            if required or value in local_files or value.startswith(('..\\', '../')) or candidate.exists():
                return str(candidate)
            return value
        _visit_paths(data, absolute)
        data.pop('local_files', None)
        expected = self.runtime_path_for(data['id']) if runtime else self.path_for(data['id'])
        if runtime and path != expected:
            raise ValueError('运行快照与画布标识不一致')
        if local and path == expected:
            secrets = self._secrets()
            key = ('runtime:' if runtime else 'workflow:') + data['id']
            _restore_secrets(data, secrets.get(key, secrets.get(data['id'], {})))
        return data

    def _resolve_workflow_path(self, identity):
        candidate = Path(str(identity))
        return candidate.resolve() if candidate.suffix.lower() == '.json' else self.path_for(identity)

    def load_workflow(self, identity):
        path = self._resolve_workflow_path(identity)
        with self.lock:
            data = self._read_document(path, local=path.parent == self.root)
            return initialize_runtime(workflow_document(data))

    def load_runtime(self, canvas_id):
        with self.lock:
            try:
                authority = self._read_document(self.path_for(canvas_id), local=True)
                if authority['id'] != str(canvas_id):
                    raise ValueError('画布 JSON 标识与文件名不一致')
            except (FileNotFoundError, ValueError, TypeError, KeyError, AttributeError):
                self._discard_unusable_runtime(canvas_id)
                return None
            # Keep workflow read errors outside this block: only the disposable
            # snapshot may fall back when permissions/sharing locks prevent reads.
            try:
                document = self._read_document(self.runtime_path_for(canvas_id), runtime=True, local=True)
                if _configuration_hash(document, self.root) != _configuration_hash(authority, self.root):
                    raise ValueError('运行快照与画布配置不一致')
                run = document.get('run') or {}
                if not isinstance(run, dict):
                    raise ValueError('画布运行状态格式错误')
                if run:
                    if not isinstance(run.get('id'), str) or not run['id']:
                        raise ValueError('画布运行标识无效')
                    run['batch_count'] = canvas_model.normalize_batch_count(run.get('batch_count', 1))
                    batch_index = run.setdefault('batch_index', 0)
                    if (isinstance(batch_index, bool) or not isinstance(batch_index, int)
                            or not 0 <= batch_index < run['batch_count']):
                        raise ValueError('画布当前批次无效')
                    graph = run.get('snapshot')
                    validate_document(graph)
                    graph['batch_count'] = canvas_model.normalize_batch_count(graph.get('batch_count', 1))
                    if graph['id'] != document['id'] or not isinstance(run.get('nodes'), dict):
                        raise ValueError('画布运行快照标识无效')
                    if not isinstance(run.get('cache', {}), dict):
                        raise ValueError('画布最近结果缓存格式错误')
                    graph_ids = {node['id'] for node in graph['nodes']}
                    for node_id, state, current in _run_states(document):
                        if ((current and node_id not in graph_ids) or not isinstance(node_id, str)
                                or not isinstance(state, dict) or not isinstance(state.get('items', []), list)):
                            raise ValueError('画布运行节点状态格式错误')
                        if any(not isinstance(item, dict) for item in state.get('items', [])):
                            raise ValueError('画布任务状态格式错误')
                return self.normalize_session(_available_runtime_results(document))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self._discard_unusable_runtime(canvas_id)
                return None

    def load(self, identity):
        path = self._resolve_workflow_path(identity)
        with self.lock:
            try:
                data = self._read_document(path, local=path.parent == self.root)
            except (FileNotFoundError, ValueError, TypeError, KeyError, AttributeError):
                if path.parent == self.root and _ID.fullmatch(path.stem):
                    self.discard_runtime(path.stem)
                raise
            if path != self.path_for(data['id']):
                if path.parent == self.root and _ID.fullmatch(path.stem):
                    self.discard_runtime(path.stem)
                return self.import_workflow(path)
            # One-time migration of former combined documents. Commit the full
            # snapshot first, so interrupted migration cannot lose accepted tasks.
            legacy = ('run' in data or any(any(key in node for key in RUNTIME_FIELDS)
                                           or node.get('params', {}).get('files') for node in data['nodes']))
            snapshot_path = self.runtime_path_for(data['id'])
            if legacy:
                previous_stat = path.stat()
                if not snapshot_path.exists():
                    self.save_runtime(data)
                self.save(data)
                os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
            try:
                snapshot_exists = snapshot_path.exists()
            except OSError:
                self._discard_unusable_runtime(data['id'])
                snapshot_exists = False
            if snapshot_exists:
                restored = self.load_runtime(data['id'])
                if restored is not None:
                    return restored
            return initialize_runtime(workflow_document(data))

    def update_runtime_record(self, record, updater):
        """Merge a real task result into only its one matching existing snapshot."""
        origin = record.get('origin') or {}
        canvas_id = origin.get('canvas_id')
        if not isinstance(canvas_id, str) or not _ID.fullmatch(canvas_id):
            return False
        with self.lock:
            document = self.load_runtime(canvas_id)
            if document is None:
                return False
            round_id = origin.get('round_id') or origin.get('execution_id')
            if round_id and document.get('run', {}).get('id') != round_id:
                return False
            updated = updater(document)
            if updated is None:
                return False
            return self.save_runtime(updated) is not None

    def export_workflow(self, document, path):
        """Export a single JSON file; passwords stay in local storage only."""
        public = _remove_secrets(canvas_model.normalize_app_urls(workflow_document(document)))
        target = Path(path).resolve()
        local_files = set()
        def relative(value, required=False):
            if isinstance(value, str) and value and os.path.isabs(value):
                try:
                    value = os.path.relpath(value, target.parent)
                except ValueError:
                    pass
                local_files.add(value)
            return value
        _visit_paths(public, relative, include_results=False)
        public['local_files'] = sorted(local_files)
        _atomic_json(target, public)
        return target

    def import_workflow(self, path):
        with self.lock:
            document = self.load_workflow(path)
            document = initialize_runtime(_remove_secrets(workflow_document(document)))
            document['id'] = uuid.uuid4().hex
            canvas_model.normalize_app_urls(document)
            self.save(document)
            return document

    def missing_assets(self, document):
        missing = []
        probe = copy.deepcopy(document)
        def check(value, required=False):
            if (isinstance(value, str) and value and '://' not in value and not os.path.exists(value)
                    and (required or os.path.isabs(value))):
                missing.append(value)
            return value
        _visit_paths(probe, check)
        return list(dict.fromkeys(missing))

    def password_for(self, origin):
        try:
            canvas_id = str(origin.get('canvas_id') or '')
            if not self.path_for(canvas_id).is_file() or not self.runtime_path_for(canvas_id).is_file():
                return ''
            round_id = origin.get('round_id') or origin.get('execution_id')
            node_id = origin.get('node_id')
            if not round_id or not node_id:
                return ''
            with self.lock:
                private = self._secrets().get('runtime:' + canvas_id, {})
                alias = '@node:{}:{}'.format(round_id or '', node_id)
                if alias in private:
                    return str(private[alias])
                # An old unscoped route may belong to a later canvas run. A
                # missing exact round identity must request the original secret.
                return ''
        except (OSError, ValueError, TypeError, AttributeError):
            return ''

    def export_package(self, document, path, include_results=False):
        public = _remove_secrets(copy.deepcopy(document))
        # An export is a reusable project, not a cross-account task recovery file.
        public['run'] = {}
        public.pop('local_files', None)
        for node in public['nodes']:
            node['status'] = 'IDLE'
            node.pop('message', None)
            if not include_results:
                node['results'] = []
                node['fingerprint'] = ''
                node.pop('result_signatures', None)
            for result in node.get('results', []):
                # A task ID here is inert result provenance, not a resumable
                # task. Keep it consistent with cached content signatures.
                result.pop('url', None)
        files, seen = {}, {}
        def collect(value, required=False):
            if not value or '://' in str(value):
                return value
            source = Path(value)
            if not source.is_file():
                if not required and not source.is_absolute() and not str(value).startswith(('..\\', '../')):
                    return value  # Existing server-upload reference in an App default.
                raise ValueError('打包所需文件不存在：' + str(value))
            key = os.path.normcase(str(source.resolve()))
            if key not in seen:
                safe_name = re.sub(r'[^\w. -]', '_', source.name)[:160] or 'file'
                archive_name = 'assets/{}-{}'.format(uuid.uuid4().hex[:12], safe_name)
                seen[key] = archive_name
                files[archive_name] = source
            return seen[key]
        _visit_paths(public, collect, include_results=include_results)
        if len(files) + 1 > MAX_PACKAGE_FILES or sum(p.stat().st_size for p in files.values()) > MAX_PACKAGE_BYTES:
            raise ValueError('画布包超过支持的大小或文件数量')
        target = Path(path).resolve()
        if target.exists() and target.is_dir():
            raise ValueError('导出路径必须是文件')
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.canvas-export-', suffix='.zip', dir=target.parent)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                archive.writestr('canvas.json', json.dumps(public, ensure_ascii=False, indent=2))
                for archive_name, source in files.items():
                    archive.write(source, archive_name)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def import_package(self, path):
        canvas_id = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.canvas-import-', dir=self.root))
        destination = self.root / 'assets' / canvas_id
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_PACKAGE_FILES or sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES:
                    raise ValueError('画布包超过支持的大小或文件数量')
                normalized = set()
                for info in infos:
                    raw = info.filename
                    relative = PurePosixPath(raw)
                    mode = info.external_attr >> 16
                    if ('\\' in raw or ':' in raw or relative.is_absolute() or '..' in relative.parts
                            or stat.S_ISLNK(mode) or not relative.parts
                            or any(_unsafe_component(part) for part in relative.parts)):
                        raise ValueError('画布包包含不安全的资源路径')
                    key = str(relative).casefold()
                    if key in normalized:
                        raise ValueError('画布包包含重复资源路径')
                    normalized.add(key)
                    if raw != 'canvas.json' and (not relative.parts or relative.parts[0] != 'assets'):
                        raise ValueError('画布包包含未知文件')
                if 'canvas.json' not in normalized:
                    raise ValueError('画布包缺少 canvas.json')
                info = archive.getinfo('canvas.json')
                if info.file_size > MAX_DOCUMENT_BYTES:
                    raise ValueError('画布文件过大')
                data = json.loads(archive.read(info).decode('utf-8-sig'))
                validate_document(data)
                data = _remove_secrets(data)
                data['id'] = canvas_id
                data['run'] = {}
                # Validate references before writing any extracted asset.
                def relocate(value, required=False):
                    if not isinstance(value, str) or not value:
                        return value
                    if not required and value.lower().startswith(('https://', 'http://')):
                        return value
                    relative = PurePosixPath(value)
                    if (not required and '\\' not in value and ':' not in value and not relative.is_absolute()
                            and '..' not in relative.parts and relative.parts
                            and relative.parts[0] != 'assets'):
                        return value  # An RH server-upload reference, not a packaged file.
                    if ('\\' in value or ':' in value or relative.is_absolute()
                            or '..' in relative.parts or not relative.parts
                            or relative.parts[0] != 'assets' or value.casefold() not in normalized):
                        raise ValueError('画布引用了包外资源或缺失资源')
                    return str(destination.joinpath(*relative.parts[1:]))
                _visit_paths(data, relocate)
                for info in infos:
                    if info.filename == 'canvas.json' or info.is_dir():
                        continue
                    relative = PurePosixPath(info.filename)
                    target = staging.joinpath(*relative.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, open(target, 'wb') as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
            self.save(data)
            return data
        finally:
            # Only task-owned staging paths under this exact root are removed.
            if staging.exists() and staging.parent.resolve() == self.root and staging.name.startswith('.canvas-import-'):
                shutil.rmtree(staging)
