"""Shared bounded media-list editor for model App cards and canvas inspectors."""
import os
from PyQt5 import QtCore, QtGui, QtWidgets
from .rh_multi_inputs import values, validate_file, file_suffixes
from .image_import import ImageDropFilter


class _MediaDropFilter(ImageDropFilter):
    def eventFilter(self, widget, event):
        mime = (event.mimeData() if event.type() == QtCore.QEvent.Drop else
                QtWidgets.QApplication.clipboard().mimeData()
                if event.type() == QtCore.QEvent.KeyPress and event.matches(QtGui.QKeySequence.Paste) else None)
        if mime is not None:
            local = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            if local:
                # Let this model validate every supplied file, including formats
                # absent from the generic browser; never silently drop a subset.
                self.callback(local)
                if event.type() == QtCore.QEvent.Drop:event.acceptProposedAction()
                return True
        return super().eventFilter(widget, event)


class MediaListEditor(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(list)

    def __init__(self, param, paths=(), parent=None):
        super().__init__(parent)
        self.param = param
        self.limit = int(param.get('maxInputNum') or 1)
        self.setObjectName('rhMediaListEditor')
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0, 0, 0, 0);layout.setSpacing(6)
        self.summary = QtWidgets.QLabel(self);self.summary.setWordWrap(True);layout.addWidget(self.summary)
        self.listing = QtWidgets.QListWidget(self)
        self.listing.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.listing.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.listing.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.listing.setMinimumHeight(100);self.listing.setMaximumHeight(180)
        layout.addWidget(self.listing)
        row = QtWidgets.QHBoxLayout();layout.addLayout(row)
        for title, callback in [('添加文件', self.browse), ('移除', self.remove), ('↑', lambda:self.move(-1)), ('↓', lambda:self.move(1))]:
            button=QtWidgets.QPushButton(title,self);button.clicked.connect(callback);row.addWidget(button)
            button.setObjectName('rhSecondaryButton')
            if title in ('↑','↓'):button.setMaximumWidth(34);button.setToolTip('上移所选文件' if title=='↑' else '下移所选文件')
        self.path_edit = QtWidgets.QLineEdit(self);self.path_edit.setPlaceholderText('粘贴文件路径或 HTTPS 地址，按 Enter 添加')
        self.path_edit.returnPressed.connect(self.add_text);layout.addWidget(self.path_edit)
        self.preview = QtWidgets.QLabel(self);self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMaximumHeight(110);layout.addWidget(self.preview)
        self.message = QtWidgets.QLabel(self);self.message.setWordWrap(True);layout.addWidget(self.message)
        self.listing.model().rowsMoved.connect(self._changed)
        self.listing.currentItemChanged.connect(self._preview)
        self.listing.itemDoubleClicked.connect(lambda item:QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(item.data(QtCore.Qt.UserRole)) if item.data(QtCore.Qt.UserRole).startswith('https://')
            else QtCore.QUrl.fromLocalFile(item.data(QtCore.Qt.UserRole))))
        self._drop = _MediaDropFilter(self.listing.viewport(), self.add_paths, param['type'].lower())
        self.set_paths(paths)

    def paths(self):
        return [self.listing.item(i).data(QtCore.Qt.UserRole) for i in range(self.listing.count())]

    def set_paths(self, paths):
        self.listing.clear()
        for path in values(paths):
            item=QtWidgets.QListWidgetItem(os.path.basename(path) or path)
            item.setData(QtCore.Qt.UserRole,path);item.setToolTip(path)
            if not path.startswith('https://') and not os.path.isfile(path):item.setText('⚠ '+item.text())
            self.listing.addItem(item)
        self._summary()
        if self.listing.count():self.listing.setCurrentRow(0)
        else:self.preview.clear();self.preview.hide()

    def _summary(self):
        self.summary.setText(f'{self.listing.count()} / {self.limit} 个文件 · 按顺序作为同一次请求的输入')

    def _changed(self, *_):
        self.message.clear();self._summary();self.changed.emit(self.paths())

    def add_paths(self, paths):
        paths=[str(p).strip().strip('"') for p in paths if str(p).strip()]
        try:
            if len(self.paths())+len(paths)>self.limit:raise ValueError(f'此参数最多允许 {self.limit} 个文件；本次未添加，请减少数量。')
            for path in paths:validate_file(path,self.param)
        except ValueError as error:self.message.setText(str(error));return False
        self.message.clear();self.set_paths(self.paths()+paths);self._changed();return True

    def browse(self):
        filters = '模型支持的文件 (' + ' '.join('*' + suffix for suffix in sorted(file_suffixes(self.param))) + ')'
        paths,_=QtWidgets.QFileDialog.getOpenFileNames(self,'添加输入文件','',filters)
        if paths:self.add_paths(paths)

    def add_text(self):
        if self.add_paths(self.path_edit.text().splitlines()):self.path_edit.clear()

    def remove(self):
        for item in self.listing.selectedItems():self.listing.takeItem(self.listing.row(item))
        self._changed()

    def move(self, direction):
        indices=sorted((self.listing.row(i) for i in self.listing.selectedItems()),reverse=direction>0)
        if not indices or min(indices)+direction<0 or max(indices)+direction>=self.listing.count():return
        for index in indices:
            item=self.listing.takeItem(index);self.listing.insertItem(index+direction,item);item.setSelected(True)
        self._changed()

    def _preview(self, item, *_):
        self.preview.clear()
        if item is None:self.preview.hide();return
        path=item.data(QtCore.Qt.UserRole)
        self.preview.setText('双击列表项打开文件')
        if self.param['type']=='IMAGE' and os.path.isfile(path):
            reader=QtGui.QImageReader(path);reader.setAutoTransform(True)
            size=reader.size()
            if size.isValid() and size.width()*size.height()<=32000000:
                reader.setScaledSize(size.scaled(200,100,QtCore.Qt.KeepAspectRatio))
                image=reader.read()
                if not image.isNull():self.preview.setPixmap(QtGui.QPixmap.fromImage(image))
        self.preview.show()
