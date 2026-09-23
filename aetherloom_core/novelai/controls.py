"""Scrollable NovelAI settings, character prompts, and local references."""
from copy import deepcopy
import math
import uuid

from PyQt5 import QtCore, QtGui, QtWidgets

from .styles import workspace_stylesheet as app_stylesheet, workspace_palette as palette
from .prompt_editor import NovelAIPromptEdit, PromptNavigation
from .references import ReferencesEditor, decimal, guard_wheel, editor_stylesheet, set_tone
from .positioning import PositionModeSelector


_DEPENDENT_FIELDS = {'model', 'action', 'sampler', 'noise_schedule',
                     'transparent_background', 'variety_boost',
                     'quality_preset', 'uc_preset', 'augment_method'}


def _combo(values):
    widget = guard_wheel(QtWidgets.QComboBox())
    for text, value in values:
        widget.addItem(text, value)
    widget.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    widget.setMinimumContentsLength(7)
    return widget


def _select(widget, value):
    index = widget.findData(value)
    if index < 0 and widget.isEditable():
        widget.setCurrentIndex(-1)
        widget.setEditText(str(value))
        return
    if index < 0:
        widget.addItem(str(value), value)
        index = widget.count() - 1
    widget.setCurrentIndex(index)


def _combo_value(widget):
    if widget.isEditable():
        # Editing a preset's display text does not clear Qt's old itemData.
        # Only exact preset labels map to a wire value; custom text stays text.
        index = widget.findText(widget.currentText(), QtCore.Qt.MatchExactly | QtCore.Qt.MatchCaseSensitive)
        return widget.itemData(index) if index >= 0 else widget.currentText()
    return widget.currentData()


def _integer(minimum, maximum, value, step=1):
    widget = guard_wheel(QtWidgets.QSpinBox())
    widget.setRange(minimum, maximum)
    widget.setSingleStep(step)
    widget.setValue(value)
    return widget


def _prompt(parent, height, placeholder):
    widget = NovelAIPromptEdit(parent, minimum_height=height,
                              maximum_height=240 if height < 96 else 320,
                              compact=height < 96)
    widget.setPlaceholderText(placeholder)
    return widget


def _seed_reset_button(callback, *, compact=False):
    button = QtWidgets.QToolButton()
    button.setObjectName('novelaiSeedReset')
    button.setText('↺')
    button.setToolTip('重置为随机种子（-1）')
    button.setAccessibleName('重置为随机种子')
    button.setFocusPolicy(QtCore.Qt.NoFocus)
    button.setFixedSize(24 if compact else 32, 28 if compact else 34)
    button.clicked.connect(callback)
    return button


class _Section(QtWidgets.QWidget):
    def __init__(self, title, expanded=True, parent=None, *, vertical=False):
        super().__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(7)
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setObjectName('novelaiSectionTitle')
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.body = QtWidgets.QWidget()
        self.form = (QtWidgets.QVBoxLayout if vertical else QtWidgets.QFormLayout)(self.body)
        self.form.setContentsMargins(0, 0, 0, 2)
        if vertical:
            self.form.setSpacing(8)
        else:
            self.form.setHorizontalSpacing(8)
            self.form.setVerticalSpacing(7)
            self.form.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            self.form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
            self.form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        outer.addWidget(self.toggle)
        outer.addWidget(self.body)
        self.toggle.toggled.connect(self._toggled)
        self.toggle.setChecked(expanded)
        self._toggled(expanded)

    def _toggled(self, expanded):
        self.body.setVisible(expanded)
        self.toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)


class _SectionNavigator:
    """Legacy section navigation now scrolls the single continuous sidebar."""
    def __init__(self, scroll):
        self.scroll = scroll
        self._sections = []
        self._names = []
        self._current = 0
        self._target = None
        self._focus_timer = QtCore.QTimer(scroll)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._scroll_to_target)

    def add(self, widget, title):
        self._sections.append(widget)
        self._names.append(title)

    def count(self):
        return len(self._sections)

    def widget(self, index):
        return self.scroll if 0 <= index < len(self._sections) else None

    def currentIndex(self):
        return self._current

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self._sections):
            return False
        return self.focus(self._sections[index])

    def focus(self, section):
        """Expand and reveal a visible section after Qt updates its layout."""
        if section is None or section.isHidden():
            return False
        if section in self._sections:
            self._current = self._sections.index(section)
        if isinstance(section, _Section):
            section.toggle.setChecked(True)
        self._target = section
        self._scroll_to_target()
        # Showing a mode's fields and expanding a body both post layout events.
        # The first scroll can still see the previous content size and range.
        self._focus_timer.start(0)
        return True

    def _scroll_to_target(self):
        section = self._target
        if section is None or section.isHidden():
            return
        content = self.scroll.widget()
        content.layout().activate()
        y = section.mapTo(content, QtCore.QPoint(0, 0)).y()
        self.scroll.verticalScrollBar().setValue(max(0, y - 8))

    def cancel_pending_focus(self):
        self._focus_timer.stop()
        self._target = None

    def setTabText(self, index, text):
        self._names[index] = text
        section = self._sections[index]
        if isinstance(section, _Section):
            section.toggle.setText(text)

    def tabText(self, index):
        return self._names[index]

    def setTabEnabled(self, index, enabled):
        self._sections[index].setEnabled(enabled)

    def isTabEnabled(self, index):
        return self._sections[index].isEnabled()

    def setEnabled(self, enabled):
        self.scroll.widget().setEnabled(enabled)

    def isEnabled(self):
        return self.scroll.widget().isEnabled()


