"""Atomic, resumable output downloads shared by RH execution and recovery."""

from contextlib import contextmanager
import base64
import binascii
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import random
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit
import zlib

import requests
from urllib3.exceptions import HTTPError as UrllibHTTPError


class OutputDownloadError(RuntimeError):
    """At least one output could not be completely saved."""

    def __init__(self, message, *, failures=None, completed_paths=None):
        super().__init__(message)
        self.failures = failures or []
        self.completed_paths = completed_paths or []


class OutputDownloadCancelled(OutputDownloadError):
    """The caller stopped a download; completed outputs remain reusable."""


class _AttemptFailure(Exception):
    def __init__(self, reason, *, retryable=False, status=None, retry_after=None):
        self.reason, self.retryable = reason, retryable
        self.status, self.retry_after = status, retry_after
        super().__init__(reason)


_CHUNK_SIZE = 1024 * 1024
_MAX_RETRY_DELAY = 30.0
_WINDOWS_PATH_LIMIT = 259  # Traditional MAX_PATH includes the terminating NUL.
_MEDIA_EXTENSIONS = frozenset(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff',
                             '.avif', '.heic', '.svg', '.mp4', '.mov', '.mkv', '.webm', '.avi',
                             '.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'))


def _check_cancelled(cancelled):
    if cancelled is None:
        return
    try:
        stop = cancelled()
    except Exception:
        stop = True  # A destroyed owner must not leave an orphaned download.
    if stop:
        raise OutputDownloadCancelled('Output download cancelled') from None


_registry_lock = threading.Lock()
_task_locks = {}
# The legacy GRC decoder stores its grid dimensions in module globals.
GRC_DECODE_LOCK = threading.RLock()


@contextmanager
def _task_lock(task_id, output_dir, cancelled=None):
    key = (str(task_id), os.path.normcase(os.path.realpath(output_dir)))
    with _registry_lock:
        entry = _task_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    acquired = False
    try:
        while not acquired:
            _check_cancelled(cancelled)
            acquired = entry[0].acquire(timeout=0.05)
        _check_cancelled(cancelled)
        yield
    finally:
        if acquired:
            entry[0].release()
        with _registry_lock:
            entry[1] -= 1
            if not entry[1]:
                _task_locks.pop(key, None)


def _digest_file(path, cancelled=None):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            _check_cancelled(cancelled)
            digest.update(chunk)
    return digest.hexdigest()


def _url_identity(url):
    """Drop only recognised signing credentials, preserving resource locators.

    Raw query values are preserved, including unknown token/id/key/versionId
    fields. An ordinary business parameter named Expires is never stripped.
    """
    parsed = urlsplit(url)
    fields = parsed.query.split('&') if parsed.query else []
    names = {unquote(field.split('=', 1)[0]).lower() for field in fields}
    aws = 'x-amz-signature' in names
    oss = 'x-oss-signature' in names or {'ossaccesskeyid', 'signature'} <= names
    cloudfront = {'key-pair-id', 'signature'} <= names
    cos = {'q-sign-algorithm', 'q-signature'} <= names

    def credential(field):
        name = unquote(field.split('=', 1)[0]).lower()
        return ((aws and name.startswith('x-amz-'))
                or (oss and (name.startswith('x-oss-') or name in {'ossaccesskeyid', 'signature', 'expires', 'security-token'}))
                or (cloudfront and name in {'key-pair-id', 'signature', 'expires', 'policy'})
                or (cos and name in {'q-sign-algorithm', 'q-ak', 'q-sign-time', 'q-key-time',
                                     'q-header-list', 'q-url-param-list', 'q-signature', 'x-cos-security-token'}))

    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path,
                       '&'.join(field for field in fields if not credential(field)), ''))


