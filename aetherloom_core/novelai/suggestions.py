"""Debounced official tag suggestions; one request and one pending prefix."""
import hashlib
import weakref
from collections import OrderedDict

from PyQt5 import QtCore, sip

from .credentials import tokens_for
from .jobs import Job


class TagSuggestions(QtCore.QObject):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self._job = None
        self._pending = None
        self._closed = False
        self._revision = 0
        self._cache = OrderedDict()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._dispatch)
        page.controls.tagPrefixChanged.connect(self.request)
        page.controls.model.currentIndexChanged.connect(self.invalidate)
        page.controls.dataset_mode.currentIndexChanged.connect(self.invalidate)

    def invalidate(self, *_):
        self._revision += 1
        self._pending = None
        self._timer.stop()
        self._cache.clear()
        editor = self.page.controls.active_prompt_editor()
        if editor is not None and not sip.isdeleted(editor):
            reset = getattr(editor, 'reset_tag_suggestions', None)
            if callable(reset):
                reset()
            editor._hide_popup()

    def _preferences(self):
        from .preferences import normalize_suggestion_preferences
        return normalize_suggestion_preferences(getattr(self.page, 'prompt_suggestion_preferences', None))

    def preferences_changed(self):
        self.invalidate()
        if self._job is not None:
            self._job.cancel()
        from .prompt_editor import NovelAIPromptEdit
        for editor in self.page.findChildren(NovelAIPromptEdit):
            editor.reset_tag_suggestions()
        editor = self.page.controls.active_prompt_editor()
        if editor is not None and not sip.isdeleted(editor) and editor.hasFocus():
            editor.show_suggestions()

    def request(self, editor, prefix):
        self._pending = None
        self._timer.stop()
        if (self._closed or sip.isdeleted(editor) or not editor.hasFocus()
                or not editor.isVisible() or editor.isReadOnly() or not editor.isEnabled()
                or editor._ime_composing or editor._completion_editing
                or prefix == editor._dismissed_prefix):
            return
        if not self._preferences()['online']:
            return
        keys = tokens_for(self.page.owner)
        token = keys[0] if keys else None
        model = self.page.controls.model.currentData()
        mode = self.page.controls.dataset_mode.currentData()
        key = (hashlib.sha256(token.encode()).hexdigest() if token else None, model, mode, prefix, self._revision)
        request = (weakref.ref(editor), editor._completion_context(), key, token)
        if not keys:
            self._set_state(request, 'missing_key')
            return  # Local completion remains available without a connection.
        if not 2 <= len(prefix.strip()) <= 200:
            self._set_state(request, 'local')
            return
        self._pending = request
        if key in self._cache:
            tags = self._cache.pop(key)
            self._cache[key] = tags
            self._deliver(self._pending, tags)
            self._pending = None
        else:
            self._set_state(request, 'local')
            self._timer.start()

    def _valid_editor(self, request):
        editor_ref, context, key, _ = request
        editor = editor_ref()
        if (self._closed or not self._preferences()['online'] or key[4] != self._revision
                or editor is None or sip.isdeleted(editor) or not editor.hasFocus()
                or not editor.isVisible() or editor.isReadOnly() or not editor.isEnabled()
                or editor._ime_composing or editor._completion_editing
                or editor._dismissed_prefix == key[3]
                or editor._completion_context() != context
                or editor._get_prefix_before_cursor() != key[3]
                or self.page.controls.model.currentData() != key[1]
                or self.page.controls.dataset_mode.currentData() != key[2]):
            return None
        keys = tokens_for(self.page.owner)
        credential = hashlib.sha256(keys[0].encode()).hexdigest() if keys else None
        if credential != key[0]:
            return None
        return editor

    def _set_state(self, request, state):
        editor = self._valid_editor(request)
        if editor is not None:
            setter = getattr(editor, 'set_tag_suggestion_state', None)
            if callable(setter):
                setter(state, request[2][3])

    def _dispatch(self):
        if self._closed or self._job is not None or self._pending is None:
            return
        request, self._pending = self._pending, None
        if self._valid_editor(request) is None:
            return
        _, _, key, token = request
        from .client import suggest_tags
        # All request values are captured on the GUI thread; workers read no widgets.
        job = self._job = Job(lambda unused: suggest_tags(
            token, key[1], key[3], timeout=8, dataset_mode=key[2]), self)
        job.succeeded.connect(lambda tags: self._loaded(request, tags))
        job.failed.connect(lambda _error: self._set_state(request, 'unavailable'))
        job.finished.connect(self._finished)
        self._set_state(request, 'loading')
        job.start()

    def _loaded(self, request, values):
        if self._closed or request[2][4] != self._revision:
            return
        tags = []
        for item in values[:100] if isinstance(values, list) else []:
            text = item.get('tag', '') if isinstance(item, dict) else item
            if isinstance(text, str) and text.strip() and len(text) <= 300 and text not in tags:
                tags.append(text)
            if len(tags) == 10:
                break
        if not tags:
            self._set_state(request, 'empty')
            return
        key = request[2]
        self._cache.pop(key, None)
        self._cache[key] = tags
        while len(self._cache) > 128:
            self._cache.popitem(last=False)
        self._deliver(request, tags)

    def _deliver(self, request, tags):
        editor = self._valid_editor(request)
        if editor is not None:
            editor.show_online_tags(tags, request[2][3])
            self._set_state(request, 'online')

    def _finished(self):
        job, self._job = self._job, None
        if job is not None:
            job.deleteLater()
        if self._pending is not None and not self._closed:
            self._timer.start()

    def close(self):
        self._closed = True
        self._timer.stop()
        self._pending = None
        self._cache.clear()
        if self._job is not None:
            self._job.cancel()
