"""Independent node inspectors reusing AetherLoom's exact parameter editors."""
import copy
import os
import shutil
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_parameters import RhNumberSpinBox, RhEnumComboBox, configure_list_combo
from aetherloom_core.prompt_history import PromptHistory
from aetherloom_core.ui.widgets import CompletionTextEdit
from .model import parameter_key, field_type


FILE_FILTERS = {
    'image': '图像 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff);;所有文件 (*)',
    'video': '视频 (*.mp4 *.webm *.mov *.mkv *.avi);;所有文件 (*)',
    'audio': '音频 (*.wav *.mp3 *.flac *.ogg *.m4a *.aac);;所有文件 (*)',
}


class FileList(QtWidgets.QListWidget):
    files_changed = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
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
        if not os.path.isfile(path):
            item.setText('⚠ ' + item.text())
            item.setForeground(QtGui.QColor('#e2a268'))
        self.addItem(item)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and os.path.isfile(url.toLocalFile()):
                    self.add_path(url.toLocalFile())
            self.files_changed.emit(self.paths())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


def parse_indices(text):
    parts = [part.strip() for part in str(text).replace('，', ',').split(',') if part.strip()]
    values = [int(part) for part in parts]
    if not values or any(value < 1 for value in values):
        raise ValueError('请输入从 1 开始的结果序号，例如 1, 3')
    return list(dict.fromkeys(values))