def _output_name(task_id, url):
    task = re.sub(r'[^\w-]', '_', task_id)[:32] or 'task'
    task_hash = hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:10]
    url_hash = hashlib.sha256(_url_identity(url).encode('utf-8')).hexdigest()
    name = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .') or 'output'
    stem, suffix = os.path.splitext(name)
    name = stem[:80] + suffix[:16]
    return f'rh_{task}_{task_hash}_{url_hash[:20]}_{name}', url_hash


def _windows_path_units(value):
    """Win32 counts UTF-16 units, so a non-BMP character occupies two slots."""
    return len(str(value).encode('utf-16-le', errors='surrogatepass')) // 2


def _require_windows_path(path):
    if os.name == 'nt' and _windows_path_units(path) > _WINDOWS_PATH_LIMIT:
        raise OutputDownloadError('输出目录路径过长，请缩短输出目录后重试。')


def _destination_name(task_id, url, directory):
    """Keep existing names unless the app directory exhausts Windows' budget.

    Compact names retain both identity digests and the media extension; they
    stay stable across retries and recognised signed-URL renewal.
    """
    filename, url_hash = _output_name(task_id, url)
    if os.name != 'nt' or _windows_path_units(directory / filename) <= _WINDOWS_PATH_LIMIT:
        return filename, url_hash
    suffix = os.path.splitext(filename)[1]
    task_hash = hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:10]
    filename = f'rh_{task_hash}_{url_hash[:20]}{suffix}'
    _require_windows_path(directory / filename)
    return filename, url_hash


def _valid_cached(path, receipt, task_id, url_hash, metadata=None, cancelled=None):
    try:
        with open(receipt, 'r', encoding='utf-8') as source:
            saved = json.load(source)
        metadata = metadata or {}
        return (saved.get('task_id') == task_id and saved.get('url_sha256') == url_hash
                and path.is_file() and path.stat().st_size == saved.get('size', 0)
                and saved.get('size', 0) > 0
                and (metadata.get('size') is None or saved.get('size') == metadata['size'])
                and (metadata.get('sha256') is None or saved.get('sha256') == metadata['sha256'])
                and _digest_file(path, cancelled) == saved.get('sha256'))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _write_receipt(receipt, record):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=receipt.parent,
                                         suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(record, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, receipt)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _valid_decoded_cache(path, receipt, task_id, url_hash, metadata, token, cancelled):
    if not token:
        return None
    from aetherloom_core.rh_storage import valid_decoded_output
    try:
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        if (saved.get('task_id') != task_id or saved.get('url_sha256') != url_hash
                or saved.get('size', 0) <= 0
                or (metadata['size'] is not None and saved.get('size') != metadata['size'])
                or (metadata['sha256'] is not None and saved.get('sha256') != metadata['sha256'])):
            return None
        return valid_decoded_output(path, task_id, token, source_info=saved, cancelled=cancelled)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _metadata(record):
    """Optional local contract: integer byte count and hex SHA-256 of saved bytes.

    These optional fields are not assumed to be part of RunningHub's output
    schema. Strings with size units and unspecified checksum algorithms are not
    interpreted, and ETag is never treated as a checksum.
    """
    size, checksum = record.get('fileSize'), record.get('sha256')
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise _AttemptFailure('invalid-fileSize-metadata')
    if checksum is not None:
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
            raise _AttemptFailure('invalid-sha256-metadata')
        checksum = checksum.lower()
    return dict(size=size, sha256=checksum)


