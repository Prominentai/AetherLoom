"""Independent node inspectors reusing AetherLoom's exact parameter editors."""
import copy
import os
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_parameters import RhNumberSpinBox, RhEnumComboBox, configure_list_combo
from aetherloom_core.rh_model_picker import ModelField, model_resource_type
from aetherloom_core.prompt_history import PromptHistory
from aetherloom_core.ui.widgets import CompletionTextEdit
from .model import parameter_key, field_type, node_title
from .model import MEDIA_SUFFIXES, MODEL_KINDS, MERGE_KINDS, RESULT_TYPES, ITEM_OUTPUT_TYPES
from .media_inputs import accepts
from . import collections, utility_nodes


FILE_FILTERS = {kind: label + ' (' + ' '.join('*' + suffix for suffix in sorted(MEDIA_SUFFIXES[kind])) + ')'
                for kind, label in (('image', '图像'), ('video', '视频'), ('audio', '音频'))}


class FileList(QtWidgets.QListWidget):
    files_changed = QtCore.pyqtSignal(list)

    def __init__(self, parent=None, kind='image'):
        super().__init__(parent)
        self.kind = kind
        self.setAcceptDrops(True)
        self.setDragDropMode(self.InternalMove)
        self.setSelectionMode(self.ExtendedSelection)
        self.setMinimumHeight(130)
        self.model().rowsMoved.connect(lambda: self.files_changed.emit(self.paths()))

    def paths(self):
        return [self.item(i).data(QtCore.Qt.UserRole) for i in range(self.count())]

    def set_paths(self, paths):
        self.clear()
        for path in paths:
            self.add_path(path)

    def add_path(self, path):
        item = QtWidgets.QListWidgetItem(os.path.basename(str(path)) or str(path))
        item.setData(QtCore.Qt.UserRole, str(path))
        item.setToolTip(str(path))
        if os.path.isdir(path):
            item.setText('文件夹 · ' + item.text())
        elif not os.path.isfile(path):
            item.setText('⚠ ' + item.text())
            item.setForeground(QtGui.QColor('#e2a268'))
        self.addItem(item)

    def import_paths(self, paths):
        previous = {os.path.normcase(os.path.abspath(p)) for p in self.paths()}
        added = 0
        for value in paths:
            raw = str(value).strip().strip('"')
            if not raw:continue
            path = os.path.abspath(os.path.expanduser(raw))
            key = os.path.normcase(path)
            if key not in previous and accepts(path, self.kind):
                self.add_path(path);previous.add(key);added += 1
        if added:self.files_changed.emit(self.paths())
        return added

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.count():
            painter = QtGui.QPainter(self.viewport())
            painter.setPen(self.palette().color(QtGui.QPalette.PlaceholderText))
            painter.drawText(self.viewport().rect().adjusted(10, 6, -10, -6),
                             QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap,
                             '拖入' + {'image':'图像', 'video':'视频', 'audio':'音频'}.get(self.kind, '文件') + '或文件夹\n也可使用下方按钮添加')

    def dragEnterEvent(self, event):
        from aetherloom_core.image_import import accepts_mime
        if accepts_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        from aetherloom_core.image_import import accepts_mime
        if accepts_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasFormat('application/x-qabstractitemmodeldatalist'):
            from aetherloom_core.image_import import import_mime
            if import_mime(event.mimeData(),self,self.import_paths,self.kind):event.acceptProposedAction();return
        if event.mimeData().hasUrls():
            if self.import_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]):
                event.acceptProposedAction()
            else:event.ignore()
        else:
            super().dropEvent(event)


def parse_indices(text, unique=True):
    parts = [part.strip() for part in str(text).replace('，', ',').split(',') if part.strip()]
    values = [int(part) for part in parts]
    if not values or any(value < 1 for value in values):
        raise ValueError('请输入从 1 开始的结果序号，例如 1, 3')
    return list(dict.fromkeys(values)) if unique else values


