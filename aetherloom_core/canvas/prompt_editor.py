"""Prompt forms shared by the inline canvas and full node inspector."""
import copy
import uuid
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_parameters import RhNumberSpinBox, RhEnumComboBox
from aetherloom_core.ui.widgets import CompletionTextEdit
from . import prompt_nodes


def _hint(text, parent):
    label = QtWidgets.QLabel(text, parent)
    label.setWordWrap(True);label.setTextFormat(QtCore.Qt.PlainText)
    label.setObjectName('canvasMuted')
    return label


class StylesDialog(QtWidgets.QDialog):
    """Edit a private style library; nothing is saved until Apply is accepted."""

    def __init__(self, styles, selected=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle('管理提示词样式');self.resize(780, 620)
        self.records = {};self.original_selected = list(selected);self._current = None
        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(_hint('样式保存在当前节点。{prompt} 代入当前提示词；没有占位符时追加。导入 UTF-8 的 A1111 CSV 或 JSON，同名样式会替换。', self))
        body = QtWidgets.QHBoxLayout();root.addLayout(body, 1)
        left = QtWidgets.QVBoxLayout();body.addLayout(left, 1)
        self.list = QtWidgets.QListWidget(self);self.list.setObjectName('canvasPromptStyleLibrary')
        self.list.setMinimumWidth(170);left.addWidget(self.list, 1)
        controls = QtWidgets.QHBoxLayout();left.addLayout(controls)
        self.add_button = QtWidgets.QPushButton('新增', self)
        self.remove_button = QtWidgets.QPushButton('删除', self)
        controls.addWidget(self.add_button);controls.addWidget(self.remove_button)
        self.import_button = QtWidgets.QPushButton('导入 CSV / JSON…', self);left.addWidget(self.import_button)
        right = QtWidgets.QVBoxLayout();body.addLayout(right, 3)
        right.addWidget(QtWidgets.QLabel('样式名称', self))
        self.name = QtWidgets.QLineEdit(self);self.name.setObjectName('canvasPromptStyleName');right.addWidget(self.name)
        self.positive = self._editor(right, '正向样式', 'canvasPromptStylePositive')
        self.negative = self._editor(right, '反向样式', 'canvasPromptStyleNegative')
        self.error = QtWidgets.QLabel(self);self.error.setWordWrap(True);self.error.setObjectName('canvasWarning')
        self.error.setTextFormat(QtCore.Qt.PlainText);root.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel, self)
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText('应用样式库')
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消')
        root.addWidget(buttons)
        self.list.currentItemChanged.connect(self._select)
        self.name.textChanged.connect(self._rename)
        self.add_button.clicked.connect(self.add_style)
        self.remove_button.clicked.connect(self.remove_style)
        self.import_button.clicked.connect(self.choose_import)
        buttons.accepted.connect(self._accept);buttons.rejected.connect(self.reject)
        for style in prompt_nodes.normalize_styles(styles):self._append(style, style['name'])
        if self.list.count():self.list.setCurrentRow(0)
        else:self._select(None)

    def _editor(self, layout, title, object_name):
        row = QtWidgets.QHBoxLayout();row.addWidget(QtWidgets.QLabel(title, self), 1)
        editor = CompletionTextEdit(self);editor.setObjectName(object_name)
        editor.setAcceptRichText(False);editor.setMinimumHeight(130)
        editor.setPlaceholderText('例如：cinematic lighting, {prompt}')
        editor.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        for glyph, tip, callback in (('↶', '撤销文本编辑', editor.undo), ('↷', '重做文本编辑', editor.redo)):
            button = QtWidgets.QToolButton(self);button.setText(glyph);button.setToolTip(tip)
            button.clicked.connect(callback);row.addWidget(button)
        layout.addLayout(row);layout.addWidget(editor, 1)
        return editor

    def _append(self, style, original_name=None):
        identity = uuid.uuid4().hex
        record = dict(name=style['name'], original_name=original_name)
        for key in ('positive', 'negative'):
            document = QtGui.QTextDocument(self);document.setPlainText(style.get(key, ''))
            record[key] = document
        self.records[identity] = record
        item = QtWidgets.QListWidgetItem(style['name']);item.setData(QtCore.Qt.UserRole, identity)
        self.list.addItem(item)
        return item

    def _select(self, item, previous=None):
        self._current = item.data(QtCore.Qt.UserRole) if item is not None else None
        record = self.records.get(self._current)
        for editor in (self.name, self.positive, self.negative):editor.setEnabled(record is not None)
        self.remove_button.setEnabled(record is not None)
        with QtCore.QSignalBlocker(self.name):self.name.setText(record['name'] if record else '')
        for key in ('positive', 'negative'):
            editor = getattr(self, key)
            with QtCore.QSignalBlocker(editor):
                if record:editor.setDocument(record[key])
                else:
                    blank = QtGui.QTextDocument(self);editor.setDocument(blank)

    def _rename(self, text):
        if self._current is None:return
        self.records[self._current]['name'] = text
        self.list.currentItem().setText(text or '（未命名）')
        self.error.clear()

    def add_style(self):
        if self.list.count() >= prompt_nodes.MAX_STYLES:
            self.error.setText('样式库最多 1000 项');return
        names = {record['name'] for record in self.records.values()};index = 1
        while '样式 ' + str(index) in names:index += 1
        item = self._append(dict(name='样式 ' + str(index), positive='{prompt}', negative=''))
        self.list.setCurrentItem(item);self.name.setFocus();self.name.selectAll()
        self.error.clear()

    def remove_style(self):
        item = self.list.currentItem()
        if item is None:return
        identity = item.data(QtCore.Qt.UserRole)
        self.list.takeItem(self.list.row(item));self.records.pop(identity, None)
        if not self.list.count():self._select(None)
        self.error.clear()

    def styles(self):
        rows = []
        for index in range(self.list.count()):
            record = self.records[self.list.item(index).data(QtCore.Qt.UserRole)]
            rows.append(dict(name=record['name'], positive=record['positive'].toPlainText(), negative=record['negative'].toPlainText()))
        return prompt_nodes.normalize_styles(rows)

    def selected(self):
        renamed = {record['original_name']: record['name'].strip() for record in self.records.values() if record['original_name'] is not None}
        return [renamed[name] for name in self.original_selected if name in renamed]

    def import_text(self, text, file_format):
        """Used by the explicit file picker and by offline UI checks."""
        incoming = prompt_nodes.import_styles(text, file_format)
        existing = {record['name'].strip(): identity for identity, record in self.records.items()}
        if self.list.count() + sum(style['name'] not in existing for style in incoming) > prompt_nodes.MAX_STYLES:
            raise ValueError('合并后的样式库超过 1000 项')
        # Validate the complete replacement first; a failed import is atomic.
        replacements = {style['name']: style for style in incoming}
        merged = [replacements.get(style['name'], style) for style in self.styles()]
        merged.extend(style for style in incoming if style['name'] not in existing)
        prompt_nodes.normalize_styles(merged)
        for style in incoming:
            identity = existing.get(style['name'])
            if identity is None:self._append(style)
            else:
                for key in ('positive', 'negative'):self.records[identity][key].setPlainText(style[key])
        if self.list.currentRow() < 0 and self.list.count():self.list.setCurrentRow(0)
        self.error.setText('已导入 ' + str(len(incoming)) + ' 个样式；应用后保存到当前节点。')

    def choose_import(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, '导入提示词样式', '', '样式文件 (*.csv *.json)')
        if not filename:return
        try:
            with Path(filename).open('rb') as stream:data = stream.read(prompt_nodes.MAX_TEXT * 4 + 1)
            if len(data) > prompt_nodes.MAX_TEXT * 4:raise ValueError('样式文件过大')
            self.import_text(data.decode('utf-8-sig'), Path(filename).suffix)
        except (OSError, UnicodeError, ValueError) as error:self.error.setText(str(error))

    def _accept(self):
        try:self.styles()
        except ValueError as error:self.error.setText(str(error));return
        self.accept()


