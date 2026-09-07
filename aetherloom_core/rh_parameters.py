"""Exact RH numeric editors and main-thread snapshots of application inputs."""
import copy
import json
import re
from decimal import Decimal, InvalidOperation, localcontext

from PyQt5 import QtCore, QtGui, QtWidgets


def _scroll_form(widget, event):
    """Route the wheel to the nearest scrolling form instead of changing a field."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QtWidgets.QAbstractScrollArea):
            viewport = parent.viewport()
            forwarded = QtGui.QWheelEvent(
                QtCore.QPointF(viewport.mapFromGlobal(event.globalPos())), event.globalPosF(),
                event.pixelDelta(), event.angleDelta(), event.buttons(), event.modifiers(),
                event.phase(), event.inverted(), event.source())
            QtWidgets.QApplication.sendEvent(viewport, forwarded)
            if forwarded.isAccepted():
                event.accept()
                return
        parent = parent.parentWidget()
    event.ignore()


class RhNumberSpinBox(QtWidgets.QAbstractSpinBox):
    """Spin editor without QSpinBox's int32 or QDoubleSpinBox's rounding limits."""
    valueChanged = QtCore.pyqtSignal(object)

    def __init__(self, parent=None, *, integer=False):
        super().__init__(parent)
        self.integer = integer
        self._minimum = self._maximum = None
        self._step = Decimal(1 if integer else '0.1')
        self._value = '0'
        self._draft_dirty = False
        self._error_message = ''
        self._normal_tooltip = None
        self.lineEdit().setText(self._value)
        self.lineEdit().textChanged.connect(self._changed)
        self.editingFinished.connect(self._finish_edit)
        self.setKeyboardTracking(False)

    def _number(self, text):
        text = str(text).strip()
        if self.integer and not re.fullmatch(r'[+-]?\d+', text):
            raise ValueError('请输入整数')
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError('请输入有效数值') from exc
        if not value.is_finite():
            raise ValueError('请输入有限数值')
        if self._minimum is not None and value < self._minimum:
            raise ValueError(f'数值不能小于 {self._minimum}')
        if self._maximum is not None and value > self._maximum:
            raise ValueError(f'数值不能大于 {self._maximum}')
        return value

    def validate(self, text, pos):
        try:
            self._number(text)
            return QtGui.QValidator.Acceptable, text, pos
        except ValueError:
            pattern = r'[+-]?\d*' if self.integer else r'[+-]?(?:\d*\.?\d*)(?:[eE][+-]?\d*)?'
            state = QtGui.QValidator.Intermediate if re.fullmatch(pattern, text) else QtGui.QValidator.Invalid
            return state, text, pos

    def _changed(self, text):
        # Keystrokes are a draft, not a stream of committed values/file writes.
        self._draft_dirty = text != self._value
        self._show_error('')

    def _finish_edit(self):
        self.commit()

    def _show_error(self, message):
        if message == self._error_message:
            return
        if self._normal_tooltip is None:
            self._normal_tooltip = self.toolTip()
        self._error_message = message
        self.setProperty('inputError', bool(message))
        self.setToolTip(message or self._normal_tooltip)
        self.lineEdit().setStyleSheet('border-bottom: 2px solid #e55c6a;' if message else '')

    def commit(self):
        """Validate only at the edit boundary; keep invalid drafts for correction."""
        text = self.text().strip()
        try:
            self._number(text)
        except ValueError as error:
            self._show_error(str(error))
            return False
        self.setValue(text)
        return True

    def focusOutEvent(self, event):
        # QAbstractSpinBox's built-in correction has no knowledge of our Decimal
        # value. Let Qt finish focus handling, then restore the exact draft before
        # notifying consumers; an unfinished exponent must not become an old value.
        edit = self.lineEdit()
        text, position = edit.text(), edit.cursorPosition()
        with QtCore.QSignalBlocker(self), QtCore.QSignalBlocker(edit):
            super().focusOutEvent(event)
            if edit.text() != text:
                edit.setText(text)
                edit.setCursorPosition(position)
        self.editingFinished.emit()

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self.editingFinished.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def value(self):
        return int(self._value) if self.integer else self._value

    def setValue(self, value):
        text = str(value).strip()
        self._number(text)
        changed = self._value != text
        self._value = text
        if self.text() != text:
            self.lineEdit().setText(text)
        self._draft_dirty = False
        self._show_error('')
        if changed:
            self.valueChanged.emit(self.value())

    def setExternalValue(self, value):
        """A watcher refresh must not replace the user's active or invalid draft."""
        if self.hasFocus() or self.lineEdit().hasFocus() or self._draft_dirty:
            return False
        self.setValue(value)
        return True

    def setSingleStep(self, value):
        step = Decimal(str(value))
        if step.is_finite() and step > 0 and (not self.integer or step == step.to_integral_value()):
            self._step = step

    def configure(self, field_data):
        """Use the workflow's numeric bounds/step when supplied, without guessing."""
        if isinstance(field_data, str):
            try:
                field_data = json.loads(field_data)
            except (ValueError, TypeError):
                return
        if isinstance(field_data, list):
            field_data = next((part for part in reversed(field_data) if isinstance(part, dict)), {})
        if not isinstance(field_data, dict):
            return
        for key, attribute in (('min', '_minimum'), ('max', '_maximum')):
            try:
                value = Decimal(str(field_data[key]))
                if value.is_finite():
                    setattr(self, attribute, value)
            except (KeyError, ValueError, InvalidOperation):
                pass
        if self._minimum is not None and self._maximum is not None and self._minimum > self._maximum:
            self._minimum = self._maximum = None
        try:
            self.setSingleStep(field_data['step'])
        except (KeyError, ValueError, InvalidOperation):
            pass

    def stepBy(self, steps):
        try:
            current = self._number(self.text())
        except ValueError as error:
            self._show_error(str(error))
            return
        with localcontext() as context:
            current_tuple = current.as_tuple()
            step_tuple = self._step.as_tuple()
            # Addition aligns both operands to their smallest exponent. Include
            # the step's fractional digits and the multiplication by steps too.
            exponent = min(current_tuple.exponent, step_tuple.exponent)
            context.prec = max(
                50,
                len(current_tuple.digits) + current_tuple.exponent - exponent,
                len(step_tuple.digits) + step_tuple.exponent - exponent + len(str(abs(steps))),
            ) + 2
            value = current + self._step * steps
        if self._minimum is not None:
            value = max(value, self._minimum)
        if self._maximum is not None:
            value = min(value, self._maximum)
        self.setValue(str(int(value)) if self.integer else format(value, 'f'))

    def stepEnabled(self):
        try:
            value = self._number(self.text())
        except ValueError:
            return QtWidgets.QAbstractSpinBox.StepNone
        if self.isReadOnly():
            return QtWidgets.QAbstractSpinBox.StepNone
        flags = QtWidgets.QAbstractSpinBox.StepNone
        if self._maximum is None or value < self._maximum:
            flags |= QtWidgets.QAbstractSpinBox.StepUpEnabled
        if self._minimum is None or value > self._minimum:
            flags |= QtWidgets.QAbstractSpinBox.StepDownEnabled
        return flags

    def wheelEvent(self, event):
        # Scrolling through a form must not silently change a parameter.
        _scroll_form(self, event)


