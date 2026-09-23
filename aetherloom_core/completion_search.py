"""Bounded background lookup for the application's local prompt editors."""
import threading
import weakref

from PyQt5 import QtCore, QtWidgets, sip

from .tag_search import search, vocabulary_version


class _SearchJob(QtCore.QObject):
    completed = QtCore.pyqtSignal(object)

    def __init__(self, manager, query, limit, parent):
        super().__init__(parent)
        self.manager, self.query, self.limit = manager, query, limit

    def start(self):
        threading.Thread(target=self._run, name='Prompt tag lookup', daemon=True).start()

    def _run(self):
        try:
            matches = search(self.manager, self.query, self.limit)
        except Exception:
            matches = []
        finally:
            self.manager = None
        try:
            self.completed.emit(matches)
        except RuntimeError:
            pass  # The application can close while the index is being built.


def _key(editor, query, limit):
    return (id(editor), editor._completion_revision, editor.textCursor().position(),
            editor.document().revision(), int(sip.unwrapinstance(editor.document())), query, id(editor._manager),
            limit, tuple(sorted(editor._completion_options().items())), vocabulary_version(editor._manager))


class CompletionSearch(QtCore.QObject):
    """One worker and one latest pending query; no per-keystroke thread growth."""
    def __init__(self, parent):
        super().__init__(parent)
        self._job = None
        self._active = self._pending = self._latest = None
        self._closed = False
        parent.aboutToQuit.connect(self.close)

    def request(self, editor, query, limit):
        if self._closed:
            return
        request = (weakref.ref(editor), _key(editor, query, limit), editor._manager)
        self._latest = request[1]
        self._pending = request
        self._dispatch()

    def _editor(self, request):
        if sip.isdeleted(self) or self._closed or request is None:
            return None
        reference, key, manager = request
        editor = reference()
        if (key != self._latest or editor is None or sip.isdeleted(editor)
                or not editor.hasFocus() or not editor.isVisible() or editor.isReadOnly()
                or not editor.isEnabled() or editor._manager is not manager
                or editor._ime_composing or editor._completion_editing
                or editor._get_prefix_before_cursor() != key[5]
                or _key(editor, key[5], key[7]) != key):
            return None
        return editor

    def _dispatch(self):
        if self._closed or self._job is not None or self._pending is None:
            return
        request, self._pending = self._pending, None
        if self._editor(request) is None:
            return
        self._active = request
        _, key, manager = request
        self._job = _SearchJob(manager, key[5], key[7], self)
        self._job.completed.connect(self._finished, QtCore.Qt.QueuedConnection)
        self._job.start()

    @QtCore.pyqtSlot(object)
    def _finished(self, matches):
        job, request = self._job, self._active
        self._job = self._active = None
        editor = self._editor(request)
        if editor is not None:
            editor._show_popup(matches)
        if job is not None and not sip.isdeleted(job):
            job.deleteLater()
        self._dispatch()

    def close(self):
        self._closed = True
        self._active = self._pending = self._latest = None


def request_matches(editor, query, limit):
    app = QtWidgets.QApplication.instance()
    service = getattr(app, '_prompt_completion_search', None)
    if service is None or sip.isdeleted(service):
        service = app._prompt_completion_search = CompletionSearch(app)
    service.request(editor, query, min(100, max(1, limit)))