def _header_checksums(headers):
    """HTTP content bytes: Content-MD5, legacy Digest SHA-256, RFC 9530 SHA-256."""
    checksums = []

    def add(algorithm, value, header):
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise _AttemptFailure('invalid-checksum-header', retryable=True) from None
        if len(decoded) != (16 if algorithm == 'md5' else 32):
            raise _AttemptFailure('invalid-checksum-header', retryable=True)
        checksums.append((algorithm, decoded, header))

    if 'content-md5' in headers:
        add('md5', headers['content-md5'].strip(), 'content-md5')
    for header in ('digest', 'content-digest'):
        for field in headers.get(header, '').split(','):
            algorithm, separator, value = field.strip().partition('=')
            if algorithm.strip().lower() != 'sha-256':
                continue
            if not separator:
                raise _AttemptFailure('invalid-checksum-header', retryable=True)
            value = value.strip()
            if header == 'content-digest':
                match = re.fullmatch(r':([A-Za-z0-9+/=]+):(?:\s*;[^,]*)?', value)
                if match is None:
                    raise _AttemptFailure('invalid-checksum-header', retryable=True)
                value = match.group(1)
            elif value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            add('sha256', value, header)
    return checksums


class _WireReader:
    """Keep wire digests separate from decompressed file digests."""
    def __init__(self, response, cancelled, algorithms=()):
        self.response, self.cancelled = response, cancelled
        self.total = 0
        self.digests = {name: hashlib.md5(usedforsecurity=False) if name == 'md5' else hashlib.new(name)
                        for name in set(algorithms)}

    def record(self, chunk):
        _check_cancelled(self.cancelled)
        self.total += len(chunk)
        for digest in self.digests.values():
            digest.update(chunk)
        return chunk

    def read(self, size=-1):
        _check_cancelled(self.cancelled)
        try:
            chunk = self.response.raw.read(size, decode_content=False)
        except (requests.RequestException, UrllibHTTPError, OSError) as exc:
            raise _AttemptFailure(type(exc).__name__, retryable=True) from None
        return self.record(chunk)


def _decoded_chunks(response, wire, encoding):
    if encoding in ('gzip', 'x-gzip'):
        # gzip.GzipFile checks its CRC/footer and handles concatenated members;
        # read(CHUNK_SIZE) also bounds the decompressed allocation per iteration.
        with gzip.GzipFile(fileobj=wire, mode='rb') as source:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
    elif encoding == 'deflate':
        decoder = zlib.decompressobj()
        while True:
            pending = wire.read(_CHUNK_SIZE)
            if not pending:
                break
            while pending:
                _check_cancelled(wire.cancelled)
                chunk = decoder.decompress(pending, _CHUNK_SIZE)
                pending = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise _AttemptFailure('invalid-content-encoding', retryable=True)
                if chunk:
                    yield chunk
        if not decoder.eof:
            raise _AttemptFailure('incomplete-content-encoding', retryable=True)
    elif encoding not in ('', 'identity'):
        raise _AttemptFailure('unsupported-content-encoding')
    else:
        # iter_content works with requests and lightweight response adapters.
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                yield wire.record(chunk)
        except (requests.RequestException, UrllibHTTPError) as exc:
            raise _AttemptFailure(type(exc).__name__, retryable=True) from None


def _validate_payload(prefix, headers, record, url):
    declared = str(record.get('fileType') or '').lower()
    if declared in {'text', 'txt', 'json', 'html', 'htm', 'text/plain', 'application/json', 'text/html'}:
        return
    suffix = os.path.splitext(urlsplit(url).path)[1].lower()
    expected_media = (declared.startswith(('image/', 'video/', 'audio/'))
                      or declared in {'image', 'video', 'audio'}
                      or '.' + declared in _MEDIA_EXTENSIONS
                      or suffix in _MEDIA_EXTENSIONS)
    content_type = headers.get('content-type', '').split(';', 1)[0].strip().lower()
    start = prefix.lstrip(b'\xef\xbb\xbf \r\n\t').lower()
    html = (content_type in {'text/html', 'application/xhtml+xml'}
            or re.match(br'(?:<!doctype\s+html\b|<html\b|<head\b|<body\b)', start))
    if html and (expected_media or suffix not in {'.txt', '.json', '.html', '.htm', '.csv', '.md', '.xml', '.yaml', '.yml'}):
        raise _AttemptFailure('unexpected-non-media-response', retryable=True)
    if not expected_media:
        return
    json_error = (content_type in {'application/json', 'text/json', 'application/problem+json'}
                  or start.startswith((b'{', b'[')))
    xml_error = re.match(br'(?:<\?xml[^>]*>\s*)?<error(?:\s|>)', start)
    if html or json_error or xml_error:
        raise _AttemptFailure('unexpected-non-media-response', retryable=True)