class _CharacterCard(QtWidgets.QFrame):
    changed = QtCore.pyqtSignal()
    remove_requested = QtCore.pyqtSignal(object)
    move_requested = QtCore.pyqtSignal(object, int)

    def __init__(self, value, parent=None):
        super().__init__(parent)
        self.setObjectName('novelaiCharacterCard')
        self._position_identity = uuid.uuid4().hex
        self._raw = deepcopy(value)
        self._use_coords = bool(value.get('use_coords', False))
        self._free_coordinates = True
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(4)
        self.title = QtWidgets.QToolButton()
        self.title.setText('角色')
        self.title.setObjectName('novelaiCharacterTitle')
        self.title.setCheckable(True)
        self.title.setChecked(True)
        self.title.setArrowType(QtCore.Qt.DownArrow)
        self.title.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        head.addWidget(self.title)
        self.name = QtWidgets.QLineEdit(str(value.get('name', '') or ''))
        self.name.setObjectName('novelaiCharacterName')
        self.name.setPlaceholderText('名称（可选）')
        self.name.setAccessibleName('角色名称')
        self.name.setMinimumWidth(0)
        self.name.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.name.textChanged.connect(self.changed)
        head.addWidget(self.name, 1)
        self.placement_badge = QtWidgets.QLabel()
        self.placement_badge.setObjectName('novelaiPositionBadge')
        head.addWidget(self.placement_badge)
        self.enabled = QtWidgets.QCheckBox()
        self.enabled.setObjectName('novelaiCharacterEnabled')
        self.enabled.setCheckable(True)
        self.enabled.setChecked(value.get('enabled', True) is not False)
        self.enabled.setFixedSize(22, 24)
        self.enabled.setAccessibleName('启用角色')
        self.enabled.toggled.connect(self._enabled_changed)
        head.addWidget(self.enabled)
        for label, tooltip, direction in [('↑', '向前移动', -1), ('↓', '向后移动', 1)]:
            button = QtWidgets.QToolButton()
            button.setText(label)
            button.setObjectName('novelaiIconButton')
            button.setToolTip(tooltip)
            button.setFixedSize(22, 28)
            button.clicked.connect(lambda checked=False, d=direction: self.move_requested.emit(self, d))
            head.addWidget(button)
        remove = QtWidgets.QToolButton()
        remove.setText('×')
        remove.setObjectName('novelaiIconButton')
        remove.setProperty('destructive', True)
        remove.setToolTip('移除此角色')
        remove.setFixedSize(22, 28)
        remove.clicked.connect(lambda: self.remove_requested.emit(self))
        head.addWidget(remove)
        layout.addLayout(head)
        self.body = QtWidgets.QWidget()
        layout.addWidget(self.body)
        layout = QtWidgets.QVBoxLayout(self.body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.title.toggled.connect(self._toggle_body)
        self.prompt = _prompt(self, 26, '角色外观、动作与服装')
        self.prompt.setAccessibleName('角色提示词')
        self.prompt.setPlainText(str(value.get('prompt', '')))
        self.negative = _prompt(self, 26, '此角色的不期望内容（可选）')
        self.negative.setAccessibleName('角色不期望内容')
        self.negative.setPlainText(str(value.get('negative_prompt', '')))
        self.prompt_tabs = QtWidgets.QTabWidget()
        self.prompt_tabs.setObjectName('novelaiPromptTabs')
        self.prompt_tabs.setDocumentMode(True)
        self.prompt_tabs.tabBar().setExpanding(True)
        self.prompt_tabs.addTab(self.prompt, '提示词')
        self.prompt_tabs.addTab(self.negative, '不期望内容')
        expand = QtWidgets.QToolButton()
        expand.setText('↗')
        expand.setObjectName('novelaiIconButton')
        expand.setToolTip('展开当前角色提示词')
        expand.clicked.connect(lambda: self.prompt_tabs.currentWidget().open_expanded())
        self.prompt_tabs.setCornerWidget(expand)
        self.prompt.heightChanged.connect(self._fit_prompt_tab)
        self.negative.heightChanged.connect(self._fit_prompt_tab)
        self.prompt_tabs.currentChanged.connect(self._fit_prompt_tab)
        self.prompt_tabs.currentChanged.connect(lambda _: self.prompt_tabs.currentWidget().setFocus())
        self._fit_prompt_tab()
        layout.addWidget(self.prompt_tabs)
        self.positions = QtWidgets.QWidget()
        positions = QtWidgets.QHBoxLayout(self.positions)
        positions.setContentsMargins(0, 0, 0, 0)
        positions.setSpacing(8)
        self.coordinates = {}
        for axis, label in [('x', 'X · 左右'), ('y', 'Y · 上下')]:
            column = QtWidgets.QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(4)
            axis_label = QtWidgets.QLabel(label)
            axis_label.setObjectName('novelaiMuted')
            column.addWidget(axis_label)
            stack = QtWidgets.QStackedWidget()
            stack.setMinimumWidth(0)
            stack.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            free = decimal(0, 1, float(value.get(axis, .5)), .05, 3)
            grid = _combo([])
            grid.setMinimumContentsLength(2)
            for editor in (free, grid):
                editor.setMinimumWidth(0)
                editor.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            self._set_grid_value(grid, float(value.get(axis, .5)))
            stack.addWidget(free)
            stack.addWidget(grid)
            column.addWidget(stack)
            positions.addLayout(column, 1)
            self.coordinates[axis] = (stack, free, grid)
            free.setToolTip('0 为左侧，1 为右侧' if axis == 'x' else '0 为顶部，1 为底部')
            grid.setToolTip('从左到右的五列' if axis == 'x' else '从上到下的五行')
            free.valueChanged.connect(self._coordinate_edited)
            grid.currentIndexChanged.connect(self._coordinate_edited)
        layout.addWidget(self.positions)
        self.position_warning = QtWidgets.QLabel()
        self.position_warning.setObjectName('novelaiMuted')
        self.position_warning.setWordWrap(True)
        set_tone(self.position_warning, 'warning')
        layout.addWidget(self.position_warning)
        self.snap_button = QtWidgets.QPushButton('对齐最近网格')
        self.snap_button.setObjectName('novelaiSecondaryButton')
        self.snap_button.setToolTip('将此角色的 X、Y 对齐到最近的网格中心；仅点击后才修改坐标。')
        self.snap_button.clicked.connect(self.snap_to_grid)
        layout.addWidget(self.snap_button)
        self._update_position_display()
        self.prompt.textChanged.connect(self.changed)
        self.negative.textChanged.connect(self.changed)
        self._enabled_changed(self.enabled.isChecked(), emit=False)

    def _enabled_changed(self, enabled, emit=True):
        self.enabled.setToolTip('停用此角色，保留提示词与位置' if enabled else '启用此角色')
        self.body.setEnabled(enabled)
        effect = self.body.graphicsEffect()
        if effect is None:
            effect = QtWidgets.QGraphicsOpacityEffect(self.body)
            self.body.setGraphicsEffect(effect)
        effect.setOpacity(1. if enabled else .42)
        if emit:
            self.changed.emit()

    def _toggle_body(self, expanded):
        self.body.setVisible(expanded)
        self.title.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)

    def _fit_prompt_tab(self, *_):
        self.prompt_tabs.setFixedHeight(self.prompt_tabs.currentWidget().height()
                                       + max(26, self.prompt_tabs.tabBar().sizeHint().height()) + 4)

    @staticmethod
    def _set_grid_value(grid, value):
        values = (.1, .3, .5, .7, .9)
        with QtCore.QSignalBlocker(grid):
            grid.clear()
            for v in values:
                grid.addItem(f'{v:.1f}', v)
            index = next((i for i, v in enumerate(values) if abs(v - value) < 1e-6), -1)
            if index < 0:
                grid.addItem(f'原值 {value:g}', value)
                index = grid.count() - 1
                grid.model().item(index).setEnabled(False)
            grid.setCurrentIndex(index)

    def needs_grid_alignment(self):
        return self._use_coords and not self._free_coordinates and any(
            not any(abs(float(grid.currentData()) - v) < 1e-6 for v in (.1, .3, .5, .7, .9))
            for _, _, grid in self.coordinates.values())

    def _update_position_display(self):
        self.positions.setEnabled(self._use_coords)
        self.positions.setVisible(self._use_coords)
        self.placement_badge.setText('手动' if self._use_coords else 'AI')
        self.placement_badge.setToolTip('手动指定角色位置' if self._use_coords else 'AI 根据提示词自动安排位置')
        invalid = self.needs_grid_alignment()
        self.position_warning.setText('原坐标不在 V4.5 网格内。可重新选择坐标，或对齐最近网格。' if invalid else '')
        self.position_warning.setVisible(invalid)
        self.snap_button.setVisible(invalid)

    def _coordinate_edited(self, *_):
        self._update_position_display()
        self.changed.emit()

    def snap_to_grid(self):
        if not self.needs_grid_alignment():
            return
        for _, _, grid in self.coordinates.values():
            value = float(grid.currentData())
            nearest = min((.1, .3, .5, .7, .9), key=lambda v: (round(abs(v - value), 12), v))
            self._set_grid_value(grid, nearest)
        self._coordinate_edited()

    def set_coordinates_enabled(self, enabled):
        self._use_coords = bool(enabled)
        self._update_position_display()

    def set_position(self, x, y):
        """Apply an explicit visual edit without changing the positioning mode."""
        for axis, value in (('x', x), ('y', y)):
            _, free, grid = self.coordinates[axis]
            with QtCore.QSignalBlocker(free):
                free.setValue(value)
            self._set_grid_value(grid, value)
        self._update_position_display()

    def set_capabilities(self, caps):
        free_coordinates = bool(caps.get('free_coordinates', False))
        if free_coordinates != self._free_coordinates:
            for stack, free, grid in self.coordinates.values():
                if free_coordinates:
                    with QtCore.QSignalBlocker(free):
                        free.setValue(float(grid.currentData()))
                else:
                    self._set_grid_value(grid, free.value())
                stack.setCurrentIndex(0 if free_coordinates else 1)
        self._free_coordinates = free_coordinates
        self._update_position_display()

    def value(self):
        result = deepcopy(self._raw)
        result.update(name=self.name.text(), enabled=self.enabled.isChecked(),
                      prompt=self.prompt.toPlainText(), negative_prompt=self.negative.toPlainText(),
                      use_coords=self._use_coords)
        for axis, (_, free, grid) in self.coordinates.items():
            result[axis] = free.value() if self._free_coordinates else float(grid.currentData())
        return result


