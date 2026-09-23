"""NovelAI prompt editing without changing the application's other editors."""
import math

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.ui.widgets import CompletionTextEdit
from aetherloom_core.prompt_tokens import completion_token


class NovelAIPromptEdit(CompletionTextEdit):
    uses_local_completion_service = False
    prompt_weight_syntax = 'nai'
    activated = QtCore.pyqtSignal(object)
    prefixChanged = QtCore.pyqtSignal(object, str)
    tagSuggestionsRequested = QtCore.pyqtSignal(object)
    heightChanged = QtCore.pyqtSignal(int)

    def __init__(self, parent=None, *, minimum_height=96, maximum_height=280,
                 compact=False, auto_height=True):
        super().__init__(parent, limit=10)
        from .tag_popup import TagSuggestionsPopup
        self._popup.deleteLater()
        self._popup = TagSuggestionsPopup(self)
        self._popup.itemClicked.connect(self._on_item_clicked)
        self._popup.dismissed.connect(self.dismiss_tags)
        self._suggestion_state = 'idle'
        self._candidate_prefix = None
        self._candidate_context = None
        self._online_tags = []
        self._local_tags = []
        self._local_revision = 0
        self._local_state = 'ready'
        self._auto_height = auto_height
        self._minimum_prompt_height = minimum_height
        self._maximum_prompt_height = maximum_height
        self._height_pending = False
        self._expanded = False
        self._dismissed_prefix = None
        self._dismissed_context = None
        self._height_timer = QtCore.QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._update_height)
        self.setProperty('naiCompactPrompt', compact)
        self.setAcceptRichText(False)
        self.setMinimumWidth(0)
        self.setCursorWidth(2)
        self.document().setDocumentMargin(1 if compact else 3)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
                           if auto_height else QtWidgets.QSizePolicy.Expanding)
        if auto_height:
            self.setFixedHeight(minimum_height)
        self.document().documentLayout().documentSizeChanged.connect(self._schedule_height)

    def _completion_options(self):
        # Parentheses are literal tag characters in NovelAI. App-wide SD
        # completion preferences must not add backslashes or underscores here.
        return dict(escape_parentheses=False, replace_spaces=False, visible_tags=10)

    def _suggestion_options(self):
        from .preferences import normalize_suggestion_preferences
        provider = getattr(self, '_suggestion_preferences_provider', None)
        if provider is not None:
            return normalize_suggestion_preferences(provider())
        owner = self.parentWidget()
        while owner is not None:
            values = getattr(owner, 'prompt_suggestion_preferences', None)
            if isinstance(values, dict):
                return normalize_suggestion_preferences(values)
            owner = owner.parentWidget()
        return normalize_suggestion_preferences(None)

    def _refresh_candidates(self, prefix, *, reset=False):
        options = self._suggestion_options()
        self._popup.set_sources(options['online'], options['local'])
        context = self._query_context()
        if reset or prefix != self._candidate_prefix or context != self._candidate_context:
            self._candidate_prefix = prefix
            self._candidate_context = context
            self._online_tags = []
            self._suggestion_state = 'idle'
            self._local_tags = []
            self._local_revision += 1
            self._local_state = 'ready'
            if options['local'] and 1 <= len(prefix.strip()) <= 200 and self._manager:
                from .local_suggestions import request_local_tags
                self._local_state = 'loading'
                request_local_tags(self, prefix)
        if not options['online']:
            self._online_tags = []
        if not options['local']:
            self._local_tags = []

    def reset_tag_suggestions(self):
        self._dismissed_prefix = None
        self._dismissed_context = None
        self._candidate_prefix = None
        self._candidate_context = None
        self._online_tags = []
        self._local_tags = []
        self._local_revision += 1
        self._local_state = 'ready'
        self._suggestion_state = 'idle'
        self._hide_popup()

    def show_suggestions(self):
        """Explicit invocation also works with online suggestions disabled."""
        self._dismissed_prefix = None
        self._dismissed_context = None
        self.setFocus(QtCore.Qt.OtherFocusReason)
        self._refresh_candidates(self._get_prefix_before_cursor(), reset=True)
        self._render_suggestions()
        self._emit_prefix()

    def _render_suggestions(self):
        prefix = self._get_prefix_before_cursor()
        options = self._suggestion_options()
        self._popup.set_sources(options['online'], options['local'])
        if (not self.hasFocus() or self.isReadOnly() or not self.isEnabled()
                or not prefix.strip() or len(prefix.strip()) > 200 or prefix == self._dismissed_prefix
                or not self._popup.active_source):
            self._hide_popup()
            return
        self._show_popup()

    def _get_prefix_before_cursor(self):
        cursor = self.textCursor()
        if cursor.hasSelection():
            return ''
        cursor.movePosition(QtGui.QTextCursor.Start, QtGui.QTextCursor.KeepAnchor)
        token = completion_token(self.toPlainText(), len(cursor.selectedText()), syntax='nai')
        return token.query if token is not None else ''

    def _query_context(self):
        return self.textCursor().position(), self.document().revision()

    def _emit_prefix(self):
        if self.hasFocus() and not self.isReadOnly():
            self.prefixChanged.emit(self, self._get_prefix_before_cursor())

    def _on_text_changed(self):
        self._dismissed_prefix = None
        self._dismissed_context = None
        if self.hasFocus() and not self.isReadOnly():
            prefix = self._get_prefix_before_cursor()
            self._refresh_candidates(prefix, reset=True)
            self._render_suggestions()
        else:
            self._hide_popup()
        self._emit_prefix()
        if hasattr(self, '_auto_height'):
            self._schedule_height()

    def _on_cursor_position_changed(self):
        super()._on_cursor_position_changed()
        if hasattr(self, '_candidate_prefix') and self.hasFocus():
            if self._query_context() != self._dismissed_context:
                self._dismissed_prefix = None
                self._dismissed_context = None
            self._refresh_candidates(self._get_prefix_before_cursor())
            self._render_suggestions()
        self._emit_prefix()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.activated.emit(self)
        self._refresh_candidates(self._get_prefix_before_cursor())
        self._render_suggestions()
        self._emit_prefix()

    def show_online_tags(self, tags, prefix):
        if (self.hasFocus() and not self.isReadOnly() and self._suggestion_options()['online']
                and prefix and prefix == self._get_prefix_before_cursor()
                and prefix != self._dismissed_prefix):
            tags = [tag for tag in tags if isinstance(tag, str) and tag.strip()][:10]
            if tags:
                self._refresh_candidates(prefix)
                self._online_tags = [tag.strip().replace('_', ' ') for tag in tags]
                self._suggestion_state = 'online'
                self._render_suggestions()

    def show_local_tags(self, tags, prefix, revision):
        if (self.hasFocus() and self._suggestion_options()['local'] and revision == self._local_revision
                and prefix == self._get_prefix_before_cursor() and prefix != self._dismissed_prefix):
            self._local_tags = [tag.strip().replace('_', ' ') for tag in tags
                                if isinstance(tag, str) and tag.strip()][:10]
            self._local_state = 'ready' if self._local_tags else 'empty'
            self._render_suggestions()

    def dismiss_tags(self):
        self._dismissed_prefix = self._get_prefix_before_cursor()
        self._dismissed_context = self._query_context()
        self._hide_popup()

    def _cancel_suggestions(self):
        # Shared outside-click/Escape handling also covers a still-pending query
        # whose popup has not appeared yet.
        self.dismiss_tags()

    def set_tag_suggestion_state(self, state, prefix):
        if (not self.hasFocus() or self.isReadOnly() or not self.isEnabled()
                or prefix != self._get_prefix_before_cursor() or prefix == self._dismissed_prefix):
            return
        if not prefix.strip() or len(prefix.strip()) > 200:
            self._hide_popup()
            return
        self._refresh_candidates(prefix)
        self._suggestion_state = 'idle' if state == 'local' else state
        if state not in ('online', 'local'):
            self._online_tags = []
        self._render_suggestions()

    def keyPressEvent(self, event):
        if (self._popup.isVisible() and event.modifiers() == QtCore.Qt.AltModifier
                and event.key() in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right)):
            self._popup.switch_source(-1 if event.key() == QtCore.Qt.Key_Left else 1)
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_Escape:
            if self._popup.isVisible():
                self.dismiss_tags()
                event.accept()
                return
            self._dismissed_prefix = self._get_prefix_before_cursor()
            self._dismissed_context = self._query_context()
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        self._dismissed_prefix = self._get_prefix_before_cursor()
        self._dismissed_context = self._query_context()
        super().focusOutEvent(event)

    def _show_popup(self):
        if self._get_prefix_before_cursor() == self._dismissed_prefix:
            return
        self._popup.set_suggestions(self._local_tags, self._online_tags, self._get_prefix_before_cursor(),
                                   getattr(self.window(), '_theme_mode', 'dark'),
                                   online_state=self._suggestion_state, local_state=self._local_state)
        self._popup.ensurePolished()
        below = self.mapToGlobal(self.rect().bottomLeft()) + QtCore.QPoint(0, 2)
        above = self.mapToGlobal(self.rect().topLeft())
        screen = QtWidgets.QApplication.screenAt(below) or QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = min(max(self.width(), 520 if len(self._popup._sources) == 2 else 280), available.width())
        height = self._popup.layout_height(width)
        space_below = available.bottom() + 1 - below.y()
        space_above = above.y() - available.top()
        if space_below >= height or space_below >= space_above:
            height = min(height, max(1, space_below))
            y = below.y()
        else:
            height = min(height, max(1, space_above))
            y = above.y() - height
        x = max(available.left(), min(below.x(), available.right() + 1 - width))
        self._popup.setFixedSize(width, height)
        self._popup.move(x, max(available.top(), min(y, available.bottom() + 1 - height)))
        self._popup_cursor_position = self.textCursor().position()
        self._popup.show()

    def _insert_completion(self, completion):
        self.insert_tag(completion)

    def insert_tag(self, tag):
        if self.isReadOnly() or not self.isEnabled() or not isinstance(tag, str) or not tag.strip():
            return
        self._insert_prompt_tag(tag.strip().replace('_', ' '))

    def request_tags(self):
        self.activated.emit(self)
        self.tagSuggestionsRequested.emit(self)

    def header(self, title):
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QtWidgets.QLabel(title)
        label.setObjectName('novelaiPromptHeading')
        label.setBuddy(self)
        self.setAccessibleName(title)
        layout.addWidget(label, 1)
        for text, tip, slot in (('标签', '标签建议（在线 / 本地）', self.request_tags),
                                ('↗', '展开编辑提示词', self.open_expanded)):
            button = QtWidgets.QToolButton()
            button.setText(text)
            button.setToolTip(tip)
            button.setAccessibleName(tip + ' · ' + title)
            button.setObjectName('novelaiIconButton')
            button.setFixedSize(40 if text == '标签' else 24, 22)
            button.clicked.connect(slot)
            layout.addWidget(button)
        return row

    def createStandardContextMenu(self, *args):
        menu = super().createStandardContextMenu(*args)
        menu.addSeparator()
        action = menu.addAction('标签建议…', self.request_tags)
        action.setEnabled(self.isEnabled() and not self.isReadOnly())
        if not self._expanded:
            menu.addAction('展开编辑提示词…', self.open_expanded)
        return menu

    def _schedule_height(self, *_):
        if self._auto_height and not self._height_pending:
            self._height_pending = True
            self._height_timer.start(0)

    def _update_height(self):
        self._height_pending = False
        if not self._auto_height or self._expanded:
            return
        chrome = max(2, self.height() - self.viewport().height())
        target = max(self._minimum_prompt_height, min(self._maximum_prompt_height,
                     math.ceil(self.document().size().height()) + chrome))
        if target != self.height():
            self.setFixedHeight(target)
            self.heightChanged.emit(target)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_auto_height'):
            self._schedule_height()

    def open_expanded(self):
        if self._expanded:
            return
        self._hide_popup()
        dialog = QtWidgets.QDialog(self.window())
        dialog.setWindowTitle(self.accessibleName() or '编辑提示词')
        dialog.setObjectName('novelaiExpandedPrompt')
        dialog.resize(720, 500)
        dialog.setMinimumSize(420, 280)
        from .styles import workspace_stylesheet
        from .references import editor_stylesheet
        mode = getattr(self.window(), '_theme_mode', 'dark')
        dialog._theme_mode = mode
        dialog.setStyleSheet(workspace_stylesheet(mode, '#novelaiExpandedPrompt')
                            + editor_stylesheet(mode, 'novelaiExpandedPrompt'))
        layout = QtWidgets.QVBoxLayout(dialog)
        editor = NovelAIPromptEdit(dialog, auto_height=False)
        editor._suggestion_preferences_provider = self._suggestion_options
        editor._expanded = True
        editor.setDocument(self.document())
        editor.setReadOnly(self.isReadOnly())
        editor.setTextCursor(self.textCursor())
        editor.activated.connect(self.activated)
        editor.prefixChanged.connect(self.prefixChanged)
        editor.tagSuggestionsRequested.connect(self.tagSuggestionsRequested)
        layout.addWidget(editor, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._expanded = True
        editor.setFocus(QtCore.Qt.OtherFocusReason)
        try:
            dialog.exec_()
            self.setTextCursor(editor.textCursor())
        finally:
            self._expanded = False
            self.document().setTextWidth(self.viewport().width())
            self.activated.emit(self)
            self._schedule_height()
            dialog.deleteLater()


class PromptNavigation:
    """Compatibility navigation for the two always-visible main prompt panes."""
    def __init__(self, editors):
        self._editors = list(editors)
        self._current = 0
        for index, editor in enumerate(self._editors):
            editor.activated.connect(lambda _, index=index: self._select(index))

    def _select(self, index):
        self._current = index

    def count(self):
        return len(self._editors)

    def currentIndex(self):
        return self._current

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._editors):
            self._current = index
            self._editors[index].setFocus(QtCore.Qt.OtherFocusReason)

    def currentWidget(self):
        return self._editors[self._current]

    def widget(self, index):
        return self._editors[index] if 0 <= index < len(self._editors) else None
