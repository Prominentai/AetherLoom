"""Node-local text-operation presets, shared by inline and full LLM forms."""
import copy
import weakref

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from aetherloom_core.rh_parameters import RhEnumComboBox
from . import text_processing


def _replace_document(editor, text):
    """Replace one preset as a text undo step without emitting partial node edits."""
    document = editor.document()
    text = str(text)
    document._canvas_source_text = text
    if document.toPlainText() == text:return
    blockers = [QtCore.QSignalBlocker(view) for view in getattr(document, '_canvas_editors', ())
                if not sip.isdeleted(view)]
    try:
        cursor = QtGui.QTextCursor(document)
        cursor.beginEditBlock()
        cursor.select(QtGui.QTextCursor.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
    finally:
        blockers.clear()


class PresetControls(QtCore.QObject):
    def __init__(self, inspector):
        super().__init__(inspector)
        self.inspector = inspector
        self.prompt = self.system = self.section = None
        self._changing = False
        self._wrap_timer = QtCore.QTimer(self)
        self._wrap_timer.setSingleShot(True)
        self._wrap_timer.timeout.connect(self._sync_wrap_width)
        self._params = copy.deepcopy(inspector.node.get('params', {}))
        identity = ('text_operation_views', inspector.doc_id, inspector.node['id'])
        self._views = inspector.histories.setdefault(identity, weakref.WeakSet())
        self._views.add(self)
        row = QtWidgets.QHBoxLayout();row.setSpacing(5)
        label = QtWidgets.QLabel('处理');row.addWidget(label)
        self.mode = RhEnumComboBox()
        self.mode.setObjectName('canvasTextOperation')
        self.mode.setMinimumWidth(0);self.mode.setMinimumContentsLength(4)
        self.mode.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.mode.setAccessibleName('文本处理模式')
        for mode, title in text_processing.PRESETS:self.mode.addItem(title, mode)
        self.mode.setCurrentIndex(self.mode.findData(text_processing.operation(self._params)))
        self.mode.setToolTip('各模式保留本节点的系统提示词草稿；模型连接保持不变。')
        row.addWidget(self.mode, 1)
        self.reset = QtWidgets.QToolButton();self.reset.setObjectName('canvasResetTextPreset')
        self.reset.setText('恢复预设')
        self.reset.setToolTip('恢复当前模式初始系统提示词；保留输入文本和其他模式的草稿。')
        self.reset.setAccessibleName('恢复当前文本处理预设')
        self.reset.clicked.connect(lambda: self.apply(reset=True));row.addWidget(self.reset)
        inspector.form.addLayout(row)
        self.mode.currentIndexChanged.connect(lambda: self.apply(mode=self.mode.currentData()))
        inspector.text_processing_fields = {'mode': self.mode, 'reset': self.reset}

    def bind(self, prompt, system, section=None):
        self.prompt, self.system, self.section = prompt, system, section
        for editor in (prompt, system):
            editor.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
            editor.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
            editor.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            editor.installEventFilter(self)
            editor.viewport().installEventFilter(self)
        prompt.setPlaceholderText('输入待处理的文本…')
        prompt.setReadOnly(not prompt.isEnabled())
        self._refresh_caption()
        self._wrap_timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show, QtCore.QEvent.Hide, QtCore.QEvent.FocusIn):
            self._wrap_timer.start(0)
        return super().eventFilter(watched, event)

    def _sync_wrap_width(self):
        # One QTextDocument supplies undo for both views, so Qt's last resized
        # editor otherwise imposes its wider line layout on the narrow node.
        for editor in (self.prompt, self.system):
            if editor is None or sip.isdeleted(editor):continue
            document = editor.document()
            views = [view for view in getattr(document, '_canvas_editors', ())
                     if not sip.isdeleted(view) and view.isVisible() and view.viewport().width() > 1]
            if not views:continue
            width = min(view.viewport().width() for view in views)
            if abs(document.textWidth() - width) > 1:
                document.setTextWidth(width)
            for view in views:view.horizontalScrollBar().setValue(0)

    def _refresh_caption(self):
        if self.section is not None and self.system is not None:
            self.section.toggle.setText('系统提示词' + (' · 已填写' if self.system.toPlainText() else ' · 可选'))

    def _capture(self):
        # Editors share their live QTextDocument, including edits made in the
        # other view before its deferred inspector refresh has occurred.
        params = copy.deepcopy(self.inspector.node.get('params', self._params))
        if self.prompt is not None:params['prompt'] = self.prompt.toPlainText()
        if self.system is not None:params['system_prompt'] = self.system.toPlainText()
        return params

    def apply(self, mode=None, *, reset=False):
        if self._changing or self.system is None:return
        self._changing = True
        try:
            current = self._capture()
            # Only node creation inherits global settings. Existing nodes use
            # their own copied preset baselines and independent draft history.
            fresh = (text_processing.reset_preset(current) if reset else
                     text_processing.select_preset(current, mode))
            _replace_document(self.system, fresh.get('system_prompt', ''))
            self.inspector.changed.emit('params', fresh)
            for view in list(self._views):
                if sip.isdeleted(view):continue
                view._params = copy.deepcopy(fresh)
                # The page replaces document nodes during edits. The other
                # inspector can still hold the previous node until its deferred
                # refresh; keep its local preset/draft metadata current as well.
                view.inspector.node = dict(view.inspector.node, params=copy.deepcopy(fresh))
                with QtCore.QSignalBlocker(view.mode):
                    view.mode.setCurrentIndex(view.mode.findData(text_processing.operation(fresh)))
                view._refresh_caption()
        finally:
            self._changing = False


def selector(inspector):
    controls = PresetControls(inspector)
    inspector._text_processing_controls = controls
    return controls