def _download_once(url, record, metadata, destination, receipt, task_id, url_hash, timeout, cancelled):
    response, temporary = None, None
    try:
        _check_cancelled(cancelled)
        try:
            response = requests.get(url, timeout=timeout, stream=True, headers={'Accept-Encoding': 'identity'})
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            raise _AttemptFailure(type(exc).__name__, retryable=True) from None
        except requests.RequestException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            retryable = status in (408, 425, 429) or isinstance(status, int) and 500 <= status <= 599
            raise _AttemptFailure(type(exc).__name__, retryable=retryable, status=status) from None
        _check_cancelled(cancelled)
        status = response.status_code
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        if status != 200:
            raise _AttemptFailure('http-status', status=status,
                                  retryable=status in (408, 425, 429) or 500 <= status <= 599,
                                  retry_after=headers.get('retry-after'))
        encoding = headers.get('content-encoding', '').strip().lower()
        checksums = _header_checksums(headers)
        expected_length = headers.get('content-length')
        if expected_length is not None:
            if not re.fullmatch(r'\d+', expected_length.strip()):
                raise _AttemptFailure('invalid-content-length', retryable=True)
            expected_length = int(expected_length)
        total, prefix = 0, bytearray()
        digest = hashlib.sha256()
        wire = _WireReader(response, cancelled, (algorithm for algorithm, _expected, _header in checksums))
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.part', delete=False) as output:
            temporary = Path(output.name)
            try:
                for chunk in _decoded_chunks(response, wire, encoding):
                    _check_cancelled(cancelled)
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    if len(prefix) < 4096:
                        prefix.extend(chunk[:4096 - len(prefix)])
            except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
                raise _AttemptFailure(type(exc).__name__, retryable=True) from None
            _check_cancelled(cancelled)
            if not total:
                raise _AttemptFailure('empty-output', retryable=True)
            if expected_length is not None and wire.total != expected_length:
                raise _AttemptFailure('content-length-mismatch', retryable=True)
            if metadata['size'] is not None and metadata['size'] != total:
                raise _AttemptFailure('file-size-mismatch', retryable=True)
            if metadata['sha256'] is not None and metadata['sha256'] != digest.hexdigest():
                raise _AttemptFailure('sha256-mismatch', retryable=True)
            for algorithm, expected, header in checksums:
                if wire.digests[algorithm].digest() != expected:
                    raise _AttemptFailure(header + '-mismatch', retryable=True)
            _validate_payload(bytes(prefix), headers, record, url)
            output.flush()
            os.fsync(output.fileno())
        _check_cancelled(cancelled)
        os.replace(temporary, destination)
        _write_receipt(receipt, {'task_id': task_id, 'url_sha256': url_hash,
                                'size': total, 'sha256': digest.hexdigest()})
    except (OutputDownloadCancelled, _AttemptFailure):
        raise
    except OSError as exc:
        # Local read/write/fsync/replace failures cannot be repaired by another GET.
        raise _AttemptFailure(type(exc).__name__) from None
    except Exception as exc:
        raise _AttemptFailure(type(exc).__name__) from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _retry_delay(attempt, base, retry_after):
    backoff = min(_MAX_RETRY_DELAY, base * 2 ** min(attempt - 1, 10))
    if base:
        backoff += random.uniform(0, min(0.25, backoff * 0.25))
    server_delay = 0.0
    if retry_after:
        try:
            server_delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                server_delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                pass
    if not math.isfinite(server_delay):
        server_delay = _MAX_RETRY_DELAY if server_delay > 0 else 0
    return min(_MAX_RETRY_DELAY, max(0, server_delay, backoff))


