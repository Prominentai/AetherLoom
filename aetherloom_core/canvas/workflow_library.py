"""Searchable local workflow library; snapshots are loaded only after selection."""
from datetime import datetime
from PyQt5 import QtCore, QtWidgets


class _CatalogModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None):super().__init__(parent);self.entries = []
    def rowCount(self, parent=QtCore.QModelIndex()):return 0 if parent.isValid() else len(self.entries)
    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():return None
        item = self.entries[index.row()]
        if role == QtCore.Qt.UserRole:return item
        if role == QtCore.Qt.UserRole + 1:return item['modified']
        if role == QtCore.Qt.DisplayRole:
            detail = item['error'] or f"{item['nodes']} 个节点 · {item['edges']} 条连线 · " + datetime.fromtimestamp(item['modified']).strftime('%Y-%m-%d %H:%M')
            if item.get('snapshot') and not item['error']:detail += ' · 有运行快照'
            if item.get('session_edit'):detail += ' · 本次会话未保存'
            return item['name'] + '\n' + detail
        if role == QtCore.Qt.ToolTipRole:return item['path'] + ('\n' + item['error'] if item['error'] else '') + ('\n未保存修改仅保留在本次会话，运行或手动保存后写入文件。' if item.get('session_edit') else '')
        if role == QtCore.Qt.SizeHintRole:return QtCore.QSize(320, 68)
    def replace(self, entries):self.beginResetModel();self.entries = entries;self.endResetModel()


class _Signals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object, str)


class _ReadCatalog(QtCore.QRunnable):
    def __init__(self, store, signals):super().__init__();self.store, self.signals = store, signals
    def run(self):
        try:result, error = self.store.catalog(), ''
        except Exception as exc:result, error = [], str(exc)
        try:self.signals.finished.emit(result, error)
        except RuntimeError:pass


class WorkflowLibrary(QtWidgets.QDialog):
    def __init__(self, page):
        super().__init__(page.owner)
        self.page = page;self.selected_path = '';self._loading = False
        self.setWindowTitle('打开画布');self.setObjectName('canvasWorkflowLibrary')
        screen = self.screen().availableGeometry();self.resize(min(760, screen.width()-60), min(620, screen.height()-80))
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(20, 18, 20, 18);layout.setSpacing(12)
        title = QtWidgets.QLabel('本地画布');title.setObjectName('libraryTitle');layout.addWidget(title)
        note = QtWidgets.QLabel('选择后恢复该画布的最新匹配快照；缺失的结果文件自动跳过。');note.setObjectName('libraryNote');note.setWordWrap(True);layout.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit();self.search.setPlaceholderText('搜索画布名称…');self.search.setClearButtonEnabled(True)
        self.sort = QtWidgets.QComboBox();self.sort.addItems(['最近更新', '名称排序'])
        self.reload_button = QtWidgets.QPushButton('刷新');self.reload_button.clicked.connect(self.reload)
        row.addWidget(self.search, 1);row.addWidget(self.sort);row.addWidget(self.reload_button);layout.addLayout(row)
        self.model = _CatalogModel(self);self.proxy = QtCore.QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model);self.proxy.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.listing = QtWidgets.QListView();self.listing.setModel(self.proxy);self.listing.setWordWrap(True)
        self.listing.setUniformItemSizes(True);self.listing.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.listing.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers);layout.addWidget(self.listing, 1)
        self.status = QtWidgets.QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout();self.file_button = QtWidgets.QPushButton('从 JSON 文件打开…');row.addWidget(self.file_button);row.addStretch()
        cancel = QtWidgets.QPushButton('取消');cancel.clicked.connect(self.reject);row.addWidget(cancel)
        self.open_button = QtWidgets.QPushButton('打开画布');self.open_button.setObjectName('libraryOpen');self.open_button.setDefault(True);row.addWidget(self.open_button);layout.addLayout(row)
        self.open_button.clicked.connect(self.choose);self.listing.doubleClicked.connect(self.choose)
        self.file_button.clicked.connect(self.browse)
        self.search.textChanged.connect(self.filter);self.sort.currentIndexChanged.connect(self.reorder)
        self.listing.selectionModel().selectionChanged.connect(self.selection)
        self.signals = _Signals(self);self.signals.finished.connect(self.loaded)
        colors = page.scene.colors
        self.setStyleSheet('QDialog#canvasWorkflowLibrary{background:'+colors['surface']+';color:'+colors['text']+';}'
            'QLabel{background:transparent;color:'+colors['text']+';}QLabel#libraryTitle{font-size:22px;font-weight:600;}QLabel#libraryNote{color:'+colors['muted']+';}'
            'QListView,QLineEdit,QComboBox{background:'+colors['input']+';color:'+colors['text']+';border:1px solid '+colors['border']+';border-radius:6px;padding:6px;}'
            'QComboBox QAbstractItemView{background:'+colors['input']+';color:'+colors['text']+';selection-background-color:'+colors['accent']+';selection-color:white;}'
            'QListView::item{padding:8px;}QListView::item:selected{background:'+colors['accent']+';color:white;}'
            'QPushButton{background:'+colors['surface']+';color:'+colors['text']+';border:1px solid '+colors['border']+';border-radius:6px;padding:7px 12px;}'
            'QPushButton:hover{border-color:'+colors['accent']+';background:'+colors['accent_soft']+';}'
            'QPushButton#libraryOpen{background:'+colors['accent']+';border-color:'+colors['accent']+';color:white;}'
            'QPushButton:disabled{background:'+colors['input']+';color:'+colors['muted']+';border-color:'+colors['border']+';}')
        self.reload()

    def reload(self):
        if self._loading:return
        self._loading = True;self.reload_button.setEnabled(False);self.open_button.setEnabled(False)
        self.status.setText('正在读取画布列表…')
        QtCore.QThreadPool.globalInstance().start(_ReadCatalog(self.page.store, self.signals))

    def loaded(self, entries, error):
        self._loading = False;self.reload_button.setEnabled(True)
        by_id = {entry['id']: entry for entry in entries}
        by_id.update(getattr(self.page, '_session_edits', {}))
        entries = list(by_id.values())
        self.model.replace(entries);self.reorder()
        self.status.setText('读取失败：' + error if error else f'共 {len(entries)} 张画布' if entries else '还没有已保存的画布，可从 JSON 文件导入。')
        if self.proxy.rowCount():self.listing.setCurrentIndex(self.proxy.index(0, 0))
        self.selection()

    def reorder(self, *unused):
        recent = self.sort.currentIndex() == 0
        self.proxy.setSortRole(QtCore.Qt.UserRole + 1 if recent else QtCore.Qt.DisplayRole)
        self.proxy.sort(0, QtCore.Qt.DescendingOrder if recent else QtCore.Qt.AscendingOrder)

    def filter(self, text):
        self.proxy.setFilterFixedString(text)
        if self.proxy.rowCount():self.listing.setCurrentIndex(self.proxy.index(0, 0))
        self.status.setText(f'找到 {self.proxy.rowCount()} 张画布');self.selection()

    def selection(self, *unused):
        entry = self.listing.currentIndex().data(QtCore.Qt.UserRole)
        self.open_button.setEnabled(bool(entry and not entry['error'] and not self._loading))

    def choose(self, *unused):
        entry = self.listing.currentIndex().data(QtCore.Qt.UserRole)
        if entry and not entry['error'] and not self._loading:self.selected_path = entry['path'];self.accept()

    def browse(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '打开工作流 JSON', str(self.page.store.root), 'AetherLoom 工作流 (*.json)')
        if path:self.selected_path = path;self.accept()
