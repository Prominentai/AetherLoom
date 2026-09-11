"""Compact canvas form presentation; expansion state is UI-only and session-local."""
from PyQt5 import QtCore, QtGui, QtWidgets


class FieldLabel(QtWidgets.QLabel):
    """Single-line parameter caption; full text remains in text()/tooltip."""
    def sizeHint(self):
        return QtCore.QSize(min(200, self.fontMetrics().horizontalAdvance(self.text())), 22)

    def minimumSizeHint(self):
        return QtCore.QSize(0, 22)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(QtGui.QPalette.WindowText))
        painter.drawText(self.contentsRect(), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                         self.fontMetrics().elidedText(self.text(), QtCore.Qt.ElideRight, self.contentsRect().width()))


class TextToolsState(QtCore.QObject):
    def __init__(self, editor, tools):
        super().__init__(editor);self.tools = tools
        editor.installEventFilter(self)

    def eventFilter(self, editor, event):
        if event.type() == QtCore.QEvent.EnabledChange:self.tools.setEnabled(editor.isEnabled())
        return False


class NumberDrag(QtCore.QObject):
    """Scrub a number from its caption, committing one canvas edit on release."""
    def __init__(self, label, editor):
        super().__init__(label)
        self.label, self.editor = label, editor
        self.origin = None
        self.dragged = False
        label.setCursor(QtCore.Qt.SizeHorCursor if editor.isEnabled() else QtCore.Qt.ArrowCursor)
        label.setFocusPolicy(QtCore.Qt.ClickFocus)
        label.setToolTip(label.toolTip() + '\n左右拖动调整数值；直接点击数值可输入。Esc 取消拖动。')
        label.installEventFilter(self)

    def finish(self, cancel=False):
        if self.origin is None:return
        self.origin = None
        if not self.dragged:return
        if cancel:
            blocker = QtCore.QSignalBlocker(self.editor)
            self.editor.setValue(self.original_value)
            self.editor.lineEdit().setText(self.original_text)
            del blocker
        elif str(self.editor.value()) != str(self.original_value):
            self.editor.valueChanged.emit(self.editor.value())
        self.dragged = False

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
            if not self.editor.isEnabled() or self.editor.isReadOnly():return False
            try:self.editor._number(self.editor.text())
            except ValueError:return False
            self.origin = event.globalPos().x()
            self.original_text, self.original_value = self.editor.text(), self.editor.value()
            self.dragged = False
            self.label.setFocus(QtCore.Qt.MouseFocusReason)
            return True
        if event.type() == QtCore.QEvent.MouseMove and self.origin is not None:
            distance = event.globalPos().x() - self.origin
            if not self.dragged and abs(distance) < 6:return True
            self.dragged = True
            blocker = QtCore.QSignalBlocker(self.editor)
            self.editor.setValue(self.original_text)
            self.editor.stepBy(int(distance / 6))
            del blocker
            return True
        if event.type() == QtCore.QEvent.MouseButtonRelease and self.origin is not None:
            self.finish()
            return True
        if event.type() == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape and self.origin is not None:
            self.finish(cancel=True)
            return True
        if event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.FocusOut):self.finish(cancel=True)
        return super().eventFilter(watched, event)


class FoldSection(QtWidgets.QWidget):
    def __init__(self, title, parent, histories, identity, expanded=False):
        super().__init__(parent)
        self.setObjectName('canvasFoldSection')
        self._histories, self._identity = histories, ('form_section',) + tuple(identity)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3);layout.setSpacing(5)
        self.toggle = QtWidgets.QToolButton(self)
        self.toggle.setObjectName('canvasSectionToggle')
        self.toggle.setText(title);self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.body = QtWidgets.QWidget(self)
        self.body_layout = QtWidgets.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 2, 0, 0);self.body_layout.setSpacing(6)
        self.summary = QtWidgets.QWidget(self)
        self.summary_layout = QtWidgets.QVBoxLayout(self.summary)
        self.summary_layout.setContentsMargins(0, 0, 0, 0);self.summary_layout.setSpacing(2)
        self.port_summaries = {}
        layout.addWidget(self.toggle);layout.addWidget(self.summary);layout.addWidget(self.body)
        self.toggle.toggled.connect(self._toggle)
        self.set_expanded(bool(histories.get(self._identity, expanded)))

    def _toggle(self, expanded):
        self.body.setVisible(expanded)
        self.summary.setVisible(not expanded and bool(self.port_summaries))
        self.toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self._histories[self._identity] = expanded

    def set_expanded(self, expanded):
        self.toggle.setChecked(expanded)
        self._toggle(expanded)

    def add_port(self, key, label):
        if key not in self.port_summaries:
            widget = QtWidgets.QLabel(label, self.summary)
            widget.setObjectName('canvasFieldLabel');widget.setMinimumHeight(22)
            widget.setToolTip('展开此组可编辑参数；端口仍可直接连接。')
            self.summary_layout.addWidget(widget)
            self.port_summaries[key] = widget
        self.summary.setVisible(not self.toggle.isChecked())
        return self.port_summaries[key]


def field_caption(field, definition=None):
    definition = definition or {}
    raw = str(field.get('description') or field.get('nodeName') or field.get('fieldName') or '参数')
    from .model import app_field_label
    short = app_field_label(field, definition)
    required = definition.get('required', field.get('required'))
    title = short + (' *' if required is True else '')
    help_text = str(definition.get('description') or field.get('_model_help') or raw)
    details = [help_text, '参数：' + str(field.get('fieldName', ''))]
    if required is not None:details.append('必填' if required else '可选；未连接时使用本节点设置')
    constraints = definition or (field.get('fieldData') if isinstance(field.get('fieldData'), dict) else {})
    limits = [label + str(constraints[key]) for key, label in (('min', '最小 '), ('max', '最大 '), ('step', '步长 '))
              if constraints.get(key) is not None]
    if limits:details.append(' · '.join(limits))
    return title, '\n'.join(details)[:1600]
