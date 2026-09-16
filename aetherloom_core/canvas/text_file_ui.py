"""Local text-file inputs with bounded, asynchronous content previews."""
import os
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_parameters import RhEnumComboBox
from .editors import FileList
from .text_files import TEXT_SUFFIXES, ENCODINGS, MAX_FILES, encoding_name, resolve_files, read_preview


FILE_FILTER = '文本文件 (' + ' '.join('*' + suffix for suffix in sorted(TEXT_SUFFIXES)) + ')'


def _accepts(path):
    return os.path.isdir(path) or (Path(path).suffix.lower() in TEXT_SUFFIXES and os.path.isfile(path))


def _mime_paths(mime):
    if mime.hasUrls():return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
    if mime.hasText():
        return [QtCore.QUrl(line).toLocalFile() if line.startswith('file:') else line
                for line in mime.text().splitlines() if line.strip()]
    return []


class TextFileList(FileList):
    rejected = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent, kind='text_file')

    def import_paths(self, paths):
        if not self.isEnabled():return 0
        previous = {os.path.normcase(os.path.abspath(path)) for path in self.paths()}
        added = 0
        for value in paths:
            raw = str(value).strip().strip('"')
            if not raw:continue
            path = os.path.abspath(os.path.expanduser(raw))
            key = os.path.normcase(path)
            if key not in previous and _accepts(path):
                if self.count() >= MAX_FILES:
                    self.rejected.emit(f'最多添加 {MAX_FILES} 个输入路径，请分批导入。')
                    break
                self.add_path(path);previous.add(key);added += 1
        if added:self.files_changed.emit(self.paths())
        return added

    def dragEnterEvent(self, event):
        if event.source() is self:
            QtWidgets.QListWidget.dragEnterEvent(self, event)
        elif self.isEnabled() and any(_accepts(path) for path in _mime_paths(event.mimeData())):
            event.acceptProposedAction()
        else:event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self:
            QtWidgets.QListWidget.dragMoveEvent(self, event)
        elif self.isEnabled() and any(_accepts(path) for path in _mime_paths(event.mimeData())):
            event.acceptProposedAction()
        else:event.ignore()

    def dropEvent(self, event):
        if event.source() is self:
            QtWidgets.QListWidget.dropEvent(self, event)
        elif self.import_paths(_mime_paths(event.mimeData())):event.acceptProposedAction()
        else:event.ignore()


class _PreviewSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(int, str, str)


class _PreviewJob(QtCore.QRunnable):
    def __init__(self, generation, path, encoding):
        super().__init__()
        self.generation, self.path, self.encoding = generation, path, encoding
        self.signals = _PreviewSignals()

    def run(self):
        try:
            paths = resolve_files([self.path])
            if not paths:raise ValueError('文件夹中没有支持的文本文件。')
            text = read_preview(paths[0], self.encoding, max_bytes=65536, max_chars=8000)
            info = ('文件夹内 ' + str(len(paths)) + ' 个文本文件 · 预览 ' + Path(paths[0]).name
                    if os.path.isdir(self.path) else Path(paths[0]).name)
        except Exception as error:
            text, info = '无法预览：' + str(error), '请检查路径或切换编码后重试'
        try:self.signals.finished.emit(self.generation, text, info)
        except RuntimeError:pass


