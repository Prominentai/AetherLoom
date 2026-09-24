"""Independent NovelAI node forms, sharing the website-compatible editors."""
import copy

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.novelai import catalog
from aetherloom_core.novelai.controls import DrawingControls
from aetherloom_core.novelai.prompt_editor import NovelAIPromptEdit
from aetherloom_core.novelai.suggestions import TagSuggestions
from aetherloom_core.prompt_history import PromptHistory
from aetherloom_core.rh_parameters import RhNumberSpinBox, RhEnumComboBox
from .inline_text import bind_document
from .node_form import FieldLabel, FoldSection, NumberDrag


_SCALARS = {
    'width': ('宽度', True, 64, 4096, 64), 'height': ('高度', True, 64, 4096, 64),
    'steps': ('步数', True, 1, 50, 1), 'scale': ('提示词引导', False, 0, 1000000, .5),
    'seed': ('种子', True, -1, 4294967295, 1), 'n_samples': ('图像张数', True, 1, 8, 1),
    'strength': ('重绘强度', False, 0, 1, .05), 'noise': ('附加噪声', False, 0, 1, .05),
    'inpaint_strength': ('遮罩内强度', False, 0, 1, .05),
}


def _options(inspector):
    from .novelai_nodes import local_options
    return local_options(inspector.node)


def _update(inspector, **changes):
    values = _options(inspector)
    values.update(copy.deepcopy(changes))
    inspector.changed.emit('params.options', values)


def _bind_prompt(inspector, editor, key):
    identity = (inspector.doc_id, inspector.node['id'], 'options.' + key)
    bind_document(editor, _options(inspector).get(key, ''), identity, inspector.histories)
    editor.setProperty('canvasTextKey', 'options.' + key)
    editor.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
    editor.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
    editor.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    editor._canvas_prompt_lock = _PromptLock(editor)
    if inspector.embedded and inspector.model_owner is not None:
        popup = editor._popup
        popup.setParent(inspector.model_owner, popup.windowFlags())
        popup.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        editor.destroyed.connect(popup.deleteLater)
    return identity


class _PromptLock(QtCore.QObject):
    """Connected fields remain read-only in their expanded shared-document view."""
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.original_readonly = editor.isReadOnly()
        editor.installEventFilter(self)
        self.sync()

    def sync(self):
        editor = self.editor
        enabled = editor.isEnabled()
        editor.setReadOnly(self.original_readonly or not enabled)
        parent = editor.parentWidget()
        layout = parent.layout() if parent is not None else None
        index = layout.indexOf(editor) if layout is not None else -1
        if index > 0:
            header = layout.itemAt(index - 1).widget()
            if header is not None:
                for button in header.findChildren(QtWidgets.QToolButton):button.setEnabled(enabled)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.EnabledChange:self.sync()
        return False


def _suggestions(inspector, controls):
    from aetherloom_core.novelai.preferences import normalize_suggestion_preferences
    from aetherloom_core.novelai import storage
    from aetherloom_core.paths import current_dir
    inspector.owner = inspector.model_owner
    inspector.controls = controls
    page = getattr(getattr(inspector.model_owner, 'novelai_page', None), 'workspace', None)
    values = (getattr(page, 'prompt_suggestion_preferences', None) if page is not None
              else storage.load_settings(current_dir).get('prompt_suggestions'))
    inspector.prompt_suggestion_preferences = normalize_suggestion_preferences(values)
    inspector._novelai_suggestions = TagSuggestions(inspector)
    inspector.destroyed.connect(inspector._novelai_suggestions.close)


class _InlinePrompts(QtWidgets.QWidget):
    tagPrefixChanged = QtCore.pyqtSignal(object, str)

    def __init__(self, inspector):
        super().__init__(inspector)
        self.hide()
        values = _options(inspector)
        self.model = RhEnumComboBox(self)
        self.model.addItem(values['model'], values['model'])
        self.dataset_mode = RhEnumComboBox(self)
        self.dataset_mode.addItem(values.get('dataset_mode', 'anime'), values.get('dataset_mode', 'anime'))
        self._active = None

    def bind(self, editor):
        editor.activated.connect(lambda current: setattr(self, '_active', current))
        editor.prefixChanged.connect(self.tagPrefixChanged)
        editor.tagSuggestionsRequested.connect(lambda current: current.show_suggestions())
        if self._active is None:self._active = editor

    def active_prompt_editor(self):
        return self._active


