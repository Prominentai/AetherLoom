"""App output folders and verified receipts for locally decoded derivatives."""

import hashlib
import json
import os
from pathlib import Path
import re
import threading


DECODED_FOLDER = '本地解码'
_migration_lock = threading.RLock()


def task_records_root():
    from aetherloom_core.paths import current_dir
    return Path(current_dir) / 'task_records' / 'runninghub'


def receipt_directory(output_dir, kind):
    """Keep proofs outside output folders; migrate the previous layout lazily."""
    if kind not in ('downloads', 'decoded'):
        raise ValueError('Unknown receipt kind')
    directory = Path(output_dir).resolve()
    identity = os.path.normcase(str(directory))
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]
    target = task_records_root() / 'receipts' / key / kind
    legacy = directory / ('.rh_downloads' if kind == 'downloads' else '.rh_decoded')
    with _migration_lock:
        if legacy.is_dir() and not legacy.is_symlink():
            from aetherloom_core.rh_outputs import _write_receipt
            with os.scandir(legacy) as entries:
                for entry in entries:
                    if not re.fullmatch(r'[0-9a-f]{64}\.json', entry.name) or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        if entry.stat().st_size > 65536:
                            continue
                        record = json.loads(Path(entry.path).read_text(encoding='utf-8'))
                        if not isinstance(record, dict) or not str(record.get('task_id') or '').strip():
                            continue
                        destination = target / entry.name
                        if not destination.exists():
                            target.mkdir(parents=True, exist_ok=True)
                            _write_receipt(destination, record)
                        Path(entry.path).unlink()
                    except (OSError, ValueError, TypeError):
                        continue
            try:
                legacy.rmdir()
            except OSError:
                pass
    return target


def app_output_directories(root, app_name, app_id):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(app_name or '')).strip(' .')[:40].rstrip(' .')
    name = name or 'App'
    # Always include an identity: equal names and Windows case folding must not
    # merge two apps, and a renamed app must not redirect an existing task.
    identity = str(app_id or '')
    label = re.sub(r'[^a-zA-Z0-9_-]', '_', identity)[:24] or 'unknown'
    if label != identity or not identity.isdecimal():
        label = label[:12] + '-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]
    directory = Path(root).resolve() / (name + '__' + label)
    return str(directory), str(directory)


def decoded_output_path(output_dir, source, extension):
    """Save derivatives alongside the app's original outputs."""
    source = Path(source)
    extension = str(extension or source.suffix)
    if not re.fullmatch(r'\.[\w-]{1,15}', extension):
        extension = '.bin'
    root = Path(output_dir).resolve()
    name = source.stem + '_restored' + extension
    units = lambda value: len(str(value).encode('utf-16-le', errors='surrogatepass')) // 2
    if units(name) > 240 or (os.name == 'nt' and units(root / name) > 248):
        name = 'decoded_' + hashlib.sha256(source.name.encode('utf-8')).hexdigest()[:24] + extension
    target = root / name
    if os.name == 'nt' and units(target) > 248:
        raise ValueError('本地解码目录过长，请缩短输出目录')
    return str(target)


def _receipt_path(source):
    source = Path(source)
    key = hashlib.sha256(source.name.encode('utf-8')).hexdigest()
    return receipt_directory(source.parent, 'decoded') / (key + '.json')


def _derivative_receipt_path(path):
    path = Path(path)
    key = hashlib.sha256(('decoded\0' + path.name).encode('utf-8')).hexdigest()
    return receipt_directory(path.parent, 'decoded') / (key + '.json')


def is_decoded_output(path, output_dir, *, task_id=None, token=None, cancelled=None):
    """Identify a derivative by its receipt, never by its filename alone."""
    from aetherloom_core.rh_outputs import _digest_file
    path, root = Path(path).resolve(), Path(output_dir).resolve()
    if path.parent == root / DECODED_FOLDER:
        return True  # Legacy derivatives returned by the verified downloader.
    if path.parent != root:
        return False
    try:
        record = json.loads(_derivative_receipt_path(path).read_text(encoding='utf-8'))
        return (record.get('decoded_name') == path.name
                and record.get('decoded_folder') == ''
                and (task_id is None or record.get('task_id') == str(task_id))
                and (token is None or record.get('token') == str(token))
                and path.is_file() and path.stat().st_size == record.get('size', 0)
                and record.get('size', 0) > 0
                and _digest_file(path, cancelled) == record.get('sha256'))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def remember_decoded_output(source, decoded, task_id, token, *, cancelled=None):
    """Commit proof of both original and derivative, without storing a password.

    token is an opaque random identifier for this run's immutable decode setup,
    not a password hash. The raw download's receipt remains a separate proof.
    """
    from aetherloom_core.rh_outputs import _check_cancelled, _digest_file, _write_receipt
    source, decoded = Path(source), Path(decoded)
    if (not str(task_id or '').strip() or not token or decoded.resolve() == source.resolve()
            or decoded.resolve().parent != source.resolve().parent):
        raise ValueError('Invalid decoded output location or token')
    if not source.is_file() or not decoded.is_file() or source.stat().st_size <= 0 or decoded.stat().st_size <= 0:
        raise ValueError('The source and decoded output must be nonempty files')
    record = dict(task_id=str(task_id), token=str(token), source_name=source.name,
                  source_size=source.stat().st_size, source_sha256=_digest_file(source, cancelled),
                  decoded_name=decoded.name, decoded_folder='',
                  size=decoded.stat().st_size, sha256=_digest_file(decoded, cancelled))
    _check_cancelled(cancelled)
    receipt = _receipt_path(source)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    _write_receipt(_derivative_receipt_path(decoded), record)
    _write_receipt(receipt, record)