class Inspector(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(str, object)
    rebind_requested = QtCore.pyqtSignal(str)
    password_requested = QtCore.pyqtSignal(str)
    install_requested = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str)

    def __init__(self, node, doc_id, edges, histories, parent=None, missing_app=False, changed_definition=False):
        super().__init__(parent)
        self.node = node
        self.results_list = None
        self.install_button = None
        self.numeric = []
        self.histories = histories
        self.form = QtWidgets.QVBoxLayout(self)
        self.form.setContentsMargins(15, 14, 15, 16)
        self.form.setSpacing(11)
        title = QtWidgets.QLabel('节点设置')
        title.setObjectName('canvasSectionTitle')
        self.form.addWidget(title)
        name = QtWidgets.QLineEdit(str(node.get('title', '')))
        name.setPlaceholderText('节点名称')
        name.editingFinished.connect(lambda: self.changed.emit('title', name.text().strip() or '节点'))
        self.form.addWidget(name)
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
        if node['kind'] == 'app':
            self._app_link(node)
            self._app_fields(node, doc_id, edges)
        elif node['kind'] in FILE_FILTERS:
            self._files(node)
        elif node['kind'] == 'text':
            editor = self._text_editor(node.get('params', {}).get('text', ''), (doc_id, node['id'], 'text'))
            editor.textChanged.connect(lambda: self.changed.emit('params.text', editor.toPlainText()))
            hint = QtWidgets.QLabel('文本会作为一个完整输入传递。支持提示词补全和本次会话的文本回退 / 前进。')
            hint.setWordWrap(True)
            hint.setObjectName('canvasMuted')
            self.form.addWidget(hint)
        elif node['kind'] == 'select':
            type_combo = RhEnumComboBox()
            for label, value in [('全部类型', 'any'), ('图像', 'image'), ('视频', 'video'), ('音频', 'audio'), ('文本', 'text')]:
                type_combo.addItem(label, value)
            type_combo.setCurrentIndex(max(0, type_combo.findData(node.get('params', {}).get('type', 'any'))))
            type_combo.currentIndexChanged.connect(lambda: self.changed.emit('params.type', type_combo.currentData()))
            self.form.addWidget(QtWidgets.QLabel('结果类型'))
            self.form.addWidget(type_combo)
            indices = QtWidgets.QLineEdit(', '.join(map(str, node.get('params', {}).get('indices') or [1])))
            indices.setPlaceholderText('例如 1, 3（从 1 开始）')
            indices.editingFinished.connect(lambda: self._indices_changed(indices, 'params.indices'))
            self.form.addWidget(QtWidgets.QLabel('保留的序号'))
            self.form.addWidget(indices)
        if node['kind'] != 'app':
            reuse_hint = QtWidgets.QLabel('内置节点自动复用未变化的有效结果。')
            reuse_hint.setObjectName('canvasBuiltinReuseHint')
            reuse_hint.setWordWrap(True)
            self.form.addWidget(reuse_hint)
        if node.get('results') or node['kind'] in ('app', 'select', 'preview'):
            self._results(node)
        self.form.addStretch(1)

    def _app_link(self, node):
        from .model import app_reference
        app = node.get('app') or {}
        self.form.addWidget(QtWidgets.QLabel('App 链接 · ' + str(app.get('webapp_id') or '')))
        self.app_url_editor = QtWidgets.QLineEdit()
        self.app_url_editor.setMaxLength(2048)
        self.app_url_editor.setPlaceholderText('https://www.runninghub.cn/webapp/…')
        self.app_url_error = QtWidgets.QLabel()
        self.app_url_error.setWordWrap(True)
        self.app_url_error.setObjectName('canvasWarning')
        try:
            reference = app_reference(app)
            self.app_url_editor.setText(reference['url'])
            self.app_url_editor.setToolTip(reference['url'])
            self.app_url_error.hide()
        except ValueError as error:
            self.app_url_error.setText(str(error))
        def commit():
            candidate = dict(app, url=self.app_url_editor.text().strip())
            try:
                if not candidate['url']:
                    raise ValueError('请填写此 App 的官方链接。')
                candidate.pop('url_error', None)
                reference = app_reference(candidate)
            except ValueError as error:
                self.app_url_error.setText(str(error))
                self.app_url_error.show()
                return
            self.app_url_error.hide()
            self.app_url_editor.setText(reference['url'])
            self.app_url_editor.setToolTip(reference['url'])
            self.changed.emit('app.url', reference['url'])
        self.app_url_editor.editingFinished.connect(commit)
        self.form.addWidget(self.app_url_editor)
        self.form.addWidget(self.app_url_error)

    def _indices_changed(self, editor, path):
        try:
            values = parse_indices(editor.text())
            editor.setProperty('invalid', False)
            self.changed.emit(path, values)
        except (ValueError, TypeError):
            editor.setProperty('invalid', True)
            self.message.emit('请输入从 1 开始的结果序号，例如 1, 3')

    def _text_editor(self, text, identity):
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        back, forward = QtWidgets.QToolButton(), QtWidgets.QToolButton()
        back.setText('回退')
        forward.setText('前进')
        row.addWidget(back)
        row.addWidget(forward)
        self.form.addLayout(row)
        editor = CompletionTextEdit(self)
        editor.setPlainText(str(text))
        editor.setMinimumHeight(125)
        editor.setMaximumHeight(240)
        editor.setAcceptRichText(False)
        PromptHistory(editor, back, forward, self.histories.setdefault(identity, []))
        self.form.addWidget(editor)
        return editor

    def _app_fields(self, node, doc_id, edges):
        connected = {edge['input'] for edge in edges if edge['target'] == node['id']}
        params = node.get('params', {})
        fields = node.get('app', {}).get('nodes', [])
        for index, field in enumerate(fields):
            key = parameter_key(field)
            value = params.get(key, field.get('fieldValue', ''))
            label = str(field.get('description') or field.get('nodeName') or field.get('fieldName') or f'参数 {index + 1}')
            title = QtWidgets.QLabel(label + ('  · 连线输入' if key in connected else ''))
            title.setWordWrap(True)
            title.setToolTip(f"{field.get('nodeId', '')} · {field.get('fieldName', '')}")
            self.form.addWidget(title)
            kind = str(field.get('fieldType') or '').upper()
            media_type = field_type(field)
            if kind in ('FLOAT', 'DOUBLE', 'NUMBER', 'INT', 'INTEGER'):
                editor = RhNumberSpinBox(integer=kind in ('INT', 'INTEGER'))
                editor.configure(field.get('fieldData'))
                try:
                    editor.setValue(value)
                except (ValueError, TypeError):
                    editor.lineEdit().setText(str(value))
                self.numeric.append(editor)
                editor.valueChanged.connect(lambda value, k=key: self.changed.emit('params.' + k, str(value)))
                self.form.addWidget(editor)
            elif kind in ('LIST', 'BOOLEAN', 'BOOL'):
                editor = RhEnumComboBox()
                if kind in ('BOOLEAN', 'BOOL'):
                    editor.addItems(['true', 'false'])
                    editor.setCurrentText(str(value).lower())
                    editor.currentTextChanged.connect(lambda value, k=key: self.changed.emit('params.' + k, value))
                else:
                    configure_list_combo(editor, field.get('fieldData'), value)
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
                if media_type in ('image', 'video', 'audio', 'file'):
                    browse = QtWidgets.QToolButton()
                    browse.setText('选择')
                    browse.clicked.connect(lambda unused=False, e=editor, k=key, t=media_type: self._pick_parameter_file(e, k, t))
                    browse.setEnabled(key not in connected)
                    row.addWidget(browse)
                self.form.addLayout(row)
            editor.setEnabled(key not in connected)
            if key in connected:
                editor.setToolTip('运行时使用连线输入；断开连线后恢复此处保存的值。')
        row = QtWidgets.QFormLayout()
        count = RhNumberSpinBox(integer=True)
        count.configure({'min': 1, 'max': 99, 'step': 1})
        count.setValue(node.get('run_count', 1))
        count.valueChanged.connect(lambda value: self.changed.emit('run_count', int(value)))
        self.numeric.append(count)
        row.addRow('每组输入运行次数', count)
        self.form.addLayout(row)
        reuse = QtWidgets.QCheckBox('过滤重复运行')
        reuse.setObjectName('canvasAppFilterRepeats')
        reuse.setChecked(bool(node.get('filter_repeats', False)))
        reuse.setToolTip('开启后，参数、实际输入和结果均未变化时复用，包括后续画布批次。默认关闭；强制重跑忽略此设置。')
        reuse.toggled.connect(lambda value: self.changed.emit('filter_repeats', value))
        self.form.addWidget(reuse)
        self.decode_group = QtWidgets.QGroupBox('本地解码')
        decode = node.get('decode_settings', {})
        group = QtWidgets.QFormLayout(self.decode_group)
        enabled = QtWidgets.QCheckBox('下载后进行本地解码')
        enabled.setChecked(bool(decode.get('enabled', False)))
        enabled.toggled.connect(lambda value: self.changed.emit('decode_settings.enabled', value))
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
        supply_password = QtWidgets.QPushButton('为未完成任务补充解码密码')
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
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择本地输入', '', FILE_FILTERS.get(kind.lower(), '所有文件 (*)'))
        if path:
            editor.setText(path)
            self.changed.emit('params.' + key, path)

    def _files(self, node):
        hint = QtWidgets.QLabel('拖入文件或点击添加；拖动条目可以调整输入顺序。')
        hint.setWordWrap(True)
        hint.setObjectName('canvasMuted')
        self.form.addWidget(hint)
        files = FileList()
        files.set_paths(node.get('params', {}).get('files', []))
        files.files_changed.connect(lambda values: self.changed.emit('params.files', values))
        self.form.addWidget(files)
        row = QtWidgets.QHBoxLayout()
        for text, callback in [('添加', lambda: self._add_files(files, node['kind'])),
                               ('重新定位', lambda: self._relocate(files, node['kind'])),
                               ('移除', lambda: self._remove_files(files))]:
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        self.form.addLayout(row)

    def _add_files(self, files, kind):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '添加素材', '', FILE_FILTERS[kind])
        for path in paths:
            files.add_path(path)
        if paths:
            files.files_changed.emit(files.paths())

    def _relocate(self, files, kind):
        index = files.currentRow()
        if index < 0:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '重新定位文件', '', FILE_FILTERS[kind])
        if path:
            paths = files.paths()
            paths[index] = path
            files.set_paths(paths)
            files.files_changed.emit(paths)

    def _remove_files(self, files):
        for item in files.selectedItems():
            files.takeItem(files.row(item))
        files.files_changed.emit(files.paths())

    def _results(self, node):
        title = QtWidgets.QLabel('最近结果')
        self.results_title = title
        title.setObjectName('canvasSectionTitle')
        self.form.addWidget(title)
        listing = QtWidgets.QListWidget()
        self.results_list = listing
        listing.setMinimumHeight(110)
        listing.setSelectionMode(listing.ExtendedSelection)
        self.update_results(node.get('results', []))
        listing.itemDoubleClicked.connect(lambda item: self._open_result(item.data(QtCore.Qt.UserRole)))
        self.form.addWidget(listing)
        row = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton('打开所选')
        open_button.clicked.connect(lambda: self._open_result(listing.currentItem().data(QtCore.Qt.UserRole)) if listing.currentItem() else None)
        save_button = QtWidgets.QPushButton('另存副本')
        save_button.clicked.connect(lambda: self._save_results(listing))
        row.addWidget(open_button)
        row.addWidget(save_button)
        self.form.addLayout(row)

    def update_results(self, results):
        if self.results_list is None:
            return
        listing = self.results_list
        listing.clear()
        self.results_title.setText(f'最近结果 · {len(results)}')
        for index, result in enumerate(results):
            value = result if isinstance(result, dict) else {'path': str(result)}
            path = value.get('path', '')
            text = value.get('text', '')
            item = QtWidgets.QListWidgetItem(f'{index + 1}. ' + (os.path.basename(path) if path else str(text)[:100]))
            item.setData(QtCore.Qt.UserRole, value)
            item.setToolTip(path or str(text))
            listing.addItem(item)

    def _open_result(self, result):
        path = result.get('path')
        if path and os.path.isfile(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        elif result.get('text'):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle('文本结果')
            dialog.resize(620, 420)
            layout = QtWidgets.QVBoxLayout(dialog)
            editor = QtWidgets.QPlainTextEdit(str(result['text']))
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
        destination = QtWidgets.QFileDialog.getExistingDirectory(self, '选择副本保存目录')
        if not destination:
            return
        count = 0
        try:
            for item in selected:
                result = item.data(QtCore.Qt.UserRole)
                path = result.get('path')
                if path and os.path.isfile(path):
                    target = Path(destination) / Path(path).name
                    suffix = 1
                    while target.exists():
                        target = Path(destination) / f'{Path(path).stem}_{suffix}{Path(path).suffix}'
                        suffix += 1
                    shutil.copy2(path, target)
                elif 'text' in result:
                    target = Path(destination) / '文本结果.txt'
                    suffix = 1
                    while target.exists():
                        target = Path(destination) / f'文本结果_{suffix}.txt'
                        suffix += 1
                    target.write_text(str(result['text']), encoding='utf-8')
                else:
                    continue
                count += 1
            self.message.emit(f'已另存 {count} 个结果。')
        except OSError as error:
            self.message.emit(f'另存失败：{error}')

    def validate(self):
        for editor in self.numeric:
            if editor.isEnabled() and not editor.commit():
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
        hint = QtWidgets.QLabel('输入使用连线选中的结果；断开后恢复节点的手填参数。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        mode = RhEnumComboBox()
        for text, value in [('首个匹配结果', 'first'), ('指定结果序号', 'index'), ('全部匹配结果逐项运行', 'all')]:
            mode.addItem(text, value)
        mode.setCurrentIndex(max(0, mode.findData(edge.get('mode', 'first'))))
        layout.addWidget(mode)
        indices = QtWidgets.QLineEdit(', '.join(map(str, edge.get('indices') or [1])))
        indices.setPlaceholderText('例如 1, 3（从 1 开始）')
        indices.setVisible(mode.currentData() == 'index')
        layout.addWidget(indices)
        mode.currentIndexChanged.connect(lambda: (indices.setVisible(mode.currentData() == 'index'), self.changed.emit('mode', mode.currentData())))
        indices.editingFinished.connect(lambda: self._indices(indices))
        explanation = QtWidgets.QLabel('多个输入按顺序配对，单项可复用；多个列表长度不一致时会阻止提交。')
        explanation.setWordWrap(True)
        explanation.setObjectName('canvasMuted')
        layout.addWidget(explanation)
        layout.addStretch()

    def _indices(self, editor):
        try:
            self.changed.emit('indices', parse_indices(editor.text()))
        except ValueError:
            self.message.emit('请输入有效结果序号，例如 1, 3。')