class _ThemeFollower(QtCore.QObject):
    def __init__(self, inspector, controls):
        super().__init__(inspector)
        self.inspector, self.controls = inspector, controls
        self.mode = None
        inspector.installEventFilter(self)
        self.refresh()

    def refresh(self):
        mode = getattr(self.inspector.model_owner, '_theme_mode', 'dark')
        if self.mode == mode:return
        self.mode = mode
        from .appearance import canvas_palette
        from aetherloom_core.rh_ui import palette
        colors = canvas_palette(palette(mode))
        self.controls.content.setStyleSheet('''
            QWidget#canvasNovelAIContent {background:transparent;}
            QWidget#canvasNovelAIContent QToolButton#novelaiIconButton,
            QWidget#canvasNovelAIContent QToolButton#canvasTextHistoryButton {
                min-width:0;min-height:0;padding:0;border:none;background:transparent;font-size:12px;
            }
            QWidget#canvasNovelAIContent QToolButton#novelaiSectionTitle {
                text-align:left;padding:5px 1px;min-height:22px;background:transparent;border:none;
                border-bottom:1px solid ''' + colors['border'] + ''';
            }
            QWidget#canvasNovelAIContent QLabel#novelaiPromptHeading {font-weight:600;}
            QWidget#canvasNovelAIContent QLabel#novelaiMuted,
            QWidget#canvasNovelAIContent QLabel#novelaiCapability {
                font-size:11px;color:''' + colors['muted'] + ''';background:transparent;
            }
        ''')
        self.controls.references.apply_theme(mode)
        if hasattr(self.controls.position_mode, 'apply_theme'):
            self.controls.position_mode.apply_theme(mode)

    def eventFilter(self, obj, event):
        if event.type() in (QtCore.QEvent.PaletteChange, QtCore.QEvent.StyleChange, QtCore.QEvent.Show):
            self.refresh()
        return False


def _prompt(inspector, port, field, title, facade):
    label = FieldLabel(title, inspector)
    inspector.port_widgets[port] = label
    row = QtWidgets.QHBoxLayout();row.setSpacing(3);row.addWidget(label, 1)
    back, forward = QtWidgets.QToolButton(), QtWidgets.QToolButton()
    for button, text, tip in ((back, '↶', '回退文本记录'), (forward, '↷', '前进文本记录')):
        button.setText(text);button.setToolTip(tip);button.setFixedSize(25, 22)
        button.setObjectName('canvasTextHistoryButton');row.addWidget(button)
    inspector.form.addLayout(row)
    editor = NovelAIPromptEdit(inspector, minimum_height=74, maximum_height=136, compact=True)
    editor.setPlaceholderText('输入 NovelAI 标签或描述…')
    identity = _bind_prompt(inspector, editor, field)
    editor.setEnabled(port not in inspector.connected_inputs)
    back.setEnabled(editor.isEnabled());forward.setEnabled(editor.isEnabled())
    PromptHistory(editor, back, forward, inspector.histories.setdefault(identity, []))
    editor.textChanged.connect(lambda: _update(inspector, **{field: editor.toPlainText()}))
    inspector.form.addWidget(editor)
    facade.bind(editor)
    return editor


def _file(inspector, port, field, title):
    label = FieldLabel(title, inspector);inspector.form.addWidget(label)
    inspector.port_widgets[port] = label
    row = QtWidgets.QHBoxLayout();row.setSpacing(4)
    edit = QtWidgets.QLineEdit(str(_options(inspector).get(field, '')))
    edit.setMinimumWidth(0);edit.setPlaceholderText('本地路径，或拖入图像')
    edit.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
    edit.setToolTip('连接输入会覆盖此路径；断开后恢复。底图每次接收一张，List 逐项运行。')
    choose = QtWidgets.QToolButton();choose.setText('选择')
    enabled = port not in inspector.connected_inputs
    edit.setEnabled(enabled);choose.setEnabled(enabled)
    def accept(paths):
        if not paths:return False
        edit.setText(str(paths[0]));_update(inspector, **{field: str(paths[0])});return True
    def pick():
        path, _ = QtWidgets.QFileDialog.getOpenFileName(inspector.dialog_parent, '选择' + title, '',
            '图像 (*.png *.jpg *.jpeg *.webp *.bmp)')
        if path:accept([path])
    choose.clicked.connect(pick)
    edit.editingFinished.connect(lambda: _update(inspector, **{field: edit.text().strip()}))
    from aetherloom_core.image_import import ImageDropFilter
    edit._drop_filter = ImageDropFilter(edit, accept)
    row.addWidget(edit, 1);row.addWidget(choose);inspector.form.addLayout(row)
    inspector.novelai_fields[port] = edit
    return edit


