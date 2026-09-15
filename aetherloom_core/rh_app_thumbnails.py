"""Bounded background covers for RH application cards.

Call ``request`` on the GUI thread. Workers only handle files and captured signal
emitters; the queued dashboard slot resolves the guarded weak reference on the
GUI thread. Source identity lives inside the PNG, so replacing a cover and its
metadata is one atomic operation and signed URLs never enter the cache metadata.
"""
import hashlib
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import weakref

from PIL import Image, PngImagePlugin
from PyQt5 import QtCore, sip

from .media_limits import load_media_frame, VIDEO_EXTENSIONS


THUMBNAIL_SIZE = (640, 420)
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_CACHE_BYTES = 2 * 1024 * 1024
MAX_WORKERS = 3
_SOURCE_KEY = 'aetherloom_source_sha256'
_VERSION_KEY = 'aetherloom_thumbnail_version'
_VERSION = '1'


def _source_identity(url):
    parts = urlsplit(str(url).strip())
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise ValueError('Thumbnail URL must use HTTP or HTTPS')
    if parts.username or parts.password:
        raise ValueError('Thumbnail URL must not contain credentials')
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    port = parts.port
    if port and (scheme, port) not in (('http', 80), ('https', 443)):
        host += ':' + str(port)
    normalized = urlunsplit((scheme, host, parts.path or '/', parts.query, ''))
    return normalized, hashlib.sha256(normalized.encode('utf-8')).hexdigest()


class _ButtonReference(weakref.ref):
    """Resolved by Dashboard.apply_thumbnail, never by a worker."""

    def __new__(cls, button, token):
        return super().__new__(cls, button)

    def __init__(self, button, token):
        self.token = token

    def __call__(self):
        button = super().__call__()
        if (button is None or sip.isdeleted(button)
                or getattr(button, '_rh_thumbnail_token', None) is not self.token):
            return None
        return button


def _valid_cache(path, source):
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return False
        with Image.open(path) as image:
            if (image.format != 'PNG' or image.width > THUMBNAIL_SIZE[0]
                    or image.height > THUMBNAIL_SIZE[1]
                    or image.info.get(_SOURCE_KEY) != source
                    or image.info.get(_VERSION_KEY) != _VERSION):
                return False
            image.load()  # Existence and a readable header do not prove validity.
            return image.width > 0 and image.height > 0
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _has_source_metadata(path):
    try:
        with Image.open(path) as image:
            return _SOURCE_KEY in image.info
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _temporary(directory, suffix):
    fd, path = tempfile.mkstemp(prefix='.rh-cover-', suffix=suffix, dir=str(directory))
    os.close(fd)
    return Path(path)


def _remove_temporary(path):
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _prepare_png(source_path, destination, source):
    temporary = _temporary(destination.parent, '.png')
    try:
        frame, _, _ = load_media_frame(str(source_path), THUMBNAIL_SIZE)
        try:
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text(_SOURCE_KEY, source)
            metadata.add_text(_VERSION_KEY, _VERSION)
            with temporary.open('wb') as stream:
                frame.save(stream, format='PNG', pnginfo=metadata)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            frame.close()
        if not _valid_cache(temporary, source):
            raise ValueError('Invalid thumbnail')
        return temporary
    except Exception:
        _remove_temporary(temporary)
        raise