class TextFilePreview(QtWidgets.QPlainTextEdit):
    paths_dropped = QtCore.pyqtSignal(list)
    loaded = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('canvasTextFilePreview')
        self.setReadOnly(True)
        self.setAcceptDrops(True)
        self.setMinimumSize(80, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setPlaceholderText('拖入文本文件或文件夹\n\n支持 TXT、Markdown、CSV、JSON、YAML 等格式。\n也可使用下方按钮导入。')
        self.setLineWrapMode(self.WidgetWidth)
        self.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setToolTip('只读输入预览，最多读取 64 KiB / 显示 8000 字符；运行时读取完整文本。')
        self._generation = 0
        self._signature = None
        self._job = None
        self._pending = None
        self._connected = False

    def set_input(self, path, encoding, connected=False, force=False):
        signature = (path, encoding, connected)
        if not force and signature == self._signature:return
        self._signature = signature
        self._connected = connected
        self._generation += 1
        self._pending = None
        if connected:
            self.setPlainText('已连接路径输入\n\n运行时读取上游传入的文件或文件夹。\n本地路径已保留，断开连接后恢复。')
            return
        if not path:self.clear();return
        self.setPlainText('正在读取文本…')
        request = (self._generation, path, encoding)
        if self._job is not None:self._pending = request
        else:self._start(request)

    def _start(self, request):
        self._job = _PreviewJob(*request)
        self._job.signals.finished.connect(self._finished)
        QtCore.QThreadPool.globalInstance().start(self._job)

    @QtCore.pyqtSlot(int, str, str)
    def _finished(self, generation, text, info):
        self._job = None
        if generation == self._generation:
            self.setPlainText(text)
            self.loaded.emit(info)
        pending, self._pending = self._pending, None
        if pending is not None:self._start(pending)

    def dragEnterEvent(self, event):
        if not self._connected and any(_accepts(path) for path in _mime_paths(event.mimeData())):
            event.acceptProposedAction()
        else:event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = [path for path in _mime_paths(event.mimeData()) if _accepts(path)]
        if paths and not self._connected:
            self.paths_dropped.emit(paths);event.acceptProposedAction()
        else:event.ignore()


class TextFileEditor(QtWidgets.QWidget):
    def __init__(self, panel, node):
        super().__init__(panel)
        self.panel = panel
        self._selection_key = ('text_file_selection', panel.doc_id, node['id'])
        self._manage_key = ('text_file_manage', panel.doc_id, node['id'], panel.embedded)
        self.setObjectName('canvasTextFileEditor')
        self.setMinimumWidth(0)
        self._state = None
        root = QtWidgets.QVBoxLayout(self);root.setContentsMargins(0, 0, 0, 0);root.setSpacing(7)
        self.title = QtWidgets.QLabel('文件 / 文件夹路径');self.title.setObjectName('canvasFieldLabel')
        root.addWidget(self.title);panel.port_widgets['path'] = self.title
        self.preview = TextFilePreview(self);root.addWidget(self.preview, 1)
        self.caption = QtWidgets.QLabel();self.caption.setMinimumWidth(0)
        self.caption.setTextFormat(QtCore.Qt.PlainText)
        self.caption.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.caption.setObjectName('canvasTextFileCaption')
        self.preview.loaded.connect(self.caption.setToolTip)
        self.files = TextFileList(self);self.files.setObjectName('canvasInputFiles')
        self.files.rejected.connect(panel.message.emit)
        self.files.setMinimumHeight(65);self.files.setMaximumHeight(100)
        self.preview.paths_dropped.connect(self.files.import_paths)
        self.local = QtWidgets.QWidget(self)
        local_layout = QtWidgets.QVBoxLayout(self.local);local_layout.setContentsMargins(0, 0, 0, 0);local_layout.setSpacing(6)
        navigation = QtWidgets.QHBoxLayout();navigation.setSpacing(5)
        navigation.addWidget(self.caption, 1)
        self.previous = self._button('‹', lambda: self.files.setCurrentRow(max(0, self.files.currentRow() - 1)), tool=True)
        self.following = self._button('›', lambda: self.files.setCurrentRow(min(self.files.count() - 1, self.files.currentRow() + 1)), tool=True)
        self.previous.setToolTip('上一个输入');self.following.setToolTip('下一个输入')
        navigation.addWidget(self.previous);navigation.addWidget(self.following)
        root.addLayout(navigation)
        row = QtWidgets.QHBoxLayout();row.setSpacing(6)
        row.addWidget(QtWidgets.QLabel('编码'))
        self.encoding = RhEnumComboBox();self.encoding.setObjectName('canvasTextFileEncoding')
        for key, label in ENCODINGS:self.encoding.addItem('自动识别' if key == 'auto' else label, key)
        self.encoding.setToolTip('自动识别 UTF-8、含 BOM 的 UTF-16 或 GB18030；识别有误时可指定编码。')
        self.encoding.setMinimumWidth(0);self.encoding.setMinimumContentsLength(8)
        self.encoding.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.encoding, 1)
        self.reload = self._button('刷新', lambda: self._preview(force=True), tool=True)
        row.addWidget(self.reload);root.addLayout(row)
        toolbar = QtWidgets.QHBoxLayout();toolbar.setSpacing(5)
        self.browse = self._button('导入文件', self._browse)
        self.folder = self._button('文件夹', self._folder)
        toolbar.addWidget(self.browse);toolbar.addWidget(self.folder)
        self.manage = self._button('管理', None, tool=True);self.manage.setCheckable(True)
        toolbar.addWidget(self.manage);local_layout.addLayout(toolbar)
        self.details = QtWidgets.QWidget(self)
        detail = QtWidgets.QVBoxLayout(self.details);detail.setContentsMargins(0, 0, 0, 0);detail.setSpacing(6)
        detail.addWidget(self.files)
        row = QtWidgets.QHBoxLayout();row.setSpacing(5)
        self.path = QtWidgets.QLineEdit();self.path.setObjectName('canvasInputPath')
        self.path.setPlaceholderText('文件或文件夹路径，回车添加');self.path.setMinimumWidth(0)
        self.add = self._button('添加', self._import_path)
        row.addWidget(self.path, 1);row.addWidget(self.add);detail.addLayout(row)
        self.path.returnPressed.connect(self._import_path)
        row = QtWidgets.QHBoxLayout();row.setSpacing(5)
        row.addWidget(self._button('定位文件', lambda: self._relocate(False)))
        row.addWidget(self._button('定位文件夹', lambda: self._relocate(True)))
        row.addWidget(self._button('移除', self._remove))
        detail.addLayout(row)
        local_layout.addWidget(self.details);root.addWidget(self.local)
        self.details.hide();self.manage.toggled.connect(self.details.setVisible)
        self.manage.toggled.connect(lambda shown: panel.histories.__setitem__(self._manage_key, shown))
        self.hint = QtWidgets.QLabel('多文件按列表顺序逐项输出文本；文件夹只读取当前层，按文件名排序。')
        self.hint.setWordWrap(True);self.hint.setObjectName('canvasMuted')
        self.hint.setToolTip('支持 TXT / MD / CSV / TSV / JSON / JSONL / YAML / YML / LOG。\n单文件最多 4 MiB，每次最多 512 个文件、合计 32 MiB。')
        root.addWidget(self.hint)
        self.files.files_changed.connect(self._files_changed)
        self.files.currentRowChanged.connect(lambda _: self._preview())
        self.encoding.currentIndexChanged.connect(self._encoding_changed)
        self.refresh(node)
        self.manage.setChecked(panel.histories.get(self._manage_key, not panel.embedded))

    def _button(self, text, callback, tool=False):
        button = QtWidgets.QToolButton(self) if tool else QtWidgets.QPushButton(text, self)
        if tool:button.setText(text)
        else:button.setAutoDefault(False)
        button.setCursor(QtCore.Qt.PointingHandCursor)
        if callback is not None:button.clicked.connect(callback)
        return button

    def refresh(self, node):
        values = list(node.get('params', {}).get('files') or [])
        encoding = encoding_name(node.get('params', {}).get('encoding', 'auto'))
        connected = 'path' in self.panel.connected_inputs
        state = (tuple(values), encoding, connected)
        if state == self._state:return
        self._state = state
        if self.files.paths() != values:
            index = max(0, self.files.currentRow())
            selected = self.panel.histories.get(self._selection_key)
            if selected in values:index = values.index(selected)
            with QtCore.QSignalBlocker(self.files):
                self.files.set_paths(values)
                if values:self.files.setCurrentRow(min(index, len(values) - 1))
        with QtCore.QSignalBlocker(self.encoding):
            self.encoding.setCurrentIndex(max(0, self.encoding.findData(encoding)))
        self.local.setEnabled(not connected)
        self.reload.setEnabled(not connected)
        self._preview()

    def _preview(self, force=False):
        paths = self.files.paths();index = max(0, self.files.currentRow())
        path = paths[index] if index < len(paths) else ''
        if path:self.panel.histories[self._selection_key] = path
        connected = 'path' in self.panel.connected_inputs
        self.preview.set_input(path, self.encoding.currentData(), connected, force=force)
        self.caption.setText((f'本地 {len(paths)} 项已保留' if connected else
                              f'{index + 1} / {len(paths)} · {Path(path).name or path}' if path else '尚未导入文本文件'))
        self.caption.setToolTip(path)
        self.previous.setEnabled(not connected and index > 0)
        self.following.setEnabled(not connected and index + 1 < len(paths))

    def _files_changed(self, values):
        self.panel.changed.emit('params.files', values)
        if self.files.count() and self.files.currentRow() < 0:self.files.setCurrentRow(0)
        self._preview()

    def _encoding_changed(self, *_):
        self.panel.changed.emit('params.encoding', self.encoding.currentData())
        self._preview()

    def _browse(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self.panel.dialog_parent, '导入文本文件', '', FILE_FILTER)
        self.files.import_paths(paths)

    def _folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self.panel.dialog_parent, '选择文本文件夹')
        if path:self.files.import_paths([path])

    def _import_path(self):
        if self.files.import_paths([self.path.text()]):self.path.clear()
        else:self.panel.message.emit('路径不存在、不是支持的文本格式，或已在列表中。')

    def _relocate(self, directory=False):
        index = self.files.currentRow()
        if index < 0:return
        if directory:
            selected = QtWidgets.QFileDialog.getExistingDirectory(self.panel.dialog_parent, '重新定位文本文件夹')
        else:selected, _ = QtWidgets.QFileDialog.getOpenFileName(self.panel.dialog_parent, '重新定位文本文件', '', FILE_FILTER)
        if selected and _accepts(selected):
            values = self.files.paths();values[index] = selected
            with QtCore.QSignalBlocker(self.files):self.files.set_paths(values);self.files.setCurrentRow(index)
            self.files.files_changed.emit(values)

    def _remove(self):
        for item in self.files.selectedItems():self.files.takeItem(self.files.row(item))
        self.files.files_changed.emit(self.files.paths())


def build(panel, node):
    editor = panel.text_file_editor = TextFileEditor(panel, node)
    panel.refresh_input_preview = lambda: editor.refresh(panel.node)
    panel.form.addWidget(editor, 1)