def _scalar(inspector, key):
    label, integer, minimum, maximum, step = _SCALARS[key]
    row_widget = QtWidgets.QFrame(inspector);row_widget.setObjectName('canvasScalarRow')
    row = QtWidgets.QHBoxLayout(row_widget);row.setContentsMargins(7, 0, 3, 0);row.setSpacing(5)
    caption = FieldLabel(label, row_widget);caption.setObjectName('canvasFieldLabel')
    edit = RhNumberSpinBox(integer=integer)
    edit.configure({'min': minimum, 'max': maximum, 'step': step})
    try:edit.setValue(_options(inspector).get(key, minimum))
    except ValueError:
        # Imported incompatible values remain visible until explicitly corrected.
        edit.lineEdit().setText(str(_options(inspector).get(key, '')))
    edit.setEnabled(key not in inspector.connected_inputs)
    edit.setMinimumWidth(0);edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    edit.setAlignment(QtCore.Qt.AlignRight)
    edit.valueChanged.connect(lambda value: _update(inspector, **{key: int(value) if integer else float(value)}))
    row.addWidget(caption);row.addWidget(edit, 1)
    if key == 'seed':
        reset = QtWidgets.QToolButton();reset.setText('↺');reset.setToolTip('随机种子（-1）')
        reset.setEnabled(edit.isEnabled());reset.clicked.connect(lambda: edit.setValue(-1));row.addWidget(reset)
    caption._drag = NumberDrag(caption, edit)
    inspector.port_widgets[key] = row_widget
    inspector.numeric.append(edit);inspector.form.addWidget(row_widget)
    inspector.novelai_fields[key] = edit


def _open_details(inspector, tab=0, section=None):
    parent = inspector.parentWidget()
    while parent is not None:
        page = getattr(parent, 'page', None)
        if page is not None and callable(getattr(page, '_open_settings', None)):
            page._open_settings(inspector.node['id'])
            panel = page._inspector
            if panel is not None and panel.tabs is not None:
                panel.tabs.setCurrentIndex(tab)
                controls = getattr(panel, 'novelai_controls', None)
                target = getattr(controls, section + '_section', None) if section else None
                if target is not None:
                    target.toggle.setChecked(True)
                    scroll = panel.tabs.widget(tab)
                    scroll.widget().layout().activate()
                    scroll.ensureWidgetVisible(target)
            return
        parent = parent.parentWidget()


def _choice(inspector, label, key, choices):
    row = QtWidgets.QHBoxLayout();row.setSpacing(5)
    caption = FieldLabel(label, inspector);row.addWidget(caption)
    combo = RhEnumComboBox();combo.setMinimumWidth(0)
    combo.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
    for title, value in choices:combo.addItem(title, value)
    value = _options(inspector).get(key);index = combo.findData(value)
    if index < 0:combo.addItem(str(value), value);index = combo.count() - 1
    combo.setCurrentIndex(index);row.addWidget(combo, 1);inspector.form.addLayout(row)
    combo.currentIndexChanged.connect(lambda: _update(inspector, **{key: combo.currentData()}))
    inspector.novelai_fields[key] = combo
    return combo


