"""Choose a local image's purpose without changing the editor or networking."""
import copy
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from .catalog import MODELS
from .storage import import_options


PROMPT_FIELDS = frozenset({
    'prompt', 'negative_prompt', 'characters', 'character_position_mode',
    'quality_preset', 'uc_preset', 'chunks',
})
GENERATION_FIELDS = frozenset({
    'model', 'width', 'height', 'steps', 'scale', 'sampler', 'noise_schedule',
    'n_samples', 'cfg_rescale', 'strength', 'noise', 'transparent_background',
    'straight_alpha', 'variety_boost', 'color_correct', 'add_original_image',
    'inpaint_strength', 'extra_noise_seed', 'skip_cfg_above_sigma',
    'dynamic_thresholding', 'sm', 'sm_dyn', 'prefer_brownian',
    'normalize_reference_strength_multiple',
})


class _PurposeChoice(QtWidgets.QFrame):
    """Let the description and free space select the same radio choice."""

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.rect().contains(event.pos()):
            if self.button.isEnabled():
                self.button.click()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ImageImportDialog(QtWidgets.QDialog):
    """Return ``image``, ``vibe``, ``precise`` or ``parameters`` on acceptance.

    ``reference_capabilities`` describes the *current editor*, not the model
    saved in the image. It accepts ``vibe``/``precise`` booleans and optional
    ``vibe_reason``/``precise_reason`` text for unavailable choices. Missing
    capabilities disable reference choices. ``parameters_only`` prevents every
    image action, including when the image has no readable metadata. With
    ``image_count > 1``, reference actions apply to the full caller selection;
    image and parameter actions apply to the first path only.

    Only ``parameters`` uses ``selected_options()``. The caller merges that
    patch; paths, references, request action and connection settings are never
    imported. ``actual_options`` may contain resolved history prompts.
    """

    def __init__(self, path, options=None, parent=None, *, mode='dark',
                 actual_options=None, reference_capabilities=None,
                 parameters_only=False, image_count=1):
        super().__init__(parent)
        self.result_action = ''
        self._parameters_only = bool(parameters_only)
        self._options = import_options(copy.deepcopy(options) if options is not None else {})
        self._actual = import_options(copy.deepcopy(actual_options)) if actual_options is not None else copy.deepcopy(self._options)
        caps = reference_capabilities if isinstance(reference_capabilities, dict) else {}
        prompt_available = bool(PROMPT_FIELDS & (self._options.keys() | self._actual.keys()))
        generation_available = bool(GENERATION_FIELDS & self._options.keys())
        seed = self._options.get('seed')
        valid_seed = isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed <= 4294967295
        self._has_parameters = prompt_available or generation_available or valid_seed
        self.setObjectName('novelaiImageImportDialog')
        self.setWindowTitle('NovelAI · 读取图片参数' if self._parameters_only else 'NovelAI · 导入图片')
        self.setMinimumWidth(320)
        self.resize(480, 630 if self._has_parameters and not self._parameters_only else 480)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        title = QtWidgets.QLabel('读取图片生成参数' if self._parameters_only else '这张图片要怎么用？')
        title.setObjectName('naiImportTitle')
        title.setWordWrap(True)
        root.addWidget(title)
        self.filename = QtWidgets.QLabel(Path(path).name)
        self.filename.setObjectName('naiImportMuted')
        self.filename.setTextFormat(QtCore.Qt.PlainText)
        self.filename.setWordWrap(True)
        self.filename.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.filename.setToolTip(str(path))
        root.addWidget(self.filename)
        if image_count > 1:
            selection_hint = QtWidgets.QLabel(
                f'已选择 {image_count} 张图片，仅从第一张读取生成参数。' if self._parameters_only else
                f'已选择 {image_count} 张图片。参考图用途将导入全部图片；底图和参数只使用第一张。')
            selection_hint.setObjectName('naiImportMuted')
            selection_hint.setWordWrap(True)
            selection_hint.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            root.addWidget(selection_hint)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setObjectName('naiImportScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        content = QtWidgets.QWidget()
        content.setObjectName('naiImportContent')
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 0, 4, 0)
        body.setSpacing(10)
        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)

        self.action_group = QtWidgets.QButtonGroup(self)
        self.action_buttons = {}
        self.action_hints = {}
        self.action_rows = {}
        self._action_available = {
            'image': not self._parameters_only,
            'vibe': not self._parameters_only and bool(caps.get('vibe')),
            'precise': not self._parameters_only and bool(caps.get('precise')),
            'parameters': self._has_parameters,
        }
        choices = (
            ('image', '作为底图', '用这张图片开始图生图，继续调整或重绘。'),
            ('vibe', '作为 Vibe 参考图', '借用画风、色彩和氛围，添加到参考图面板。'),
            ('precise', '作为 Precise 参考图', '用于更精确的人物或画风参考，添加后可调整类型。'),
            ('parameters', '读取生成参数', '选择要导入的提示词、生成设置和种子。'
             if self._has_parameters else '图片没有可读取的 NovelAI 生成参数。'),
        )
        for action, label, hint in choices:
            if action in ('vibe', 'precise') and not self._action_available[action]:
                hint = str(caps.get(action + '_reason') or f'当前模型或生成模式不支持 {label[3:]}。')
            row = _PurposeChoice()
            row.setObjectName('naiImportChoice')
            row_layout = QtWidgets.QVBoxLayout(row)
            row_layout.setContentsMargins(12, 8, 12, 8)
            row_layout.setSpacing(3)
            radio = QtWidgets.QRadioButton(label)
            radio.setMinimumHeight(25)
            radio.setProperty('importAction', action)
            radio.setEnabled(self._action_available[action])
            radio.setToolTip(hint)
            radio.setAccessibleDescription(hint)
            row.button = radio
            self.action_group.addButton(radio)
            self.action_buttons[action] = radio
            self.action_rows[action] = row
            row_layout.addWidget(radio)
            description = QtWidgets.QLabel(hint)
            description.setObjectName('naiImportMuted')
            description.setTextFormat(QtCore.Qt.PlainText)
            description.setWordWrap(True)
            description.setMinimumWidth(0)
            description.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            row_layout.addWidget(description)
            self.action_hints[action] = description
            body.addWidget(row)
            row.setVisible(not self._parameters_only or action == 'parameters')
        self.image_button = self.action_buttons['image']

        self.parameters_frame = QtWidgets.QFrame()
        self.parameters_frame.setObjectName('naiImportFields')
        fields = QtWidgets.QVBoxLayout(self.parameters_frame)
        fields.setContentsMargins(14, 12, 14, 12)
        fields.setSpacing(8)
        names = {item['id']: item['name'] for item in MODELS}
        model = self._options.get('model', '')
        model = model if isinstance(model, str) else ''
        summary = names.get(model, model)[:180]
        if self._options.get('width') and self._options.get('height'):
            summary += (' · ' if summary else '') + f"{self._options['width']} × {self._options['height']}"
        if summary:
            label = QtWidgets.QLabel(summary)
            label.setObjectName('naiImportMuted')
            label.setTextFormat(QtCore.Qt.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            fields.addWidget(label)

        self.prompt_check = self._check('提示词与角色', '包含负面提示词、角色位置及提示词预设。', fields)
        self.prompt_check.setChecked(prompt_available)
        self.prompt_check.setEnabled(prompt_available)

        self.prompt_version = QtWidgets.QComboBox()
        self.prompt_version.setAccessibleName('导入的提示词版本')
        self.prompt_version.addItem('实际生成提示词', 'actual')
        self.prompt_version.addItem('原始编辑提示词', 'original')
        self.prompt_version.setToolTip('实际版本保留本次随机词结果，并关闭质量和负面预设以避免重复添加。')
        has_actual = any(key in self._options for key in (
            'resolved_prompt', 'resolved_negative_prompt', 'resolved_characters', 'resolved_negative_characters'))
        differs = has_actual and any(self._actual.get(key) != self._options.get(key) for key in PROMPT_FIELDS)
        self.prompt_version.setVisible(differs)
        self._has_actual_choice = differs
        if not differs:
            self.prompt_version.setCurrentIndex(1)
        self.prompt_check.toggled.connect(self.prompt_version.setEnabled)
        self.prompt_version.setEnabled(prompt_available)
        fields.addWidget(self.prompt_version)

        self.generation_check = self._check('模型与生成设置', '包含尺寸、采样器、步数、引导强度等参数。', fields)
        self.generation_check.setChecked(generation_available)
        self.generation_check.setEnabled(generation_available)
        self.seed_check = self._check('种子', str(seed) if valid_seed else '图像没有记录可复用的种子。', fields)
        self.seed_check.setEnabled(valid_seed)
        body.addWidget(self.parameters_frame)
        hint = QtWidgets.QLabel('导入参数会保留当前底图、遮罩和参考图。')
        hint.setObjectName('naiImportMuted')
        hint.setWordWrap(True)
        fields.addWidget(hint)
        body.addStretch(1)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        self.cancel_button = QtWidgets.QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        self.import_button = QtWidgets.QPushButton()
        self.import_button.setObjectName('naiImportPrimary')
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(lambda: self._choose(self.selected_action()))
        buttons.addWidget(self.import_button)
        root.addLayout(buttons)
        for check in (self.prompt_check, self.generation_check, self.seed_check):
            check.toggled.connect(self._update_import_enabled)
        for radio in self.action_buttons.values():
            radio.toggled.connect(self._update_import_enabled)
        initial = 'parameters' if self._has_parameters or self._parameters_only else 'image'
        self.action_buttons[initial].setChecked(True)
        self._update_import_enabled()
        self.apply_theme(mode)

    @staticmethod
    def _check(text, hint, layout):
        checkbox = QtWidgets.QCheckBox(text)
        checkbox.setMinimumHeight(26)
        checkbox.setToolTip(hint)
        layout.addWidget(checkbox)
        label = QtWidgets.QLabel(hint)
        label.setObjectName('naiImportMuted')
        label.setTextFormat(QtCore.Qt.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(label)
        return checkbox

    def selected_action(self):
        button = self.action_group.checkedButton()
        return button.property('importAction') if button is not None else ''

    def _update_import_enabled(self):
        action = self.selected_action()
        for name, row in self.action_rows.items():
            active = name == action
            if row.property('active') != active:
                row.setProperty('active', active)
                row.style().unpolish(row)
                row.style().polish(row)
                row.update()
        self.parameters_frame.setVisible(action == 'parameters' and self._has_parameters)
        labels = {'image': '设为底图', 'vibe': '添加 Vibe 参考',
                  'precise': '添加 Precise 参考', 'parameters': '导入所选参数'}
        self.import_button.setText(labels.get(action, '确认用途'))
        enabled = self._action_available.get(action, False)
        if action == 'parameters':
            enabled = enabled and any(check.isEnabled() and check.isChecked()
                                      for check in (self.prompt_check, self.generation_check, self.seed_check))
        self.import_button.setEnabled(enabled)

    def _choose(self, action):
        if not self._action_available.get(action, False):
            return
        if self._parameters_only and action != 'parameters':
            return
        if action == 'parameters' and not any(check.isEnabled() and check.isChecked()
                for check in (self.prompt_check, self.generation_check, self.seed_check)):
            return
        self.result_action = action
        self.accept()

    def reject(self):
        self.result_action = ''
        super().reject()

    def select_parameters(self):
        """Select parameter import; use ``parameters_only`` to restrict actions."""
        self.action_buttons['parameters'].setChecked(True)
        self._update_import_enabled()
        self.import_button.setFocus(QtCore.Qt.OtherFocusReason)

    def selected_options(self):
        """Return only selected, non-sensitive editor fields after acceptance."""
        if self.result() != self.Accepted or self.result_action != 'parameters':
            return {}
        patch = {}
        if self.prompt_check.isEnabled() and self.prompt_check.isChecked():
            source = self._actual if self._has_actual_choice and self.prompt_version.currentData() == 'actual' else self._options
            patch.update({key: copy.deepcopy(source[key]) for key in PROMPT_FIELDS if key in source})
        if self.generation_check.isEnabled() and self.generation_check.isChecked():
            patch.update({key: copy.deepcopy(self._options[key]) for key in GENERATION_FIELDS if key in self._options})
        if self.seed_check.isEnabled() and self.seed_check.isChecked():
            patch['seed'] = self._options['seed']
        return patch

    def selected_fields(self):
        return set(self.selected_options())

    def apply_theme(self, mode):
        from .styles import workspace_palette as palette
        p = palette(mode)
        self.setStyleSheet(f'''
            QDialog#novelaiImageImportDialog {{background:{p['canvas']};color:{p['text']};}}
            QDialog#novelaiImageImportDialog QLabel {{color:{p['text']};background:transparent;border:none;font-size:12px;}}
            QDialog#novelaiImageImportDialog QLabel#naiImportTitle {{font-size:18px;font-weight:600;}}
            QDialog#novelaiImageImportDialog QLabel#naiImportMuted {{color:{p['muted']};font-size:12px;}}
            QDialog#novelaiImageImportDialog QScrollArea#naiImportScroll,
            QDialog#novelaiImageImportDialog QWidget#naiImportContent {{background:transparent;border:none;}}
            QDialog#novelaiImageImportDialog QFrame#naiImportChoice {{background:{p['surface']};border:1px solid {p['border']};border-radius:7px;}}
            QDialog#novelaiImageImportDialog QFrame#naiImportChoice[active="true"] {{background:{p['accent_soft']};border-color:{p['accent']};}}
            QDialog#novelaiImageImportDialog QFrame#naiImportFields {{background:{p['surface']};border:1px solid {p['border']};border-radius:9px;}}
            QDialog#novelaiImageImportDialog QCheckBox,
            QDialog#novelaiImageImportDialog QRadioButton {{color:{p['text']};background:transparent;border:none;font-size:13px;spacing:8px;}}
            QDialog#novelaiImageImportDialog QCheckBox:disabled,
            QDialog#novelaiImageImportDialog QRadioButton:disabled {{color:{p['muted']};}}
            QDialog#novelaiImageImportDialog QRadioButton::indicator {{width:13px;height:13px;border:1px solid {p['muted']};border-radius:7px;background:{p['input']};}}
            QDialog#novelaiImageImportDialog QRadioButton::indicator:checked {{border-color:{p['accent']};background:{p['accent']};}}
            QDialog#novelaiImageImportDialog QRadioButton::indicator:disabled {{border-color:{p['border']};background:{p['input']};}}
            QDialog#novelaiImageImportDialog QComboBox {{background:{p['input']};color:{p['text']};border:1px solid {p['border']};border-radius:5px;padding:6px 8px;min-height:20px;}}
            QDialog#novelaiImageImportDialog QComboBox QAbstractItemView {{background:{p['surface']};color:{p['text']};selection-background-color:{p['accent_soft']};selection-color:{p['text']};}}
            QDialog#novelaiImageImportDialog QPushButton {{background:{p['surface']};color:{p['text']};border:1px solid {p['border']};border-radius:6px;padding:8px 14px;min-height:18px;font-size:12px;}}
            QDialog#novelaiImageImportDialog QPushButton:hover {{background:{p['hover']};border-color:{p['accent']};}}
            QDialog#novelaiImageImportDialog QPushButton:focus {{border-color:{p['accent']};}}
            QDialog#novelaiImageImportDialog QPushButton#naiImportPrimary {{background:{p['accent']};border-color:{p['accent']};color:{'#242333' if mode != 'light' else '#ffffff'};font-weight:600;}}
            QDialog#novelaiImageImportDialog QPushButton:disabled {{background:{p['input']};color:{p['muted']};border-color:{p['border']};}}
        ''')
