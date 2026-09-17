"""Validated canvas preferences and a self-contained, cancel-safe editor."""
import copy
import math

from PyQt5 import QtCore, QtGui, QtWidgets


DEFAULTS = {
    'grid_style': 'dots', 'grid_size': 24, 'snap_to_grid': False,
    'link_style': 'curve', 'link_width': 1.8, 'link_opacity': 60,
    'highlight_connections': True, 'link_arrows': True,
    'zoom_speed': 1.15, 'release_action': 'search',
    'tooltips': True, 'tooltip_delay': 600,
    'preview_quality': 'balanced', 'hover_playback': True,
    'autosave_delay': 700, 'undo_limit': 40,
    'shortcuts': {
        'run': 'Ctrl+Return', 'stop': 'Ctrl+Alt+Return', 'find': 'Ctrl+F',
        'fit_selected': '.', 'preferences': 'Ctrl+,', 'add': 'Tab',
    },
    'favorites': [], 'recent': [],
}

_ENUMS = {
    'grid_style': ('dots', 'lines', 'none'),
    'link_style': ('curve', 'orthogonal', 'straight'),
    'release_action': ('search', 'none'),
    'preview_quality': ('economy', 'balanced', 'quality'),
}
_NUMBERS = {
    'grid_size': (8, 96, int), 'link_width': (1., 4., float),
    'link_opacity': (15, 100, int), 'zoom_speed': (1.03, 1.4, float),
    'tooltip_delay': (200, 2000, int), 'autosave_delay': (300, 5000, int),
    'undo_limit': (10, 100, int),
}
_SHORTCUT_NAMES = {
    'run': '运行画布', 'stop': '终止当前画布', 'find': '查找画布节点',
    'fit_selected': '适应所选节点', 'preferences': '画布偏好', 'add': '添加节点',
}
_RESERVED = {
    'Ctrl+Z', 'Ctrl+Y', 'Ctrl+Shift+Z', 'Ctrl+C', 'Ctrl+X', 'Ctrl+V',
    'Ctrl+A', 'Ctrl+S', 'Ctrl+Shift+S', 'Ctrl+O', 'Ctrl+N',
    'Alt+Return', 'Alt+Enter', 'Alt+F4',
    'Ctrl+Insert', 'Shift+Insert',
}
_RESERVED_KEYS = {
    QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace,
    QtCore.Qt.Key_Space, QtCore.Qt.Key_Escape,
}


def _shortcut_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        return ''
    sequence = QtGui.QKeySequence(value, QtGui.QKeySequence.PortableText)
    if sequence.count() != 1:
        return ''
    key = sequence[0] & ~int(QtCore.Qt.KeyboardModifierMask)
    if not key or key == QtCore.Qt.Key_unknown:
        return ''
    return sequence.toString(QtGui.QKeySequence.PortableText)


def _reserved_shortcut(text):
    sequence = QtGui.QKeySequence(text, QtGui.QKeySequence.PortableText)
    key = sequence[0] & ~int(QtCore.Qt.KeyboardModifierMask) if text else 0
    if text in _RESERVED or key in _RESERVED_KEYS:
        return True
    if QtGui.QGuiApplication.instance() is not None:
        for standard in (QtGui.QKeySequence.Copy, QtGui.QKeySequence.Cut, QtGui.QKeySequence.Paste,
                         QtGui.QKeySequence.Undo, QtGui.QKeySequence.Redo, QtGui.QKeySequence.SelectAll):
            if any(sequence.matches(binding) == QtGui.QKeySequence.ExactMatch
                   for binding in QtGui.QKeySequence.keyBindings(standard)):
                return True
    return False


def shortcut_errors(shortcuts):
    """Return actionable errors without silently replacing a user's edits."""
    errors, used = [], {}
    for key, label in _SHORTCUT_NAMES.items():
        text = _shortcut_text(shortcuts.get(key))
        if not text:
            errors.append((key, f'请为“{label}”设置一个有效组合键。'))
        elif _reserved_shortcut(text):
            errors.append((key, f'“{label}”使用了保留快捷键 {text}，请换一个组合键。'))
        elif text in used:
            errors.append((key, f'“{label}”与“{used[text]}”使用了相同的快捷键 {text}。'))
        else:
            used[text] = label
    return errors


