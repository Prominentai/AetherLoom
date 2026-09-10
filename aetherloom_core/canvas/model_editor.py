"""Qt inspector for the four API/Agent model-node categories."""
import copy
from PyQt5 import QtCore, QtWidgets
from aetherloom_core.rh_parameters import RhEnumComboBox
from aetherloom_core import api_manager
from aetherloom_core.agent_search import PROVIDERS
from .model import MODEL_KINDS
from .model_nodes import connection


def build(inspector, node, doc_id, edges, owner):
    category = MODEL_KINDS[node['kind']]
    config = copy.deepcopy(node.get('model_config', {}))
    root_form = inspector.form
    tabs = QtWidgets.QTabWidget();tabs.setObjectName('canvasNodeSettingsTabs');tabs.setDocumentMode(True)
    tabs.setUsesScrollButtons(True);tabs.tabBar().setExpanding(True)
    tabs.tabBar().setDrawBase(False);tabs.tabBar().setElideMode(QtCore.Qt.ElideNone)
    layouts = []
    for title in ('输入', '模型设置', '其他设置', '最近结果'):
        scroll = QtWidgets.QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setObjectName('canvasNodeTabScroll')
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        content = QtWidgets.QWidget();layout = QtWidgets.QVBoxLayout(content)
        content.setObjectName('canvasNodeTabContent')
        layout.setContentsMargins(2, 12, 5, 8);layout.setSpacing(11)
        scroll.setWidget(content);tabs.addTab(scroll, title);layouts.append(layout)
    root_form.addWidget(tabs, 1);inspector.tabs = tabs;inspector.tab_forms = layouts
    form = layouts[1]
    hint = QtWidgets.QLabel('仅此画布节点生效。连接与授权复用 API 管理；修改模型、提示词或其他参数不会写回 API 管理、设置中心或其他节点。')
    hint.setWordWrap(True);hint.setObjectName('canvasMuted');form.addWidget(hint)
    combo = RhEnumComboBox()
    for entry in api_manager.get_providers(category):
        identity = entry['key']
        if identity == 'custom':identity = 'custom_' + category
        combo.addItem(entry['name'], identity)
    index = combo.findData(config.get('provider'))
    if index < 0:
        combo.insertItem(0, '请选择本机模型连接', '');index = 0
    combo.setCurrentIndex(index)
    form.addWidget(QtWidgets.QLabel('供应商 / 账户连接'));form.addWidget(combo)
    model_edit = RhEnumComboBox();model_edit.setEditable(True)
    form.addWidget(QtWidgets.QLabel('模型名称 / Agent LLM'));form.addWidget(model_edit)
    timeout = QtWidgets.QSpinBox();timeout.setRange(1, 3600);timeout.setSuffix(' 秒')
    timeout.setValue(int(config.get('timeout') or 120));timeout.wheelEvent = lambda event:event.ignore()
    form.addWidget(QtWidgets.QLabel('单次请求超时'));form.addWidget(timeout)
    search = QtWidgets.QCheckBox('联网搜索 · 由 Agent 按需使用');form.addWidget(search)
    sync = QtWidgets.QPushButton('重新读取此连接配置');form.addWidget(sync)
    form = layouts[0];inspector.form = form
    form.addWidget(QtWidgets.QLabel('提示词 · 连接后使用上游文本'))
    prompt = inspector._text_editor(node.get('params', {}).get('prompt', ''), (doc_id, node['id'], 'prompt'))
    prompt.textChanged.connect(lambda:inspector.changed.emit('params.prompt', prompt.toPlainText()))
    system_group = QtWidgets.QWidget();system_form = QtWidgets.QVBoxLayout(system_group)
    system_form.setContentsMargins(0, 0, 0, 0);system_form.setSpacing(11)
    form.addWidget(system_group);inspector.form = system_form
    system_label = QtWidgets.QLabel('系统提示词 / Agent 图像要求');system_form.addWidget(system_label)
    system = inspector._text_editor(node.get('params', {}).get('system_prompt', ''), (doc_id, node['id'], 'system_prompt'))
    system.textChanged.connect(lambda:inspector.changed.emit('params.system_prompt', system.toPlainText()))
    inspector.form = form
    compat = QtWidgets.QCheckBox('Agent 图像提示词兼容模式（合并到用户文本）')
    compat.setChecked(config.get('merge_system_prompt') is True);form.addWidget(compat)
    if category in ('vision', 'image_edit'):
        form.addWidget(QtWidgets.QLabel('本地图像 · 未连接时使用，一组输入处理一张'))
        row = QtWidgets.QHBoxLayout();image_path = QtWidgets.QLineEdit(node.get('params', {}).get('image', ''))
        image_path.setPlaceholderText('也可连接“图像导入”节点')
        choose = QtWidgets.QPushButton('选择');row.addWidget(image_path, 1);row.addWidget(choose);form.addLayout(row)
        image_path.editingFinished.connect(lambda:inspector.changed.emit('params.image', image_path.text().strip()))
        def pick():
            path, _ = QtWidgets.QFileDialog.getOpenFileName(inspector, '选择图像', '', '图像 (*.png *.jpg *.jpeg *.webp *.gif)')
            if path:image_path.setText(path);inspector.changed.emit('params.image', path)
        choose.clicked.connect(pick)
    if category in ('text2img', 'image_edit'):
        size_hint = QtWidgets.QLabel('尺寸：供应商默认。比例要求可写入提示词，不强制传入 size。')
        size_hint.setWordWrap(True);size_hint.setObjectName('canvasMuted');form.addWidget(size_hint)
    form.addStretch(1);form = layouts[1]
    reuse = QtWidgets.QCheckBox('过滤重复运行');reuse.setChecked(node.get('filter_repeats', False));layouts[2].addWidget(reuse)
    reuse.toggled.connect(lambda value:inspector.changed.emit('filter_repeats', value))
    def save():inspector.changed.emit('model_config', copy.deepcopy(config))
    def refresh():
        with QtCore.QSignalBlocker(model_edit):
            model_edit.clear()
            names = api_manager.get_models_for_provider(category, combo.currentData())
            if owner and config.get('provider') == (owner.api_config_fields[category]['provider'].currentData()):
                current = owner.api_config_fields[category]['model']
                names = [current.itemText(i) for i in range(current.count())]
            model_edit.addItems(names);model_edit.setEditText(config.get('model', ''))
        with QtCore.QSignalBlocker(timeout):timeout.setValue(int(config.get('timeout') or 120))
        protocol = config.get('protocol', '')
        searching = category in ('llm', 'vision') and protocol in PROVIDERS
        search.setVisible(searching)
        with QtCore.QSignalBlocker(search):search.setChecked(config.get('web_search', True) is True)
        agent_image = category in ('text2img', 'image_edit') and protocol in ('agent_codex', 'agent_grok')
        visible = category in ('llm', 'vision') or agent_image
        system_group.setVisible(visible);compat.setVisible(agent_image)
        with QtCore.QSignalBlocker(compat):compat.setChecked(config.get('merge_system_prompt') is True)
    def selected():
        if not owner or not combo.currentData():return
        try:
            fresh = connection(owner, category, combo.currentData())
            # Switching credentials must preserve this node's prompt and compatibility option.
            fresh['merge_system_prompt'] = config.get('merge_system_prompt', False)
            config.clear();config.update(fresh);refresh();save()
        except Exception as error:inspector.message.emit(str(error))
    def model_changed():
        config['model'] = model_edit.currentText().strip()
        if category in ('text2img', 'image_edit'):
            from aetherloom_core.image_model_catalog import follow_model_endpoint
            config['endpoint'] = follow_model_endpoint(config.get('protocol', ''), category, config['model'], config.get('endpoint', ''))
        save()
    refresh()
    combo.currentIndexChanged.connect(selected)
    sync.clicked.connect(selected)
    model_edit.lineEdit().editingFinished.connect(model_changed)
    model_edit.activated.connect(lambda *_:model_changed())
    timeout.valueChanged.connect(lambda value:(config.update(timeout=value), save()))
    search.toggled.connect(lambda value:(config.update(web_search=value), save()))
    compat.toggled.connect(lambda value:(config.update(merge_system_prompt=value), save()))
    # Expose stable widgets for focus management and offline Qt verification.
    inspector.model_fields = dict(provider=combo, model=model_edit, prompt=prompt, system=system, timeout=timeout, search=search)
    form.addStretch(1);inspector.form=layouts[2];inspector._other_options(node)
    inspector.form.addStretch(1);inspector.form = layouts[3]
