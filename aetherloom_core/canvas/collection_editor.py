"""Compact, consistent settings for List / Batch transformations."""
import copy
from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from aetherloom_core.rh_parameters import RhEnumComboBox, RhNumberSpinBox
from . import collections, model


def build(panel, node):
    kind, params = node['kind'], node.get('params', {})
    route, description, example = collections.HELP[kind]
    if not panel.embedded:
        summary = QtWidgets.QLabel(route);summary.setObjectName('canvasCollectionRoute');panel.form.addWidget(summary)
        hint = QtWidgets.QLabel(description);hint.setWordWrap(True);panel.form.addWidget(hint)
    else:panel.setToolTip(description + '\n' + example)
    form = QtWidgets.QFormLayout();form.setFieldGrowthPolicy(form.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(form.WrapLongRows);form.setVerticalSpacing(10);panel.form.addLayout(form)
    def number(key, label, minimum, maximum, default):
        editor = RhNumberSpinBox(integer=True);editor.setObjectName('collection_' + key)
        editor.configure({'min': minimum, 'max': maximum});editor.setValue(params.get(key, default))
        editor.valueChanged.connect(lambda value:panel.changed.emit('params.' + key, int(value)))
        panel.numeric.append(editor);form.addRow(label, editor)
        return editor
    if kind in ('list2batch', 'rebatch'):
        number('batch_size', '每组数量' + ('（0 = 全部）' if kind == 'list2batch' else ''),
               0 if kind == 'list2batch' else 1, 256, 0 if kind == 'list2batch' else 1)
    if kind in ('list_select', 'batch_select'):
        types = RhEnumComboBox();types.setObjectName('collection_type');types.addItem('全部类型', 'any')
        for key, label in model.filter_type_options(params.get('type', 'any'), batch=kind == 'list_select'):
            if key != 'any':types.addItem(label, key)
        types.setCurrentIndex(max(0, types.findData(params.get('type', 'any'))))
        types.currentIndexChanged.connect(lambda:panel.changed.emit('params.type', types.currentData()))
        form.addRow('内容类型', types)
        mode = RhEnumComboBox();mode.setObjectName('collection_selection_mode')
        mode.addItem('指定索引', 'indices');mode.addItem('连续范围', 'range')
        mode.setCurrentIndex(max(0, mode.findData(params.get('selection_mode', 'indices'))));form.addRow('取项方式', mode)
        indices = QtWidgets.QLineEdit(', '.join(map(str, params.get('indices', [0]))))
        indices.setObjectName('collection_indices');indices.setPlaceholderText('例如 0, 2, -1');form.addRow('索引', indices)
        start = number('start', '起始索引', -1_000_000, 1_000_000, 0)
        length = number('length', '数量', 1, 1_000_000, 1)
        def commit_indices():
            try:
                value = [int(s.strip()) for s in indices.text().replace('，', ',').split(',') if s.strip()]
                candidate = copy.deepcopy(node);candidate['params']['indices'] = value;collections.validate(candidate)
                indices.setProperty('invalid', False);panel.changed.emit('params.indices', value)
            except (TypeError, ValueError) as error:
                indices.setProperty('invalid', True);panel.message.emit(str(error))
        indices.editingFinished.connect(commit_indices)
        def visibility():
            explicit = mode.currentData() == 'indices'
            for widget, visible in ((indices, explicit), (start, not explicit), (length, not explicit)):
                widget.setVisible(visible);form.labelForField(widget).setVisible(visible)
        mode.currentIndexChanged.connect(lambda:(visibility(), panel.changed.emit('params.selection_mode', mode.currentData())))
        visibility()
        note = QtWidgets.QLabel('索引从 0 开始；-1 表示最后一项。先按类型筛选，再取项。越界会报错，不截断。')
        note.setObjectName('canvasMuted');note.setWordWrap(True);panel.form.addWidget(note)
    if panel.embedded:return
    help_toggle = QtWidgets.QToolButton();help_toggle.setText('示例与规则')
    help_toggle.setCheckable(True);help_toggle.setArrowType(Qt.RightArrow)
    help_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    panel.form.addWidget(help_toggle)
    sample = QtWidgets.QLabel(example + '\n\nList 逐项传递；Batch 整组传递。文件只引用、不转换尺寸。')
    sample.setObjectName('canvasCollectionExample');sample.setWordWrap(True);panel.form.addWidget(sample)
    sample.hide()
    help_toggle.toggled.connect(lambda checked:(sample.setVisible(checked), help_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)))


def legacy_notice(panel, node):
    note = QtWidgets.QLabel('此节点保留旧画布规则。切换新版会改变下次运行的 List / Batch 处理方式；当前运行不受影响。')
    note.setWordWrap(True);note.setObjectName('canvasMuted');panel.form.addWidget(note)
    button = QtWidgets.QPushButton('切换新版 List / Batch 规则');button.setObjectName('collection_upgrade')
    button.clicked.connect(lambda:panel.changed.emit('params', collections.upgraded(node)));panel.form.addWidget(button)
