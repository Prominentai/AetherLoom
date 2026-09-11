"""Searchable model selection without changing configuration until accepted."""
from PyQt5 import QtCore, QtWidgets


class ModelSelector(QtWidgets.QDialog):
    def __init__(self, owner, title, names, selected):
        super().__init__(owner)
        self.setWindowTitle('选择模型 · ' + title)
        self._theme_source = lambda: owner.api_page.styleSheet().replace('#api_page_root', '')
        self.setStyleSheet(self._theme_source())
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20,20,20,20);layout.setSpacing(12)
        self.search = QtWidgets.QLineEdit();self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText('搜索模型名称或 ID')
        layout.addWidget(self.search)
        self.count = QtWidgets.QLabel();self.count.setObjectName('apiMuted');layout.addWidget(self.count)
        self.source = QtCore.QStringListModel(sorted(set(names), key=str.casefold), self)
        self.proxy = QtCore.QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.source);self.proxy.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.list = QtWidgets.QListView()
        self.list.setStyleSheet('QListView { border: 1px solid palette(mid); border-radius: 8px; padding: 6px; }'
                                'QListView::item { padding: 5px 8px; }'
                                'QListView::item:selected { background: palette(highlight); color: palette(highlighted-text); border-radius: 4px; }')
        self.list.setModel(self.proxy);self.list.setUniformItemSizes(True)
        self.list.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        layout.addWidget(self.list,1)
        note = QtWidgets.QLabel('列表为当前候选模型，未逐项验证可用性。也可直接填写服务提供的模型 ID。')
        note.setObjectName('apiMuted');note.setWordWrap(True);layout.addWidget(note)
        self.model_id = QtWidgets.QLineEdit(selected);self.model_id.setClearButtonEnabled(True)
        self.model_id.setPlaceholderText('手动输入模型 ID')
        form = QtWidgets.QFormLayout();form.addRow('模型 ID',self.model_id);layout.addLayout(form)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok|QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.button(self.buttons.Ok).setText('使用此模型')
        self.buttons.button(self.buttons.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept);self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.search.textChanged.connect(self.filter)
        self.list.selectionModel().currentChanged.connect(
            lambda index,_previous:self.model_id.setText(str(index.data() or '')) if index.isValid() else None)
        self.list.activated.connect(self.use_index)
        self.model_id.textChanged.connect(lambda value:self.buttons.button(self.buttons.Ok).setEnabled(bool(value.strip())))
        self.buttons.button(self.buttons.Ok).setEnabled(bool(selected.strip()))
        self.filter('')
        self.resize(600,520)
        screen = owner.screen()
        if screen:
            area = screen.availableGeometry();self.resize(min(600,area.width()-40),min(520,area.height()-60))
        self.search.setFocus()

    def filter(self, text):
        self.proxy.setFilterFixedString(text.strip())
        matched,total = self.proxy.rowCount(),self.source.rowCount()
        self.count.setText(f'{matched} / {total} 个候选模型' if matched else
                           ('没有匹配项，可在下方手填模型 ID' if total else '暂无候选模型，可手填 ID，或返回配置页获取列表'))

    def use_index(self, index):
        self.model_id.setText(str(index.data() or ''))
        self.accept()

    def accept(self):
        if self.model_id.text().strip():super().accept()

    @property
    def selected_model(self):
        return self.model_id.text().strip()