def valid_decoded_output(source, task_id, token, *, source_info=None, cancelled=None):
    """Return the derivative only after checking identity, setup and file bytes.

    source_info may only be a verified original-download receipt. Without it,
    an existing original is hashed; a missing original cannot prove the link.
    """
    from aetherloom_core.rh_outputs import _check_cancelled, _digest_file
    if not token:
        return None
    source = Path(source)
    try:
        record = json.loads(_receipt_path(source).read_text(encoding='utf-8'))
        if not isinstance(record, dict) or (record.get('task_id'), record.get('token'), record.get('source_name')) != (str(task_id), str(token), source.name):
            return None
        name = record.get('decoded_name')
        if not isinstance(name, str) or not name or '/' in name or '\\' in name or ':' in name or name in {'.', '..'}:
            return None
        folder = record.get('decoded_folder', DECODED_FOLDER)
        if folder not in ('', DECODED_FOLDER):
            return None
        decoded = source.parent / folder / name
        if (decoded.resolve() == source.resolve()
                or decoded.resolve().parent != (source.parent / folder).resolve()
                or not decoded.is_file()):
            return None
        if source_info is None:
            if not source.is_file():
                return None
            source_info = dict(size=source.stat().st_size, sha256=_digest_file(source, cancelled))
        if (record.get('source_size') != source_info.get('size')
                or record.get('source_sha256') != source_info.get('sha256')
                or not isinstance(record.get('source_size'), int) or record['source_size'] <= 0
                or not isinstance(record.get('source_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', record['source_sha256'])):
            return None
        _check_cancelled(cancelled)
        if (decoded.stat().st_size != record.get('size') or record.get('size', 0) <= 0
                or _digest_file(decoded, cancelled) != record.get('sha256')):
            return None
        # A pruned reverse receipt must not let a derivative be decoded again
        # as if it were an original download.
        if folder == '' and not is_decoded_output(decoded, source.parent,
                task_id=task_id, token=token, cancelled=cancelled):
            return None
        return str(decoded)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def prune_output_receipts(output_dirs, delete_if_idle, *, cancelled=lambda: False):
    """Stream receipts to the lifecycle's retry-aware cleanup policy.

    output_dirs contains exact app output directories, never media subtrees.
    The caller serializes deletion with task startup and protects active tasks.
    """
    visited = set()

    def discard(item):
        _, name, size, modified = item
        path = Path(name)
        try:
            with path.open('r', encoding='utf-8') as handle:
                record = json.loads(handle.read(65537)) if size <= 65536 else None
            task_id = str(record.get('task_id') or '') if isinstance(record, dict) else ''
        except (OSError, ValueError, TypeError):
            task_id = ''
        if not cancelled():
            delete_if_idle(path, task_id, (size, modified))

    def receipt_dirs():
        for output_dir in output_dirs:
            if cancelled():
                return
            for kind, legacy in (('downloads', '.rh_downloads'), ('decoded', '.rh_decoded')):
                yield receipt_directory(output_dir, kind)
                yield Path(output_dir) / legacy  # Malformed legacy records can be pruned too.
        # Include orphan proofs even if their old output directory was removed.
        try:
            with os.scandir(task_records_root() / 'receipts') as entries:
                for entry in entries:
                    if cancelled():
                        return
                    if re.fullmatch(r'[0-9a-f]{32}', entry.name) and entry.is_dir(follow_symlinks=False):
                        yield Path(entry.path) / 'downloads'
                        yield Path(entry.path) / 'decoded'
        except OSError:
            pass

    for receipts in receipt_dirs():
        if cancelled():
            return
        if receipts in visited or receipts.is_symlink():
            continue
        visited.add(receipts)
        try:
            with os.scandir(receipts) as entries:
                for entry in entries:
                    if cancelled():
                        return
                    if not re.fullmatch(r'[0-9a-f]{64}\.json', entry.name):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    item = (stat.st_mtime, entry.path, stat.st_size, stat.st_mtime_ns)
                    discard(item)
        except OSError:
            continue
