"""Asynchronous, selection-only text tools shared by App and canvas editors."""
import copy
import functools
import uuid

from PyQt5 import QtCore, QtGui, QtWidgets, sip


LABELS = {'zh': '翻译成中文', 'en': '翻译成英文', 'polish': '润色'}
_BUSY = 'aetherloomSelectionTextTask'


def owner_for(editor):
    widget = editor
    while widget is not None:
        candidates = (getattr(widget, 'model_owner', None), getattr(widget, '_canvas_owner', None),
                      getattr(getattr(widget, 'page', None), 'owner', None), widget)
        for owner in candidates:
            if owner is not None and isinstance(getattr(owner, 'api_settings', None), dict):
                return owner
        widget = widget.parentWidget()
    return None


def prepare(owner, text, mode):
    """Capture settings/credentials on the GUI thread; workers use only values."""
    settings = copy.deepcopy(getattr(owner, 'api_settings', {}) or {})
    keys = copy.deepcopy(getattr(owner, '_apikeys', {}) or {})
    if mode in ('zh', 'en'):
        from .translation import snapshot, translate
        return functools.partial(translate, snapshot(settings, keys), text, mode)
    from .api_credentials import get_credentials
    from .resources import DEFAULT_POLISH_SYSTEM_PROMPT
    from api_calls.call_llm import call_llm
    config = settings.get('llm') or {}
    config.update(get_credentials(keys, config.get('provider'), 'llm'))
    if not config.get('endpoint') or not config.get('model'):
        raise ValueError('请先在 API 管理中配置大语言模型，再使用润色。')
    prompt = (getattr(owner, 'settings', {}) or {}).get('polish_system_prompt')
    if not isinstance(prompt, str) or not prompt.strip():prompt = DEFAULT_POLISH_SYSTEM_PROMPT
    return functools.partial(call_llm, config['endpoint'], config.get('api_key', ''), config['model'], prompt, text,
                             temperature=.4, timeout=int(config.get('timeout') or 90),
                             provider=config.get('protocol') or config.get('provider'), web_search=config.get('web_search'))


def notice(editor, title, message, result=None):
    box = QtWidgets.QMessageBox(owner_for(editor) or editor.window())
    box.setAttribute(QtCore.Qt.WA_DeleteOnClose)
    box.setWindowTitle(title);box.setTextFormat(QtCore.Qt.PlainText)
    box.setText(message);box.setIcon(QtWidgets.QMessageBox.Information)
    box.setStandardButtons(QtWidgets.QMessageBox.Ok)
    if result:
        box.setDetailedText(result)
        button = box.addButton('复制结果', QtWidgets.QMessageBox.ActionRole)
        button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(result))
    box.open()


class _Signals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object, str)


class _Job(QtCore.QRunnable):
    def __init__(self, callback):
        super().__init__()
        self.callback, self.signals = callback, _Signals()

    def run(self):
        result, error = None, ''
        try:
            result = self.callback()
            if not isinstance(result, str) or not result.strip():
                error = '服务未返回有效文本，原文已保留。'
        except Exception as exc:
            from api_calls.provider_client import ProviderAPIError
            error = str(exc) if isinstance(exc, ProviderAPIError) else '请求失败，请检查模型配置和网络后重试。原文已保留。'
        finally:
            self.callback = None
        try:self.signals.finished.emit(result, error)
        except RuntimeError:pass


def _release(document, token, *unused):
    if not sip.isdeleted(document) and document.property(_BUSY) == token:
        document.setProperty(_BUSY, None)


