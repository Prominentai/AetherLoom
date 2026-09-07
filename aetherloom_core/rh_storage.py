"""App output folders and verified receipts for locally decoded derivatives."""

import hashlib
import json
import os
from pathlib import Path
import re


DECODED_FOLDER = '本地解码'


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
    return str(directory), str(directory / DECODED_FOLDER)


def decoded_output_path(output_dir, source, extension):
    """Retain readable names unless the extra directory exceeds Windows limits."""
    source = Path(source)
    extension = str(extension or source.suffix)
    if not re.fullmatch(r'\.[\w-]{1,15}', extension):
        extension = '.bin'
    root = Path(output_dir).resolve() / DECODED_FOLDER
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
    return source.parent / '.rh_decoded' / (key + '.json')


def is_decoded_output(path, output_dir):
    return Path(path).resolve().parent == (Path(output_dir) / DECODED_FOLDER).resolve()


def remember_decoded_output(source, decoded, task_id, token, *, cancelled=None):
    """Commit proof of both original and derivative, without storing a password.

    token is an opaque random identifier for this run's immutable decode setup,
    not a password hash. The raw download's receipt remains a separate proof.
    """
    from aetherloom_core.rh_outputs import _check_cancelled, _digest_file, _write_receipt
    source, decoded = Path(source), Path(decoded)
    if not token or not is_decoded_output(decoded, source.parent):
        raise ValueError('Invalid decoded output location or token')
    if not source.is_file() or not decoded.is_file() or source.stat().st_size <= 0 or decoded.stat().st_size <= 0:
        raise ValueError('The source and decoded output must be nonempty files')
    record = dict(task_id=str(task_id), token=str(token), source_name=source.name,
                  source_size=source.stat().st_size, source_sha256=_digest_file(source, cancelled),
                  decoded_name=decoded.name, size=decoded.stat().st_size, sha256=_digest_file(decoded, cancelled))
    _check_cancelled(cancelled)
    receipt = _receipt_path(source)
    receipt.parent.mkdir(parents=True, exist_ok=True)
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
        decoded = source.parent / DECODED_FOLDER / name
        if not is_decoded_output(decoded, source.parent) or not decoded.is_file():
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
        return str(decoded)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
