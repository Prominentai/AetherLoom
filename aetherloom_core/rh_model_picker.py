"""Shared, bounded public model search for App and canvas parameter editors."""
import re
import threading

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from .rh_connections import ensure_connections, open_connection_settings


_queries = threading.BoundedSemaphore(2)


def model_resource_type(field):
    kind = str(field.get('fieldType') or '').strip().upper()
    value = str(field.get('fieldValue') or '').lower()
    if kind in ('CKPT', 'CHECKPOINT', 'LORA', 'UNET', 'GGUF'):
        return 'CHECKPOINT' if kind == 'CKPT' else 'GGUF' if kind == 'UNET' and value.endswith('.gguf') else kind
    # Older App definitions sometimes expose model loaders as text/list fields.
    if kind in ('', 'STRING', 'TEXT', 'LIST', 'MODEL'):
        name = str(field.get('fieldName') or '').lower()
        return {'ckpt_name': 'CHECKPOINT', 'lora_name': 'LORA',
                'unet_name': 'GGUF' if value.endswith('.gguf') else 'UNET',
                'gguf_name': 'GGUF'}.get(name)
    return None


def connection_owner(widget):
    current = widget
    while current is not None:
        if getattr(current, '_canvas_owner', None) is not None:
            return current._canvas_owner
        if hasattr(current, '_rh_connection_settings'):
            return current
        current = current.parentWidget()
    return widget.window()


class ModelField(QtWidgets.QWidget):
    """Keep the existing QLineEdit persistence and immutable snapshot contract."""
    def __init__(self, field, value, parent=None):
        super().__init__(parent)
        self.resource_type = model_resource_type(field)
        self.dialog = None
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.editor = QtWidgets.QLineEdit(str(value), self)
        self.editor.setMinimumWidth(0)
        self.editor.setPlaceholderText('模型文件名')
        self.editor.setToolTip('可手动输入，或从公共模型、本地收藏和我的上传中选择')
        self.button = QtWidgets.QPushButton('选择模型', self)
        self.button.setObjectName('rhSecondaryButton')
        self.button.setMinimumHeight(36)
        self.button.setAutoDefault(False)
        self.button.clicked.connect(self.open_picker)
        row.addWidget(self.editor, 1)
        row.addWidget(self.button)

    def open_picker(self):
        if self.dialog is None or sip.isdeleted(self.dialog):
            owner = connection_owner(self)
            self.dialog = ModelPicker(ensure_connections(owner), self.resource_type,
                                      self.editor.text(), owner)
            self.dialog.selected.connect(self._selected)
            self.dialog.finished.connect(self._picker_finished)
            self.destroyed.connect(self.dialog.close)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    @QtCore.pyqtSlot(int)
    def _picker_finished(self, result):
        if self.sender() is self.dialog:
            self.dialog = None

    @QtCore.pyqtSlot(str)
    def _selected(self, value):
        if value == self.editor.text():
            return
        self.editor.selectAll()
        self.editor.insert(value)
        self.editor.setFocus(QtCore.Qt.OtherFocusReason)
        self.editor.editingFinished.emit()