class SelectionTask(QtCore.QObject):
    def __init__(self, editor, cursor, mode, callback):
        super().__init__(editor)
        self.editor, self.document, self.mode = editor, editor.document(), mode
        self.start, self.end = cursor.selectionStart(), cursor.selectionEnd()
        self.original = cursor.selection().toPlainText()
        self.conflicted = False
        self.token = uuid.uuid4().hex
        self.document.setProperty(_BUSY, self.token)
        self.document.contentsChange.connect(self._changed)
        self.destroyed.connect(functools.partial(_release, self.document, self.token))
        self.job = _Job(callback)
        self.job.signals.finished.connect(self._finished, QtCore.Qt.QueuedConnection)
        history = getattr(editor, '_prompt_history', None)
        if history:history.record(source='before_selection_' + mode)

    @QtCore.pyqtSlot(int, int, int)
    def _changed(self, position, removed, added):
        if self.conflicted:return
        if (removed and position < self.end and position + removed > self.start
                or added and self.start < position < self.end):
            self.conflicted = True
        elif position <= self.start:
            self.start += added - removed;self.end += added - removed

    @QtCore.pyqtSlot(object, str)
    def _finished(self, result, error):
        try:
            if not sip.isdeleted(self.document):
                self.document.contentsChange.disconnect(self._changed)
            _release(self.document, self.token)
            editor = self.editor
            if error:
                notice(editor, LABELS[self.mode] + '失败', error)
                return
            if (self.conflicted or sip.isdeleted(self.document) or editor.document() is not self.document
                    or editor.isReadOnly() or not editor.isEnabled()):
                notice(editor, '文本已变化', '原选区或编辑状态已改变，结果未自动替换。可以复制结果后手动使用。', result)
                return
            cursor = QtGui.QTextCursor(self.document)
            cursor.setPosition(self.start);cursor.setPosition(self.end, QtGui.QTextCursor.KeepAnchor)
            if cursor.selection().toPlainText() != self.original:
                notice(editor, '文本已变化', '原选区已改变，结果未自动替换。可以复制结果后手动使用。', result)
                return
            if result == self.original:return
            history = getattr(editor, '_prompt_history', None)
            if history:history.record()
            cursor.beginEditBlock();cursor.insertText(result);cursor.endEditBlock()
            editor.setTextCursor(cursor)
            if history:history.record(source='selection_' + self.mode)
            editor._hide_popup()
        finally:
            self.job = None
            self.editor._selection_text_task = None
            self.deleteLater()


def start(editor, mode, cursor=None):
    if sip.isdeleted(editor) or mode not in LABELS or editor.isReadOnly() or not editor.isEnabled():return
    cursor = QtGui.QTextCursor(cursor or editor.textCursor())
    if cursor.document() is not editor.document() or not cursor.hasSelection():return
    text = cursor.selection().toPlainText()
    if not text.strip() or editor.document().property(_BUSY):return
    owner = owner_for(editor)
    if owner is None:return
    try:callback = prepare(owner, text, mode)
    except (ValueError, TypeError):
        notice(editor, LABELS[mode], '请检查 API 管理中的大语言模型或翻译配置。润色需要配置大语言模型。')
        return
    task = editor._selection_text_task = SelectionTask(editor, cursor, mode, callback)
    try:QtCore.QThreadPool.globalInstance().start(task.job)
    except RuntimeError:task._finished(None, '暂时无法启动文本处理，请稍后重试。')


def add_actions(editor, menu):
    owner = owner_for(editor)
    if owner is None:return
    # Keep popups outside graphics proxies; the app menu theme also applies.
    proxy = menu.graphicsProxyWidget()
    if proxy is not None:
        proxy.setWidget(None)
        if proxy.scene() is not None:proxy.scene().removeItem(proxy)
        proxy.deleteLater()
    menu.setParent(owner, menu.windowFlags())
    editor.destroyed.connect(menu.close)
    editor.destroyed.connect(menu.deleteLater)
    cursor = QtGui.QTextCursor(editor.textCursor())
    enabled = (editor.isEnabled() and not editor.isReadOnly() and bool(cursor.selection().toPlainText().strip())
               and not editor.document().property(_BUSY))
    menu.addSeparator()
    for mode, label in LABELS.items():
        action = menu.addAction(label)
        action.setObjectName('selectionText_' + mode)
        action.setEnabled(bool(enabled))
        action.setToolTip('仅替换选中的文字；结果可撤销' if enabled else '请先选中文字；处理期间不能重复提交')
        action.triggered.connect(lambda checked=False, mode=mode: start(editor, mode, cursor))