class _QuickSettings(QtWidgets.QWidget):
    """Small reparentable mirrors; DrawingControls remains the only source."""
    def __init__(self, controls):
        super().__init__(controls)
        self.controls = controls
        self.setObjectName('novelaiQuickSettings')
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.fields = {}
        self._cells = []
        self._columns = 0
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(5)
        self.grid.setVerticalSpacing(5)
        for key, label in (('steps', '步数'), ('scale', '引导'), ('seed', '种子'), ('sampler', '采样器')):
            source = controls._fields[key]
            cell = QtWidgets.QWidget()
            box = QtWidgets.QVBoxLayout(cell)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(3)
            title = QtWidgets.QLabel(label)
            title.setObjectName('novelaiQuickLabel')
            box.addWidget(title)
            if key == 'steps':
                widget = _integer(source.minimum(), source.maximum(), source.value(), source.singleStep())
                widget.valueChanged.connect(source.setValue)
            elif key == 'scale':
                widget = decimal(source.minimum(), source.maximum(), source.value(), source.singleStep(), source.decimals())
                widget.valueChanged.connect(source.setValue)
            elif key == 'seed':
                widget = QtWidgets.QLineEdit()
                widget.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(r'-1|[0-9]{1,10}'), widget))
                widget.textChanged.connect(source.setText)
                widget.editingFinished.connect(source.editingFinished)
            else:
                widget = _combo([])
                widget.setMinimumContentsLength(0)
                widget.currentIndexChanged.connect(self._sampler_selected)
                widget.view().setMinimumWidth(190)
            widget.setAccessibleName('快速设置 · ' + label)
            widget.setMinimumWidth(0)
            widget.setFixedHeight(28)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            if key == 'seed':
                seed_row = QtWidgets.QHBoxLayout()
                seed_row.setSpacing(3)
                seed_row.addWidget(widget, 1)
                self.seed_reset_button = _seed_reset_button(controls.reset_seed, compact=True)
                seed_row.addWidget(self.seed_reset_button)
                box.addLayout(seed_row)
            else:
                box.addWidget(widget)
            self.fields[key] = widget
            self._cells.append(cell)
        controls.seed.textChanged.connect(self.sync)
        self._relayout(4)
        self.sync()
        self.hide()

    def _sampler_selected(self, index):
        source = self.controls._fields['sampler']
        index = source.findData(self.fields['sampler'].currentData())
        if index >= 0:
            source.setCurrentIndex(index)

    def _relayout(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        for cell in self._cells:
            self.grid.removeWidget(cell)
        for index, cell in enumerate(self._cells):
            self.grid.addWidget(cell, index // columns, index % columns)
        for index in range(4):
            self.grid.setColumnStretch(index, (3 if index > 1 else 2) if index < columns else 0)

    def resizeEvent(self, event):
        self._relayout(4 if self.width() >= 280 else 2)
        super().resizeEvent(event)

    def sync(self, *_):
        short = {'k_euler_ancestral': 'Euler a', 'k_euler': 'Euler',
                 'k_dpmpp_2s_ancestral': '2S a', 'k_dpmpp_2m': '2M',
                 'k_dpmpp_2m_sde': '2M SDE', 'k_dpmpp_sde': 'SDE', 'k_dpm_2': 'DPM2'}
        for key, widget in self.fields.items():
            source = self.controls._fields[key]
            with QtCore.QSignalBlocker(widget):
                if isinstance(widget, QtWidgets.QComboBox):
                    rows = [(source.itemText(i), source.itemData(i), source.model().item(i).isEnabled())
                            for i in range(source.count())]
                    old = [(widget.itemData(i), widget.itemData(i, QtCore.Qt.ToolTipRole), widget.model().item(i).isEnabled())
                           for i in range(widget.count())]
                    if old != [(value, text, enabled) for text, value, enabled in rows]:
                        widget.clear()
                        for text, value, enabled in rows:
                            widget.addItem(short.get(value, text), value)
                            index = widget.count() - 1
                            widget.setItemData(index, text, QtCore.Qt.ToolTipRole)
                            widget.model().item(index).setEnabled(enabled)
                    widget.setCurrentIndex(widget.findData(source.currentData()))
                    widget.setToolTip(source.currentText())
                elif isinstance(widget, QtWidgets.QAbstractSpinBox):
                    if (widget.minimum(), widget.maximum()) != (source.minimum(), source.maximum()):
                        widget.setRange(source.minimum(), source.maximum())
                    widget.setSingleStep(source.singleStep())
                    if widget.value() != source.value():
                        widget.setValue(source.value())
                    widget.setToolTip(source.toolTip())
                else:
                    if widget.text() != source.text():
                        widget.setText(source.text())
                    widget.setToolTip(source.toolTip())
                widget.setEnabled(source.isEnabled())
        self.seed_reset_button.setEnabled(self.controls.seed.isEnabled())

    def apply_theme(self, mode):
        p = palette(mode)
        self.setStyleSheet(f"""
            QWidget#novelaiQuickSettings {{background:transparent;}}
            QWidget#novelaiQuickSettings QLabel {{background:transparent;color:{p['muted']};font-size:10px;}}
            QWidget#novelaiQuickSettings QSpinBox, QWidget#novelaiQuickSettings QDoubleSpinBox,
            QWidget#novelaiQuickSettings QLineEdit, QWidget#novelaiQuickSettings QComboBox {{
                background:{p['input']};color:{p['text']};border:1px solid {p['border']};border-radius:3px;
                font-size:10px;padding:2px 3px;selection-background-color:{p['accent_soft']};}}
            QWidget#novelaiQuickSettings QSpinBox:focus, QWidget#novelaiQuickSettings QDoubleSpinBox:focus,
            QWidget#novelaiQuickSettings QLineEdit:focus, QWidget#novelaiQuickSettings QComboBox:focus {{border-color:{p['accent']};}}
            QWidget#novelaiQuickSettings QSpinBox::up-button, QWidget#novelaiQuickSettings QDoubleSpinBox::up-button,
            QWidget#novelaiQuickSettings QSpinBox::down-button, QWidget#novelaiQuickSettings QDoubleSpinBox::down-button {{width:12px;}}
            QWidget#novelaiQuickSettings QComboBox::drop-down {{width:14px;border:none;}}
            QWidget#novelaiQuickSettings QAbstractItemView {{background:{p['surface']};color:{p['text']};
                selection-background-color:{p['accent_soft']};selection-color:{p['accent']};border:1px solid {p['border']};}}
            QWidget#novelaiQuickSettings QToolButton#novelaiSeedReset {{background:{p['input']};color:{p['muted']};
                border:1px solid {p['border']};border-radius:3px;padding:0;font-size:14px;}}
            QWidget#novelaiQuickSettings QToolButton#novelaiSeedReset:hover {{background:{p['hover']};color:{p['accent']};}}
            QWidget#novelaiQuickSettings QToolButton#novelaiSeedReset:disabled {{color:{p['border']};}}
        """)


class DrawingControls(QtWidgets.QWidget):
    """GUI-only editor. ``settings()`` returns a detached, plain Python snapshot."""
    changed = QtCore.pyqtSignal()
    resetDefaultsRequested = QtCore.pyqtSignal()
    positionEditRequested = QtCore.pyqtSignal()
    prompt_editor_activated = QtCore.pyqtSignal(object)
    tagSuggestionsRequested = QtCore.pyqtSignal(object)
    tagPrefixChanged = QtCore.pyqtSignal(object, str)

    def __init__(self, owner=None):
        super().__init__(owner if isinstance(owner, QtWidgets.QWidget) else None)
        from . import catalog
        self._catalog = catalog
        self.owner = owner
        self._loading = False
        self._busy = False
        self._mode = 'dark'
        self._position_editing = False
        self._raw = {}
        self._fields = {}
        self._field_rows = {}
        self._characters = []
        self._active_prompt = None
        self._caps = {}
        self.setObjectName('novelaiControls')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setMinimumWidth(280)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        settings_header = QtWidgets.QHBoxLayout()
        settings_header.setContentsMargins(12, 6, 12, 0)
        settings_title = QtWidgets.QLabel('绘图参数')
        settings_title.setObjectName('novelaiSettingsTitle')
        settings_header.addWidget(settings_title)
        settings_header.addStretch(1)
        self.reset_defaults_button = QtWidgets.QPushButton('恢复默认参数')
        self.reset_defaults_button.setObjectName('novelaiResetDefaults')
        self.reset_defaults_button.setAccessibleName('恢复默认绘图参数')
        self.reset_defaults_button.setToolTip(
            '恢复尺寸、步数、引导、采样器、种子等生成参数；保留当前模型、模式、提示词、角色和输入素材。\n'
            '不影响连接设置、任务队列和已有结果。')
        self.reset_defaults_button.clicked.connect(self.resetDefaultsRequested)
        settings_header.addWidget(self.reset_defaults_button)
        root.addLayout(settings_header)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setObjectName('novelaiControlsScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(0)
        self.content = QtWidgets.QWidget()
        self.content.setObjectName('novelaiContinuousControls')
        self.content.setMinimumWidth(0)
        parameters = QtWidgets.QVBoxLayout(self.content)
        parameters.setContentsMargins(12, 8, 12, 12)
        parameters.setSpacing(12)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll)
        self.tabs = _SectionNavigator(self.scroll)
        model_box = QtWidgets.QWidget()
        model_form = QtWidgets.QFormLayout(model_box)
        model_form.setContentsMargins(0, 0, 0, 0)
        model_form.setHorizontalSpacing(10)
        model_form.setVerticalSpacing(8)
        model_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        parameters.addWidget(model_box)
        self.tabs.add(model_box, '参数')
        model_row = QtWidgets.QWidget()
        model_layout = QtWidgets.QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(5)
        self.model = self._register('model', _combo(
            [(entry['name'].removeprefix('NAI Diffusion '), entry['id']) for entry in catalog.MODELS]))
        for index, entry in enumerate(catalog.MODELS):
            self.model.setItemData(index, entry['name'], QtCore.Qt.ToolTipRole)
        self.model.setMinimumWidth(0)
        self.model.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.dataset_mode = self._register('dataset_mode', _combo([('Anime', 'anime'), ('Furry', 'furry')]))
        self.dataset_mode.setMinimumContentsLength(4)
        self.dataset_mode.setMaximumWidth(88)
        self.dataset_mode.setToolTip('选择 Anime 或 Furry 提示词数据集')
        self.dataset_mode.setAccessibleName('提示词数据集')
        model_layout.addWidget(self.model, 1)
        model_layout.addWidget(self.dataset_mode)
        model_form.addRow('模型', model_row)
        self._field_rows['model'] = (model_form, model_form.labelForField(model_row), model_row)
        self.action = self._add(model_form, '模式', 'action', _combo([
            ('文生图', 'generate'), ('图生图', 'img2img'), ('局部重绘', 'infill'),
            ('超分辨率', 'upscale'), ('Director 图像工具', 'augment')]))
        self.capability_note = QtWidgets.QLabel()
        self.capability_note.setObjectName('novelaiCapability')
        self.capability_note.setWordWrap(True)
        parameters.addWidget(self.capability_note)

        prompts = self.prompt_section = _Section('提示词', vertical=True)
        prompts.toggle.hide()
        self.prompt = self._register('prompt', _prompt(self, 96, '描述场景、画风与构图；多角色时将人数写在这里'))
        self.negative_prompt = self._register('negative_prompt', _prompt(self, 96, '不期望出现在画面中的内容'))
        self.prompt_tabs = PromptNavigation([self.prompt, self.negative_prompt])
        for editor, title, preset_label, preset_key in (
                (self.prompt, '提示词', '质量预设', 'quality_preset'),
                (self.negative_prompt, '不期望内容', '负面预设', 'uc_preset')):
            prompt_page = QtWidgets.QWidget()
            prompt_layout = QtWidgets.QVBoxLayout(prompt_page)
            prompt_layout.setContentsMargins(0, 0, 0, 0)
            prompt_layout.setSpacing(6)
            prompt_layout.addWidget(editor.header(title))
            prompt_layout.addWidget(editor)
            preset_row = QtWidgets.QHBoxLayout()
            preset_row.setContentsMargins(0, 0, 0, 0)
            preset_row.setSpacing(8)
            label = QtWidgets.QLabel(preset_label)
            label.setObjectName('novelaiMuted')
            preset_field = self._register(preset_key, _combo([]))
            label.setBuddy(preset_field)
            preset_field.setAccessibleName(preset_label)
            preset_row.addWidget(label)
            preset_row.addWidget(preset_field, 1)
            prompt_layout.addLayout(preset_row)
            prompts.form.addWidget(prompt_page)
        parameters.addWidget(prompts)

        basic = self.generation_section = _Section('图像设置')
        size_row = QtWidgets.QWidget()
        size_layout = QtWidgets.QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(7)
        self.width = self._register('width', _integer(64, 4096, 832, 64))
        self.height = self._register('height', _integer(64, 4096, 1216, 64))
        self.width.setToolTip('宽度，像素；需为 64 的倍数，总面积不超过 3,145,728 像素。')
        self.height.setToolTip('高度，像素；需为 64 的倍数，总面积不超过 3,145,728 像素。')
        size_layout.addWidget(self.width, 1)
        size_layout.addWidget(QtWidgets.QLabel('×'))
        size_layout.addWidget(self.height, 1)
        swap = QtWidgets.QToolButton()
        swap.setText('⇄')
        swap.setObjectName('novelaiIconButton')
        swap.setToolTip('交换宽高')
        swap.clicked.connect(self._swap_size)
        size_layout.addWidget(swap)
        basic.form.addRow('尺寸', size_row)
        preset = _combo([('选择常用尺寸…', None), ('竖图 · 832 × 1216', (832, 1216)),
                         ('方图 · 1024 × 1024', (1024, 1024)), ('横图 · 1216 × 832', (1216, 832)),
                         ('大竖图 · 1024 × 1536', (1024, 1536)), ('大横图 · 1536 × 1024', (1536, 1024))])
        preset.currentIndexChanged.connect(lambda: self._size_preset(preset))
        basic.form.addRow(preset)
        self._add(basic.form, '步数', 'steps', _integer(1, 50, 23))
        self._add(basic.form, '提示词引导', 'scale', decimal(0, 1_000_000, 7, .5, 2))
        self._fields['scale'].setToolTip('通常使用 0–10；与官网一致，可手动输入更高的非负引导值。')
        self.seed = QtWidgets.QLineEdit('-1')
        self.seed.setMinimumHeight(34)
        self.seed.setAlignment(QtCore.Qt.AlignRight)
        self.seed.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(r'-1|[0-9]{1,10}'), self.seed))
        self.seed.setPlaceholderText('-1 为随机种子')
        self.seed.setToolTip('-1 为随机；固定种子范围 0–4294967295')
        self._register('seed', self.seed)
        seed_row = QtWidgets.QWidget()
        seed_layout = QtWidgets.QHBoxLayout(seed_row)
        seed_layout.setContentsMargins(0, 0, 0, 0)
        seed_layout.setSpacing(5)
        seed_layout.addWidget(self.seed, 1)
        self.seed_reset_button = _seed_reset_button(self.reset_seed)
        seed_layout.addWidget(self.seed_reset_button)
        basic.form.addRow('种子', seed_row)
        self._field_rows['seed'] = (basic.form, basic.form.labelForField(seed_row), seed_row)
        self._add(basic.form, '张数', 'n_samples', _integer(1, 8, 1))
        self.stream = self._add(basic.form, '', 'stream', QtWidgets.QCheckBox('实时预览（Streaming）'))
        self.stream.setToolTip('生成过程中在画布显示中间预览；完成后显示最终图片。')
        self.stream_note = QtWidgets.QLabel(self.stream.toolTip())
        self.stream_note.setObjectName('novelaiMuted')
        self.stream_note.setWordWrap(True)
        basic.form.addRow(self.stream_note)
        parameters.addWidget(basic)

        advanced = self.advanced_section = _Section('高级采样', expanded=False)
        self._add(advanced.form, '采样器', 'sampler', _combo([
            ('Euler Ancestral', 'k_euler_ancestral'), ('Euler', 'k_euler'),
            ('DPM++ 2S Ancestral', 'k_dpmpp_2s_ancestral'), ('DPM++ 2M', 'k_dpmpp_2m'),
            ('DPM++ 2M SDE', 'k_dpmpp_2m_sde'), ('DPM++ SDE', 'k_dpmpp_sde'), ('DPM2', 'k_dpm_2')]))
        self._add(advanced.form, '噪声调度', 'noise_schedule', _combo([
            ('Karras', 'karras'), ('Exponential', 'exponential'), ('Polyexponential', 'polyexponential')]))
        self._add(advanced.form, 'CFG Rescale', 'cfg_rescale', decimal(0, 1, 0))
        self._add(advanced.form, '', 'variety_boost', QtWidgets.QCheckBox('多样性增强'))
        self._add(advanced.form, '', 'transparent_background', QtWidgets.QCheckBox('透明背景'))
        self._add(advanced.form, '', 'straight_alpha', QtWidgets.QCheckBox('使用 Straight Alpha'))
        parameters.addWidget(advanced)

        self.image_section = _Section('图生图与局部重绘', expanded=False)
        self._add(self.image_section.form, '重绘强度', 'strength', decimal(0, 1, .7))
        self._add(self.image_section.form, '附加噪声', 'noise', decimal(0, 1, 0))
        self._add(self.image_section.form, '遮罩内重绘强度', 'inpaint_strength', decimal(0, 1, 1))
        self._fields['inpaint_strength'].setToolTip('1 完全重绘遮罩区域；降低数值可保留该区域原有的形状与细节。')
        self._add(self.image_section.form, '', 'color_correct', QtWidgets.QCheckBox('匹配原图颜色'))
        self._add(self.image_section.form, '', 'add_original_image', QtWidgets.QCheckBox('保留未重绘区域'))
        note = QtWidgets.QLabel('在画布中导入底图；局部重绘需绘制或导入遮罩。')
        note.setObjectName('novelaiMuted')
        note.setWordWrap(True)
        self.image_section.form.addRow(note)
        parameters.addWidget(self.image_section)
        self.tools_section = _Section('图像工具设置', expanded=True, vertical=True)
        self._add(self.tools_section.form, '处理方式', 'augment_method', _combo([
            ('上色', 'colorize'), ('去除背景', 'bg-removal'), ('提取线稿', 'lineart'),
            ('提取草图', 'sketch'), ('改变表情', 'emotion'), ('清理画面', 'declutter'),
            ('清理画面 · 保留气泡', 'declutter-keep-bubbles')]))
        self._add(self.tools_section.form, '色彩抑制', 'defry', _integer(0, 5, 0))
        # Official Director emotion selector / 266-9080def4f2212ae0.js.
        emotion = _combo([(f'{label} · {value}', value) for label, value in (
            ('自然', 'neutral'), ('开心', 'happy'), ('悲伤', 'sad'), ('生气', 'angry'),
            ('害怕', 'scared'), ('惊讶', 'surprised'), ('疲倦', 'tired'), ('兴奋', 'excited'),
            ('紧张', 'nervous'), ('思考', 'thinking'), ('困惑', 'confused'), ('害羞', 'shy'),
            ('厌恶', 'disgusted'), ('得意', 'smug'), ('无聊', 'bored'), ('大笑', 'laughing'),
            ('恼怒', 'irritated'), ('情欲', 'aroused'), ('尴尬', 'embarrassed'), ('担忧', 'worried'),
            ('爱慕', 'love'), ('坚定', 'determined'), ('受伤', 'hurt'), ('俏皮', 'playful'))])
        emotion.setEditable(True)
        emotion.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        emotion.lineEdit().setPlaceholderText('选择表情，或输入英文名称')
        emotion.setMinimumHeight(34)
        emotion.setToolTip('官网的 24 种表情；也保留自定义英文输入。选择预设时提交对应英文名称。')
        self._add(self.tools_section.form, '表情', 'emotion', emotion)
        self.tool_prompt = self._add(self.tools_section.form, '工具提示词', 'tool_prompt',
            _prompt(self, 112, '可选：描述上色细节或表情特征，如 red hair, blue eyes'))
        self.tool_prompt.setToolTip('仅用于当前 Director 工具；不会带入主提示词、质量预设或负面预设。')
        self._add(self.tools_section.form, '模糊系数', 'declared_blur_sigma', _combo([
            ('关闭', 0.), ('0.30', .3), ('0.35', .35), ('0.40', .4), ('0.45', .45), ('0.50', .5)]))
        tool_note = self.tool_note = QtWidgets.QLabel()
        tool_note.setObjectName('novelaiMuted')
        tool_note.setWordWrap(True)
        self.tools_section.form.addWidget(tool_note)
        parameters.addWidget(self.tools_section)
        parameters.addStretch(1)

        self.character_section = _Section('角色', vertical=True)
        characters_layout = self.character_section.form
        parameters.insertWidget(3, self.character_section)
        self.tabs.add(self.character_section, '角色')
        self.character_note = QtWidgets.QLabel()
        self.character_note.setObjectName('novelaiMuted')
        self.character_note.setWordWrap(True)
        characters_layout.addWidget(self.character_note)
        self.position_mode = PositionModeSelector(self)
        self.position_mode.modeChanged.connect(self._coordinates_changed)
        characters_layout.addWidget(self.position_mode)
        self.position_canvas_button = QtWidgets.QPushButton('画面角色位置')
        self.position_canvas_button.setCheckable(True)
        self.position_canvas_button.setObjectName('novelaiPositionCanvasButton')
        self.position_canvas_button.setMinimumHeight(34)
        self.position_canvas_button.setAccessibleName('在主画面调整角色位置')
        self.position_canvas_button.clicked.connect(self._edit_character_positions)
        characters_layout.addWidget(self.position_canvas_button)
        self.add_character_button = QtWidgets.QPushButton('＋ 添加角色')
        self.add_character_button.setObjectName('novelaiAddButton')
        self.add_character_button.clicked.connect(lambda: self._add_character())
        self.characters_layout = QtWidgets.QVBoxLayout()
        self.characters_layout.setSpacing(8)
        characters_layout.addLayout(self.characters_layout)
        characters_layout.addWidget(self.add_character_button)

        self.references_section = _Section('参考图', expanded=False, vertical=True)
        references_layout = self.references_section.form
        parameters.insertWidget(4, self.references_section)
        self.tabs.add(self.references_section, '参考图')
        self.references = ReferencesEditor(owner)
        self._fields['normalize_reference_strength_multiple'] = self.references.normalize_strength
        references_layout.addWidget(self.references)
        self.references.changed.connect(self._references_changed)
        self._sections_by_key = {
            'parameters': model_box, 'prompts': self.prompt_section,
            'generation': basic, 'advanced': advanced, 'image': self.image_section,
            'tools': self.tools_section, 'characters': self.character_section,
            'references': self.references_section,
        }
        self.model.currentIndexChanged.connect(self._model_changed)
        for key, refresh in (
                ('action', self._action_changed), ('sampler', self._sampler_changed),
                ('noise_schedule', self._capabilities_changed),
                ('transparent_background', self._capabilities_changed),
                ('variety_boost', self._capabilities_changed),
                ('quality_preset', self._action_changed), ('uc_preset', self._action_changed),
                ('augment_method', self._action_changed)):
            widget = self._fields[key]
            signal = widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else widget.currentIndexChanged
            signal.connect(lambda *unused, refresh=refresh: self._dependent_changed(refresh))
        self.action.currentIndexChanged.connect(self._focus_action_section)
        self.set_settings(catalog.default_options())
        self.quick_settings = _QuickSettings(self)
        self.apply_theme(getattr(owner, '_theme_mode', 'dark'))

    def _register(self, key, widget):
        self._fields[key] = widget
        if isinstance(widget, NovelAIPromptEdit):
            self._bind_prompt_editor(widget)
        if isinstance(widget, QtWidgets.QTextEdit):
            widget.textChanged.connect(self._emit_changed)
        elif isinstance(widget, QtWidgets.QComboBox):
            if key not in _DEPENDENT_FIELDS:
                signal = widget.editTextChanged if widget.isEditable() else widget.currentIndexChanged
                signal.connect(self._emit_changed)
        elif isinstance(widget, QtWidgets.QAbstractSpinBox):
            widget.valueChanged.connect(self._emit_changed)
        elif isinstance(widget, QtWidgets.QCheckBox):
            if key not in _DEPENDENT_FIELDS:
                widget.toggled.connect(self._emit_changed)
        elif isinstance(widget, QtWidgets.QLineEdit):
            widget.editingFinished.connect(self._emit_changed)
        return widget

    def focus_section(self, key):
        """Reveal a settings section without changing the selected mode or values.

        Return False for unknown keys or sections hidden by the current mode.
        Existing numeric tab navigation remains available for older callers.
        """
        key = {'generate': 'parameters', 'img2img': 'image', 'infill': 'image',
               'augment': 'tools', 'upscale': 'tools'}.get(key, key)
        return self.tabs.focus(self._sections_by_key.get(key))

    def _focus_action_section(self, *_):
        if not self._loading:
            self.focus_section(self.action.currentData())

    def _bind_prompt_editor(self, editor):
        editor.activated.connect(self._prompt_activated)
        editor.prefixChanged.connect(self.tagPrefixChanged)
        editor.tagSuggestionsRequested.connect(self.tagSuggestionsRequested)

    def _prompt_activated(self, editor):
        self._active_prompt = editor
        self.prompt_editor_activated.emit(editor)

    def active_prompt_editor(self):
        from PyQt5 import sip
        if self._active_prompt is not None and not sip.isdeleted(self._active_prompt):
            return self._active_prompt
        return self.prompt

    def _add(self, layout, label, key, widget):
        self._register(key, widget)
        if isinstance(layout, QtWidgets.QFormLayout):
            if label:
                layout.addRow(label, widget)
            else:
                layout.addRow(widget)
            self._field_rows[key] = (layout, layout.labelForField(widget))
        else:
            row_widget = QtWidgets.QWidget()
            form = QtWidgets.QFormLayout(row_widget)
            form.setContentsMargins(0, 0, 0, 0)
            form.setHorizontalSpacing(8)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
            form.addRow(label, widget)
            if label:
                form.labelForField(widget).setMinimumWidth(72)
            layout.addWidget(row_widget)
            self._field_rows[key] = (form, form.labelForField(widget), row_widget)
        return widget

    def _show_field(self, key, visible):
        """Hide a field together with its form label, without losing its value."""
        self._fields[key].setVisible(visible)
        row = self._field_rows.get(key)
        if row and row[1] is not None:
            row[1].setVisible(visible)
        if row and len(row) > 2:
            row[2].setVisible(visible)

    def _label_field(self, key, text):
        row = self._field_rows.get(key)
        if row and row[1] is not None:
            row[1].setText(text)

    def _sync_quick_settings(self):
        if hasattr(self, 'quick_settings'):
            self.quick_settings.sync()

    def _emit_changed(self, *unused):
        if not self._loading:
            self._sync_quick_settings()
            self.changed.emit()

    def reset_seed(self):
        if self.seed.isEnabled() and self.seed.text() != '-1':
            self.seed.setText('-1')
            # Programmatic text changes do not emit editingFinished.
            self._emit_changed()

    def _swap_size(self):
        width, height = self.width.value(), self.height.value()
        self._loading = True
        self.width.setValue(height)
        self.height.setValue(width)
        self._loading = False
        self._emit_changed()

    def _size_preset(self, preset):
        dimensions = preset.currentData()
        if dimensions:
            self._loading = True
            self.width.setValue(dimensions[0])
            self.height.setValue(dimensions[1])
            preset.setCurrentIndex(0)
            self._loading = False
            self._emit_changed()

    def _add_character(self, value=None, emit=True):
        if value is None:
            value = {'prompt': '', 'negative_prompt': '', 'x': .5, 'y': .5,
                     'use_coords': self.position_mode.mode() is True}
        card = _CharacterCard(value, self)
        self._bind_prompt_editor(card.prompt)
        self._bind_prompt_editor(card.negative)
        card.changed.connect(self._character_changed)
        card.remove_requested.connect(self._remove_character)
        card.move_requested.connect(self._move_character)
        card.set_capabilities(self._caps)
        self._characters.append(card)
        self.characters_layout.addWidget(card)
        self._update_character_count()
        self._action_changed()
        if emit:
            self._emit_changed()

    def _remove_character(self, card):
        if self._active_prompt in (card.prompt, card.negative):
            self._active_prompt = self.prompt
        self._characters.remove(card)
        self.characters_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        self._update_character_count()
        self._action_changed()
        self._emit_changed()

    def _move_character(self, card, direction):
        index = self._characters.index(card)
        target = index + direction
        if 0 <= target < len(self._characters):
            self._characters[index], self._characters[target] = self._characters[target], card
            self.characters_layout.removeWidget(card)
            self.characters_layout.insertWidget(target, card)
            self._update_character_count()
            self._emit_changed()

    def _update_character_count(self):
        count = len(self._characters)
        active = [card for card in self._characters if card.enabled.isChecked()]
        maximum = int(self._caps.get('max_characters', 6))
        self.tabs.setTabText(1, f'角色 {count}' if count else '角色')
        self.add_character_button.setEnabled(not self._busy and len(active) < maximum
                                             and count < 256 and self._caps.get('characters', True))
        for index, card in enumerate(self._characters, 1):
            card.title.setText(f'角色 {index}')
        if not self._loading and active:
            states = {card._use_coords for card in active}
            self.position_mode.set_mode(None if len(states) > 1 else next(iter(states)))
        self.position_mode.set_free_coordinates(bool(self._caps.get('free_coordinates')))
        self.position_canvas_button.setVisible(self.position_mode.mode() is True)
        self.position_canvas_button.setEnabled(self._position_edit_enabled())
        self.position_canvas_button.setToolTip('在中间主画面按生成尺寸比例拖动角色编号；再次点击结束定位。' if count else '请先添加角色。')
        self.character_section.toggle.setToolTip(f'最多 {maximum} 个角色。人数写在主提示词，角色特征分别填写。')
        text = f'最多 {maximum} 个角色。'
        issues = []
        if len(active) > maximum:
            issues.append(f'当前启用了 {len(active)} 个角色，超过此模型的 {maximum} 个限制，请停用或减少角色。')
        if self.position_mode.mode() is None:
            issues.append('原设置混用了自动和手动定位，请在下方统一选择。')
        invalid = sum(card.needs_grid_alignment() for card in active)
        if invalid:
            issues.append(f'{invalid} 个角色需要调整网格坐标，也可切换为 AI 自动定位。')
        self.character_note.setText('\n'.join([text] + issues))
        self.character_note.setVisible(bool(issues))
        self.character_note.setToolTip('定位方式对所有角色统一生效；切换 AI 自动定位会保留手动坐标供下次使用。')
        set_tone(self.character_note, 'warning' if issues else 'muted')

    def _character_changed(self):
        self._update_character_count()
        self._action_changed()
        self._emit_changed()

    def _position_edit_enabled(self):
        return (not self._busy and self.position_mode.mode() is True
                and any(card.enabled.isChecked() for card in self._characters)
                and self.action.currentData() in ('generate', 'img2img', 'infill'))

    def _edit_character_positions(self):
        # The page owns the central editor and decides whether to open or close
        # it. A standalone controls widget never launches an auxiliary window.
        self.position_canvas_button.setChecked(self._position_editing)
        if self._position_edit_enabled():
            self.positionEditRequested.emit()

    def position_editor_state(self):
        active = [(index, card) for index, card in enumerate(self._characters) if card.enabled.isChecked()]
        return dict(characters=[dict(card.value(), _display_index=index + 1) for index, card in active],
                    ids=[card._position_identity for _, card in active],
                    indices=[index for index, _ in active],
                    all_characters=[card.value() for card in self._characters],
                    all_ids=[card._position_identity for card in self._characters],
                    size=(self.width.value(), self.height.value()),
                    free_coordinates=bool(self._caps.get('free_coordinates')),
                    enabled=self._position_edit_enabled())

    @staticmethod
    def _valid_position(x, y):
        return all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and 0 <= value <= 1 and math.isfinite(value) for value in (x, y))

    def set_character_position(self, index, x, y):
        if (not self._position_edit_enabled() or isinstance(index, bool)
                or not isinstance(index, int) or not 0 <= index < len(self._characters)
                or not self._valid_position(x, y)):
            return False
        card = self._characters[index]
        if not card.enabled.isChecked():
            return False
        before = card.value()
        card.set_position(x, y)
        after = card.value()
        if (before['x'], before['y']) == (after['x'], after['y']):
            return False
        self._character_changed()
        return True

    def restore_character_positions(self, state):
        if not isinstance(state, dict):
            return False
        ids = state.get('all_ids', state.get('ids'))
        values = state.get('all_characters', state.get('characters'))
        if not isinstance(ids, list) or not isinstance(values, list):
            return False
        originals = {identity: value for identity, value in zip(ids, values)
                     if isinstance(identity, str) and isinstance(value, dict)}
        changed = False
        for card in self._characters:
            value = originals.get(card._position_identity)
            if not value or not self._valid_position(value.get('x'), value.get('y')):
                continue
            before = card.value()
            card.set_position(value['x'], value['y'])
            after = card.value()
            changed |= (before['x'], before['y']) != (after['x'], after['y'])
        if changed:
            self._character_changed()
        return changed

    def set_position_editing(self, editing):
        self._position_editing = bool(editing)
        with QtCore.QSignalBlocker(self.position_canvas_button):
            self.position_canvas_button.setChecked(self._position_editing)
        self.position_canvas_button.setText('结束定位' if self._position_editing else '画面角色位置')

    def _coordinates_changed(self, enabled):
        if self._loading:
            return
        for card in self._characters:
            card.set_coordinates_enabled(enabled)
        self._update_character_count()
        self._action_changed()
        self._emit_changed()

    def _references_changed(self):
        count = len(self.references.value())
        self.tabs.setTabText(2, f'参考图 {count}' if count else '参考图')
        self._emit_changed()

    def _model_changed(self, *_):
        self._dependent_changed(self._capabilities_changed)

    def _dependent_changed(self, refresh):
        # Snapshots and UI state must agree at the public changed signal. Nested
        # updates are part of this same edit, including reference capabilities.
        loading = self._loading
        self._loading = True
        try:
            refresh()
        finally:
            self._loading = loading
        self._emit_changed()

    def _capabilities_changed(self):
        try:
            self._caps = dict(self._catalog.capabilities(self.model.currentData()))
        except (KeyError, ValueError):
            self._caps = {}
        caps = self._caps
        for key, capability in [('noise_schedule', 'noise_schedule'), ('variety_boost', 'variety_boost'),
                                ('stream', 'stream'), ('transparent_background', 'transparency'),
                                ('straight_alpha', 'transparency')]:
            widget = self._fields[key]
            incompatible = ((key == 'noise_schedule' and widget.currentData() != 'karras') or
                            (key in ('variety_boost', 'transparent_background') and widget.isChecked()))
            widget.setEnabled(not self._busy and (bool(caps.get(capability, False)) or incompatible))
        for key, capability in [('quality_preset', 'quality_presets'), ('uc_preset', 'uc_presets')]:
            widget = self._fields[key]
            current = widget.currentData()
            labels = {'standard': '标准', 'light': '轻量', 'none': '关闭', 'heavy': '完整',
                      'humanFocus': '人物优先', 'furryFocus': '兽设优先'}
            with QtCore.QSignalBlocker(widget):
                widget.clear()
                for item in caps.get(capability, []):
                    widget.addItem(labels.get(item, item), item)
                if current is not None:
                    _select(widget, current)
            for index in range(widget.count()):
                widget.model().item(index).setEnabled(widget.itemData(index) in caps.get(capability, []))
        for index in range(self.action.count()):
            action = self.action.itemData(index)
            self.action.model().item(index).setEnabled(action in ('generate', 'augment', 'upscale') or bool(caps.get(action, False)))
        for card in self._characters:
            card.set_capabilities(caps)
        self._update_character_count()
        self._action_changed()
        self._sampler_changed()
        self._sync_quick_settings()

    def _sampler_changed(self):
        sampler = self._fields['sampler']
        variable_schedule = bool(self._caps.get('noise_schedule'))
        dpm_index = sampler.findData('k_dpm_2')
        if dpm_index >= 0:
            sampler.model().item(dpm_index).setEnabled(variable_schedule)
        schedule = self._fields['noise_schedule']
        for index in range(schedule.count()):
            value = schedule.itemData(index)
            allowed = value == 'karras' if not variable_schedule else (
                value in ('exponential', 'polyexponential') if sampler.currentData() == 'k_dpm_2'
                else value in ('karras', 'exponential', 'polyexponential'))
            schedule.model().item(index).setEnabled(allowed)
        self._action_changed()
        self._sync_quick_settings()

    def _action_changed(self):
        action = self.action.currentData()
        caps = self._caps
        tool_mode = action in ('augment', 'upscale')
        generation = not tool_mode
        editing = not self._busy
        size_step = 64
        if action == 'img2img':
            size_step = 1 if self._raw.get('upscaled_enhance') else 32 if self._raw.get('enhancement') else 64
        for widget, axis in ((self.width, '宽度'), (self.height, '高度')):
            widget.setSingleStep(size_step)
            widget.setToolTip(f'{axis}，像素；需为 {size_step} 的倍数，总面积不超过 3,145,728 像素。')
        self.position_canvas_button.setEnabled(self._position_edit_enabled())
        for section in (self.prompt_section, self.generation_section, self.advanced_section,
                        self.character_section, self.references_section):
            section.setVisible(generation)
            section.setEnabled(editing and generation)
        self.model.setEnabled(editing and generation)
        self.dataset_mode.setEnabled(editing and generation)
        self._show_field('model', generation)
        for key in ('prompt', 'negative_prompt', 'quality_preset', 'uc_preset', 'width',
                    'height', 'steps', 'scale', 'seed', 'n_samples', 'sampler', 'cfg_rescale'):
            self._fields[key].setEnabled(editing and generation)
        for key, capability in [('noise_schedule', 'noise_schedule'), ('variety_boost', 'variety_boost'),
                                ('stream', 'stream'), ('transparent_background', 'transparency'),
                                ('straight_alpha', 'transparency')]:
            widget = self._fields[key]
            incompatible_value = ((key == 'noise_schedule' and widget.currentData() != 'karras') or
                                  (key in ('variety_boost', 'transparent_background') and widget.isChecked()))
            applicable = bool(caps.get(capability, False)) or incompatible_value
            # Existing incompatible values remain visible so the user can fix
            # them explicitly; switching models never silently resets a value.
            self._show_field(key, generation and applicable)
            widget.setEnabled(editing and generation and applicable)
        self.stream_note.setVisible(generation and bool(caps.get('stream', False)))

        image_mode = action in ('img2img', 'infill')
        self.image_section.setVisible(image_mode)
        self.image_section.setEnabled(editing and image_mode)
        if image_mode:
            self.image_section.toggle.setChecked(True)
        for key in ('strength', 'noise'):
            self._show_field(key, action == 'img2img')
            self._fields[key].setEnabled(editing and action == 'img2img')
        for key in ('inpaint_strength', 'add_original_image'):
            self._show_field(key, action == 'infill')
            self._fields[key].setEnabled(editing and action == 'infill')
        # The website uses fixed color correction per image operation; this
        # legacy preference stays in snapshots but is not an independent knob.
        self._show_field('color_correct', False)
        self._fields['color_correct'].setEnabled(False)

        method = self._fields['augment_method'].currentData()
        prompted_tool = action == 'augment' and method in ('colorize', 'emotion')
        self.tools_section.setVisible(tool_mode)
        self.tools_section.setEnabled(editing and tool_mode)
        self.tools_section.toggle.setText('V5 Curated · 2× 超分辨率' if action == 'upscale' else 'Director 图像工具')
        tool_fields = {'augment_method': action == 'augment',
                       'defry': prompted_tool, 'tool_prompt': prompted_tool,
                       'emotion': action == 'augment' and method == 'emotion',
                       'declared_blur_sigma': action == 'upscale'}
        for key, applicable in tool_fields.items():
            self._show_field(key, applicable)
            self._fields[key].setEnabled(editing and applicable)
        self._label_field('defry', '表情减弱' if method == 'emotion' else '色彩抑制')
        self._fields['defry'].setToolTip(
            '0 为正常效果；1–5 逐步减弱，5 最弱。' if method == 'emotion' else
            '0 不抑制；提高数值可减少杂色和过强的颜色，适合已有颜色的底图。')
        notes = {
            'colorize': '为线稿或底图上色。提示词可选；色彩抑制 0–5，用于减轻杂色与过度饱和。',
            'emotion': '改变单个角色的表情。建议使用正面的动漫人物；0 为正常效果，5 最弱。',
            'bg-removal': '去除背景，返回遮罩、生成与混合版本，可在结果中分别查看。',
            'lineart': '从底图提取线稿，无需生成提示词。',
            'sketch': '将底图转换为草图，无需生成提示词。',
            'declutter': '清理画面中的文字、气泡和其他杂物。',
            'declutter-keep-bubbles': '清理画面文字与杂物，同时保留气泡。',
            'pixel-snap': '像素对齐是官网浏览器本地工具，目前不能通过此 API 调用；请选择其他工具。',
        }
        self.tool_note.setText('按底图原始尺寸放大 2 倍；生成尺寸、步数和种子不参与此操作。'
                               if action == 'upscale' else notes.get(method, '选择要使用的图像工具。') +
                               ('\n输入按官网等比调整至约 1–3 百万像素。' if method != 'pixel-snap' else ''))
        method_widget = self._fields['augment_method']
        pixel_snap_index = method_widget.findData('pixel-snap')
        if pixel_snap_index >= 0:
            method_widget.setItemText(pixel_snap_index, '像素对齐（暂不支持）')
            method_widget.model().item(pixel_snap_index).setEnabled(False)

        reference_caps = dict(caps)
        if tool_mode:
            reference_caps['vibe'] = reference_caps['precise'] = False
        if action == 'infill':
            reference_caps['vibe'] = False
        self.references.set_capabilities(reference_caps, self.model.currentData())
        mode = self.position_mode.mode()
        placement = ('AI 自动定位' if mode is False else
                     ('手动自由定位' if caps.get('free_coordinates') else '手动网格定位') if mode is True
                     else '角色定位待统一')
        features = [placement]
        if caps.get('vibe'):
            features.append('Vibe')
        if caps.get('precise'):
            features.append('精确参考')
        text = ' · '.join(features)
        unsupported_action = generation and action != 'generate' and not caps.get(action, False)
        if unsupported_action:
            text = '此模型不支持当前模式。请切换模型或生成模式。'
        elif not caps.get('vibe') and not caps.get('precise'):
            text += ' · 不支持参考图'
        incompatible = []
        if generation:
            if self.position_mode.mode() is None:
                incompatible.append('角色定位方式需统一')
            if any(card.enabled.isChecked() and card.needs_grid_alignment() for card in self._characters):
                incompatible.append('角色坐标需对齐网格')
            for key, capability, name in [('quality_preset', 'quality_presets', '质量预设'), ('uc_preset', 'uc_presets', '负面预设')]:
                if self._fields[key].currentData() not in caps.get(capability, []):
                    incompatible.append(name + '需重新选择')
            if self._fields['sampler'].currentData() == 'k_dpm_2' and self._fields['noise_schedule'].currentData() == 'karras':
                incompatible.append('DPM2 需搭配指数噪声调度')
            if not caps.get('noise_schedule') and self._fields['noise_schedule'].currentData() != 'karras':
                incompatible.append('噪声调度需改为 Karras')
            if not caps.get('transparency') and self._fields['transparent_background'].isChecked():
                incompatible.append('需关闭透明背景')
            if not caps.get('variety_boost') and self._fields['variety_boost'].isChecked():
                incompatible.append('需关闭多样性增强')
            if incompatible:
                text += '。已保留原设置：' + '；'.join(incompatible) + '。'
        else:
            text = ('使用官网 V5 Curated 超分模型，每次处理一张底图。' if action == 'upscale' else
                    '使用独立的 Director 图像工具，每次处理一张底图。')
            if action == 'augment' and method == 'pixel-snap':
                incompatible.append('像素对齐暂不支持')
                text = notes['pixel-snap']
        self.capability_note.setText(text)
        self.capability_note.setVisible(bool(incompatible or tool_mode or unsupported_action))
        self.model.setToolTip(text)
        set_tone(self.capability_note, 'warning' if incompatible or unsupported_action else 'muted')
        self._sync_quick_settings()

    def settings(self):
        """Do not normalize widgets during capture; preserve unknown advanced fields."""
        result = deepcopy(self._raw)
        for key, widget in self._fields.items():
            if isinstance(widget, QtWidgets.QTextEdit):
                value = widget.toPlainText()
            elif isinstance(widget, QtWidgets.QComboBox):
                value = _combo_value(widget)
            elif isinstance(widget, QtWidgets.QAbstractSpinBox):
                value = widget.value()
            elif isinstance(widget, QtWidgets.QCheckBox):
                value = widget.isChecked()
            else:
                value = widget.text()
                if key == 'seed':
                    try:
                        value = int(value)
                    except ValueError:
                        value = self._raw.get('seed', -1)
            result[key] = value
        result['characters'] = [card.value() for card in self._characters]
        result['character_position_mode'] = {False: 'auto', True: 'manual', None: 'mixed'}[self.position_mode.mode()]
        result['references'] = self.references.value()
        result['reference_mode'] = self.references.family.currentData()
        return result

    def set_settings(self, settings):
        self.tabs.cancel_pending_focus()
        merged = deepcopy(self._catalog.default_options())
        merged.update(deepcopy(settings or {}))
        if (isinstance(settings, dict) and settings.get('action') == 'augment'
                and 'tool_prompt' not in settings):
            merged['tool_prompt'] = str(settings.get('prompt', '') or '')
        self._loading = True
        try:
            self._raw = merged
            for key, widget in self._fields.items():
                if key not in merged:
                    continue
                value = merged[key]
                with QtCore.QSignalBlocker(widget):
                    if isinstance(widget, QtWidgets.QTextEdit):
                        widget.setPlainText(str(value or ''))
                    elif isinstance(widget, QtWidgets.QComboBox):
                        _select(widget, value)
                    elif isinstance(widget, QtWidgets.QAbstractSpinBox):
                        widget.setValue(value)
                    elif isinstance(widget, QtWidgets.QCheckBox):
                        widget.setChecked(bool(value))
                    else:
                        widget.setText(str(value))
            for card in self._characters:
                if self._active_prompt in (card.prompt, card.negative):
                    self._active_prompt = self.prompt
                self.characters_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
            self._characters = []
            self._capabilities_changed()
            for value in merged.get('characters', []):
                if isinstance(value, dict):
                    self._add_character(value, emit=False)
            states = {card._use_coords for card in self._characters if card.enabled.isChecked()}
            # Per-character flags are authoritative for old presets and copied
            # tasks. The UI-only preference matters when no characters exist.
            positioning = (None if len(states) > 1 else next(iter(states)) if states else
                           merged.get('character_position_mode') == 'manual')
            self.position_mode.set_mode(positioning)
            self.references.set_value(merged.get('references', []),
                                      family=settings.get('reference_mode') if isinstance(settings, dict) else None)
            self._references_changed()
            self._update_character_count()
            self._action_changed()
        finally:
            self._loading = False
        self._sync_quick_settings()

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.reset_defaults_button.setEnabled(not self._busy)
        self.tabs.setEnabled(not self._busy)
        if not busy:
            self._capabilities_changed()
        self._sync_quick_settings()

    def apply_theme(self, mode):
        self._mode = mode
        colors = palette(mode)
        self.setStyleSheet(app_stylesheet(mode).replace('#rhAppPage', '#novelaiControls') +
            editor_stylesheet(mode, 'novelaiControls') + f'''
            QWidget#novelaiControls {{background:{colors['surface']};border:none;border-radius:0;}}
            QWidget#novelaiControls QLabel#novelaiSettingsTitle {{color:{colors['muted']};font-size:12px;font-weight:600;}}
            QWidget#novelaiControls QPushButton#novelaiResetDefaults {{background:transparent;color:{colors['muted']};
                border:1px solid {colors['border']};border-radius:4px;padding:4px 7px;font-size:11px;}}
            QWidget#novelaiControls QPushButton#novelaiResetDefaults:hover {{background:{colors['hover']};color:{colors['accent']};}}
            QWidget#novelaiControls QPushButton#novelaiResetDefaults:disabled {{color:{colors['border']};}}
            QWidget#novelaiControls QScrollArea#novelaiControlsScroll,
            QWidget#novelaiControls QWidget#novelaiContinuousControls {{background:{colors['surface']};border:none;}}
            QWidget#novelaiControls QToolButton#novelaiSectionTitle {{background:transparent;
                border:none;border-bottom:1px solid {colors['border']};border-radius:0;
                text-align:left;font-size:12px;font-weight:600;padding:7px 0;}}
            QWidget#novelaiControls QToolButton#novelaiSectionTitle:hover {{color:{colors['accent']};}}
            QWidget#novelaiControls QToolButton#novelaiSeedReset {{background:{colors['input']};color:{colors['muted']};
                border:1px solid {colors['border']};border-radius:4px;padding:0;font-size:16px;}}
            QWidget#novelaiControls QToolButton#novelaiSeedReset:hover {{background:{colors['hover']};color:{colors['accent']};}}
            QWidget#novelaiControls QToolButton#novelaiSeedReset:disabled {{color:{colors['border']};}}
            QWidget#novelaiControls QFrame#novelaiCharacterCard {{background:{colors['surface']};
                border:1px solid {colors['border']};border-radius:4px;}}
            QWidget#novelaiControls QFrame#novelaiCharacterCard:hover {{border-color:{colors['muted']};}}
            QWidget#novelaiControls QToolButton#novelaiCharacterTitle {{background:transparent;color:{colors['text']};border:none;padding:4px 0;text-align:left;font-size:12px;font-weight:600;}}
            QWidget#novelaiControls QToolButton#novelaiCharacterTitle:hover {{color:{colors['accent']};}}
            QWidget#novelaiControls QCheckBox#novelaiCharacterEnabled {{padding:0;spacing:0;}}
            QWidget#novelaiControls QLineEdit#novelaiCharacterName {{border:none;border-bottom:1px solid {colors['border']};
                border-radius:0;background:transparent;padding:2px 3px;min-height:20px;font-size:11px;}}
            QWidget#novelaiControls QLabel#novelaiPromptHeading {{color:{colors['text']};font-size:12px;font-weight:600;}}
            QWidget#novelaiControls QTextEdit[naiCompactPrompt="true"] {{padding:1px 5px;font-size:12px;border-radius:4px;}}
            QWidget#novelaiControls QLabel#novelaiMuted[tone="warning"],
            QWidget#novelaiControls QLabel#novelaiCapability[tone="warning"] {{color:{colors['warning']};}}
            QWidget#novelaiControls QLabel#novelaiPositionBadge {{background:{colors['accent_soft']};
                color:{colors['accent']};border-radius:4px;padding:2px 5px;font-size:10px;}}
            QWidget#novelaiControls QPushButton#novelaiPositionCanvasButton {{background:{colors['accent_soft']};
                color:{colors['accent']};border:1px solid {colors['border']};font-weight:600;}}
            QWidget#novelaiControls QPushButton#novelaiPositionCanvasButton:hover {{border-color:{colors['accent']};}}
            QWidget#novelaiControls QPushButton#novelaiPositionCanvasButton:disabled {{background:{colors['input']};
                color:{colors['muted']};}}
            QWidget#novelaiControls QTabWidget::pane {{border:none;background:transparent;}}
            QWidget#novelaiControls QTabBar {{background:transparent;border:none;border-radius:0;}}
            QWidget#novelaiControls QTabBar::tab {{background:transparent;color:{colors['muted']};
                padding:5px 8px;font-size:11px;border:none;border-bottom:2px solid transparent;}}
            QWidget#novelaiControls QTabBar::tab:first {{border-top-left-radius:3px;}}
            QWidget#novelaiControls QTabBar::tab:last {{border-top-right-radius:3px;}}
            QWidget#novelaiControls QTabBar::tab:hover {{color:{colors['text']};background:{colors['hover']};}}
            QWidget#novelaiControls QTabBar::tab:selected {{color:{colors['accent']};font-weight:600;
                border-bottom-color:{colors['accent']};background:{colors['accent_soft']};}}
        ''')
        self.references.apply_theme(mode)
        self.position_mode.apply_theme(mode)
        self.quick_settings.apply_theme(mode)
