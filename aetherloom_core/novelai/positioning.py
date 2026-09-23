"""Compact, explicit automatic/manual character-position selection."""
from PyQt5 import QtCore, QtWidgets

from .styles import workspace_palette as palette


class PositionModeSelector(QtWidgets.QWidget):
    """Keep imported mixed states until the user chooses one global mode."""

    modeChanged = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = False
        self._free_coordinates = True
        self._theme_mode = 'dark'
        self._theme_timer = QtCore.QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.timeout.connect(lambda: self.apply_theme(self._theme_mode))
        self.setObjectName('novelaiPositionMode')
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.segments = QtWidgets.QFrame(self)
        self.segments.setObjectName('novelaiPositionSegments')
        self.segments.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        row = QtWidgets.QHBoxLayout(self.segments)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)
        self.auto_button = QtWidgets.QToolButton(self.segments)
        self.auto_button.setText('AI 自动')
        self.auto_button.setObjectName('novelaiPositionAutomatic')
        self.manual_button = QtWidgets.QToolButton(self.segments)
        self.manual_button.setObjectName('novelaiPositionManual')
        self._buttons = QtWidgets.QButtonGroup(self)
        self._buttons.setExclusive(True)
        for index, button in enumerate((self.auto_button, self.manual_button)):
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setMinimumWidth(0)
            button.setMinimumHeight(32)
            button.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            button.setFocusPolicy(QtCore.Qt.StrongFocus)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            self._buttons.addButton(button, index)
            row.addWidget(button, 1)
        self.auto_button.clicked.connect(lambda: self._choose(False))
        self.manual_button.clicked.connect(lambda: self._choose(True))
        layout.addWidget(self.segments)

        self.hint_label = QtWidgets.QLabel(self)
        self.hint_label.setObjectName('novelaiPositionHint')
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextFormat(QtCore.Qt.PlainText)
        self.hint_label.setMinimumWidth(0)
        self.hint_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(self.hint_label)
        self.set_mode(False)
        self.apply_theme('dark')

    def showEvent(self, event):
        super().showEvent(event)
        # Windows Qt can keep the constructor's colors through its first
        # inherited-style polish. Reapply once after that polish completes.
        self._theme_timer.start(0)

    def mode(self):
        return self._value

    def set_mode(self, mode):
        """Set False/True/None silently; None leaves both segments unchecked."""
        if mode is not None and not isinstance(mode, bool):
            raise TypeError('Position mode must be False, True, or None')
        self._value = mode
        # Temporarily relax exclusivity so an imported mixed state can clear
        # both checks. Normal user clicks always keep one selected afterward.
        self._buttons.setExclusive(False)
        with QtCore.QSignalBlocker(self.auto_button), QtCore.QSignalBlocker(self.manual_button):
            self.auto_button.setChecked(mode is False)
            self.manual_button.setChecked(mode is True)
        self._buttons.setExclusive(True)
        self._update_text()

    def set_free_coordinates(self, enabled):
        self._free_coordinates = bool(enabled)
        self._update_text()

    def _choose(self, manual):
        if self._value is manual:
            return
        self.set_mode(manual)
        self.modeChanged.emit(manual)

    def _update_text(self):
        manual_name = '手动自由定位' if self._free_coordinates else '手动网格定位'
        automatic = 'AI 会根据角色提示词自动安排位置。'
        manual = ('在中间画面拖动角色编号，或填写 X / Y；0.5 为画面中央。' if self._free_coordinates else
                  '打开位置画布选择 5 × 5 网格，或填写 X / Y。')
        self.manual_button.setText(manual_name)
        self.auto_button.setToolTip(automatic)
        self.manual_button.setToolTip(manual)
        self.auto_button.setAccessibleName('AI 自动定位')
        self.manual_button.setAccessibleName(manual_name)
        mixed = self._value is None
        message = ('现有角色的定位方式不一致。请选择一种模式统一设置。' if mixed else
                   manual if self._value else automatic)
        self.hint_label.setText(message)
        self.hint_label.setToolTip(message)
        self.setAccessibleName('角色定位模式')
        self.setAccessibleDescription(message)
        tone = 'warning' if mixed else 'muted'
        if self.hint_label.property('positionTone') != tone:
            self.hint_label.setProperty('positionTone', tone)
            self.hint_label.style().unpolish(self.hint_label)
            self.hint_label.style().polish(self.hint_label)
            self.hint_label.update()

    def apply_theme(self, mode):
        self._theme_mode = mode
        p = palette(mode)
        self.setStyleSheet(f"""
            QWidget#novelaiPositionMode {{background:transparent;border:none;}}
            QWidget#novelaiPositionMode QFrame#novelaiPositionSegments {{
                background:{p['input']};border:1px solid {p['border']};border-radius:9px;
            }}
            QWidget#novelaiPositionMode QToolButton {{
                background:transparent;color:{p['muted']};border:1px solid transparent;
                border-radius:6px;padding:3px 4px;min-width:0;min-height:26px;font-size:12px;
            }}
            QWidget#novelaiPositionMode QToolButton:hover {{
                background:{p['hover']};color:{p['text']};
            }}
            QWidget#novelaiPositionMode QToolButton:checked {{
                background:{p['accent_soft']};color:{p['accent']};border-color:{p['accent']};
                font-weight:600;
            }}
            QWidget#novelaiPositionMode QToolButton:focus {{border-color:{p['accent']};}}
            QWidget#novelaiPositionMode QToolButton:disabled {{
                background:transparent;color:{p['muted']};border-color:transparent;
            }}
            QWidget#novelaiPositionMode QToolButton:checked:disabled {{
                background:{p['hover']};color:{p['muted']};border-color:{p['border']};
            }}
            QWidget#novelaiPositionMode QLabel#novelaiPositionHint {{
                background:transparent;border:none;padding:0;color:{p['muted']};font-size:11px;
            }}
            QWidget#novelaiPositionMode QLabel#novelaiPositionHint[positionTone="warning"] {{
                color:{p['warning']};
            }}
        """)