class ModelPicker(QtWidgets.QDialog):
    selected = QtCore.pyqtSignal(str)
    finished_query = QtCore.pyqtSignal(int, object, str)
    PAGE_SIZE = 30

    def __init__(self, settings, resource_type, current_value='', parent=None, library=False):
        super().__init__(parent)
        from .rh_model_thumbnails import thumbnails
        from .rh_model_cards import ModelTabs
        from .rh_model_favorites import favorites, uploads, TYPES
        from .rh_model_bases import base_models
        from .rh_parameters import RhEnumComboBox
        self.library = library
        self.favorites = favorites(settings.owner)
        self.uploads=uploads(settings.owner);self.base_models=base_models(settings.owner)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, not library)
        self.setObjectName('rhModelPicker')
        self.setWindowTitle('RH 模型库' if library else '选择模型')
        self.resize(1080, 780); self.setMinimumSize(440, 580)
        if library:self.setWindowFlags(QtCore.Qt.Widget);self.setMinimumSize(0, 0)
        self.settings, self.resource_type, self.current_value = settings, resource_type, str(current_value)
        self.generation, self.busy, self.page, self.start_page = 0, False, 0, 1
        self.has_next, self.total, self.loaded_filter, self.pending_search = False, 0, None, False
        self.total_pages = 0
        self.cards, self.known_bases, self.known_tags = [], set(), {}
        self.selected_card = None
        self.thumbs = thumbnails(settings.owner)
        self.visible_timer = QtCore.QTimer(self); self.visible_timer.setInterval(160)
        self.visible_timer.timeout.connect(self._visible)
        from .ui import design
        box = QtWidgets.QVBoxLayout(self)
        design.page_layout(self, box)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('RH 模型库' if library else '选择模型');title.setObjectName('rhModelHeading')
        design.title(title)
        self.site = QtWidgets.QLabel();self.site.setObjectName('rhModelMuted');self.site.setWordWrap(True)
        connection = QtWidgets.QPushButton('连接设置');connection.setObjectName('rhModelSecondary')
        connection.clicked.connect(lambda: open_connection_settings(settings.owner))
        titles = QtWidgets.QVBoxLayout();titles.setSpacing(4)
        titles.addWidget(title);titles.addWidget(self.site)
        header.addLayout(titles, 1);header.addWidget(connection);box.addWidget(design.header(header))
        self.current_label = QtWidgets.QLabel('当前模型：' + self.current_value)
        self.current_label.setTextFormat(QtCore.Qt.PlainText);self.current_label.setObjectName('rhModelMuted')
        self.current_label.setWordWrap(True);self.current_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.current_label.setMaximumHeight(36);self.current_label.setToolTip(self.current_value)
        box.addWidget(self.current_label)
        self.current_label.setVisible(not library)
        sources = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight);self.scope_row = sources
        self.sources = ModelTabs();self.sources.setObjectName('rhModelTags')
        self.sources.addTab('公共模型');self.sources.addTab('本地收藏');self.sources.addTab('我的上传')
        self.sources.setExpanding(False);self.sources.setDrawBase(False)
        sources.addWidget(self.sources, 1)
        box.addLayout(sources)
        self.scope_hint=QtWidgets.QLabel(self);self.scope_hint.setWordWrap(True);self.scope_hint.setObjectName('rhModelMuted')
        self.scope_hint.hide()
        self.create_button = QtWidgets.QPushButton('＋ 自建收藏');self.create_button.setObjectName('rhModelSecondary')
        self.create_button.clicked.connect(lambda:self.edit_favorite())
        self.import_button = QtWidgets.QPushButton('导入收藏');self.import_button.setObjectName('rhModelSecondary')
        self.import_button.clicked.connect(self.import_favorites)
        scope_buttons=QtWidgets.QWidget();scope_layout=QtWidgets.QHBoxLayout(scope_buttons);scope_layout.setContentsMargins(0,0,0,0)
        scope_layout.addWidget(self.import_button);scope_layout.addWidget(self.create_button)
        sources.addWidget(scope_buttons)
        search_row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit();self.search.setPlaceholderText('搜索模型名称');self.search.setClearButtonEnabled(True)
        self.search.setMaxLength(100)
        self.search_button = QtWidgets.QPushButton('搜索');self.search_button.setObjectName('rhModelPrimary')
        self.search_button.clicked.connect(lambda: self.request(1))
        self.search.returnPressed.connect(lambda: self.request(1))
        self.filter_button = QtWidgets.QPushButton('筛选');self.filter_button.setCheckable(True)
        self.filter_button.setObjectName('rhModelSecondary')
        search_row.addWidget(self.search, 1);search_row.addWidget(self.search_button);search_row.addWidget(self.filter_button)
        box.addLayout(search_row)
        self.tags = ModelTabs();self.tags.setObjectName('rhModelTags')
        self.tags.setToolTip('本次浏览已加载模型的分类')
        self.tags.setExpanding(False);self.tags.setUsesScrollButtons(True);self.tags.setDrawBase(False)
        self.tags.addTab('全部');self.tags.setTabData(0, None)
        self.tags.currentChanged.connect(self._tag_changed);box.addWidget(self.tags)
        self.tags.hide()
        from .rh_model_dialogs import ModelDialog
        self.filter_panel = ModelDialog(self, '筛选模型', '按模型类型和基础模型缩小范围；基础模型支持多选。', (540, 650))
        self.filter_panel.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        self.filter_panel.setObjectName('rhModelPicker')
        filters = self.filter_panel.body
        type_row = QtWidgets.QWidget()
        type_layout = QtWidgets.QHBoxLayout(type_row);type_layout.setContentsMargins(0, 0, 0, 0)
        type_label = QtWidgets.QLabel('模型类型')
        self.kind = RhEnumComboBox()
        if library:self.kind.addItem('全部类型', '')
        for value in TYPES:self.kind.addItem(value,value)
        self.kind.setCurrentIndex(max(0,self.kind.findData(resource_type)))
        type_label.setBuddy(self.kind)
        type_layout.addWidget(type_label);type_layout.addWidget(self.kind, 1)
        clear_type = QtWidgets.QPushButton('取消选定');clear_type.setObjectName('rhModelSecondary')
        clear_type.clicked.connect(lambda:self.kind.setCurrentIndex(0));type_layout.addWidget(clear_type)
        filters.addWidget(type_row);type_row.setVisible(library)
        filters.addWidget(QtWidgets.QLabel('基础模型 · 可多选'))
        self.base_search=QtWidgets.QLineEdit();self.base_search.setPlaceholderText('搜索基础模型类别');self.base_search.textChanged.connect(self._search_bases);filters.addWidget(self.base_search)
        self.base_list = QtWidgets.QListWidget();self.base_list.setObjectName('rhModelBaseList')
        self.base_list.setMinimumHeight(190);self.base_list.setMaximumHeight(300)
        self.base_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.base_list.setTextElideMode(QtCore.Qt.ElideRight)
        self.base_list.setUniformItemSizes(True)
        self.base_list.setToolTip('官网基础模型枚举，可多选；也支持自定义名称。')
        self.base_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.base_list.itemChanged.connect(self._filters_changed);filters.addWidget(self.base_list)
        self.base = QtWidgets.QLineEdit();self.base.setPlaceholderText('其他基础模型名称，多个名称用逗号分隔')
        self.base.setClearButtonEnabled(True);self.base.returnPressed.connect(lambda: self.request(1));filters.addWidget(self.base)
        self.filter_summary=QtWidgets.QLabel();self.filter_summary.setObjectName('rhModelMuted');self.filter_summary.setWordWrap(True)
        filters.addWidget(self.filter_summary)
        self.filter_panel.button('清除筛选',self.reset_filters)
        self.filter_panel.button('应用筛选',self._apply_filters,primary=True)
        self.filter_panel.finished.connect(lambda _:self.filter_button.setChecked(False))
        self.filter_button.toggled.connect(self.filter_panel.setVisible)
        self.scroll = QtWidgets.QScrollArea();self.scroll.setObjectName('rhModelScroll')
        self.scroll.setWidgetResizable(True);self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.content = QtWidgets.QWidget();self.content.setObjectName('rhModelContent')
        self.grid = QtWidgets.QGridLayout(self.content);self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setSpacing(14);self.grid.setAlignment(QtCore.Qt.AlignTop)
        self.scroll.setWidget(self.content);box.addWidget(self.scroll, 1)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.status = QtWidgets.QLabel();self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setWordWrap(True);self.status.setObjectName('rhModelMuted');box.addWidget(self.status)
        footer = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight);self.footer = footer
        self.page_label = QtWidgets.QLabel();self.page_label.setObjectName('rhModelMuted')
        self.page_label.setWordWrap(True)
        self.previous = QtWidgets.QPushButton('上一组');self.previous.setObjectName('rhModelSecondary')
        self.previous.clicked.connect(lambda:self._go_page(self.page-1))
        self.next = QtWidgets.QPushButton('下一组');self.next.setObjectName('rhModelSecondary')
        self.next.clicked.connect(self._more)
        self.page_input = QtWidgets.QLineEdit();self.page_input.setFixedWidth(66)
        self.page_input.setAlignment(QtCore.Qt.AlignCenter);self.page_input.setAccessibleName('跳转页码')
        self.page_input.setToolTip('输入页码后按 Enter，或点击跳转')
        self.page_input.setValidator(QtGui.QIntValidator(1, 1, self.page_input))
        self.page_input.returnPressed.connect(self._jump_page)
        self.jump = QtWidgets.QPushButton('跳转');self.jump.setObjectName('rhModelSecondary')
        self.jump.clicked.connect(self._jump_page)
        navigation = QtWidgets.QHBoxLayout();navigation.setSpacing(6)
        for widget in (self.previous,self.page_input,self.jump,self.next):navigation.addWidget(widget)
        cancel = QtWidgets.QPushButton('关闭');cancel.setObjectName('rhModelSecondary');cancel.clicked.connect(self.close)
        footer.addWidget(self.page_label, 1);footer.addLayout(navigation);footer.addWidget(cancel)
        box.addLayout(footer)
        cancel.setVisible(not library)
        for button in self.findChildren(QtWidgets.QPushButton):button.setAutoDefault(False)
        self.search.textChanged.connect(self._filters_changed);self.base.textChanged.connect(self._filters_changed)
        self.finished_query.connect(self._finished, QtCore.Qt.QueuedConnection)
        self.thumbs.changed.connect(self.schedule_visible)
        settings.changed.connect(self._settings_changed)
        self.sources.currentChanged.connect(self._scope_changed)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.favorites.changed.connect(self._favorites_changed)
        self.uploads.changed.connect(self._uploads_changed)
        self.base_models.changed.connect(self._bases_changed)
        self._connection_changed();self.apply_theme()
        QtCore.QTimer.singleShot(0, self._initial_query)

    def mode(self):
        return getattr(self.settings.owner, '_theme_mode', 'dark')

    def apply_theme(self):
        from .rh_model_style import stylesheet
        self.setStyleSheet(stylesheet(self.mode()))
        for card in self.cards:card.cover.update()
        if hasattr(self,'filter_panel'):self.filter_panel.setStyleSheet(stylesheet(self.mode()))

    def _initial_query(self):
        if not sip.isdeleted(self) and self.isVisible():self.request(1)

    def _apply_filters(self):
        self.request(1);self.filter_button.setChecked(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'scope_row'):
            self.scope_row.setDirection(QtWidgets.QBoxLayout.TopToBottom if self.width()<640 else QtWidgets.QBoxLayout.LeftToRight)
        if hasattr(self,'footer'):
            self.footer.setDirection(QtWidgets.QBoxLayout.TopToBottom if self.width()<760 else QtWidgets.QBoxLayout.LeftToRight)

    def _filter(self):
        bases = [self.base_list.item(i).text() for i in range(self.base_list.count())
                 if self.base_list.item(i).checkState() == QtCore.Qt.Checked]
        bases.extend(v.strip() for v in self.base.text().replace('，', ',').split(',') if v.strip())
        return (self.search.text().strip(), tuple(sorted(set(bases))), self.tags.tabData(self.tags.currentIndex()),
                self.sources.currentIndex(), self.resource_type, self.settings.host)

    def _is_busy(self):
        return self.busy and self.sources.currentIndex() == 0

    def destination(self):return 'uploads' if self.sources.currentIndex()==2 else 'favorites'

    def store_for(self, bucket):return self.uploads if bucket=='uploads' else self.favorites

    def _uploads_changed(self):
        if self.sources.currentIndex()==2:self.request(max(1,self.start_page))

    def _bases_changed(self, site):
        if site!=self.settings.host:return
        with QtCore.QSignalBlocker(self.base_list):
            for base in self.base_models.options(site):
                if base in self.known_bases:continue
                self.known_bases.add(base);item=QtWidgets.QListWidgetItem(base)
                item.setFlags(item.flags()|QtCore.Qt.ItemIsUserCheckable);item.setCheckState(QtCore.Qt.Unchecked);self.base_list.addItem(item)
        self._search_bases()

    def _search_bases(self, *unused):
        text=self.base_search.text().strip().casefold()
        for i in range(self.base_list.count()):
            item=self.base_list.item(i);item.setHidden(text not in item.text().casefold())

    def _scope_changed(self, *unused):
        self.resource_type = self.kind.currentData() or ''
        self._connection_changed();self.request(1)

    def _kind_changed(self, *unused):
        self.resource_type = self.kind.currentData() or ''
        self.site.setText((self.resource_type or '全部类型')+' · '+('国际站' if self.settings.host.endswith('.ai') else '中文站'))
        self._filters_changed()

    def _settings_changed(self):
        self._connection_changed()
        if self.sources.currentIndex() != 0:self.request(1)

    def _favorites_changed(self):
        if self.sources.currentIndex() == 1:
            self.request(max(1,self.start_page))
        else:
            for card in self.cards:card.refresh_favorite()

    def edit_favorite(self, value=None):
        from .rh_model_favorite_editor import FavoriteEditor
        editor = FavoriteEditor(self, value)
        editor.saved.connect(self._favorite_saved);editor.show()

    def import_favorites(self):
        from .rh_model_import_ui import ModelImportDialog
        dialog=ModelImportDialog(self);dialog.imported.connect(self._favorite_saved);dialog.show()

    def _favorite_saved(self, value):
        if value['site'] != self.settings.host:return
        if not self.library and value['resource_type']!=self.resource_type:
            self.status.setText('已收藏其他类型的模型，可在 RH 模型库中查看。');return
        blockers = [QtCore.QSignalBlocker(w) for w in (self.kind,self.sources,self.search)]
        self.kind.setCurrentText(value['resource_type']);self.sources.setCurrentIndex(2 if value.get('bucket')=='uploads' else 1);self.search.clear()
        del blockers
        self._scope_changed()

    def toggle_favorite(self, card):
        from .rh_model_favorites import favorite_data
        try:
            existing = self.favorites.lookup(self.settings.host,card.record.get('resourceType'),card.version.get('node_token'))
            if existing:self.favorites.remove(existing['id'])
            else:
                local=card.record.get('_local_favorite') or {}
                cover=self.store_for(local.get('bucket')).cover_info(local)
                value=favorite_data(self.settings.host,card.record,card.version)
                if local:value.update(title=local.get('title'),notes=local.get('notes'),source='catalog',
                                      remote_id=local.get('remote_id',''),visibility=local.get('visibility') or ('self' if local.get('bucket')=='uploads' else 'public'))
                self.favorites.save(value,cover_bytes=cover[1].read_bytes() if cover else None)
        except Exception as error:self.status.setText('收藏操作失败：'+str(error))

    def pin_favorite(self, card):
        value = card.record.get('_local_favorite') or {}
        try:self.store_for(value.get('bucket')).set_pinned(value['id'],not value.get('pinned'))
        except Exception as error:self.status.setText('置顶失败：'+str(error))

    def copy_card(self, card):
        token = card.version.get('node_token')
        if token:QtWidgets.QApplication.clipboard().setText(token);self.status.setText('已复制 model name：'+token)

    def remove_local(self, value):
        try:self.store_for(value.get('bucket')).remove(value['id'])
        except Exception:self.status.setText('移除失败，请稍后重试')

    def _clear(self):
        self.selected_card = None
        for card in self.cards:card.hide();card.deleteLater()
        self.cards = []
        while self.grid.count():self.grid.takeAt(0)
        self.page, self.has_next, self.total, self.loaded_filter = 0, False, 0, None
        self.total_pages = 0
        self.scroll.verticalScrollBar().setValue(0)
        self._buttons()

    def _connection_changed(self):
        self.generation += 1;self.pending_search = False
        self.site.setText((self.resource_type or '全部类型') + ' · ' + ('国际站' if self.settings.host.endswith('.ai') else '中文站'))
        self.known_bases.clear();self.known_tags.clear()
        blockers = [QtCore.QSignalBlocker(w) for w in (self.base_list, self.tags, self.base)]
        self.base_list.clear();self.base.clear()
        while self.tags.count()>1:self.tags.removeTab(self.tags.count()-1)
        self.tags.hide()
        self.tags.setCurrentIndex(0);del blockers
        self._bases_changed(self.settings.host);self.base_models.refresh(self.settings.host)
        uploading=self.destination()=='uploads'
        self.create_button.setText('＋ 登记上传模型' if uploading else '＋ 自建收藏')
        self.import_button.setText('导入上传模型' if uploading else '导入收藏')
        self.scope_hint.setText('管理已上传模型的本地记录' if uploading else '保留常用模型，随时选用' if self.sources.currentIndex()==1 else '检索模型与版本，星标加入本地收藏')
        self._clear();self.status.setText('按模型名称搜索，选择版本后立即使用。')

    def _filters_changed(self, *unused):
        self.generation += 1;self.loaded_filter = None
        self.content.setEnabled(False)
        self.status.setText('点击搜索或按回车更新结果。');self._buttons()

    def _tag_changed(self, *unused):
        self._filters_changed();self.request(1)

    def reset_filters(self):
        blockers = [QtCore.QSignalBlocker(w) for w in (self.base_list, self.tags, self.base, self.kind)]
        if self.library:self.kind.setCurrentIndex(0);self.resource_type=''
        self.base_search.clear()
        for i in range(self.base_list.count()):self.base_list.item(i).setCheckState(QtCore.Qt.Unchecked)
        self.tags.setCurrentIndex(0);self.base.clear();del blockers
        self._filters_changed();self.request(1)

    def _buttons(self):
        loaded = self.loaded_filter is not None
        ready = not self._is_busy() and loaded and self.loaded_filter == self._filter()
        self.previous.setEnabled(ready and self.page > 1)
        self.next.setEnabled(ready and self.has_next)
        self.next.setText('下一组')
        self.page_input.setEnabled(ready and self.total_pages > 0)
        self.jump.setEnabled(ready and self.total_pages > 0)
        self.page_input.validator().setRange(1, max(1,self.total_pages))
        self.page_input.setText(str(self.page) if loaded and self.total_pages else '')
        self.page_label.setText(f'第 {self.page} / {self.total_pages} 页 · 共 {self.total} 个' if loaded and self.total_pages else '暂无结果' if loaded else '')
        count = len(self._filter()[1]) + (self._filter()[2] is not None) + bool(self.library and self.resource_type)
        self.filter_summary.setText('已选择 {} 个基础模型 · {}'.format(len(self._filter()[1]), self.resource_type or '全部模型类型'))
        self.filter_button.setText(f'筛选 · {count}' if count else '筛选')

    def request(self, page, append=False):
        append = False  # A group always replaces cards; no scroll-triggered accumulation.
        if self.sources.currentIndex() != 0:
            query = self._filter()
            if append and query != self.loaded_filter:return
            self.generation += 1;self.pending_search = False
            if not append:self._clear();self.start_page = max(1,page)
            try:
                result = self.store_for(self.destination()).page(self.settings.host,self.resource_type,query[0],query[1],query[2],max(1,page),self.PAGE_SIZE)
                last = max(1,(result['total']+self.PAGE_SIZE-1)//self.PAGE_SIZE)
                if page > last:
                    result = self.store_for(self.destination()).page(self.settings.host,self.resource_type,query[0],query[1],query[2],last,self.PAGE_SIZE)
                self._render_result(dict(result,filter=query))
            except Exception as error:self.status.setText('读取收藏失败：'+str(error));self._buttons()
            return
        if self.busy:
            if not append:self.pending_search = True
            return
        connection = self.settings.snapshot()
        key, host = connection['api_key'], connection['base_url']
        if not key:
            self.status.setText('请先在连接设置中添加当前站点的 API Key。');return
        if not _queries.acquire(blocking=False):
            self.status.setText('已有两个模型查询正在进行，请稍后重新搜索。');return
        query = self._filter()
        if append and query != self.loaded_filter:
            _queries.release();return
        self.generation += 1;generation = self.generation
        self.busy = True
        if query != self.loaded_filter:self._clear()
        self.start_page = max(1, page);self.content.setEnabled(False)
        self._buttons();self.status.setText('正在加载公共模型…')
        resource_type = self.resource_type

        def work():
            result, error = None, ''
            try:
                from api_calls.call_rh import list_public_models
                from .rh_model_favorites import TYPES
                types = [resource_type] if resource_type else list(TYPES)
                size = max(1,self.PAGE_SIZE//len(types))
                pages = []
                for kind in types:
                    if generation != self.generation:raise ValueError('stale')
                    page_result = list_public_models(key, kind, resource_name=query[0],
                        base_models=list(query[1]), tags=[query[2]] if query[2] is not None else [],
                        current=max(1,page), size=size, base_url=host, timeout=15)
                    pages.append(page_result)
                result = dict(records=[record for item in pages for record in item['records']],
                    current=max(1,page),total=sum(item['total'] for item in pages),
                    total_pages=max((max((item['total']+size-1)//size, page+1 if item['hasNext'] else 0) for item in pages),default=0),
                    hasNext=any(item['hasNext'] for item in pages),filter=query,append=False)
            except Exception as exc:
                code = str(getattr(exc, 'code', ''))
                detail = '（错误码 '+code+'）' if re.fullmatch(r'\d{1,6}', code) else ''
                error = '查询失败'+detail+'，可点击搜索重新查询。'
            finally:_queries.release()
            try:self.finished_query.emit(generation, result, error)
            except RuntimeError:pass

        threading.Thread(target=work, name='rh-public-models', daemon=True).start()

    @QtCore.pyqtSlot(int, object, str)
    def _finished(self, generation, result, error):
        self.busy = False
        if self.pending_search:
            self.pending_search = False;self.request(1);return
        if generation != self.generation:self._buttons();return
        if error:
            self.content.setEnabled(self.loaded_filter == self._filter())
            self.status.setText(error);self._buttons();return
        if result['current'] > max(1,result.get('total_pages',1)):
            self.request(max(1,result['total_pages']));return
        self._render_result(result)

    def _render_result(self, result):
        from .rh_model_cards import ModelCard
        self._clear()
        self.page, self.has_next, self.total = result['current'], result['hasNext'], result['total']
        self.start_page = self.page
        self.total_pages = result.get('total_pages', (self.total+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.loaded_filter = result['filter'];self.content.setEnabled(True)
        ids = {str(c.record.get('id') or c.record.get('nodeModelName')) for c in self.cards}
        for record in result['records'][:self.PAGE_SIZE]:
            identity = str(record.get('id') or record.get('nodeModelName'))
            if identity in ids:continue
            ids.add(identity)
            self.cards.append(ModelCard(record, self))
        self._metadata(result['records'])
        self._reflow();self._buttons()
        messages=['选择版本后点击星标收藏；点击封面查看详情。','本地收藏长期保留，可置顶、编辑和移除。','上传记录独立保存；星标仅加入本地收藏，移除不删除官网模型。']
        empty=['没有找到匹配模型，请调整搜索或清除筛选。','暂无匹配收藏，可从公共模型收藏，或自建、导入收藏。','暂无匹配上传记录，可登记模型，或从官网读取后导入。']
        self.status.setText(messages[self.sources.currentIndex()] if self.cards else empty[self.sources.currentIndex()])
        self.schedule_visible()

    def _metadata(self, records):
        from .rh_model_cards import model_versions
        blockers = [QtCore.QSignalBlocker(w) for w in (self.base_list, self.tags)]
        for record in records:
            for v in model_versions(record):
                base = v.get('baseModel')
                if isinstance(base,str) and base and base not in self.known_bases and len(self.known_bases)<128:
                    self.known_bases.add(base);item = QtWidgets.QListWidgetItem(base)
                    item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable);item.setCheckState(QtCore.Qt.Unchecked)
                    self.base_list.addItem(item)
            for tag in record.get('tags') or []:
                if not isinstance(tag,dict) or not str(tag.get('id','')).isdigit():continue
                identity = int(tag['id'])
                if identity not in self.known_tags and len(self.known_tags)<48:
                    name = str(tag.get('name') or identity);self.known_tags[identity] = name
                    idx=self.tags.addTab(name);self.tags.setTabData(idx,identity)
        del blockers
        self.tags.setVisible(self.tags.count() > 1)

    def _reflow(self):
        columns = max(1, min(5, (self.scroll.viewport().width()+14)//240))
        while self.grid.count():self.grid.takeAt(0)
        for col in range(5):self.grid.setColumnStretch(col, 1 if col<columns else 0)
        height = max(260, min(320, self.scroll.viewport().height() - 4))
        for index, card in enumerate(self.cards):
            card.setFixedHeight(height)
            self.grid.addWidget(card, index//columns, index%columns)

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type()==QtCore.QEvent.Resize:
            self._reflow();self.schedule_visible()
        return super().eventFilter(watched,event)

    def schedule_visible(self, *unused):
        if self.isVisible() and not self.visible_timer.isActive():self.visible_timer.start()

    def _visible(self):
        if not self.isVisible():self.visible_timer.stop();return
        viewport = self.scroll.viewport();area = viewport.rect().adjusted(0,-160,0,160)
        for card in self.cards:
            pos = card.mapTo(viewport,QtCore.QPoint())
            card.set_visible_image(area.intersects(QtCore.QRect(pos,card.size())))

    def _scrolled(self, *unused):
        self.schedule_visible()

    def _more(self):
        if self.has_next:self._go_page(self.page+1)

    def _go_page(self, page):
        if not self._is_busy() and self.loaded_filter == self._filter() and 1 <= page <= self.total_pages and page != self.page:
            self.request(page)

    def _jump_page(self):
        if self.page_input.hasAcceptableInput():self._go_page(int(self.page_input.text()))
        elif self.total_pages:self.status.setText(f'请输入 1～{self.total_pages} 范围内的页码。')

    def showEvent(self, event):
        super().showEvent(event);self.schedule_visible()

    def hideEvent(self, event):
        self.visible_timer.stop()
        self.filter_button.setChecked(False)
        for card in self.cards:card.set_visible_image(False)
        super().hideEvent(event)

    def reject(self):
        # Escape closes a picker dialog, but must not hide the embedded page.
        if not self.library:super().reject()

    def show_details(self, card):
        from .rh_model_dialogs import ModelDetailsDialog
        dialog=ModelDetailsDialog(self,card);dialog.show()

    def use_card(self, card):
        if card not in self.cards or self.loaded_filter != self._filter() or not card.use.isEnabled():return
        token = card.version.get('node_token')
        if self.library:self.copy_card(card);return
        if token:self.close();self.selected.emit(token)