def _wait_before_retry(delay, cancelled):
    deadline = time.monotonic() + delay
    while True:
        _check_cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def cleanup_output_receipts(task_id, files, output_dir):
    """Remove only this completed task's receipts, without scanning other tasks.

    Call after all outputs have been presented, including local decoding. Failed
    or interrupted tasks must retain their proofs for verified download reuse.
    Media files are never removed here. Keep the directories themselves because
    another task may already be preparing to write its receipt there.
    """
    from aetherloom_core.rh_storage import (
        DECODED_FOLDER, _receipt_path, _derivative_receipt_path, receipt_directory)
    directory = Path(output_dir).resolve()
    receipt_dirs = (receipt_directory(directory, 'downloads'), receipt_directory(directory, 'decoded'))
    if any(path.is_symlink() for path in receipt_dirs):
        return False
    complete = True
    for record in files or ():
        try:
            url = record.get('fileUrl') or record.get('url') or record.get('file')
            filename, _ = _destination_name(task_id, url, directory)
            source = directory / filename
            key = hashlib.sha256((task_id + '\0' + _url_identity(url)).encode('utf-8')).hexdigest()
            download_receipt = receipt_dirs[0] / (key + '.json')
            decode_receipt = _receipt_path(source)
            receipts = [download_receipt, decode_receipt]
            if decode_receipt.is_file():
                decoded_record = json.loads(decode_receipt.read_text(encoding='utf-8'))
                if decoded_record.get('task_id') != str(task_id):
                    complete = False
                    continue
                name = decoded_record.get('decoded_name')
                folder = decoded_record.get('decoded_folder', DECODED_FOLDER)
                if (folder == '' and isinstance(name, str) and name
                        and not any(char in name for char in '/\\:') and name not in ('.', '..')):
                    receipts.insert(0, _derivative_receipt_path(directory / name))
            for receipt in receipts:
                receipt.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, AttributeError):
            complete = False
    return complete


