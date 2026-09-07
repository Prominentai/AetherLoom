"""Cheap directory snapshots shared by the local media grid."""

import os
import threading
import hashlib

from PyQt5 import QtCore


def scan_media(folder, extensions, option='name_asc', cancelled=None, include_directories=False):
    """Read one directory level; optionally include navigable folders first."""
    records = []
    folder = os.path.abspath(folder)
    with os.scandir(folder) as entries:
        for entry in entries:
            if cancelled is not None and cancelled():
                return None
            try:
                is_dir = include_directories and entry.is_dir()
                if is_dir and (entry.name.lower() in {'.rh_downloads', '.rh_decoded'}
                               or entry.name.lower().startswith('.decode-')):
                    continue
                if not is_dir and (not entry.name.lower().endswith(extensions) or not entry.is_file()):
                    continue
                stat = entry.stat()
            except OSError:
                continue
            records.append({
                'name': entry.name,
                'path': os.path.join(folder, entry.name),
                'size_bytes': 0 if is_dir else stat.st_size,
                'mtime': stat.st_mtime,
                'mtime_ns': stat.st_mtime_ns,
                'is_dir': bool(is_dir),
            })
    # Stable ties, irrespective of filesystem enumeration order.
    if cancelled is not None and cancelled():
        return None
    records.sort(key=lambda record: record['name'])
    field = (option or 'name_asc').rsplit('_', 1)[0]
    if field == 'mtime':
        key = lambda record: record['mtime_ns']
    elif field == 'size':
        key = lambda record: record['size_bytes']
    elif field == 'ext':
        key = lambda record: os.path.splitext(record['name'])[1].lower()
    else:
        key = lambda record: record['name'].lower()
    if cancelled is not None and cancelled():
        return None
    records.sort(key=key, reverse=(option or '').endswith('_desc'))
    if include_directories:
        records.sort(key=lambda record: not record['is_dir'])
    return records


def media_snapshot(folder, records, cancelled=None):
    """Constant-size revision fingerprint, including order and pairing state.

    Keeping a tuple per file retained another ~8 MB for every 100,000 entries.
    Feed unambiguous fields into a digest instead of keeping duplicate metadata.
    """
    digest = hashlib.sha256()
    for record in records:
        if cancelled is not None and cancelled():
            return None
        name = record['name'].encode('utf-8', errors='surrogatepass')
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(f"{record['size_bytes']}:{record['mtime_ns']}:{int(bool(record.get('paired')))}:{int(bool(record.get('is_dir')))};".encode('ascii'))
    return (os.path.normcase(os.path.abspath(folder)), len(records), digest.digest())


def scan_directories(request, cancelled):
    """Do enumeration, sorting and pairing without reading any GUI objects."""
    scanned, errors, snapshots = {}, {}, {}
    for kind, folder in request['folders'].items():
        if cancelled():
            return None
        try:
            scanned[kind] = scan_media(folder, request['extensions'],
                                       request['sorts'][kind], cancelled, request.get('include_directories', False))
        except OSError as exc:
            scanned[kind] = []
            errors[kind] = str(exc)
        if scanned[kind] is None:
            return None
    for kind, records in scanned.items():
        other = 'output' if kind == 'input' else 'input'
        # Only one counterpart index is needed at a time. Holding both doubled
        # the transient set/string allocations for large parallel directories.
        other_names = set()
        for record in scanned[other]:
            if cancelled():
                return None
            if not record.get('is_dir'):
                other_names.add(record['name'].lower())
        for record in records:
            if cancelled():
                return None
            record['paired'] = not record.get('is_dir') and record['name'].lower() in other_names
        del other_names
        snapshots[kind] = media_snapshot(request['folders'][kind], records, cancelled)
        if snapshots[kind] is None:
            return None
    return dict(request=request, records=scanned, snapshots=snapshots, errors=errors)


class LocalScanController(QtCore.QObject):
    """One cancellable worker with a single latest-request slot.

    No thread waits are needed when a window closes. A slow filesystem call may
    finish later, but its daemon worker cannot deliver into the closed window.
    """
    # Internal wake-up only: the payload is always None, never a scan result.
    # Qt otherwise retains every large result until the GUI processes its event.
    completed = QtCore.pyqtSignal(int, object)

    def __init__(self, callback, parent=None, scanner=None):
        super().__init__(parent)
        self._callback = callback
        self._scanner = scanner or scan_directories
        self._lock = threading.Lock()
        self._generation = 0
        self._pending = None
        self._result = None
        self._delivery_pending = False
        self._running = False
        self._closed = False
        self.completed.connect(self._deliver, QtCore.Qt.QueuedConnection)

    def submit(self, request):
        with self._lock:
            if self._closed:
                return self._generation
            self._generation += 1
            self._pending = (self._generation, request)
            self._result = None
            start_worker = not self._running
            self._running = True
            generation = self._generation
        if start_worker:
            threading.Thread(target=self._run, name='local-media-scan', daemon=True).start()
        return generation

    def _cancelled(self, generation):
        with self._lock:
            return self._closed or generation != self._generation

    def _run(self):
        while True:
            with self._lock:
                if self._closed or self._pending is None:
                    self._running = False
                    return
                generation, request = self._pending
                self._pending = None
            try:
                result = self._scanner(request, lambda: self._cancelled(generation))
            except Exception as exc:
                result = dict(request=request, records={'input': [], 'output': []},
                              snapshots={}, errors={'scan': str(exc)})
            notify = False
            with self._lock:
                if result is not None and not self._closed and generation == self._generation:
                    self._result = (generation, result)
                    if not self._delivery_pending:
                        self._delivery_pending = True
                        notify = True
            # Do not retain the previous result in this worker's local variables
            # while the next directory is scanned.
            result = None
            if notify:
                try:
                    self.completed.emit(generation, None)
                except RuntimeError:  # the QObject parent was already destroyed
                    self.close()
                    return

    @QtCore.pyqtSlot(int, object)
    def _deliver(self, _generation, _payload):
        with self._lock:
            packet = self._result
            self._result = None
            self._delivery_pending = False
            callback = self._callback
            if self._closed or packet is None or packet[0] != self._generation:
                return
        if callback is not None:
            callback(*packet)

    def close(self):
        with self._lock:
            self._closed = True
            self._pending = None
            self._result = None
            self._callback = None