class StyleSelection(QtWidgets.QWidget):
    """An ordered style stack, with a separate private-library editor."""

    def __init__(self, panel, node):
        super().__init__(panel)
        self.panel = panel
        params = dict(prompt_nodes.defaults('prompt_styles'), **node.get('params', {}))
        self.styles = prompt_nodes.normalize_styles(params['styles'])
        self.selected = prompt_nodes.selected_styles(params, self.styles)
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0, 0, 0, 0);layout.setSpacing(5)
        row = QtWidgets.QHBoxLayout();layout.addLayout(row)
        self.combo = RhEnumComboBox(self);self.combo.setObjectName('canvasPromptStyleChoice')
        self.combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(6);self.combo.setMinimumWidth(0)
        self.add_button = QtWidgets.QPushButton('加入', self);row.addWidget(self.combo, 1);row.addWidget(self.add_button)
        self.list = QtWidgets.QListWidget(self);self.list.setObjectName('canvasPromptSelectedStyles')
        self.list.setMaximumHeight(100);self.list.setMinimumHeight(54);layout.addWidget(self.list)
        controls = QtWidgets.QHBoxLayout();layout.addLayout(controls)
        for title, callback in (('上移', lambda: self.move(-1)), ('下移', lambda: self.move(1)), ('移除', self.remove)):
            button = QtWidgets.QPushButton(title, self);button.clicked.connect(callback);controls.addWidget(button)
        self.manage_button = QtWidgets.QPushButton('管理 / 导入样式库…', self);layout.addWidget(self.manage_button)
        self.add_button.clicked.connect(self.add);self.manage_button.clicked.connect(self.manage)
        self.refresh()

    def refresh(self, current=-1):
        self.combo.clear()
        for style in self.styles:
            if style['name'] not in self.selected:self.combo.addItem(style['name'], style['name'])
        self.combo.setEnabled(bool(self.combo.count()));self.add_button.setEnabled(bool(self.combo.count()))
        self.list.clear()
        for index, name in enumerate(self.selected):self.list.addItem(str(index + 1) + '. ' + name)
        if self.selected:self.list.setCurrentRow(max(0, min(current, len(self.selected) - 1)))
        else:self.list.setToolTip('未选择样式，正向与反向提示词将原样输出')

    def _commit(self, current=-1):
        self.refresh(current)
        self.panel.changed.emit('params.selected_styles', list(self.selected))

    def add(self):
        name = self.combo.currentData()
        if name is None or name in self.selected:return
        self.selected.append(name);self._commit(len(self.selected) - 1)

    def remove(self):
        row = self.list.currentRow()
        if row < 0:return
        self.selected.pop(row);self._commit(row)

    def move(self, direction):
        row = self.list.currentRow();target = row + direction
        if row < 0 or not 0 <= target < len(self.selected):return
        self.selected[row], self.selected[target] = self.selected[target], self.selected[row]
        self._commit(target)

    def manage(self):
        dialog = StylesDialog(self.styles, self.selected, self.panel.dialog_parent)
        try:
            if dialog.exec_() != QtWidgets.QDialog.Accepted:return
            self.styles = dialog.styles();self.selected = dialog.selected();self.refresh()
            params = copy.deepcopy(self.panel.node.get('params', {}))
            params.update(styles=self.styles, selected_styles=self.selected)
            self.panel.changed.emit('params', params)
        finally:dialog.deleteLater()