def download_outputs(task_id, files, output_dir, *, max_attempts=3, retry_delay=0.5,
                     timeout=(15, 60), cancelled=None, on_retry=None, decoded_token=None):
    """Download all outputs, collecting failures after trying later files.

    Optional metadata contract: fileSize is an integer number of decoded file
    bytes; sha256 is its 64-character hexadecimal digest. Missing fields impose
    no metadata constraint. These fields are not assumed to exist in RH data.
    HTTP Content-MD5, Digest sha-256 and Content-Digest sha-256 verify wire bytes,
    including gzip/deflate content coding. ETag is not a checksum.

    Retry events contain only filename, attempt, next_attempt, max_attempts,
    delay, status and a stable reason. Exceptions never include raw URLs or
    request exception text. Cancellation is checked during task-lock waits,
    backoff, cache hashing and download chunks; blocked socket I/O uses timeout.
    With an original run's decoded_token, a separately verified local derivative
    may be returned when its raw source was deleted after successful decoding.
    """
    task_id = str(task_id or '').strip()
    if not task_id:
        raise OutputDownloadError('Missing task ID')
    if not isinstance(files, list) or not files:
        raise OutputDownloadError('Task returned no valid output list')
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise OutputDownloadError('max_attempts must be a positive integer')
    if not isinstance(retry_delay, (int, float)) or not math.isfinite(retry_delay) or retry_delay < 0:
        raise OutputDownloadError('retry_delay must be a finite nonnegative number')
    durations = timeout if isinstance(timeout, (tuple, list)) else (timeout,)
    if len(durations) not in (1, 2) or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                                          or value <= 0 for value in durations):
        raise OutputDownloadError('timeout must contain positive finite seconds')
    if isinstance(timeout, list):
        timeout = tuple(timeout)
    _check_cancelled(cancelled)
    outputs, failures = {}, []
    for index, record in enumerate(files, 1):
        label = f'output-{index}'
        try:
            if not isinstance(record, dict):
                raise _AttemptFailure('invalid-output-record')
            url = record.get('fileUrl') or record.get('url') or record.get('file')
            if not isinstance(url, str):
                raise _AttemptFailure('missing-download-url')
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                raise _AttemptFailure('invalid-download-url')
            label, _ = _output_name(task_id, url)
            metadata = _metadata(record)
            identity = _url_identity(url)
            if identity in outputs:
                previous = outputs[identity]
                for key in ('size', 'sha256'):
                    if previous[2][key] is not None and metadata[key] is not None and previous[2][key] != metadata[key]:
                        raise _AttemptFailure('conflicting-output-metadata')
                    if metadata[key] is not None:
                        previous[2][key] = metadata[key]
                # Prefer the newest signed URL for a still-unsaved resource.
                previous[0] = url
            else:
                outputs[identity] = [url, dict(record), metadata]
        except (_AttemptFailure, ValueError) as exc:
            reason = exc.reason if isinstance(exc, _AttemptFailure) else 'invalid-download-url'
            failures.append(dict(filename=label, reason=reason, status=None, attempts=0))
    paths = []
    try:
        directory = Path(output_dir).resolve()
        # Receipts are required for verified reuse. Reject an unrepresentable
        # directory before any GET or partially committed output is possible.
        from aetherloom_core.rh_storage import receipt_directory
        receipts = receipt_directory(directory, 'downloads')
        _require_windows_path(receipts / ('0' * 64 + '.json'))
        _require_windows_path(directory / ('tmp' + '0' * 8 + '.part'))
        with _task_lock(task_id, str(directory), cancelled):
            directory.mkdir(parents=True, exist_ok=True)
            receipts.mkdir(parents=True, exist_ok=True)
            for identity, (url, record, metadata) in outputs.items():
                _check_cancelled(cancelled)
                filename, url_hash = _destination_name(task_id, url, directory)
                destination = directory / filename
                receipt = receipts / (hashlib.sha256((task_id + '\0' + identity).encode('utf-8')).hexdigest() + '.json')
                if _valid_cached(destination, receipt, task_id, url_hash, metadata, cancelled):
                    paths.append(str(destination))
                    continue
                decoded = _valid_decoded_cache(destination, receipt, task_id, url_hash, metadata, decoded_token, cancelled)
                if decoded:
                    paths.append(decoded)
                    continue
                for attempt in range(1, max_attempts + 1):
                    try:
                        _download_once(url, record, metadata, destination, receipt, task_id, url_hash, timeout, cancelled)
                        paths.append(str(destination))
                        break
                    except _AttemptFailure as exc:
                        _check_cancelled(cancelled)
                        if not exc.retryable or attempt == max_attempts:
                            failures.append(dict(filename=filename, reason=exc.reason, status=exc.status, attempts=attempt))
                            break
                        delay = _retry_delay(attempt, retry_delay, exc.retry_after)
                        if on_retry is not None:
                            try:
                                on_retry(dict(filename=filename, attempt=attempt, next_attempt=attempt + 1,
                                              max_attempts=max_attempts, delay=delay, status=exc.status, reason=exc.reason))
                            except Exception:
                                pass
                        _wait_before_retry(delay, cancelled)
    except OutputDownloadCancelled as exc:
        exc.completed_paths = paths
        raise
    except OSError as exc:
        raise OutputDownloadError('Could not prepare output directory: ' + type(exc).__name__,
                                  failures=failures, completed_paths=paths) from None
    if failures:
        details = '; '.join(f"{failure['filename']} [{failure['reason']}"
                            + (f", HTTP {failure['status']}" if failure['status'] is not None else '')
                            + f", attempts {failure['attempts']}]" for failure in failures[:10])
        raise OutputDownloadError(f'Could not save {len(failures)} output(s): {details}',
                                  failures=failures, completed_paths=paths) from None
    return paths