def build_inline(inspector, node, doc_id, edges):
    from .novelai_nodes import inputs
    inspector.novelai_fields = {}
    values = _options(inspector)
    facade = _InlinePrompts(inspector)
    row = QtWidgets.QHBoxLayout();row.setSpacing(4)
    model_name = next((item['name'].replace('NAI Diffusion ', '') for item in catalog.MODELS
                       if item['id'] == values['model']), values['model'])
    label = FieldLabel(model_name if node['kind'] not in ('novelai_upscale', 'novelai_augment') else
                       ('V5 Curated · 2×' if node['kind'] == 'novelai_upscale' else 'Director'), inspector)
    label.setObjectName('canvasModelSummary');row.addWidget(label, 1)
    details = QtWidgets.QToolButton();details.setText('模型与参数');details.clicked.connect(lambda: _open_details(inspector, 1))
    row.addWidget(details);inspector.form.addLayout(row)
    ports = {item['key']: item for item in inputs(node)}
    if node['kind'] == 'novelai_augment':
        method = _choice(inspector, '处理方式', 'augment_method', [
            ('上色', 'colorize'), ('去除背景', 'bg-removal'), ('提取线稿', 'lineart'),
            ('提取草图', 'sketch'), ('改变表情', 'emotion'), ('清理画面', 'declutter'),
            ('清理画面 · 保留气泡', 'declutter-keep-bubbles')])
    if node['kind'] == 'novelai_upscale':
        _choice(inspector, '模糊系数', 'declared_blur_sigma', [('关闭', 0.)] +
                [(str(value), value) for value in (.3, .35, .4, .45, .5)])
    for port, field, title in (('image', 'image_path', '底图'), ('mask', 'mask_path', '遮罩')):
        if port in ports:_file(inspector, port, field, title)
    if 'prompt' in ports:
        field = 'tool_prompt' if node['kind'] == 'novelai_augment' else 'prompt'
        inspector.novelai_fields['prompt'] = _prompt(inspector, 'prompt', field, '提示词', facade)
        if node['kind'] == 'novelai_augment':
            def guard_tool_prompt():
                inspector.novelai_fields['prompt'].setEnabled(
                    'prompt' not in inspector.connected_inputs and method.currentData() in ('colorize', 'emotion'))
            method.currentIndexChanged.connect(guard_tool_prompt);guard_tool_prompt()
    if 'negative' in ports:
        fold = FoldSection('负面提示词', inspector, inspector.histories, (doc_id, node['id'], 'nai_negative'))
        inspector.form.addWidget(fold);original = inspector.form;inspector.form = fold.body_layout
        inspector.novelai_fields['negative'] = _prompt(inspector, 'negative', 'negative_prompt', '负面提示词', facade)
        inspector.port_widgets['negative'].hide()
        inspector.port_widgets['negative'] = fold.toggle
        inspector.form = original
    for key in _SCALARS:
        if key in ports:_scalar(inspector, key)
    if node['kind'] not in ('novelai_augment', 'novelai_upscale'):
        _choice(inspector, '采样器', 'sampler', [(name.replace('k_', '').replace('_', ' ').upper(), name)
                                                  for name in catalog.SAMPLERS])
    if 'references' in ports:
        label = FieldLabel('参考图', inspector);inspector.port_widgets['references'] = label
        row = QtWidgets.QHBoxLayout();row.addWidget(label, 1)
        count = len(values.get('references') or [])
        button = QtWidgets.QToolButton();button.setText(f'{count} 张 · 设置' if count else '添加参考图')
        button.setEnabled('references' not in inspector.connected_inputs)
        button.setToolTip('在参数分页设置 Vibe / 精确参考；连接 Batch 时一次请求使用多张参考图。')
        button.clicked.connect(lambda: _open_details(inspector, section='references'));row.addWidget(button);inspector.form.addLayout(row)
    if node['kind'] not in ('novelai_upscale', 'novelai_augment'):
        row = QtWidgets.QHBoxLayout();row.setSpacing(4)
        stream = QtWidgets.QCheckBox('实时预览');stream.setChecked(bool(values.get('stream')))
        stream.toggled.connect(lambda value: _update(inspector, stream=value));row.addWidget(stream)
        row.addStretch(1)
        roles = QtWidgets.QToolButton();roles.setText(f"角色 {len(values.get('characters') or [])} · 高级")
        roles.clicked.connect(lambda: _open_details(inspector, section='character'));row.addWidget(roles);inspector.form.addLayout(row)
    _suggestions(inspector, facade)
    inspector.form.addStretch(1)


def _tabs(inspector):
    tabs = QtWidgets.QTabWidget();tabs.setObjectName('canvasNodeSettingsTabs');tabs.setDocumentMode(True)
    tabs.setUsesScrollButtons(True);tabs.tabBar().setExpanding(True);tabs.tabBar().setDrawBase(False)
    layouts = []
    for title in ('参数', '模型', '其他', '结果'):
        scroll = QtWidgets.QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setObjectName('canvasNodeTabScroll');scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        content = QtWidgets.QWidget();content.setObjectName('canvasNodeTabContent')
        layout = QtWidgets.QVBoxLayout(content);layout.setContentsMargins(3, 12, 5, 10);layout.setSpacing(10)
        scroll.setWidget(content);tabs.addTab(scroll, title);layouts.append(layout)
    inspector.form.addWidget(tabs, 1);inspector.tabs=tabs;inspector.tab_forms=layouts
    return layouts