class RhEnumComboBox(QtWidgets.QComboBox):
    """Select with a click/keyboard; wheel over the closed field scrolls the page."""

    def wheelEvent(self, event):
        _scroll_form(self, event)


def _list_option(option):
    """Return a display label and API value; metadata entries are not options."""
    if isinstance(option, dict):
        if 'default' in option and not any(key in option for key in ('index', 'value', 'name')):
            return None
        for key in ('index', 'value', 'name'):
            value = option.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                label = option.get('name') or option.get('label') or value
                return str(label), str(value)
        return None
    if option is None or isinstance(option, list):
        return None
    return str(option), str(option)


def configure_list_combo(combo, field_data, current_value):
    """Display LIST labels while retaining scalar API values and the current choice."""
    if isinstance(field_data, str):
        try:
            field_data = json.loads(field_data)
        except (ValueError, TypeError):
            field_data = []
    if isinstance(field_data, dict):
        field_data = field_data.get('options', field_data.get('values', field_data.get('enum', [])))
    if isinstance(field_data, list) and field_data and isinstance(field_data[0], list):
        field_data = field_data[0]
    entries = []
    seen = set()
    for option in field_data if isinstance(field_data, list) else []:
        entry = _list_option(option)
        if entry is not None and entry[1] not in seen:
            entries.append(entry)
            seen.add(entry[1])
    current = '' if current_value is None else str(current_value)
    if current not in seen:
        entries.append((current, current))
    blocked = combo.blockSignals(True)
    try:
        combo.clear()
        combo._rh_options = [value for _, value in entries]
        combo._rh_initial_value = current
        for label, value in entries:
            combo.addItem(label, value)
        combo.setCurrentIndex(combo._rh_options.index(current))
    finally:
        combo.blockSignals(blocked)


def _combo_value(combo, fallback):
    options = getattr(combo, '_rh_options', [])
    position = combo.currentIndex()
    if 0 <= position < len(options):
        option = _list_option(options[position])
        if option is not None:
            return option[1]
    # Empty or incomplete metadata must not erase an existing workflow value.
    return fallback


def collect_node_values(nodes, widgets):
    """Snapshot all editor values before starting a worker or saving parameters."""
    result = copy.deepcopy(nodes or [])
    for index, entry in widgets.items():
        if not 0 <= index < len(result):
            continue
        value = None
        if entry.get('te') is not None:
            editor = entry['te']
            timer = getattr(editor, '_rh_persist_timer', None)
            if timer is not None:
                timer.stop()
            value = editor.toPlainText()
        elif entry.get('le') is not None:
            value = entry['le'].text()
        elif entry.get('ds') is not None or entry.get('sb') is not None:
            editor = entry.get('ds') if entry.get('ds') is not None else entry['sb']
            if not editor.hasAcceptableInput():
                if isinstance(editor, RhNumberSpinBox):
                    editor.commit()
                name = result[index].get('fieldName') or str(index + 1)
                raise ValueError(f'参数 {name} 的数值无效或超出范围')
            if isinstance(editor, RhNumberSpinBox):
                editor.commit()
            value = editor.text()
        elif entry.get('combo_bool') is not None:
            value = entry['combo_bool'].currentText()
        elif entry.get('combo') is not None:
            editor = entry['combo']
            value = _combo_value(editor, result[index].get('fieldValue', ''))
        if value is not None:
            result[index]['fieldValue'] = str(value)
    return result
