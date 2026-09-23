"""Small cancellable jobs; callbacks always return to the Qt thread."""
import threading
from PyQt5 import QtCore


class _PreviewMailbox:
    """Keep only the newest frame; queued Qt events never own image bytes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None
        self.scheduled = False
        self.closed = False

    def put(self, value):
        with self.lock:
            if self.closed:
                return False
            self.latest = value
            if self.scheduled:
                return False
            self.scheduled = True
            return True

    def take(self):
        with self.lock:
            value, self.latest = self.latest, None
            self.scheduled = False
            return value

    def close(self, *_):
        with self.lock:
            self.closed = True
            self.latest = None
            self.scheduled = False


class Job(QtCore.QObject):
    succeeded = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)
    progress = QtCore.pyqtSignal(object)
    preview = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal()
    _preview_ready = QtCore.pyqtSignal()

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.stop = threading.Event()
        self.operation = operation
        self.thread = None
        self._preview_mailbox = _PreviewMailbox()
        self._preview_ready.connect(self._deliver_preview, QtCore.Qt.QueuedConnection)
        # This callback does not access the deleted QObject, even when a worker
        # outlives the page/application that owned it.
        self.destroyed.connect(self._preview_mailbox.close, QtCore.Qt.DirectConnection)

    def start(self):
        self.thread = threading.Thread(target=self._run, name='NovelAI request', daemon=True)
        self.thread.start()
        return self

    def emit_safe(self, name, *args):
        if name == 'preview':
            if self.stop.is_set() or not self._preview_mailbox.put(args):
                return
            try:
                self._preview_ready.emit()
            except RuntimeError:
                self._preview_mailbox.close()
            return
        if name in ('succeeded', 'failed', 'finished'):
            self._preview_mailbox.close()
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:
            pass  # The application may already have closed its Qt objects.

    @QtCore.pyqtSlot()
    def _deliver_preview(self):
        value = self._preview_mailbox.take()
        if value is not None and not self.stop.is_set() and not self._preview_mailbox.closed:
            try:
                self.preview.emit(*value)
            except RuntimeError:
                self._preview_mailbox.close()

    def _run(self):
        try:
            value = self.operation(self)
            self.emit_safe('succeeded', value)
        except Exception as exc:
            self.emit_safe('failed', exc)
        finally:
            self.operation = None
            self.emit_safe('finished')

    def cancel(self):
        self.stop.set()
        self._preview_mailbox.close()