def _positions(inspector, controls):
    from aetherloom_core.novelai.position_canvas import CharacterPositionEditor
    state = controls.position_editor_state()
    if not state['characters']:return
    dialog = QtWidgets.QDialog(inspector.dialog_parent);dialog.setWindowTitle('角色位置 · 当前节点')
    dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
    screen = inspector.dialog_parent.screen() or QtWidgets.QApplication.primaryScreen()
    area = screen.availableGeometry()
    dialog.resize(min(850, area.width() - 60), min(720, area.height() - 80))
    box = QtWidgets.QVBoxLayout(dialog);editor = CharacterPositionEditor(dialog)
    editor.apply_theme(getattr(inspector.model_owner, '_theme_mode', 'dark'))
    editor.configure(state['characters'], state['size'], state['free_coordinates']);box.addWidget(editor)
    editor.finishRequested.connect(dialog.accept);editor.cancelRequested.connect(dialog.reject)
    if dialog.exec_() == QtWidgets.QDialog.Accepted:
        for index, value in zip(state['indices'], editor.values()):
            controls.set_character_position(index, value.get('x', .5), value.get('y', .5))
    controls.set_position_editing(False);dialog.deleteLater()


def _chunks(inspector, button):
    from aetherloom_core.novelai.dialogs import ChunksDialog
    dialog = ChunksDialog(copy.deepcopy(_options(inspector).get('chunks') or {}), inspector.dialog_parent)
    dialog.setWindowTitle('提示词片段 · 当前节点')
    if dialog.exec_() == QtWidgets.QDialog.Accepted:
        _update(inspector, chunks=dialog.chunks)
        button.setText(f'提示词片段 · {len(dialog.chunks)}')
    dialog.deleteLater()