def normalize_preferences(raw):
    """Return an independent, bounded schema; malformed saved values are safe."""
    result = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return result
    for key, choices in _ENUMS.items():
        value = raw.get(key)
        if isinstance(value, str) and value in choices:
            result[key] = value
    for key, (minimum, maximum, cast) in _NUMBERS.items():
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            number = float(value)
            if math.isfinite(number):
                result[key] = cast(max(minimum, min(maximum, number)))
        except (ValueError, TypeError, OverflowError):
            pass
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and type(raw.get(key)) is bool:
            result[key] = raw[key]
    shortcuts = raw.get('shortcuts')
    if isinstance(shortcuts, dict):
        for key in _SHORTCUT_NAMES:
            text = _shortcut_text(shortcuts.get(key))
            if text and not _reserved_shortcut(text):
                result['shortcuts'][key] = text
        # A fallback may itself meet another override. Restoring all colliding
        # entries converges on the unique defaults in at most one pass per key.
        for unused in _SHORTCUT_NAMES:
            by_sequence = {}
            for key, text in result['shortcuts'].items():
                by_sequence.setdefault(text, []).append(key)
            collisions = [keys for keys in by_sequence.values() if len(keys) > 1]
            if not collisions:
                break
            for keys in collisions:
                for key in keys:
                    result['shortcuts'][key] = DEFAULTS['shortcuts'][key]
    for key, limit in (('favorites', 500), ('recent', 50)):
        if isinstance(raw.get(key), (list, tuple)):
            seen = set()
            for value in raw[key]:
                if isinstance(value, str) and value.strip() and len(value) <= 256 and value not in seen:
                    result[key].append(value)
                    seen.add(value)
                    if len(result[key]) >= limit:
                        break
    return result


def preferences_for(owner):
    settings = getattr(owner, 'settings', None)
    return normalize_preferences(settings.get('canvas_preferences') if isinstance(settings, dict) else None)


class _WheelGuard(QtCore.QObject):
    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Wheel:
            event.ignore()
            return True
        return super().eventFilter(watched, event)