class Inspector(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(str, object)
    rebind_requested = QtCore.pyqtSignal(str)
    password_requested = QtCore.pyqtSignal(str)
    install_requested = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str)

    def __init__(self, node, doc_id, edges, histories, parent=None, missing_app=False, changed_definition=False, model_owner=None, embedded=False):
        super().__init__(parent)
        self.node = node
        self.embedded = embedded
        self.model_owner = model_owner
        self.doc_id = doc_id
        self.connected_inputs = {edge['input'] for edge in edges if edge['target'] == node['id']}
        self.port_widgets = {}
        from .model import supports_local_decode
        has_decode = supports_local_decode(node)
        self.results_list = None
        self.install_button = None
        self.numeric = []
        self.histories = histories
        self.tabs = None
        self.tab_forms = []
        self.form = QtWidgets.QVBoxLayout(self)
        self.form.setContentsMargins(15, 14, 15, 16)
        self.form.setSpacing(11)
        if not embedded:
            header = QtWidgets.QWidget(self)
            header.setObjectName('canvasInspectorHeader')
            header_layout = QtWidgets.QVBoxLayout(header)
            header_layout.setContentsMargins(0, 0, 0, 0);header_layout.setSpacing(3)
            title = QtWidgets.QLabel(node_title(node));title.setWordWrap(True)
            title.setObjectName('canvasInspectorTitle')
            header_layout.addWidget(title)
            from .model import TITLES
            subtitle = QtWidgets.QLabel(TITLES.get(node['kind'], '节点') + ' · 独立配置')
            subtitle.setObjectName('canvasInspectorSubtitle');header_layout.addWidget(subtitle)
            self.form.addWidget(header)
        root_form = self.form
        from .model import MODEL_KINDS
        if not embedded and node['kind'] not in MODEL_KINDS:
            root_form.setContentsMargins(14, 16, 14, 14)
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setObjectName('canvasNodeSettingsTabs')
            self.tabs.setDocumentMode(True)
            self.tabs.setUsesScrollButtons(True)
            self.tabs.tabBar().setExpanding(True)
            self.tabs.tabBar().setDrawBase(False)
            self.tabs.tabBar().setElideMode(QtCore.Qt.ElideNone)
            labels = (('应用', '解码', '其他', '结果') if has_decode else ('应用', '其他', '结果')) if node['kind']=='app' else ('参数', '其他', '结果')
            for label in labels:
                scroll = QtWidgets.QScrollArea()
                scroll.setObjectName('canvasNodeTabScroll')
                scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
                scroll.setWidgetResizable(True)
                scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
                content = QtWidgets.QWidget()
                content.setObjectName('canvasNodeTabContent')
                layout = QtWidgets.QVBoxLayout(content)
                layout.setContentsMargins(2, 14, 6, 10)
                layout.setSpacing(11)
                scroll.setWidget(content)
                self.tabs.addTab(scroll, label)
                self.tab_forms.append(layout)
            root_form.addWidget(self.tabs, 1)
            self.form = self.tab_forms[0]
        name = QtWidgets.QLineEdit(node_title(node))
        name.setPlaceholderText('节点名称')
        name.editingFinished.connect(lambda: self.changed.emit('title', name.text().strip() or '节点'))
        name.setObjectName('canvasNodeName')
        name.setToolTip('节点名称，仅影响显示')
        if not embedded:
            header_layout.replaceWidget(title, name)
            title.deleteLater()
        else:name.deleteLater()
        if node['kind'] in collections.KINDS and not collections.current(node):
            from .collection_editor import legacy_notice
            legacy_notice(self, node)
        if node.get('status') == 'INTERRUPTED':
            interrupted = QtWidgets.QLabel('会话已中断。已有结果仍可查看；生成与排队任务不会在重启后自动续跑。')
            interrupted.setWordWrap(True)
            interrupted.setObjectName('canvasMuted')
            self.form.addWidget(interrupted)
        if missing_app:
            label = QtWidgets.QLabel('此 App 尚未添加到本机。添加后保留当前节点的独立参数。')
            label.setWordWrap(True)
            label.setObjectName('canvasWarning')
            self.form.addWidget(label)
            self.install_button = QtWidgets.QPushButton('添加此 App')
            self.install_button.clicked.connect(lambda: self.install_requested.emit(node['id']))
            self.form.addWidget(self.install_button)
        elif changed_definition:
            label = QtWidgets.QLabel('本机 App 定义已变化。运行前请核对并重新绑定参数。')
            label.setWordWrap(True)
            label.setObjectName('canvasWarning')
            self.form.addWidget(label)
            rebind = QtWidgets.QPushButton('核对并重新绑定 App 参数')
            rebind.clicked.connect(lambda: self.rebind_requested.emit(node['id']))
            self.form.addWidget(rebind)
        self.decode_group = None
        from .model import MODEL_KINDS
        if node['kind'] in MODEL_KINDS:
            if embedded:
                from .model_editor import build_inline
                build_inline(self, node, doc_id, edges)
                return
            from .model_editor import build
            build(self, node, doc_id, edges, model_owner)
            self._results(node)
            self.form.addStretch(1)
            return
        if node['kind'] == 'app':
            self._app_fields(node, doc_id, edges)
            if embedded:
                if has_decode:
                    from .node_form import FoldSection
                    section = FoldSection('本地解码' + (' · 已启用' if node.get('decode_settings', {}).get('enabled') else ' · 未启用'),
                                          self, histories, (doc_id, node['id'], 'decode'),
                                          expanded=bool(node.get('decode_settings', {}).get('enabled')))
                    self.decode_section = section
                    self.form.addWidget(section)
                    main = self.form;self.form = section.body_layout
                    self._decode_settings(node)
                    self.decode_group.setTitle('')
                    self.form = main
                self.form.addStretch(1)
                return
            self.form.addStretch(1)
            if has_decode:
                self.form = self.tab_forms[1]
                self._decode_settings(node)
                self.form.addStretch(1)
            self.form = self.tab_forms[-2]
            self._app_options(node)
            self._other_options(node)
            self.form.addStretch(1)
            self.form = self.tab_forms[-1]
            self._results(node)
            self.form.addStretch(1)
            self.form = root_form
            return
        elif node['kind'] in utility_nodes.KINDS:
            from .utility_editor import build
            build(self, node)
        elif collections.current(node):
            from .collection_editor import build
            build(self, node)
        elif node['kind'] in FILE_FILTERS:
            self._files(node)
        elif node['kind'] in ('int', 'float'):
            from .node_form import NumberDrag
            label = QtWidgets.QLabel('数值')
            editor = RhNumberSpinBox(integer=node['kind'] == 'int')
            editor.setObjectName('canvasPrimitiveValue')
            if node['kind'] == 'int':editor.configure({'min': -(2**63), 'max': 2**63-1, 'step': 1})
            else:editor.setSingleStep('0.1')
            editor.setValue(node.get('params', {}).get('value', 0))
            editor.valueChanged.connect(lambda value:self.changed.emit('params.value', str(value)))
            self.numeric.append(editor)
            label._number_drag = NumberDrag(label, editor)
            row = QtWidgets.QHBoxLayout();row.setContentsMargins(8, 0, 2, 0);row.setSpacing(6)
            row.addWidget(label);row.addWidget(editor, 1)
            holder = QtWidgets.QFrame(self);holder.setObjectName('canvasScalarRow');holder.setLayout(row)
            self.form.addWidget(holder)
        elif node['kind'] == 'text':
            editor = self._text_editor(node.get('params', {}).get('text', ''), (doc_id, node['id'], 'text'))
            editor.textChanged.connect(lambda: self.changed.emit('params.text', editor.toPlainText()))
            hint = QtWidgets.QLabel('文本会作为一个完整输入传递。支持提示词补全和本次会话的文本回退 / 前进。')
            hint.setWordWrap(True)
            hint.setObjectName('canvasMuted')
            self.form.addWidget(hint)
        elif node['kind'] == 'preview':
            enabled = QtWidgets.QCheckBox('保存结果')
            enabled.setObjectName('canvasSaveEnabled')
            enabled.setChecked(node.get('params', {}).get('save_enabled', False))
            self.form.addWidget(enabled)
            overwrite = QtWidgets.QCheckBox('重名覆盖')
            overwrite.setObjectName('canvasSaveOverwrite')
            overwrite.setChecked(node.get('params', {}).get('overwrite', False))
            overwrite.setEnabled(enabled.isChecked())
            overwrite.setToolTip('关闭时重名按 文件名(1).后缀、文件名(2).后缀 保存；开启后替换目标同名文件。')
            overwrite.toggled.connect(lambda value:self.changed.emit('params.overwrite', value))
            enabled.toggled.connect(overwrite.setEnabled)
            self.form.addWidget(overwrite)
            self.form.addWidget(QtWidgets.QLabel('保存目录'))
            row = QtWidgets.QHBoxLayout()
            directory = QtWidgets.QLineEdit(node.get('params', {}).get('save_directory', ''))
            directory.setObjectName('canvasSaveDirectory')
            directory.setPlaceholderText('留空使用输出目录下的画布文件夹')
            directory.setClearButtonEnabled(True)
            directory.setToolTip('仅此节点生效；请输入绝对路径。同名文件自动加序号，不覆盖原文件。')
            browse = QtWidgets.QPushButton('选择')
            row.addWidget(directory, 1);row.addWidget(browse);self.form.addLayout(row)
            directory.setEnabled(enabled.isChecked());browse.setEnabled(enabled.isChecked())
            enabled.toggled.connect(lambda value:(directory.setEnabled(value), browse.setEnabled(value), self.changed.emit('params.save_enabled', value)))
            directory.editingFinished.connect(lambda:self.changed.emit('params.save_directory', directory.text().strip()))
            def choose_directory():
                selected = QtWidgets.QFileDialog.getExistingDirectory(self.dialog_parent, '选择节点保存目录', directory.text())
                if selected:
                    directory.setText(selected)
                    self.changed.emit('params.save_directory', selected)
            browse.clicked.connect(choose_directory)
            from .save_results import default_directory
            canvas_doc = {'id': doc_id, 'name': getattr(getattr(model_owner, 'canvas_page', None), 'document', {}).get('name', '画布')}
            default_path = default_directory(model_owner.output_dir, canvas_doc) if model_owner else '输出目录/canvases/画布名称_标识'
            hint = QtWidgets.QLabel('默认不保存，仅预览。保存时沿用输入名称；修改名称请在上游使用文件重命名节点。重名覆盖默认关闭，重名添加 (1)、(2)…；开启覆盖后替换同名文件。\n默认目录：' + default_path)
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] == 'filename':
            mode = RhEnumComboBox();mode.setObjectName('canvasFilenameMode')
            for label, value in [('只读取文件名', 'name'), ('只读取扩展名', 'extension'), ('全部读取', 'full')]:mode.addItem(label, value)
            params = node.get('params', {})
            mode.setCurrentIndex(max(0, mode.findData(params.get('read_mode', 'full' if params.get('include_extension', False) else 'name'))))
            mode.currentIndexChanged.connect(lambda:self.changed.emit('params.read_mode', mode.currentData()))
            self.form.addWidget(QtWidgets.QLabel('读取内容'));self.form.addWidget(mode)
            hint = QtWidgets.QLabel('例如 photo.png 分别输出 photo、png、photo.png。多文件逐项读取并保留来源关系，输出文本可连接文件重命名节点的文件名或后缀输入，不自动生成本地文件。')
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] == 'rename':
            for key, label in (('name', '文件名（不含后缀）'), ('extension', '扩展名')):
                self.form.addWidget(QtWidgets.QLabel(label))
                edit = QtWidgets.QLineEdit(node.get('params', {}).get(key, ''))
                edit.setObjectName('canvasRename_' + key)
                edit.setPlaceholderText('留空保持原值')
                edit.setEnabled(key not in self.connected_inputs)
                edit.editingFinished.connect(lambda k=key,e=edit:self.changed.emit('params.' + k, e.text().strip()))
                self.form.addWidget(edit)
                self.port_widgets[key] = edit
            hint = QtWidgets.QLabel('在临时目录生成重命名副本，后续节点读取副本；原文件与 App 输出卡片保持不变。文件名或后缀留空均保持原值，连线输入留空也不修改。同名结果分开存放，正式保存时按保存节点的重名选项处理。后缀只改名称，不转换文件格式。')
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] == 'list_select':
            types = RhEnumComboBox();types.setObjectName('canvasListSelectType')
            types.addItem('全部类型（各 List 分开取项）', 'any')
            from .model import filter_type_options
            for kind, label in filter_type_options(node['params'].get('type', 'any')):
                if kind != 'any':types.addItem(label, kind)
            types.setCurrentIndex(max(0, types.findData(node['params'].get('type', 'any'))))
            types.currentIndexChanged.connect(lambda:self.changed.emit('params.type', types.currentData()))
            self.form.addWidget(QtWidgets.QLabel('内容类型'));self.form.addWidget(types)
            indices = QtWidgets.QLineEdit(', '.join(map(str, node['params'].get('indices', [1]))))
            indices.setObjectName('canvasListSelectIndices');indices.setPlaceholderText('从 1 开始，例如 1, 3；不能为空')
            indices.editingFinished.connect(lambda:self._indices_changed(indices, 'params.indices'))
            self.form.addWidget(QtWidgets.QLabel('每组保留的序号'));self.form.addWidget(indices)
            keep = QtWidgets.QCheckBox('保留 Batch 分组');keep.setObjectName('canvasListSelectKeepBatch')
            keep.setChecked(node['params'].get('keep_batch', False))
            keep.toggled.connect(lambda checked:self.changed.emit('params.keep_batch', checked));self.form.addWidget(keep)
            hint = QtWidgets.QLabel('每接入一个来源，自动增加一个空输入端。每个输入端内的同类型 List 分别取项，每个 Batch 也独立取项；不同输入端不会先合并或按长度配对。\n\n先筛选内容类型，再在每组内按序号取值；多个序号按填写顺序输出。某组序号越界时整个节点停止并提示，不补项、不截断。\n\n默认将选中的 Batch 成员作为普通 List 输出；开启“保留 Batch 分组”后，每个 Batch 保持独立并从 Batch 端口输出。普通 List 不受此开关影响，各类型从对应端口输出。\n\n需要跨来源统一编号时，先使用“合成列表”。忽略此节点会按输入顺序原样传递，不执行筛选或取项。')
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] in MERGE_KINDS:
            output = ('展开各输入后打包为一个 Batch，最多 256 项；连接图像编辑节点时整组图像用于一次请求。'
                      if node['kind'] == 'merge_batch' else '展开各输入后输出普通列表，下游可以逐项处理。')
            hint = QtWidgets.QLabel('按输入端从上到下、各输入内部从前到后的顺序合成。每接入一条连线，自动增加一个空输入端；断开后保留其他连线的顺序。\n\n'
                                    + output + '\n\n支持单项、列表及 Batch 混合输入，不嵌套 Batch、不去重、不按长度配对。连线默认使用全部结果，也可单独选择序号。空输入端不参与运行。\n\n忽略此节点时按输入端顺序传递原结果，保留原有 Batch 分组。')
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] == 'batch2list':
            hint = QtWidgets.QLabel('batch2list · Batch 转列表\n\n按 Batch 顺序、组内顺序展开为普通结果列表，保留每项文件、类型和来源关联。连接图像编辑节点后恢复逐张运行，也可继续连接内容过滤、文件名读取、重命名或保存节点。\n\n普通列表输入保持顺序通过；忽略此节点则保留原 Batch，不做展开。下游连线选择“全部匹配结果逐项运行”可使用全部成员。')
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] == 'list2batch':
            hint = QtWidgets.QLabel('list2batch · 列表转 Batch\n\n将连线选中的全部结果按顺序打包为一个输入项，不拼接图片、不转换格式。连接图像编辑 API 节点时，整组图像用于一次编辑请求。\n\n普通图像列表直接连接编辑节点仍会逐项执行。Batch 的图片数量和总大小受供应商与模型限制。忽略此节点则恢复普通列表。')
            hint.setWordWrap(True);hint.setObjectName('canvasMuted');self.form.addWidget(hint)
        elif node['kind'] == 'select':
            type_combo = RhEnumComboBox()
            from .model import filter_type_options
            type_combo.setObjectName('canvasFilterType')
            for value, label in filter_type_options(node.get('params', {}).get('type', 'any')):
                type_combo.addItem(label, value)
            type_combo.setCurrentIndex(max(0, type_combo.findData(node.get('params', {}).get('type', 'any'))))
            type_combo.currentIndexChanged.connect(lambda: self.changed.emit('params.type', type_combo.currentData()))
            self.form.addWidget(QtWidgets.QLabel('保留的内容类型'))
            self.form.addWidget(type_combo)
            indices = QtWidgets.QLineEdit(', '.join(map(str, node.get('params', {}).get('indices') or [])))
            indices.setPlaceholderText('留空保留全部；例如 1, 3')
            indices.setObjectName('canvasFilterIndices')
            indices.editingFinished.connect(lambda: self._indices_changed(indices, 'params.indices'))
            self.form.addWidget(QtWidgets.QLabel('保留的序号'))
            self.form.addWidget(indices)
            hint = QtWidgets.QLabel('先按类型过滤，再按该类型内的序号保留内容（从 1 开始）。新连线默认传入全部内容；不修改原文件。')
            hint.setWordWrap(True)
            hint.setObjectName('canvasMuted')
            self.form.addWidget(hint)
        self.form.addStretch(1)
        if embedded:return
        self.form = self.tab_forms[1]
        self._other_options(node)
        reuse_hint = QtWidgets.QLabel('内置节点自动复用未变化的有效结果。')
        reuse_hint.setObjectName('canvasBuiltinReuseHint')
        reuse_hint.setWordWrap(True)
        self.form.addWidget(reuse_hint)
        self.form.addStretch(1)
        self.form = self.tab_forms[2]
        self._results(node)
        self.form.addStretch(1)
        self.form = root_form

    @property
    def dialog_parent(self):
        return self.model_owner if self.embedded and self.model_owner is not None else self

    def _other_options(self,node):
        group=QtWidgets.QGroupBox('其他设置');group.setObjectName('canvasOtherSettings')
        layout=QtWidgets.QVBoxLayout(group)
        enabled=QtWidgets.QCheckBox('忽略节点（旁路）');enabled.setObjectName('canvasBypass')
        enabled.setChecked(node.get('bypass',False))
        enabled.toggled.connect(lambda value:self.changed.emit('bypass',value));layout.addWidget(enabled)
        hint=QtWidgets.QLabel('忽略后不执行此节点，自动将兼容的连线输入传给下游。同类型多输入按端口顺序取第一个；无兼容输入时，下游停止，不读取旧结果。')
        hint.setWordWrap(True);hint.setObjectName('canvasMuted');layout.addWidget(hint)
        self.form.addWidget(group)

    def focus_other_settings(self):
        group=self.findChild(QtWidgets.QGroupBox,'canvasOtherSettings')
        if group and self.tabs:
            for index in range(self.tabs.count()):
                if self.tabs.widget(index).isAncestorOf(group):self.tabs.setCurrentIndex(index);break
        if group:
            group.findChild(QtWidgets.QCheckBox,'canvasBypass').setFocus()
            parent=group.parentWidget()
            while parent:
                if isinstance(parent,QtWidgets.QScrollArea):parent.ensureWidgetVisible(group);break
                parent=parent.parentWidget()

    def _indices_changed(self, editor, path):
        try:
            values = parse_indices(editor.text(), unique=self.node['kind'] != 'list_select') if editor.text().strip() else []
            if self.node['kind'] == 'list_select' and not values:raise ValueError('至少填写一个序号')
            editor.setProperty('invalid', False)
            self.changed.emit(path, values)
        except (ValueError, TypeError):
            editor.setProperty('invalid', True)
            self.message.emit('请输入从 1 开始的结果序号，例如 1, 3')

    def _text_editor(self, text, identity):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        previous = self.form.itemAt(self.form.count()-1) if self.form.count() else None
        caption = previous.widget() if previous is not None else None
        if isinstance(caption, QtWidgets.QLabel):
            self.form.takeAt(self.form.count()-1);row.addWidget(caption, 1)
        else:row.addStretch()
        tools = QtWidgets.QWidget(self)
        tools_layout = QtWidgets.QHBoxLayout(tools);tools_layout.setContentsMargins(0,0,0,0);tools_layout.setSpacing(2)
        back, forward = QtWidgets.QToolButton(), QtWidgets.QToolButton()
        back.setText('回退')
        forward.setText('前进')
        for button, glyph, tip in ((back, '↶', '回退到上一条文本记录'), (forward, '↷', '前进到下一条文本记录')):
            button.setText(glyph);button.setToolTip(tip)
            button.setObjectName('canvasTextHistoryButton')
            button.setFixedSize(26, 24)
        tools_layout.addWidget(back);tools_layout.addWidget(forward);row.addWidget(tools)
        self.form.addLayout(row)
        editor = CompletionTextEdit(self)
        editor.setProperty('canvasTextKey', identity[-1])
        # Share local undo history between inline and complete forms.
        from .inline_text import bind_document
        bind_document(editor,text,identity,self.histories)
        if self.embedded and self.model_owner is not None:
            popup = editor._popup
            popup.setParent(self.model_owner, popup.windowFlags())
            popup.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
            editor.destroyed.connect(popup.deleteLater)
        editor.setMinimumHeight(92 if self.embedded else 125)
        editor.setMaximumHeight(140 if self.embedded else 176)
        editor.setPlaceholderText('输入内容…')
        editor.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        from .node_form import TextToolsState
        editor._tools_state = TextToolsState(editor, tools)
        editor._history_tools = tools
        editor.setAcceptRichText(False)
        PromptHistory(editor, back, forward, self.histories.setdefault(identity, []))
        self.form.addWidget(editor)
        return editor

    def _app_fields(self, node, doc_id, edges):
        connected = {edge['input'] for edge in edges if edge['target'] == node['id']}
        params = node.get('params', {})
        fields = node.get('app', {}).get('nodes', [])
        from aetherloom_core.rh_multi_inputs import groups, group_values, distribute
        from aetherloom_core.rh_multi_input_ui import MediaListEditor
        multi = groups(fields, node.get('app', {}).get('model_definition'))
        hidden = {i for group in multi.values() for i in group['indices'][1:]}
        from .node_form import field_caption
        from .model import app_input_labels
        port_labels = app_input_labels(node)
        definitions = {p['fieldKey']: p for p in (node.get('app', {}).get('model_definition') or {}).get('params', []) if 'fieldKey' in p}
        main_form = self.form
        for index, field in enumerate(fields):
            if index in hidden:continue
            key = parameter_key(field)
            value = params.get(key, field.get('fieldValue', ''))
            definition = definitions.get(field.get('_model_field') or field.get('fieldName'), {})
            self.form = main_form
            label, hint = field_caption(field, definition)
            label = port_labels[key] + (' *' if definition.get('required', field.get('required')) is True else '')
            from .node_form import FieldLabel
            title = FieldLabel(label) if self.embedded else QtWidgets.QLabel(label)
            title.setObjectName('canvasFieldLabel')
            title.setWordWrap(not self.embedded)
            title.setToolTip(hint)
            self.form.addWidget(title)
            self.port_widgets[key] = title
            kind = str(field.get('fieldType') or '').upper()
            choices = definition.get('options') if definition.get('type') == 'SIZE' else None
            if choices:
                choices = [option for option in choices if str(option.get('value', '') if isinstance(option, dict) else option).lower() != 'custom']
            if choices:kind = 'LIST'
            media_type = field_type(field)
            if index in multi:
                group = multi[index]
                current = copy.deepcopy(fields)
                for item in current:item['fieldValue'] = params.get(parameter_key(item), item.get('fieldValue', ''))
                editor = MediaListEditor(group['param'], group_values(current, group['indices']), self)
                def update_files(paths, indices=group['indices']):
                    updated = copy.deepcopy(self.node.get('params', {}))
                    items = copy.deepcopy(self.node.get('app', {}).get('nodes', []))
                    distribute(items, indices, paths)
                    for i in indices:updated[parameter_key(items[i])] = items[i]['fieldValue']
                    self.changed.emit('params', updated)
                editor.changed.connect(update_files)
                linked = any(parameter_key(fields[i]) in connected for i in group['indices'])
                editor.setEnabled(not linked)
                if linked:
                    editor.setToolTip('运行时连线覆盖对应输入；断开后恢复已保存的文件列表。')
                self.form.addWidget(editor)
                for slot in (group['indices'][1:] if self.embedded else []):
                    slot_key = parameter_key(fields[slot])
                    slot_label = QtWidgets.QLabel(port_labels[slot_key], self)
                    self.form.addWidget(slot_label)
                    self.port_widgets[slot_key] = slot_label
                continue
            elif model_resource_type(field):
                editor = ModelField(field, value, self)
                editor.editor.editingFinished.connect(lambda e=editor.editor, k=key: self.changed.emit('params.' + k, e.text()))
                self.form.addWidget(editor)
            elif kind in ('FLOAT', 'DOUBLE', 'NUMBER', 'INT', 'INTEGER'):
                editor = RhNumberSpinBox(integer=kind in ('INT', 'INTEGER'))
                editor.configure(field.get('fieldData'))
                try:
                    editor.setValue(value)
                except (ValueError, TypeError):
                    editor.lineEdit().setText(str(value))
                self.numeric.append(editor)
                editor.valueChanged.connect(lambda value, k=key: self.changed.emit('params.' + k, str(value)))
                self.form.addWidget(editor)
            elif kind in ('LIST', 'COMBO', 'ENUM', 'SELECT', 'BOOLEAN', 'BOOL'):
                editor = RhEnumComboBox()
                if kind in ('BOOLEAN', 'BOOL'):
                    editor.addItems(['true', 'false'])
                    editor.setCurrentText(str(value).lower())
                    editor.currentTextChanged.connect(lambda value, k=key: self.changed.emit('params.' + k, value))
                else:
                    options = [option.get('value') if isinstance(option, dict) else option for option in choices] if choices else field.get('fieldData')
                    configure_list_combo(editor, options, value)
                    if choices:
                        editor.setObjectName('canvasSizePreset')
                        editor.setEditable(True);editor.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
                        editor.lineEdit().setPlaceholderText('预设或自定义宽*高')
                        editor.setEditText(str(value))
                        editor.lineEdit().editingFinished.connect(lambda e=editor, k=key: self.changed.emit('params.' + k, e.currentText().strip()))
                        editor.activated.connect(lambda unused, e=editor, k=key: self.changed.emit('params.' + k, e.currentText().strip()))
                    else:
                        editor.currentIndexChanged.connect(lambda unused, e=editor, k=key: self.changed.emit('params.' + k, str(e.currentData())))
                self.form.addWidget(editor)
            elif kind in ('STRING', 'TEXT') and media_type not in ('image', 'video', 'audio', 'file'):
                editor = self._text_editor(value, (doc_id, node['id'], key))
                editor.textChanged.connect(lambda e=editor, k=key: self.changed.emit('params.' + k, e.toPlainText()))
            else:
                row = QtWidgets.QHBoxLayout()
                editor = QtWidgets.QLineEdit(str(value))
                editor.editingFinished.connect(lambda e=editor, k=key: self.changed.emit('params.' + k, e.text()))
                row.addWidget(editor, 1)
                if media_type in ('image', 'video', 'audio', 'file', 'archive'):
                    browse = QtWidgets.QToolButton()
                    browse.setText('选择')
                    browse.clicked.connect(lambda unused=False, e=editor, k=key, t=media_type: self._pick_parameter_file(e, k, t))
                    browse.setEnabled(key not in connected)
                    row.addWidget(browse)
                    if media_type == 'image':
                        from aetherloom_core.mask_editor import add_mask_button
                        mask_button = add_mask_button(row, editor, self.dialog_parent,
                            node.get('params', {}).get('_masks', {}).get(key) or field.get('_mask'),
                            lambda value,k=key:self.changed.emit('params._masks',
                                dict(self.node.get('params', {}).get('_masks', {}), **{k:value})))
                        mask_button.setEnabled(key not in connected)
                    if key not in connected:
                        from aetherloom_core.image_import import ImageDropFilter
                        def accept_media(paths,e=editor,t=media_type):
                            kinds=('image','video','audio') if t=='file' else (t,)
                            if paths and os.path.isfile(paths[0]) and any(accepts(paths[0],k) for k in kinds):
                                e.setText(paths[0]);e.editingFinished.emit()
                        editor._image_drop=ImageDropFilter(editor,accept_media,media_type)
                self.form.addLayout(row)
            editor.setEnabled(key not in connected)
            if key in connected:
                editor.setToolTip('运行时使用连线输入；断开连线后恢复此处保存的值。')
            if self.embedded and isinstance(editor, (RhNumberSpinBox, RhEnumComboBox)):
                self.form.removeWidget(title);self.form.removeWidget(editor)
                holder = QtWidgets.QFrame(self)
                holder.setObjectName('canvasScalarRow')
                row = QtWidgets.QHBoxLayout(holder);row.setSpacing(5)
                row.setContentsMargins(8, 0, 3, 0)
                title.setMaximumWidth(100);title.setMinimumWidth(65)
                editor.setMinimumWidth(0)
                if isinstance(editor, RhNumberSpinBox):
                    from .node_form import NumberDrag
                    title._number_drag = NumberDrag(title, editor)
                row.addWidget(title, 2);row.addWidget(editor, 3)
                self.form.addWidget(holder)
                self.port_widgets[key] = editor
        self.form = main_form

    def _app_options(self, node):
        reuse = QtWidgets.QCheckBox('过滤重复运行')
        reuse.setObjectName('canvasAppFilterRepeats')
        reuse.setChecked(bool(node.get('filter_repeats', False)))
        reuse.setToolTip('开启后，参数、实际输入和结果均未变化时复用，包括后续画布批次。默认关闭；强制重跑忽略此设置。')
        reuse.toggled.connect(lambda value: self.changed.emit('filter_repeats', value))
        self.form.addWidget(reuse)

    def _decode_settings(self, node):
        self.decode_group = QtWidgets.QGroupBox('本地解码')
        decode = node.get('decode_settings', {})
        group = QtWidgets.QFormLayout(self.decode_group)
        group.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        enabled = QtWidgets.QCheckBox('下载后进行本地解码')
        enabled.setChecked(bool(decode.get('enabled', False)))
        enabled.toggled.connect(lambda value: self.changed.emit('decode_settings.enabled', value))
        if self.embedded and hasattr(self, 'decode_section'):
            enabled.toggled.connect(lambda value: self.decode_section.toggle.setText('本地解码 · ' + ('已启用' if value else '未启用')))
        group.addRow(enabled)
        mode = RhEnumComboBox()
        mode.addItem('GRC', 'grc')
        mode.addItem('SST', 'sst')
        mode.setCurrentIndex(max(0, mode.findData(decode.get('mode', 'grc'))))
        mode.currentIndexChanged.connect(lambda: self.changed.emit('decode_settings.mode', mode.currentData()))
        group.addRow('方式', mode)
        password = QtWidgets.QLineEdit(str(decode.get('password', '')))
        password.setEchoMode(QtWidgets.QLineEdit.Password)
        password.setPlaceholderText('此密码不会打包导出')
        password.editingFinished.connect(lambda: self.changed.emit('decode_settings.password', password.text()))
        group.addRow('密码', password)
        supply_password = QtWidgets.QPushButton('补充任务解码密码')
        supply_password.setToolTip('仅继续等待密码的本地解码，不会重新提交云端生成任务。')
        supply_password.clicked.connect(lambda: self.password_requested.emit(node['id']))
        group.addRow(supply_password)
        if decode.get('password_required') and not decode.get('password'):
            missing_password = QtWidgets.QLabel('此画布需要解码密码，请在运行前补齐。')
            missing_password.setObjectName('canvasWarning')
            missing_password.setWordWrap(True)
            group.addRow(missing_password)
        grid = RhNumberSpinBox(integer=True)
        grid.configure({'min': 4, 'max': 256})
        grid.setValue(decode.get('grid_cols', 32))
        grid.valueChanged.connect(lambda value: self.changed.emit('decode_settings.grid_cols', int(value)))
        self.numeric.append(grid)
        group.addRow('网格列数', grid)
        delete = QtWidgets.QCheckBox('解码成功后删除原图像')
        delete.setChecked(bool(decode.get('delete_original', True)))
        delete.toggled.connect(lambda value: self.changed.emit('decode_settings.delete_original', value))
        group.addRow(delete)
        self.form.addWidget(self.decode_group)

    def _pick_parameter_file(self, editor, key, kind):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.dialog_parent, '选择本地输入', '', FILE_FILTERS.get(kind.lower(), '所有文件 (*)'))
        if path:
            editor.setText(path)
            self.changed.emit('params.' + key, path)

    def _files(self, node):
        hint = QtWidgets.QLabel('选择、输入路径或拖入文件／文件夹。仅接收本节点支持的格式；文件夹在运行时读取当前层匹配文件，按文件名排序，不递归子文件夹。拖动条目可调整顺序。')
        hint.setWordWrap(True)
        hint.setObjectName('canvasMuted')
        self.form.addWidget(hint)
        files = FileList(kind=node['kind'])
        if self.embedded:
            files.setMinimumHeight(64);files.setMaximumHeight(96)
        files.setObjectName('canvasInputFiles')
        files.set_paths(node.get('params', {}).get('files', []))
        files.files_changed.connect(lambda values: self.changed.emit('params.files', values))
        self.form.addWidget(files)
        path_row = QtWidgets.QHBoxLayout()
        path_edit = QtWidgets.QLineEdit();path_edit.setObjectName('canvasInputPath')
        path_edit.setPlaceholderText('文件或文件夹路径')
        import_button = QtWidgets.QPushButton('导入')
        path_row.addWidget(path_edit, 1);path_row.addWidget(import_button);self.form.addLayout(path_row)
        def import_path():
            if files.import_paths([path_edit.text()]):path_edit.clear()
            else:self.message.emit('路径不存在、格式不匹配或已在列表中。')
        import_button.clicked.connect(import_path);path_edit.returnPressed.connect(import_path)
        row = QtWidgets.QGridLayout();row.setSpacing(6)
        for index, (text, callback) in enumerate([('文件', lambda: self._add_files(files, node['kind'])),
                               ('文件夹', lambda: self._add_folder(files)),
                               ('重新定位', lambda: self._relocate(files, node['kind'])),
                               ('移除', lambda: self._remove_files(files))]):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button, index // 2, index % 2)
        self.form.addLayout(row)

        if node['kind'] == 'image':
            mask_button = QtWidgets.QPushButton('遮罩 / 绘画');mask_button.setObjectName('canvasMaskButton')
            mask_button.setToolTip('编辑选中的图像；保存后仅替换该项输入为带遮罩的 PNG 副本')
            mask_button.clicked.connect(lambda:self._edit_input_mask(files))
            self.form.addWidget(mask_button)

    def _edit_input_mask(self, files):
        paths = files.paths()
        if not paths:
            self.message.emit('请先导入图像。');return
        index = files.currentRow()
        if index < 0:
            if len(paths) != 1:
                self.message.emit('请先选中要绘制遮罩的图像。');return
            index = 0
        path = paths[index]
        if os.path.isdir(path):
            selected, _ = QtWidgets.QFileDialog.getOpenFileName(self.dialog_parent, '选择文件夹中要绘制遮罩的图像', path, FILE_FILTERS['image'])
            if not selected:return
            from .media_inputs import resolve_files
            expanded = resolve_files([path], 'image')
            match = next((i for i, value in enumerate(expanded) if os.path.normcase(os.path.abspath(value)) == os.path.normcase(os.path.abspath(selected))), None)
            if match is None:
                self.message.emit('请选择该文件夹当前层中的图像。');return
            paths[index:index+1] = expanded
            index += match;path = paths[index]
        from aetherloom_core.mask_editor import edit_mask
        masks = list(self.node.get('params', {}).get('masks', []))
        from aetherloom_core.mask_assets import matches
        previous = next((value for value in masks if matches(value,path)),None)
        changed = edit_mask(path, self.dialog_parent, previous)
        if changed:
            files.set_paths(paths);files.setCurrentRow(index)
            files.files_changed.emit(paths)
            self.changed.emit('params.masks',[value for value in masks if not matches(value,path)]+[changed])

    def _add_files(self, files, kind):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self.dialog_parent, '添加素材', '', FILE_FILTERS[kind])
        files.import_paths(paths)

    def _add_folder(self, files):
        path = QtWidgets.QFileDialog.getExistingDirectory(self.dialog_parent, '选择输入文件夹')
        if path:files.import_paths([path])

    def _relocate(self, files, kind):
        index = files.currentRow()
        if index < 0:
            return
        if os.path.isdir(files.paths()[index]) or not Path(files.paths()[index]).suffix:
            path = QtWidgets.QFileDialog.getExistingDirectory(self.dialog_parent, '重新定位文件夹')
        else:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self.dialog_parent, '重新定位文件', '', FILE_FILTERS[kind])
        if path and accepts(path, kind):
            paths = files.paths()
            paths[index] = path
            files.set_paths(paths)
            files.files_changed.emit(paths)

    def _remove_files(self, files):
        for item in files.selectedItems():
            files.takeItem(files.row(item))
        files.files_changed.emit(files.paths())

    def _results(self, node):
        if node['kind'] == 'image_compare':
            from .image_compare import ImageCompare
            self.compare_view = ImageCompare(self)
            self.compare_view.set_results(node.get('results', []))
            self.form.addWidget(self.compare_view, 1)
        from .result_browser import ResultBrowser
        from .graphics import ThumbnailCache
        title = QtWidgets.QLabel('最近结果')
        self.results_title = title
        title.setObjectName('canvasSectionTitle')
        self.form.addWidget(title)
        application = QtWidgets.QApplication.instance()
        cache = getattr(application, '_canvas_result_previews', None)
        if cache is None:
            cache = ThumbnailCache(application, limit=12)
            application._canvas_result_previews = cache
            application.aboutToQuit.connect(cache.close)
        browser = ResultBrowser(node, cache, self)
        self.result_browser = browser
        listing = browser.listing
        self.results_list = listing
        browser.open_requested.connect(self._open_result)
        self.form.addWidget(browser, 1)
        copy_button = QtWidgets.QPushButton('复制预览文本')
        def copy_text():
            if browser.stack.currentWidget() is browser.text:
                QtWidgets.QApplication.clipboard().setText(browser.text.toPlainText())
        copy_button.clicked.connect(copy_text)
        self.form.addWidget(copy_button)
        row = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton('打开所选', self)
        open_button.clicked.connect(lambda: self._open_result(listing.currentItem().data(QtCore.Qt.UserRole)) if listing.currentItem() else None)
        save_button = QtWidgets.QPushButton('另存副本', self)
        save_button.setVisible(node['kind'] in {'app', 'preview'} | set(MODEL_KINDS))
        save_button.clicked.connect(lambda: self._save_results(listing))
        row.addWidget(open_button)
        row.addWidget(save_button)
        self.form.addLayout(row)
        def update_buttons():
            open_button.setEnabled(listing.currentItem() is not None)
            save_button.setEnabled(bool(listing.selectedItems()))
            copy_button.setVisible(listing.currentItem() is not None and browser.stack.currentWidget() is browser.text)
        listing.itemSelectionChanged.connect(update_buttons)
        listing.currentItemChanged.connect(update_buttons)
        update_buttons()

    def update_results(self, results):
        if hasattr(self, 'compare_view'):self.compare_view.set_results(results)
        if self.results_list is None:
            return
        self.results_title.setText(f'最近结果 · {len(results)}' + (' 个 Batch' if results and isinstance(results[0], dict) and results[0].get('type') == 'batch' else ''))
        self.result_browser.results = results
        self.result_browser.refresh()

    def _open_result(self, result):
        from .preview_data import path_of, text_of
        path = path_of(result)
        if path and os.path.exists(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        elif any(key in result for key in ('text', 'value')):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle('文本结果')
            dialog.resize(620, 420)
            layout = QtWidgets.QVBoxLayout(dialog)
            editor = QtWidgets.QPlainTextEdit(text_of(result))
            editor.setReadOnly(True)
            layout.addWidget(editor)
            dialog.exec_()
        else:
            self.message.emit('结果文件不存在。请重新定位或运行节点。')

    def _save_results(self, listing):
        selected = listing.selectedItems()
        if not selected:
            self.message.emit('请先选择需要另存的结果。')
            return
        destination = QtWidgets.QFileDialog.getExistingDirectory(self.dialog_parent, '选择副本保存目录', self.node.get('params', {}).get('save_directory', ''))
        if not destination:
            return
        try:
            from .save_results import save_results
            overwrite = self.findChild(QtWidgets.QCheckBox, 'canvasSaveOverwrite')
            saved = save_results([item.data(QtCore.Qt.UserRole) for item in selected], destination,
                                 overwrite=overwrite.isChecked() if overwrite is not None else False)
            count = len(saved)
            self.message.emit(f'已另存 {count} 个结果。')
        except (OSError, ValueError) as error:
            self.message.emit(f'另存失败：{error}')

    def validate(self):
        if hasattr(self,'group_editor') and not self.group_editor.validate():return False
        for editor in self.numeric:
            if editor.isEnabled() and not editor.commit():
                from .node_form import FoldSection
                ancestor = editor.parentWidget()
                while ancestor is not None:
                    if isinstance(ancestor, FoldSection):ancestor.set_expanded(True)
                    ancestor = ancestor.parentWidget()
                if self.tabs is not None:
                    for index in range(self.tabs.count()):
                        scroll = self.tabs.widget(index)
                        if scroll.isAncestorOf(editor):
                            self.tabs.setCurrentIndex(index)
                            scroll.ensureWidgetVisible(editor)
                            break
                editor.setFocus()
                return False
        return True


class EdgeInspector(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(str, object)
    message = QtCore.pyqtSignal(str)

    def __init__(self, edge, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(15, 14, 15, 16)
        title = QtWidgets.QLabel('连线设置')
        title.setObjectName('canvasSectionTitle')
        layout.addWidget(title)
        output_type = ITEM_OUTPUT_TYPES.get(edge.get('output'))
        hint = QtWidgets.QLabel(('Batch 输出：连线序号选择整组；选择组内成员请使用“列表取项”。\n' if edge.get('output') == 'batch'
                                else output_type + ' List：只传递此类型的结果，序号在此类型列表内计算。\n' if output_type else '')
                                + '输入使用连线选中的结果；断开后恢复节点的手填参数。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        mode = RhEnumComboBox()
        for text, value in [('首个匹配结果', 'first'), ('指定结果序号', 'index'), ('全部匹配结果逐项运行', 'all')]:
            mode.addItem(text, value)
        mode.setCurrentIndex(max(0, mode.findData(edge.get('mode', 'all'))))
        layout.addWidget(mode)
        indices = QtWidgets.QLineEdit(', '.join(map(str, edge.get('indices') or [1])), self)
        indices.setPlaceholderText('例如 1, 3（从 1 开始）')
        indices.setVisible(mode.currentData() == 'index')
        layout.addWidget(indices)
        mode.currentIndexChanged.connect(lambda: (indices.setVisible(mode.currentData() == 'index'), self.changed.emit('mode', mode.currentData())))
        indices.editingFinished.connect(lambda: self._indices(indices))
        explanation = QtWidgets.QLabel('批量处理请选择“全部匹配结果逐项运行”。多个分支有共同来源时按来源对应，可将一个原文件名用于它生成的多项结果；无关联时按顺序配对、单项复用。缺失或歧义会阻止执行。')
        explanation.setWordWrap(True)
        explanation.setObjectName('canvasMuted')
        layout.addWidget(explanation)
        layout.addStretch()

    def _indices(self, editor):
        try:
            self.changed.emit('indices', parse_indices(editor.text()))
        except ValueError:
            self.message.emit('请输入有效结果序号，例如 1, 3。')