def build(inspector, node, doc_id, edges):
    from .novelai_nodes import inputs
    layouts = _tabs(inspector);inspector.novelai_fields = {}
    inspector.form = layouts[0]
    ports = {item['key']: item for item in inputs(node)}
    for port, field, title in (('image', 'image_path', '底图'), ('mask', 'mask_path', '遮罩')):
        if port in ports:_file(inspector, port, field, title)
    controls = DrawingControls(inspector.model_owner)
    controls.setParent(inspector)
    controls.set_settings(_options(inspector));controls.setMinimumWidth(0)
    # Reuse one set of protocol-aware controls without nesting two scrolling areas.
    content = controls.scroll.takeWidget();layouts[0].addWidget(content)
    content.setObjectName('canvasNovelAIContent')
    content.setAutoFillBackground(False)
    content.layout().setContentsMargins(0, 0, 0, 0)
    model_box = controls._sections_by_key['parameters']
    content.layout().removeWidget(model_box);layouts[1].addWidget(model_box)
    controls._show_field('action', False)
    controls.dataset_mode.setMaximumWidth(94)
    controls.dataset_mode.setMinimumContentsLength(0)
    controls.dataset_mode.setStyleSheet('padding:4px 19px 4px 7px;')
    controls.hide()
    for name in ('character_section', 'references_section', 'advanced_section'):
        getattr(controls, name).toggle.setChecked(False)
    inspector.novelai_controls = controls
    # Theme belongs to the canvas; inherited full-page styles must not enlarge it.
    controls.setStyleSheet('')
    controls.content = content
    inspector._novelai_theme = _ThemeFollower(inspector, controls)
    for key in ('prompt', 'negative_prompt', 'tool_prompt'):
        editor = controls._fields[key]
        identity = _bind_prompt(inspector, editor, key)
        parent_layout = editor.parentWidget().layout()
        index = parent_layout.indexOf(editor) if parent_layout is not None else -1
        if index > 0 and parent_layout.itemAt(index - 1).widget() is not None:
            toolbar = parent_layout.itemAt(index - 1).widget().layout()
            if isinstance(toolbar, QtWidgets.QHBoxLayout):
                back, forward = QtWidgets.QToolButton(), QtWidgets.QToolButton()
                for button, text in ((back, '↶'), (forward, '↷')):
                    button.setText(text);button.setObjectName('canvasTextHistoryButton')
                    button.setFixedSize(25, 22);toolbar.insertWidget(1, button)
                back.setToolTip('回退文本记录');forward.setToolTip('前进文本记录')
                PromptHistory(editor, back, forward, inspector.histories.setdefault(identity, []))
    connected = inspector.connected_inputs
    def guard_connections():
        for port, field in [('prompt', 'tool_prompt' if node['kind'] == 'novelai_augment' else 'prompt'),
                             ('negative', 'negative_prompt')] + [(key, key) for key in _SCALARS]:
            if port in connected and field in controls._fields:controls._fields[field].setEnabled(False)
        if 'references' in connected:controls.references.setEnabled(False)
        controls._show_field('action', False)
    guard_connections()
    baseline = [controls.settings()]
    def changed():
        guard_connections()
        fresh = controls.settings()
        changes = {key: value for key, value in fresh.items() if value != baseline[0].get(key)}
        baseline[0] = copy.deepcopy(fresh)
        if changes:_update(inspector, **changes)
    controls.changed.connect(changed)
    controls.positionEditRequested.connect(lambda: _positions(inspector, controls))
    controls.tagSuggestionsRequested.connect(lambda editor: editor.show_suggestions())
    _suggestions(inspector, controls)
    if node['kind'] != 'novelai_upscale':
        chunks = QtWidgets.QPushButton(f"提示词片段 · {len(_options(inspector).get('chunks') or {})}")
        chunks.setObjectName('canvasNovelAIChunks')
        chunks.setToolTip('使用 !macro:名称! 引用；片段仅保存在当前节点，不修改 NovelAI 页面。')
        chunks.clicked.connect(lambda: _chunks(inspector, chunks));layouts[0].insertWidget(0, chunks)
    if node['kind'] == 'novelai_infill':
        focused = QtWidgets.QCheckBox('聚焦遮罩区域重绘');focused.setChecked(bool(_options(inspector).get('focused')))
        focused.setToolTip('发送遮罩周围的裁剪区域，完成后按遮罩合回原图。')
        focused.toggled.connect(lambda value: _update(inspector, focused=value));layouts[0].insertWidget(0, focused)
        padding = RhNumberSpinBox(integer=True);padding.configure({'min': 0, 'max': 2048, 'step': 16})
        padding.setValue(_options(inspector).get('focus_padding', 64));padding.setEnabled(focused.isChecked())
        padding.valueChanged.connect(lambda value: _update(inspector, focus_padding=int(value)))
        focused.toggled.connect(padding.setEnabled);inspector.numeric.append(padding)
        layouts[0].insertWidget(1, QtWidgets.QLabel('聚焦区域留边（像素）'));layouts[0].insertWidget(2, padding)
    if node['kind'] == 'novelai_enhance':
        maximum = QtWidgets.QCheckBox('Max Enhance')
        maximum.setChecked(bool(_options(inspector).get('upscaled_enhance')))
        maximum.setToolTip('仅支持此能力的模型可用；模型不支持时保留设置并在运行前提示。')
        def max_enhance(value):
            controls._raw['upscaled_enhance'] = value
            controls._action_changed();guard_connections()
            _update(inspector, upscaled_enhance=value)
        maximum.toggled.connect(max_enhance);layouts[0].insertWidget(0, maximum)
    hint = QtWidgets.QLabel('本节点的模型与参数独立保存。API 密钥与任务队列共用 NovelAI 页面连接设置。')
    hint.setWordWrap(True);hint.setObjectName('canvasMuted');layouts[1].addWidget(hint)
    timeout = RhNumberSpinBox(integer=True);timeout.configure({'min': 1, 'max': 600})
    try:timeout.setValue(_options(inspector).get('timeout', 90))
    except ValueError:timeout.lineEdit().setText(str(_options(inspector).get('timeout', '')))
    inspector.numeric.append(timeout)
    timeout.valueChanged.connect(lambda value: _update(inspector, timeout=int(value)))
    layouts[1].addWidget(QtWidgets.QLabel('单次请求超时（秒）'));layouts[1].addWidget(timeout)
    layouts[1].addStretch(1)
    inspector.model_fields = {'model': controls.model, 'timeout': timeout}
    inspector.form = layouts[2];inspector._app_options(node);inspector._other_options(node);inspector.form.addStretch(1)
    inspector.form = layouts[3]