def _download(url, destination):
    import requests

    temporary = None
    started = time.monotonic()
    try:
        with requests.get(url, stream=True, timeout=(5, 12)) as response:
            response.raise_for_status()
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise ValueError('Thumbnail download is too large')
            suffix = Path(urlsplit(url).path).suffix.lower()
            content_type = str(response.headers.get('Content-Type') or '').lower()
            if content_type.startswith('video/') and suffix not in VIDEO_EXTENSIONS:
                suffix = '.mp4'
            elif 'gif' in content_type:
                suffix = '.gif'
            elif suffix not in VIDEO_EXTENSIONS | {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif'}:
                suffix = '.image'
            temporary = _temporary(destination.parent, suffix)
            count = 0
            with temporary.open('wb') as stream:
                for chunk in response.iter_content(64 * 1024):
                    count += len(chunk)
                    if count > MAX_DOWNLOAD_BYTES or time.monotonic() - started > 30:
                        raise ValueError('Thumbnail download exceeded its limit')
                    stream.write(chunk)
            if count == 0:
                raise ValueError('Thumbnail download is empty')
        return temporary
    except Exception:
        _remove_temporary(temporary)
        raise


class _Job:
    def __init__(self, destination, url, source, force, listener):
        self.destination, self.url, self.source = destination, url, source
        self.force = force
        self.listeners = [listener]


class _ThumbnailPool:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.queue = queue.Queue()
        self.workers = []

    def submit(self, destination, url, source, force, listener):
        key = str(destination)
        with self.lock:
            current = self.pending.get(key)
            if current is not None and current.source == source and (not force or current.force):
                current.listeners.append(listener)
                return
            job = _Job(destination, url, source, force, listener)
            self.pending[key] = job
            # Queue each app once; replacement requests update the pending job.
            if current is None:
                self.queue.put(key)
            while len(self.workers) < MAX_WORKERS:
                worker = threading.Thread(target=self._worker, name='rh-app-cover', daemon=True)
                self.workers.append(worker)
                worker.start()

    def _current(self, job):
        return self.pending.get(str(job.destination)) is job

    def _commit(self, job, temporary):
        with self.lock:
            if not self._current(job):
                return False
            os.replace(temporary, job.destination)
            return True

    def _load(self, job):
        destination = job.destination
        if not job.force and _valid_cache(destination, job.source):
            return str(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = downloaded = None
        try:
            # Upgrade old still-image caches and GIF/video-derived PNGs once.
            # A cache stamped with another source must never be reused for a URL change.
            if not job.force and not _has_source_metadata(destination):
                suffix = Path(urlsplit(job.url).path).suffix.lower()
                legacy = [destination]
                if suffix in VIDEO_EXTENSIONS | {'.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.avif'}:
                    legacy.append(destination.with_suffix(suffix))
                for candidate in legacy:
                    if candidate.is_file():
                        try:
                            temporary = _prepare_png(candidate, destination, job.source)
                            break
                        except Exception:
                            pass
            if temporary is None:
                downloaded = _download(job.url, destination)
                temporary = _prepare_png(downloaded, destination, job.source)
            return str(destination) if self._commit(job, temporary) else ''
        finally:
            _remove_temporary(temporary)
            _remove_temporary(downloaded)

    def _worker(self):
        while True:
            key = self.queue.get()
            with self.lock:
                job = self.pending.get(key)
            try:
                result = self._load(job) if job is not None else ''
            except Exception:
                result = ''  # A failed refresh leaves the last good PNG untouched.
            with self.lock:
                if job is not None and self._current(job):
                    self.pending.pop(key, None)
                    listeners = job.listeners if result else []
                else:
                    listeners = []
                    if key in self.pending:
                        self.queue.put(key)
            self.queue.task_done()
            for emit, reference in listeners:
                try:
                    emit(reference, result)
                except RuntimeError:
                    pass  # Dashboard was disposed while its download finished.


_pool = _ThumbnailPool()


def request(dashboard, button, url, wid, root, force=False):
    """Load or repair ``root/RH_apps/wid/wid_thumb.png`` without blocking the UI.

    Failed requests may be retried by calling this again. ``force`` downloads a
    fresh cover while retaining a valid existing file until conversion succeeds.
    Empty URLs invalidate pending results for this card.
    """
    if QtCore.QThread.currentThread() != button.thread():
        raise RuntimeError('Thumbnail requests must originate on the GUI thread')
    token = object()
    button._rh_thumbnail_token = token
    if not url:
        return
    try:
        url, source = _source_identity(url)
        wid = str(wid)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', wid):
            return
        destination = Path(root).resolve() / 'RH_apps' / wid / (wid + '_thumb.png')
        listener = (dashboard.thumbnail_ready.emit, _ButtonReference(button, token))
        _pool.submit(destination, url, source, bool(force), listener)
    except (OSError, TypeError, ValueError, RuntimeError):
        return