def build(panel, node):
    from .node_form import FieldLabel, NumberDrag
    params = dict(prompt_nodes.defaults(node['kind']), **node.get('params', {}))
    for key, label, kind, default, bounds in prompt_nodes.SCHEMAS[node['kind']][2]:
        caption = FieldLabel(label, panel);caption.setObjectName('canvasFieldLabel');panel.form.addWidget(caption)
        value = params[key]
        if kind == 'text':
            editor = panel._text_editor(str(value), (panel.doc_id, node['id'], key))
            editor.textChanged.connect(lambda k=key, e=editor: panel.changed.emit('params.' + k, e.toPlainText()))
        elif kind == 'wildcards':
            # The apply boundary preserves invalid JSON drafts without making
            # them the execution configuration. Stored dictionaries render as JSON.
            editor = panel._text_editor(prompt_nodes.wildcard_text(value), (panel.doc_id, node['id'], key))
            editor.setPlaceholderText('{"天气": ["晴天", "雨天"]}')
            apply = QtWidgets.QPushButton('应用词库', panel)
            apply.setObjectName('canvasApplyWildcards')
            apply.setEnabled(key not in panel.connected_inputs)
            from .node_form import TextToolsState
            apply._enabled_state = TextToolsState(editor, apply)
            def commit(_checked=False, e=editor):
                try:prompt_nodes.parse_wildcards(e.toPlainText())
                except ValueError as error:panel.message.emit(str(error));return
                panel.changed.emit('params.wildcards', e.toPlainText())
                from .inline_text import sync_document
                sync_document(e.document(), e.toPlainText())
                panel.message.emit('词库已保存到当前节点')
            apply.clicked.connect(commit);panel.form.addWidget(apply)
            editor.setToolTip('JSON 对象：名称对应文本数组，也支持以换行分隔的字符串。编辑后点击应用词库；连接上游后使用上游 JSON。')
        elif kind == 'int':
            editor = RhNumberSpinBox(integer=True)
            editor.configure({'min': bounds[0], 'max': bounds[1], 'step': 1});editor.setValue(value)
            panel.numeric.append(editor);caption._number_drag = NumberDrag(caption, editor)
            editor.valueChanged.connect(lambda v, k=key: panel.changed.emit('params.' + k, str(v)))
            panel.form.addWidget(editor)
        else:
            caption.hide();editor = QtWidgets.QCheckBox(label, panel);editor.setChecked(value)
            editor.toggled.connect(lambda v, k=key: panel.changed.emit('params.' + k, v));panel.form.addWidget(editor)
        editor.setObjectName('canvasUtility_' + key);editor.setEnabled(key not in panel.connected_inputs)
        if any(port['key'] == key for port in prompt_nodes.inputs(node)):panel.port_widgets[key] = editor
    if node['kind'] == 'prompt_styles':
        caption = FieldLabel('样式组合（从上到下应用）', panel);caption.setObjectName('canvasFieldLabel');panel.form.addWidget(caption)
        panel.prompt_styles = StyleSelection(panel, node);panel.form.addWidget(panel.prompt_styles)
    panel.form.addWidget(_hint(prompt_nodes.HINTS[node['kind']], panel))
