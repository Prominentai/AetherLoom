"""One background local search and one latest pending query per application."""
import weakref

from PyQt5 import QtCore, QtWidgets, sip

from .jobs import Job
from aetherloom_core.tag_search import vocabulary_version


class LocalSuggestions(QtCore.QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self._job = None
        self._active_key = None
        self._latest_key = None
        self._pending = None
        self._closed = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._dispatch)
        parent.aboutToQuit.connect(self.close)

    def request(self, editor, prefix):
        if (self._closed or not 1 <= len(prefix.strip()) <= 200 or editor._manager is None
                or not editor.hasFocus() or not editor.isVisible() or editor.isReadOnly()
                or not editor.isEnabled() or editor._ime_composing or editor._completion_editing
                or editor._dismissed_prefix == prefix):
            return
        key = (id(editor), editor.textCursor().position(), prefix, editor._local_revision,
               id(editor._manager), vocabulary_version(editor._manager))
        if key == self._latest_key and (key == self._active_key or self._pending is not None):
            return
        self._latest_key = key
        self._pending = (weakref.ref(editor), key, editor._manager)
        self._timer.start(90)

    def _valid_editor(self, request):
        editor_ref, key, manager = request
        editor = editor_ref()
        if (sip.isdeleted(self) or self._closed or key != self._latest_key or editor is None or sip.isdeleted(editor)
                or not editor.hasFocus() or editor.isReadOnly() or not editor.isEnabled()
                or not editor.isVisible() or editor._ime_composing or editor._completion_editing
                or editor.textCursor().position() != key[1]
                or editor._get_prefix_before_cursor() != key[2]
                or editor._local_revision != key[3] or editor._manager is not manager
                or vocabulary_version(manager) != key[5]
                or not editor._suggestion_options()['local']
                or editor._dismissed_prefix == key[2]):
            return None
        return editor

    def _dispatch(self):
        if sip.isdeleted(self) or self._closed or self._job is not None or self._pending is None:
            return
        request, self._pending = self._pending, None
        if self._valid_editor(request) is None:
            return
        _, key, manager = request
        from .local_tags import search
        # Only plain Python vocabulary data and a captured query reach the worker.
        job = self._job = Job(lambda unused: search(manager, key[2], limit=10), self)
        self._active_key = key
        job.succeeded.connect(lambda tags: self._loaded(request, tags))
        job.failed.connect(lambda _error: self._failed(request))
        job.finished.connect(lambda: self._finished(job))
        job.start()

    def _loaded(self, request, tags):
        editor = self._valid_editor(request)
        if editor is not None:
            editor.show_local_tags(tags, request[1][2], request[1][3])

    def _failed(self, request):
        editor = self._valid_editor(request)
        if editor is not None:
            editor._local_state = 'unavailable'
            editor._render_suggestions()

    def _finished(self, job):
        if sip.isdeleted(self) or job is not self._job:
            return
        self._job, self._active_key = None, None
        if not sip.isdeleted(job):
            job.deleteLater()
        if self._pending is not None and not self._closed:
            self._timer.start(0)

    def close(self):
        self._closed = True
        self._timer.stop()
        self._pending = None
        if self._job is not None:
            self._job.cancel()


def request_local_tags(editor, prefix):
    app = QtWidgets.QApplication.instance()
    service = getattr(app, '_novelai_local_suggestions', None)
    if service is None or sip.isdeleted(service):
        service = app._novelai_local_suggestions = LocalSuggestions(app)
    service.request(editor, prefix)