class PreferencesDialog(QtWidgets.QDialog):
    """Stage edits locally; only the caller applies values after acceptance."""
    def __init__(self, values, parent=None, colors=None):
        super().__init__(parent)
        from aetherloom_core.rh_ui import palette
        self._initial = normalize_preferences(values)
        self.controls, self.shortcut_edits = {}, {}
        self._wheel_guard = _WheelGuard(self)
        self.setWindowTitle('画布偏好')
        self.setObjectName('canvasPreferences')
        self.setMinimumSize(430, 350)
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1280, 720)
        self.resize(min(660, area.width() - 40), min(660, area.height() - 60))
        self.setSizeGripEnabled(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        title = QtWidgets.QLabel('画布偏好')
        title.setObjectName('preferencesTitle')
        layout.addWidget(title)
        note = QtWidgets.QLabel('偏好应用于所有画布。点击保存后生效。')
        note.setObjectName('preferencesHint')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName('preferencesTabs')
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)

        interaction = self._tab('交互')
        form = self._section(interaction, '移动与缩放')
        self._number(form, 'zoom_speed', '滚轮缩放倍率', 1.03, 1.4, decimals=2, step=.01, suffix=' ×')
        self._check(form, 'snap_to_grid', '移动节点时吸附网格')
        self._number(form, 'grid_size', '网格间距', 8, 96, suffix=' px')
        form = self._section(interaction, '连线与提示')
        self._combo(form, 'release_action', '新连线放到空白处', [('打开兼容节点搜索', 'search'), ('取消连线', 'none')])
        self._check(form, 'tooltips', '显示悬停提示')
        self._number(form, 'tooltip_delay', '提示等待时间', 200, 2000, step=100, suffix=' ms')
        interaction.addStretch()

        display = self._tab('显示')
        form = self._section(display, '画布背景')
        self._combo(form, 'grid_style', '网格样式', [('点阵', 'dots'), ('线框', 'lines'), ('隐藏', 'none')])
        form = self._section(display, '连线外观')
        self._combo(form, 'link_style', '连线样式', [('曲线', 'curve'), ('直角折线', 'orthogonal'), ('直线', 'straight')])
        self._number(form, 'link_width', '连线宽度', 1., 4., decimals=1, step=.1, suffix=' px')
        self._number(form, 'link_opacity', '连线不透明度', 15, 100, step=5, suffix=' %')
        self._check(form, 'link_arrows', '显示方向箭头')
        self._check(form, 'highlight_connections', '高亮选中节点的关联连线')
        display.addStretch()

        previews = self._tab('预览')
        form = self._section(previews, '节点内容')
        self._combo(form, 'preview_quality', '预览质量', [('节省资源', 'economy'), ('均衡', 'balanced'), ('高质量', 'quality')])
        hint = QtWidgets.QLabel('节省资源适合大型画布；高质量会显示更多预览。')
        hint.setWordWrap(True)
        hint.setObjectName('preferencesHint')
        form.addRow(hint)
        self._check(form, 'hover_playback', '悬停播放视频 / GIF')
        previews.addStretch()

        saving = self._tab('保存与快捷键')
        form = self._section(saving, '编辑记录')
        self._number(form, 'autosave_delay', '停止编辑后自动保存', 300, 5000, step=100, suffix=' ms')
        self._number(form, 'undo_limit', '保留撤销步骤', 10, 100, step=10, suffix=' 步')
        form = self._section(saving, '快捷键')
        for key, label in _SHORTCUT_NAMES.items():
            editor = QtWidgets.QKeySequenceEdit()
            editor.setAccessibleName(label + '快捷键')
            editor.setMinimumWidth(0)
            editor.setMaximumWidth(230)
            editor.setMinimumHeight(34)
            editor.keySequenceChanged.connect(self._clear_error)
            self.shortcut_edits[key] = editor
            form.addRow(label, editor)
        hint = QtWidgets.QLabel('点击后按下组合键。复制、撤销、保存等常用编辑快捷键保留。')
        hint.setWordWrap(True)
        hint.setObjectName('preferencesHint')
        form.addRow(hint)
        saving.addStretch()

        self.error_label = QtWidgets.QLabel()
        self.error_label.setObjectName('preferencesError')
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QtWidgets.QHBoxLayout()
        self.reset_button = QtWidgets.QPushButton('恢复默认')
        self.reset_button.clicked.connect(lambda: self._set_controls(DEFAULTS))
        buttons.addWidget(self.reset_button)
        buttons.addStretch()
        cancel = QtWidgets.QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QtWidgets.QPushButton('保存偏好')
        self.save_button.setObjectName('savePreferences')
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.controls['tooltips'].toggled.connect(self.controls['tooltip_delay'].setEnabled)
        self._set_controls(self._initial)
        self._apply_theme(colors or palette(getattr(parent, '_theme_mode', 'dark')))

    def _tab(self, label):
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName('preferencesScroll')
        scroll.viewport().setObjectName('preferencesViewport')
        scroll.viewport().setAttribute(QtCore.Qt.WA_StyledBackground, True)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        content = QtWidgets.QWidget()
        content.setObjectName('preferencesContent')
        content.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(0, 14, 8, 8)
        layout.setSpacing(14)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, label)
        return layout

    def _section(self, layout, title):
        card = QtWidgets.QFrame()
        card.setObjectName('preferencesCard')
        column = QtWidgets.QVBoxLayout(card)
        column.setContentsMargins(16, 12, 16, 14)
        column.setSpacing(12)
        heading = QtWidgets.QLabel(title)
        heading.setObjectName('preferencesSection')
        column.addWidget(heading)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        form.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        column.addLayout(form)
        layout.addWidget(card)
        return form

    def _combo(self, form, key, label, choices):
        editor = QtWidgets.QComboBox()
        for text, value in choices:
            editor.addItem(text, value)
        editor.setMinimumWidth(0)
        editor.setMaximumWidth(230)
        editor.setMinimumHeight(34)
        editor.setAccessibleName(label)
        editor.installEventFilter(self._wheel_guard)
        self.controls[key] = editor
        form.addRow(label, editor)

    def _number(self, form, key, label, minimum, maximum, decimals=0, step=1, suffix=''):
        editor = QtWidgets.QDoubleSpinBox() if decimals else QtWidgets.QSpinBox()
        if decimals:
            editor.setDecimals(decimals)
        editor.setRange(minimum, maximum)
        editor.setSingleStep(step)
        editor.setSuffix(suffix)
        editor.setKeyboardTracking(False)
        editor.setMinimumWidth(0)
        editor.setMaximumWidth(230)
        editor.setMinimumHeight(34)
        editor.setAccessibleName(label)
        editor.installEventFilter(self._wheel_guard)
        self.controls[key] = editor
        form.addRow(label, editor)

    def _check(self, form, key, label):
        editor = QtWidgets.QCheckBox(label)
        editor.setMinimumHeight(30)
        self.controls[key] = editor
        form.addRow(editor)

    def _set_controls(self, values):
        for key, editor in self.controls.items():
            if isinstance(editor, QtWidgets.QCheckBox):
                editor.setChecked(values[key])
            elif isinstance(editor, QtWidgets.QComboBox):
                editor.setCurrentIndex(editor.findData(values[key]))
            else:
                editor.setValue(values[key])
        for key, editor in self.shortcut_edits.items():
            editor.setKeySequence(QtGui.QKeySequence(values['shortcuts'][key], QtGui.QKeySequence.PortableText))
        self.controls['tooltip_delay'].setEnabled(values['tooltips'])
        self._clear_error()

    def _clear_error(self, unused=None):
        if hasattr(self, 'error_label'):
            self.error_label.clear()
            self.error_label.hide()

    def _raw_values(self):
        values = copy.deepcopy(self._initial)
        for key, editor in self.controls.items():
            if isinstance(editor, QtWidgets.QCheckBox):
                values[key] = editor.isChecked()
            elif isinstance(editor, QtWidgets.QComboBox):
                values[key] = editor.currentData()
            else:
                editor.interpretText()
                values[key] = editor.value()
        values['shortcuts'] = {key: editor.keySequence().toString(QtGui.QKeySequence.PortableText)
                               for key, editor in self.shortcut_edits.items()}
        return values

    def values(self):
        return normalize_preferences(self._raw_values())

    def accept(self):
        errors = shortcut_errors(self._raw_values()['shortcuts'])
        if errors:
            key, message = errors[0]
            self.tabs.setCurrentIndex(3)
            self.error_label.setText(message)
            self.error_label.show()
            editor = self.shortcut_edits[key]
            self.tabs.currentWidget().ensureWidgetVisible(editor)
            editor.setFocus()
            return
        super().accept()

    def _apply_theme(self, colors):
        from pathlib import Path
        from aetherloom_core.paths import current_dir
        from aetherloom_core.rh_ui import palette
        p = dict(palette(), **colors)
        mode = 'light' if QtGui.QColor(p['canvas']).lightness() > 128 else 'dark'
        icon_root = Path(current_dir) / 'icons'
        up_arrow = (icon_root / f'ui-chevron-up-{mode}.svg').as_posix()
        down_arrow = (icon_root / f'ui-chevron-down-{mode}.svg').as_posix()
        check_icon = (icon_root / 'ui-check.svg').as_posix()
        qt_palette = self.palette()
        for role, key in ((QtGui.QPalette.Window, 'canvas'), (QtGui.QPalette.Base, 'input'),
                          (QtGui.QPalette.Button, 'surface'), (QtGui.QPalette.WindowText, 'text'),
                          (QtGui.QPalette.Text, 'text'), (QtGui.QPalette.ButtonText, 'text'),
                          (QtGui.QPalette.Highlight, 'accent')):
            qt_palette.setColor(role, QtGui.QColor(p[key]))
        self.setPalette(qt_palette)
        # QAbstractScrollArea's viewport keeps its own palette. Explicitly theme
        # both layers because an ancestor stylesheet can stop palette inheritance.
        for index in range(self.tabs.count()):
            scroll = self.tabs.widget(index)
            for surface in (scroll, scroll.viewport(), scroll.widget()):
                surface_palette = QtGui.QPalette(qt_palette)
                surface_palette.setColor(QtGui.QPalette.Base, QtGui.QColor(p['canvas']))
                surface.setPalette(surface_palette)
                surface.setBackgroundRole(QtGui.QPalette.Window)
                surface.setAutoFillBackground(True)
        self.setStyleSheet(f'''
            QDialog#canvasPreferences {{ background: {p['canvas']}; color: {p['text']}; }}
            QDialog#canvasPreferences QWidget {{ color: {p['text']}; font-family: 'Microsoft YaHei UI'; font-size: 13px; }}
            QDialog#canvasPreferences QScrollArea#preferencesScroll,
            QDialog#canvasPreferences QWidget#preferencesViewport,
            QDialog#canvasPreferences QWidget#preferencesContent,
            QDialog#canvasPreferences QTabWidget#preferencesTabs,
            QDialog#canvasPreferences QTabWidget#preferencesTabs QStackedWidget {{ background: {p['canvas']}; border: none; }}
            QFrame#preferencesCard {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 9px; }}
            QLabel {{ background: transparent; border: none; }}
            QDialog#canvasPreferences QLabel#preferencesTitle {{ font-size: 23px; font-weight: 600; }}
            QDialog#canvasPreferences QLabel#preferencesSection {{ font-size: 14px; font-weight: 600; }}
            QDialog#canvasPreferences QLabel#preferencesHint {{ color: {p['muted']}; font-size: 12px; }}
            QDialog#canvasPreferences QLabel#preferencesError {{ color: {p['danger']}; }}
            QTabWidget::pane {{ border: none; }}
            QTabBar::tab {{ background: transparent; color: {p['muted']}; padding: 10px 14px; border-bottom: 2px solid transparent; }}
            QTabBar::tab:selected {{ color: {p['accent']}; border-bottom-color: {p['accent']}; }}
            QTabBar::tab:hover {{ background: {p['hover']}; }}
            QComboBox, QAbstractSpinBox {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 4px 8px; }}
            QComboBox:focus, QAbstractSpinBox:focus {{ border-color: {p['accent']}; }}
            QComboBox QAbstractItemView {{ background: {p['input']}; color: {p['text']}; selection-background-color: {p['accent']}; selection-color: white; }}
            QComboBox::drop-down {{ width: 24px; border: none; background: transparent; }}
            QComboBox::down-arrow {{ image: url("{down_arrow}"); width: 12px; height: 12px; }}
            QAbstractSpinBox {{ padding-right: 25px; }}
            QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 23px; border: none; background: transparent; }}
            QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 23px; border: none; background: transparent; }}
            QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {p['hover']}; }}
            QAbstractSpinBox::up-arrow {{ image: url("{up_arrow}"); width: 10px; height: 10px; }}
            QAbstractSpinBox::down-arrow {{ image: url("{down_arrow}"); width: 10px; height: 10px; }}
            QAbstractSpinBox QLineEdit {{ background: transparent; border: none; padding: 0; }}
            QKeySequenceEdit {{ background: transparent; border: none; padding: 0; }}
            QKeySequenceEdit QLineEdit {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 6px 8px; }}
            QKeySequenceEdit QLineEdit:focus {{ border-color: {p['accent']}; }}
            QCheckBox {{ background: transparent; spacing: 9px; }}
            QCheckBox::indicator {{ width: 17px; height: 17px; background: {p['input']}; border: 1px solid {p['muted']}; border-radius: 4px; }}
            QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; image: url("{check_icon}"); }}
            QPushButton {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 8px 14px; min-height: 18px; }}
            QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
            QPushButton#savePreferences {{ background: {p['accent']}; color: white; border-color: {p['accent']}; font-weight: 600; }}
            QAbstractSpinBox:disabled {{ color: {p['muted']}; background: {p['canvas']}; }}
            QScrollBar:vertical {{ width: 8px; background: transparent; }}
            QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 4px; min-height: 28px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        ''')
