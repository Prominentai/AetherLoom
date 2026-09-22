"""Native, persistent canvas page. Execution belongs to the shared RH service."""
import copy
import json
import os
import uuid
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.paths import current_dir, resource_path
from aetherloom_core.prompt_history import TextSnapshot
from aetherloom_core.rh_ui import palette
from aetherloom_core.rh_parameters import collect_node_values, RhEnumComboBox
from . import model
from .storage import CanvasStore
from .engine import CanvasEngine
from .workflow_queue import ensure_workflow_queue
from .workflow_queue_panel import show_workflow_queue
from .graphics import CanvasScene, CanvasView, NodeItem, EdgeItem, KIND_NAMES, STATUS_NAMES
from .editors import Inspector, EdgeInspector
from .controls import CanvasStatus
from .page_preferences import CanvasPreferencesMixin, choice_key


RUNTIME_FIELDS = ('results', 'result_signatures', 'fingerprint', 'status', 'progress', 'node_progress', 'message', 'error', 'generation', 'cached', 'stale', 'activated', 'bypassed', '_restored_missing_results', '_restored_positions_ambiguous')
RUNTIME_FIELDS += ('_runtime_input_issue', '_runtime_missing_ports')
INPUT_HINT_FIELDS = ('_input_issue', '_input_missing_ports', '_input_issue_confirmed')


class _BatchCountSpinBox(QtWidgets.QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class _PackageSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, object, str)


class _PackageJob(QtCore.QRunnable):
    def __init__(self, kind, operation, signals):
        super().__init__()
        self.kind, self.operation, self.signals = kind, operation, signals

    def run(self):
        result, error = None, ''
        try:
            result = self.operation()
        except Exception as exception:
            error = str(exception)
        try:
            self.signals.finished.emit(self.kind, result, error)
        except RuntimeError:
            pass


def _schema(fields):
    return [(model.parameter_key(field), str(field.get('fieldType') or '').upper(),
             json.dumps(field.get('fieldData'), ensure_ascii=False, sort_keys=True)) for field in fields]


class NodeSearchPopup(QtWidgets.QFrame):
    """A single searchable palette for Tab, double click and loose cable ends."""
    chosen = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal(str)
    favorite_toggled = QtCore.pyqtSignal(str, str)

    def __init__(self, choices, anchor=None, parent=None, preferences=None):
        super().__init__(parent, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        self.setObjectName('canvasNodeSearch')
        self.setFont(QtGui.QFont('Microsoft YaHei UI', 9))
        self.choices = choices
        self.preferences = preferences or {}
        self.setMinimumWidth(280)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(8)
        title = QtWidgets.QLabel('添加兼容节点' if anchor else '添加节点')
        title.setObjectName('canvasSearchTitle')
        layout.addWidget(title)
        self.category = RhEnumComboBox()
        self.category.addItem('全部分类', '')
        self.category.addItem('收藏节点', '__favorites')
        self.category.addItem('最近使用', '__recent')
        for key, label in model.NODE_CATEGORIES.items():self.category.addItem(label, key)
        self.category.currentIndexChanged.connect(lambda:self._filter(self.search.text()))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索节点名称、App 或输入类型…')
        self.search.installEventFilter(self)
        self.search.textChanged.connect(self._filter)
        row = QtWidgets.QHBoxLayout();row.addWidget(self.search,1);row.addWidget(self.category)
        self.category.setMaximumWidth(120);layout.addLayout(row)
        self.listing = QtWidgets.QListWidget()
        self.listing.setUniformItemSizes(True)
        self.listing.setSpacing(2)
        self.listing.itemClicked.connect(self._choose)
        self.listing.itemActivated.connect(self._choose)
        layout.addWidget(self.listing, 1)
        self.favorite_button = QtWidgets.QPushButton('☆ 收藏节点')
        self.favorite_button.clicked.connect(self._toggle_current_favorite)
        self.listing.currentItemChanged.connect(self._favorite_changed)
        layout.addWidget(self.favorite_button)
        if anchor and anchor.get('remove_edge'):
            disconnect = QtWidgets.QPushButton('断开原连接，恢复内部值' if anchor.get('restore_internal', True) else '断开原连接')
            disconnect.clicked.connect(lambda: (self.disconnected.emit(anchor['remove_edge']), self.close()))
            layout.addWidget(disconnect)
        hint = QtWidgets.QLabel('Enter 添加 · ↑ ↓ 选择 · Esc 取消')
        hint.setObjectName('canvasSearchHint')
        layout.addWidget(hint)
        self._filter('')

    def _filter(self, text):
        text = text.strip().casefold()
        self.listing.clear()
        favorites, recent = self.preferences.get('favorites', []), self.preferences.get('recent', [])
        scope = self.category.currentData()
        order = {key: index for index, key in enumerate(recent)}
        choices = sorted(self.choices, key=lambda c: (choice_key(c['group'], c['value']) not in favorites,
                         order.get(choice_key(c['group'], c['value']), 999)))
        if scope == '__recent':
            choices.sort(key=lambda c: order.get(choice_key(c['group'], c['value']), 999))
        for choice in choices:
            key = choice_key(choice['group'], choice['value'])
            if scope == '__favorites' and key not in favorites:continue
            if scope == '__recent' and key not in recent:continue
            if scope and not scope.startswith('__') and choice['group'] != scope:continue
            if text and text not in choice.get('search', choice['label']).casefold():
                continue
            item = QtWidgets.QListWidgetItem(('★ ' if key in favorites else '') + choice['label'])
            item.setData(QtCore.Qt.UserRole, choice)
            item.setToolTip(choice.get('description', choice['label']))
            item.setSizeHint(QtCore.QSize(320, 35))
            self.listing.addItem(item)
        if self.listing.count():
            self.listing.setCurrentRow(0)
        else:
            item = QtWidgets.QListWidgetItem('没有匹配节点，请尝试其他名称')
            item.setFlags(QtCore.Qt.NoItemFlags)
            self.listing.addItem(item)

    def _favorite_changed(self, *unused):
        item = self.listing.currentItem()
        choice = item.data(QtCore.Qt.UserRole) if item else None
        self.favorite_button.setEnabled(bool(choice))
        favorite = choice and choice_key(choice['group'], choice['value']) in self.preferences.get('favorites', [])
        self.favorite_button.setText('★ 取消收藏' if favorite else '☆ 收藏节点')

    def _toggle_current_favorite(self):
        item = self.listing.currentItem()
        choice = item.data(QtCore.Qt.UserRole) if item else None
        if choice:
            self.favorite_toggled.emit(choice['group'], choice['value'])
            self._filter(self.search.text())

    def _choose(self, item):
        choice = item.data(QtCore.Qt.UserRole) if item else None
        if choice:
            self.chosen.emit(choice)
            self.close()

    def eventFilter(self, watched, event):
        if self.isVisible() and event.type() in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick):
            if not self.rect().contains(self.mapFromGlobal(event.globalPos())):
                self.close()
                return False
        if watched is self.search and event.type() == QtCore.QEvent.KeyPress:
            if event.key() in (QtCore.Qt.Key_Down, QtCore.Qt.Key_Up):
                step = 1 if event.key() == QtCore.Qt.Key_Down else -1
                self.listing.setCurrentRow((self.listing.currentRow() + step) % max(1, self.listing.count()))
                return True
            if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self._choose(self.listing.currentItem())
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        QtWidgets.QApplication.instance().installEventFilter(self)

    def hideEvent(self, event):
        application = QtWidgets.QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        super().hideEvent(event)

    def show_at(self, point, colors):
        self.setStyleSheet(f'''
            QFrame#canvasNodeSearch {{ background: {colors['surface']}; border: 1px solid {colors['border']}; border-radius: 10px; }}
            QFrame#canvasNodeSearch QLabel {{ color: {colors['text']}; background: transparent; border: none; }}
            QLabel#canvasSearchTitle {{ font-size: 14px; font-weight: 600; }}
            QLabel#canvasSearchHint {{ color: {colors['muted']}; font-size: 11px; }}
            QFrame#canvasNodeSearch QLineEdit {{ background: {colors['input']}; color: {colors['text']}; border: 1px solid {colors['accent']}; border-radius: 6px; padding: 8px; }}
            QFrame#canvasNodeSearch QComboBox {{ background: {colors['input']}; color: {colors['text']}; border: 1px solid {colors['border']}; border-radius: 6px; padding: 7px; }}
            QFrame#canvasNodeSearch QComboBox QAbstractItemView {{ background: {colors['surface']}; color: {colors['text']}; selection-background-color: {colors['accent_soft']}; selection-color: {colors['accent']}; }}
            QFrame#canvasNodeSearch QListWidget {{ color: {colors['text']}; background: transparent; border: none; outline: none; }}
            QFrame#canvasNodeSearch QListWidget::item {{ border-radius: 5px; padding: 5px; }}
            QFrame#canvasNodeSearch QListWidget::item:selected {{ color: {colors['accent']}; background: {colors['accent_soft']}; }}
            QFrame#canvasNodeSearch QPushButton {{ color: {colors['text']}; background: {colors['input']}; border: 1px solid {colors['border']}; border-radius: 6px; padding: 7px; }}
        ''')
        search_palette = self.search.palette()
        search_palette.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor(colors['muted']))
        self.search.setPalette(search_palette)
        screen = QtWidgets.QApplication.screenAt(point) or QtWidgets.QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QtCore.QRect(point, QtCore.QSize(420, 460))
        width, height = min(410, area.width()), min(440, area.height())
        self.resize(width, height)
        self.move(max(area.left(), min(point.x(), area.right() - width + 1)),
                  max(area.top(), min(point.y(), area.bottom() - height + 1)))
        self.show()
        self.search.setFocus(QtCore.Qt.PopupFocusReason)


class CanvasPage(CanvasPreferencesMixin, QtWidgets.QWidget):
    """One editing surface; the engine keeps other open/running canvases alive."""

    def __init__(self, owner, service, prepare_app=None, store=None):
        super().__init__(owner)
        self.owner, self.service = owner, service
        self.store = store or CanvasStore()
        self.settings = getattr(owner, 'settings', {})
        self._init_preferences()
        self._prepare = prepare_app or getattr(owner, '_canvas_prepare_app', None)
        existing_queue = getattr(owner, '_canvas_workflow_queue', None)
        engine = existing_queue.engine if existing_queue is not None else CanvasEngine(service, self._prepare_node, self.store, owner)
        self.workflow_queue = ensure_workflow_queue(owner, engine=engine, store=self.store)
        self.engine = self.workflow_queue.engine
        from .model_nodes import prepare as prepare_model
        self.engine.prepare_model = lambda node: prepare_model(owner, node)
        self.engine.output_root = lambda: str(owner.output_dir)
        self.engine.input_root = lambda: str(owner.input_dir)
        self.store = self.engine.store
        self._session_edits = getattr(owner, '_canvas_session_edits', None)
        if self._session_edits is None:
            self._session_edits = owner._canvas_session_edits = {}
        self.document = model.new_document()
        self.apps = {}
        self.histories = {}
        self._undo, self._redo, self._clipboard = [], [], None
        self._removed_runtime = {}
        self._updating, self._dirty, self._closed = False, False, False
        self._inspector = None
        self._selection_identity = None
        self._last_edit_path = None
        self._node_search = None
        self._queue_panel_bound = None
        self._responsive_mode = None
        self._wide_library_visible = False
        self._package_busy = False
        self._install_job = None
        self._install_busy = False
        self._install_summary = ''
        self._recovery_ready = False
        self._recovery_scheduled = set()
        self._persisted_ids = set()
        self._deleted_ids = set()
        self._package_signals = _PackageSignals(self)
        self._package_signals.finished.connect(self._package_finished)
        self.setObjectName('aetherloomCanvasPage')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setMinimumSize(0, 0)
        self._build_ui()
        # Undo coalescing only; editing never schedules a disk save.
        self._edit_group_timer = QtCore.QTimer(self)
        self._edit_group_timer.setSingleShot(True)
        self._edit_group_timer.setInterval(700)
        self._apply_preferences(self.canvas_preferences)
        self._workflow_watcher = QtCore.QFileSystemWatcher(self)
        self._workflow_watcher.directoryChanged.connect(self._workflow_directory_changed)
        self._workflow_cleanup = QtCore.QTimer(self)
        self._workflow_cleanup.setSingleShot(True)
        self._workflow_cleanup.setInterval(180)
        self._workflow_cleanup.timeout.connect(self._prune_workflows)
        self.engine.changed.connect(self._runtime_changed)
        self.workflow_queue.changed.connect(self._queue_changed)
        self.workflow_queue.error.connect(self._queue_error)
        self.refresh_apps()
        self._load_initial()
        self._watch_workflow_directory()
        self._prune_workflows()
        self.refresh_theme()
        from .cache_cleanup import CanvasCacheCleaner
        cleaner = getattr(owner, '_canvas_cache_cleaner', None)
        if cleaner is None:
            owner._canvas_cache_cleaner = CanvasCacheCleaner(self)
        else:
            cleaner.page = self

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        from aetherloom_core.ui import design
        design.page_layout(self, layout)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('画布')
        title.setObjectName('canvasPageTitle')
        design.title(title)
        titles = QtWidgets.QVBoxLayout();titles.setSpacing(4)
        titles.addWidget(title)
        subtitle = self.page_subtitle = QtWidgets.QLabel('连接节点，编排工作流');subtitle.setObjectName('canvasMuted')
        titles.addWidget(subtitle)
        heading.addLayout(titles)
        self.name_edit = QtWidgets.QLineEdit(self.document['name'])
        self.name_edit.setMaximumWidth(360)
        self.name_edit.setMinimumWidth(90)
        self.name_edit.editingFinished.connect(self._rename)
        heading.addWidget(self.name_edit, 1)
        heading.addStretch(1)
        self.save_state = QtWidgets.QLabel('本地画布')
        self.save_state.setObjectName('canvasMuted')
        heading.addWidget(self.save_state)
        layout.addWidget(design.header(heading))
        toolbar = QtWidgets.QToolBar();self.toolbar=toolbar
        toolbar.setMovable(False)
        toolbar.setIconSize(QtCore.QSize(16, 16))
        toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.palette_action = QtWidgets.QAction('显示节点库',self)
        self.palette_action.setCheckable(True)
        self.palette_action.setChecked(False)
        self.palette_action.toggled.connect(lambda checked: self.library.setVisible(checked))
        package_menu = QtWidgets.QMenu('画布',toolbar);self.document_menu=package_menu
        for label, callback in [('新建画布', self.new_canvas), ('打开画布…', self.open_canvas), ('保存', self.save), ('保存副本…', self.save_as)]:
            package_menu.addAction(label, callback)
        package_menu.addSeparator()
        package_menu.addAction('导入工作流 JSON…', self.import_canvas)
        package_menu.addAction('导出工作流 JSON…', self.export_canvas)
        package_menu.addSeparator()
        self.clear_action = package_menu.addAction('清空画布', self.clear_canvas)
        self.clear_action.setToolTip('移除全部节点和连线，保留当前画布；可通过 Ctrl+Z 撤销')
        package_menu.addAction('删除当前画布…',self.delete_canvas)
        def add_menu(menu):
            action=menu.menuAction();toolbar.addAction(action)
            toolbar.widgetForAction(action).setPopupMode(QtWidgets.QToolButton.InstantPopup)
        add_menu(package_menu)
        toolbar.addAction('打开', self.open_canvas).setToolTip('打开本地画布或工作流 JSON · Ctrl+O')
        toolbar.addAction('保存', self.save).setToolTip('保存当前画布 · Ctrl+S；点击运行时也会保存')
        self.add_node_action=toolbar.addAction('＋ 添加节点',self._quick_add_node)
        self.add_node_action.setToolTip('按分类搜索并添加节点 · 画布内按 Tab')
        toolbar.widgetForAction(self.add_node_action).setObjectName('canvasAddNodeButton')
        toolbar.addSeparator()
        self.run_action = QtWidgets.QAction('运行画布', self)
        self.run_action.triggered.connect(lambda: self.run_canvas())
        self.stop_action = QtWidgets.QAction('全部终止', self)
        self.stop_action.setToolTip('取消当前画布正在运行与排队的全部工作流组；保留已完成结果')
        self.stop_action.triggered.connect(self.stop_canvas)
        edit_menu=QtWidgets.QMenu('编辑',toolbar);self.edit_menu=edit_menu
        self.undo_action=edit_menu.addAction('撤销\tCtrl+Z',self.undo)
        self.redo_action=edit_menu.addAction('重做\tCtrl+Y',self.redo)
        edit_menu.addSeparator()
        edit_menu.addAction('复制节点\tCtrl+C',self.copy_nodes)
        edit_menu.addAction('粘贴节点\tCtrl+V',self.paste_nodes)
        edit_menu.addAction('删除所选\tDelete',self.delete_selected)
        edit_menu.addAction('全选节点\tCtrl+A',self.select_all_nodes)
        from .subgraphs import group_selected,import_group
        edit_menu.addAction('组合选中节点',lambda:group_selected(self))
        edit_menu.addAction('导入组合节点模板…',lambda:import_group(self))
        edit_menu.addSeparator()
        self.settings_action = edit_menu.addAction('打开所选设置', self._open_settings)
        self.settings_action.setShortcut(QtGui.QKeySequence('Alt+Return'))
        self.settings_action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.addAction(self.settings_action)
        add_menu(edit_menu)
        view_menu=QtWidgets.QMenu('视图',toolbar);self.view_menu=view_menu
        view_menu.addAction(self.palette_action);view_menu.addSeparator()
        view_menu.addAction('适应全部节点',lambda:self.view.fit_nodes())
        view_menu.addAction('恢复 100% 缩放',self._reset_canvas_zoom)
        add_menu(view_menu)
        spacer=QtWidgets.QWidget();spacer.setObjectName('canvasToolbarSpacer');spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        self.connection_action=toolbar.addAction('RH 连接',self._connection_settings)
        self.connection_action.setToolTip('与 RH App 主页共享站点和 API key 设置')
        self.queue_action = toolbar.addAction('工作流队列')
        self.queue_action.setCheckable(True)
        self.queue_action.triggered.connect(lambda checked: self._show_workflow_queue(checked))
        self.queue_action.setToolTip('显示 / 隐藏所有画布的工作流队列，可展开任务查看 App')
        self.undo_action.setToolTip('撤销画布编辑 · Ctrl+Z')
        self.redo_action.setToolTip('重做画布编辑 · Ctrl+Y')
        view_menu.addSeparator();view_menu.addAction(self.queue_action)
        layout.addWidget(toolbar)
        self.missing_banner = QtWidgets.QFrame()
        self.missing_banner.setObjectName('canvasMissingApps')
        missing_layout = QtWidgets.QHBoxLayout(self.missing_banner)
        missing_layout.setContentsMargins(12, 9, 12, 9)
        self.missing_label = QtWidgets.QLabel()
        self.missing_label.setTextFormat(QtCore.Qt.PlainText)
        self.missing_label.setWordWrap(True)
        self.missing_label.setMinimumWidth(0)
        missing_layout.addWidget(self.missing_label, 1)
        self.locate_missing_button = QtWidgets.QPushButton('定位节点')
        self.locate_missing_button.clicked.connect(self._locate_missing_nodes)
        missing_layout.addWidget(self.locate_missing_button)
        self.install_missing_button = QtWidgets.QPushButton('一键补齐')
        self.install_missing_button.clicked.connect(lambda: self._install_missing_apps())
        missing_layout.addWidget(self.install_missing_button)
        self.missing_banner.hide()
        layout.addWidget(self.missing_banner)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.library = QtWidgets.QFrame()
        self.library.setObjectName('canvasPanel')
        self.library.setMinimumWidth(160)
        self.library.setMaximumWidth(300)
        library_layout = QtWidgets.QVBoxLayout(self.library)
        library_layout.setContentsMargins(12, 12, 12, 12)
        library_title = QtWidgets.QLabel('节点库')
        library_title.setObjectName('canvasSectionTitle')
        library_heading=QtWidgets.QHBoxLayout();library_heading.addWidget(library_title,1)
        library_close=QtWidgets.QToolButton();library_close.setText('×');library_close.setToolTip('收起节点库，可从“视图”重新打开')
        library_close.clicked.connect(lambda:self.palette_action.setChecked(False));library_heading.addWidget(library_close)
        library_layout.addLayout(library_heading)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索节点名称或分类')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_library)
        library_search = QtWidgets.QHBoxLayout()
        library_search.addWidget(self.search, 1)
        self.library_scope = RhEnumComboBox()
        for title, value in [('全部', 'all'), ('收藏', 'favorites'), ('最近', 'recent')]:
            self.library_scope.addItem(title, value)
        self.library_scope.setMaximumWidth(85)
        self.library_scope.currentIndexChanged.connect(self._filter_library)
        library_search.addWidget(self.library_scope)
        library_layout.addLayout(library_search)
        from .controls import NodeLibrary
        self.library_list = NodeLibrary()
        self.library_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.library_list.customContextMenuRequested.connect(self._library_context_menu)
        self.library_list.setWordWrap(False);self.library_list.setTextElideMode(QtCore.Qt.ElideRight)
        self.library_list.itemActivated.connect(self._library_add)
        self.search.returnPressed.connect(lambda:self._library_add(self.library_list.currentItem()))
        library_layout.addWidget(self.library_list, 1)
        self.library_count=QtWidgets.QLabel();self.library_count.setObjectName('canvasMuted');self.library_count.setWordWrap(True);library_layout.addWidget(self.library_count)
        self.library_hint=QtWidgets.QLabel('拖入画布 / 双击添加 / Enter');self.library_hint.setObjectName('canvasMuted');self.library_hint.setWordWrap(True);library_layout.addWidget(self.library_hint)
        add = QtWidgets.QToolButton();add.setText('+');add.setToolTip('添加所选节点');self.library_add_button=add
        add.clicked.connect(lambda: self._library_add(self.library_list.currentItem()))
        add.setFixedSize(26,26);library_heading.insertWidget(1,add)
        refresh = QtWidgets.QToolButton();refresh.setText('刷新');refresh.setToolTip('刷新已添加 App')
        refresh.clicked.connect(self.refresh_apps)
        refresh.setFixedHeight(26);library_close.setFixedSize(26,26)
        library_heading.insertWidget(2,refresh)
        self.library_list.currentItemChanged.connect(lambda current,previous:add.setEnabled(current is not None and bool(current.data(0,QtCore.Qt.UserRole)) and not current.isHidden()))
        self.splitter.addWidget(self.library)
        self.library.setVisible(self.palette_action.isChecked())
        self.center = QtWidgets.QFrame()
        self.center.installEventFilter(self)
        self.center.setObjectName('canvasSurface')
        center_layout = QtWidgets.QVBoxLayout(self.center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        self.scene = CanvasScene(self)
        self.view = CanvasView(self.scene)
        self._selection_timer=QtCore.QTimer(self);self._selection_timer.setSingleShot(True)
        self._selection_timer.timeout.connect(self._selection_changed)
        self.scene.selectionChanged.connect(self._queue_selection_changed)
        self.scene.nodes_moved.connect(self._move_nodes)
        self.scene.nodes_resized.connect(self._resize_nodes)
        self.scene.result_requested.connect(self._focus_result)
        self.scene.connect_requested.connect(self._connect_nodes)
        self.scene.reconnect_requested.connect(self._reconnect_nodes)
        self.scene.disconnect_requested.connect(self._disconnect_edge)
        self.scene.add_requested.connect(self._show_node_search)
        self.scene.run_requested.connect(lambda node_id, force: self.run_canvas(target=node_id, force=force))
        self.scene.decode_requested.connect(self._focus_decode)
        self.scene.mask_requested.connect(self._edit_mask)
        self.scene.options_requested.connect(self._other_settings)
        self.scene.settings_requested.connect(self._open_settings)
        self.scene.option_changed.connect(self._node_changed)
        self.scene.batch_option_changed.connect(self._batch_node_changed)
        self.scene.run_selection_requested.connect(lambda ids,force:self.run_canvas(target=ids[0] if len(ids)==1 else ids,force=force))
        self.scene.action_requested.connect(self._action)
        self.view.files_dropped.connect(self._drop_files)
        self.view.node_dropped.connect(lambda choice,position:self._insert_choice(choice,position))
        self.view.view_changed.connect(self._view_changed)
        center_layout.addWidget(self.view)
        self.run_panel = QtWidgets.QFrame(self.center)
        self.run_panel.setObjectName('canvasRunPanel')
        run_layout = QtWidgets.QHBoxLayout(self.run_panel)
        run_layout.setContentsMargins(7, 7, 7, 7)
        run_layout.setSpacing(7)
        self._run_layout = run_layout
        self.batch_control = QtWidgets.QWidget()
        batch_layout = QtWidgets.QHBoxLayout(self.batch_control)
        batch_layout.setContentsMargins(2, 0, 2, 0)
        batch_layout.setSpacing(6)
        self.batch_label = QtWidgets.QLabel('批次')
        self.batch_label.setObjectName('canvasMuted')
        self.batch_spin = _BatchCountSpinBox()
        self.batch_spin.setObjectName('canvasBatchCount')
        self.batch_spin.setRange(1, 99)
        self.batch_spin.setValue(self.document.get('batch_count', 1))
        self.batch_spin.setKeyboardTracking(False)
        self.batch_spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.UpDownArrows)
        self.batch_spin.setAlignment(QtCore.Qt.AlignCenter)
        self.batch_spin.setFixedWidth(80)
        self.batch_spin.setMinimumHeight(34)
        self.batch_spin.setToolTip('整张画布连续执行的批次数（1–99）；单节点运行始终只执行一批，每个 App 节点对每组输入执行一次。')
        self.batch_spin.valueChanged.connect(self._batch_count_changed)
        batch_layout.addWidget(self.batch_label)
        batch_layout.addWidget(self.batch_spin)
        self.stop_button = QtWidgets.QToolButton()
        self.stop_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.stop_button.setDefaultAction(self.stop_action)
        self.stop_button.setObjectName('canvasStopButton')
        self.run_button = QtWidgets.QToolButton()
        self.run_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.run_button.setDefaultAction(self.run_action)
        self.run_button.setObjectName('canvasRunButton')
        run_layout.addWidget(self.batch_control)
        self.run_actions_widget = QtWidgets.QWidget()
        run_actions = QtWidgets.QHBoxLayout(self.run_actions_widget)
        run_actions.setContentsMargins(0, 0, 0, 0)
        run_actions.setSpacing(7)
        run_actions.addWidget(self.stop_button)
        run_actions.addWidget(self.run_button)
        run_layout.addWidget(self.run_actions_widget)
        self.splitter.addWidget(self.center)
        self.inspector_scroll = QtWidgets.QScrollArea()
        self.inspector_scroll.setObjectName('canvasInspector')
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setMinimumWidth(270)
        self.inspector_scroll.setMaximumWidth(430)
        self.inspector_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.splitter.addWidget(self.inspector_scroll)
        self.splitter.setSizes([200, 850, 320])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        layout.addWidget(self.splitter, 1)
        footer = QtWidgets.QHBoxLayout()
        self.status_label = CanvasStatus('双击 / ' + self.canvas_preferences['shortcuts']['add'] + ' 添加节点 · 中键 / 空格拖动画布')
        self.status_label.setObjectName('canvasMuted')
        self.status_label.setWordWrap(False)
        footer.addWidget(self.status_label, 1)
        self.zoom_label = QtWidgets.QLabel('100%')
        self.zoom_label.setToolTip('滚轮缩放画布 · 指向节点内滚动条时直接滚动内容\nShift + 滚轮也可滚动节点内容')
        self.zoom_label.setObjectName('canvasMuted')
        footer.addWidget(self.zoom_label)
        layout.addLayout(footer)
        self._empty_inspector()
        self._build_preference_actions()
        self._shortcuts = []
        for sequence, callback in [('Ctrl+S', self.save), ('Ctrl+Shift+S', self.save_as), ('Ctrl+N', self.new_canvas), ('Ctrl+O', self.open_canvas)]:
            shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(sequence), self)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _load_initial(self):
        try:
            active = self.store.get_active()
            if active:
                self.document = self.store.load(active)
            else:
                # An empty/new session starts blank; unrelated saved workflows
                # must not populate its nodes merely because they exist on disk.
                self.document = model.new_document()
            self._set_document(self.document)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._set_document(model.new_document())
            self._message(f'画布恢复失败：{error}')

    def refresh_apps(self):
        paths = dict(getattr(self.owner, '_rh_app_paths', {}) or {})
        app_root = Path(current_dir) / 'RH_apps'
        if app_root.is_dir():
            for path in app_root.glob('*/*.json'):
                if path.parent.name == path.stem:
                    paths.setdefault(path.stem, str(path))
        apps = {}
        for app_id, path in paths.items():
            try:
                if Path(path).stat().st_size > 8 * 1024 * 1024:
                    continue
                parsed = json.loads(Path(path).read_text(encoding='utf-8-sig'))
                data = parsed if isinstance(parsed, dict) else {}
                fields = data.get('nodeInfoList') or (data.get('data') or {}).get('nodeInfoList') or (parsed if isinstance(parsed, list) else [])
                if not isinstance(fields, list):
                    continue
                apps[str(app_id)] = {
                    'webapp_id': str(app_id), 'name': str(data.get('title') or data.get('webappName') or data.get('name') or app_id),
                    'nodes': copy.deepcopy(fields), 'base_url': str(data.get('base_url') or ''),
                }
                from aetherloom_core.rh_model_apps import metadata
                apps[str(app_id)].update(metadata(data))
                if 'url' in data:
                    apps[str(app_id)]['url'] = str(data.get('url') or '')
                if data.get('url_error'):
                    apps[str(app_id)]['url_error'] = str(data['url_error'])
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        self.apps = apps
        self.library_list.clear()
        for kind in model.LIBRARY_KINDS:
            group = model.node_category(kind)
            self.library_list.add_choice(model.TITLES[kind], group, kind,
                                         model.NODE_CATEGORIES[group] + ' · ' + model.TITLES[kind])
        for app_id, app in sorted(apps.items(), key=lambda pair: pair[1]['name'].lower()):
            from aetherloom_core.rh_model_apps import GROUPS, backend
            group = GROUPS[backend(app)]
            self.library_list.add_choice(app['name'], group, app_id,
                                         model.NODE_CATEGORIES[group] + ' · ' + app['name'] + '\n' + app_id)
        self._refresh_library_preferences()
        self._refresh_missing_apps()
        if self._selection_identity and self._inspector is not None:
            self._selection_changed(force=self._inspector is not None)

    def _connection_settings(self):
        from aetherloom_core.rh_connections import open_connection_settings
        open_connection_settings(self.owner, parent=self)

    def _show_workflow_queue(self, checked=None):
        panel = getattr(self.owner, '_canvas_workflow_queue_panel', None)
        if checked is False:
            if panel is not None:
                panel.hide()
            return panel
        panel = show_workflow_queue(self.owner, self.workflow_queue)
        if self._queue_panel_bound is not panel:
            panel.visibility_changed.connect(self._queue_panel_visibility_changed)
            self._queue_panel_bound = panel
        self.queue_action.setChecked(True)
        return panel

    def _queue_panel_visibility_changed(self, visible):
        if not self._closed:
            self.queue_action.setChecked(visible)

    def _queue_changed(self, *unused):
        if not self._closed:
            self._sync_actions()

    def _queue_error(self, message):
        if not self._closed:
            self._message(message)

    def _app_requirements(self, node_ids=None):
        requests, issues, missing_nodes = {}, [], 0
        for node in self.document['nodes']:
            if node.get('kind') != 'app' or (node_ids is not None and node['id'] not in node_ids):
                continue
            app = node.get('app') or {}
            wid = str(app.get('webapp_id') or '')
            try:
                reference = model.app_reference(app)
            except ValueError as error:
                issues.append(str(node.get('title') or wid or 'App') + '：' + str(error))
                continue
            if wid not in self.apps:
                missing_nodes += 1
                if app.get('backend') in ('rh_standard', 'rh_llm'):
                    reference.update(backend=app['backend'], model_definition=copy.deepcopy(app.get('model_definition')),
                                     nodes=copy.deepcopy(app.get('nodes') or []))
                requests.setdefault(reference['webapp_id'], reference)
        return list(requests.values()), issues, missing_nodes

    def _refresh_missing_apps(self):
        requests, issues, count = self._app_requirements()
        unsupported = [node for node in self.document['nodes'] if model.unknown_node(node)]
        pieces = []
        if requests:
            from aetherloom_core.rh_model_apps import LABELS
            counts = {kind:sum(ref.get('backend','rh_app')==kind for ref in requests) for kind in LABELS}
            names = '、'.join(f'{LABELS[kind]} {number} 个' for kind,number in counts.items() if number)
            pieces.append(f'此画布缺少：{names}，涉及 {count} 个节点。补齐后保留各节点参数和连线。')
        if issues:
            pieces.append(f'{len(issues)} 个节点的应用或模型引用无效，请检查对应节点。')
        if unsupported:
            pieces.append(f'{len(unsupported)} 个节点类型暂不支持，已保留参数和连线；请更新客户端或替换对应节点。')
        if self._install_summary:
            pieces.append(self._install_summary)
        self.missing_label.setText(' '.join(pieces))
        self.missing_label.setToolTip('\n'.join(issues + [model.node_title(n)+' ['+n['kind']+']' for n in unsupported]))
        self.install_missing_button.setText('正在补齐…' if self._install_busy else '一键补齐')
        self.install_missing_button.setEnabled(bool(requests) and not self._install_busy)
        self.missing_banner.setVisible(bool(pieces) or self._install_busy)
        self.install_missing_button.setVisible(bool(requests) or self._install_busy)
        self.locate_missing_button.setVisible(bool(requests or issues or unsupported))
        from .dependencies import refresh
        refresh(self)
        from .input_requirements import refresh as refresh_inputs
        refresh_inputs(self)
        if isinstance(self._inspector, Inspector) and getattr(self._inspector, 'install_button', None):
            self._inspector.install_button.setEnabled(not self._install_busy)

    def _locate_missing_nodes(self):
        from .dependencies import notice
        ids = [node['id'] for node in self.document['nodes'] if notice(node, self.apps)]
        # Reveal a collapsed group when its unavailable member is selected.
        groups = [node for node in self.document['nodes'] if node['kind']=='subgraph'
                  and node.get('params', {}).get('collapsed', True) and set(node.get('members', [])) & set(ids)]
        if groups:
            self._checkpoint()
            for node in groups:node.setdefault('params', {})['collapsed'] = False
            self._edited(rebuild=True)
        self.scene.clearSelection()
        for identity in ids:
            if identity in self.scene.nodes:self.scene.nodes[identity].setSelected(True)
        if ids:self.view.reveal_nodes(ids)

    def _install_missing_apps(self, node_id=None):
        if self._closed or self._install_busy:
            return
        self.refresh_apps()
        references, issues, unused = self._app_requirements({node_id} if node_id else None)
        if not references:
            self._message(issues[0] if issues else '所需应用和模型已添加到本机。')
            return
        install = getattr(self.owner, '_rh_install_apps', None)
        if install is None:
            self._message('App 添加服务尚未就绪，请重新打开客户端。')
            return
        self._install_busy = True
        self._install_summary = f'准备补齐 {len(references)} 个应用或模型…'
        self._refresh_missing_apps()
        try:
            self._install_job = install(references, self._install_progress, self._install_finished)
        except (OSError, ValueError, RuntimeError) as error:
            self._install_busy = False
            self._install_summary = '添加失败：' + str(error)
            self._refresh_missing_apps()

    def _install_progress(self, index, total, app, error):
        if self._closed:
            return
        self._install_summary = f'已处理 {index} / {total} 个应用或模型' + ('，部分添加失败。' if error else '…')
        self._refresh_missing_apps()

    def _install_finished(self, report):
        self._install_job = None
        self._install_busy = False
        if self._closed:
            return
        failures = report.get('failed') or []
        self._install_summary = (f"已补齐 {len(report.get('added') or [])} 个应用或模型。"
                                 + (f'{len(failures)} 个添加失败，悬停查看原因后重试。' if failures else ''))
        # The installer has refreshed the owner's catalog. Existing canvas node
        # definitions/values remain independent; changed schemas require rebind.
        self.refresh_apps()
        if isinstance(self._inspector, Inspector) and self._inspector.install_button is not None:
            self._selection_changed(force=True)
        if failures:
            details = '\n'.join(str(item.get('webapp_id') or 'App') + '：' + str(item.get('error') or '添加失败') for item in failures)
            self.missing_label.setToolTip(details)
        else:
            self._message(self._install_summary)
            self._install_summary = ''
            self._refresh_missing_apps()

    def _filter_library(self):
        self.library_list.scope = self.library_scope.currentData()
        visible = self.library_list.filter(self.search.text())
        self.library_add_button.setEnabled(bool(visible))
        self.library_count.setText(f'{len(visible)} 个可用节点' if visible else '没有匹配的节点')
        self.library_hint.setText('拖入画布 / 双击添加 / Enter' if visible else '试试其他关键词；App 需先添加。')

    def _quick_add_node(self):
        position=self.view.mapToScene(self.view.available_rect().center().toPoint())
        self._show_node_search(position)

    def _reset_canvas_zoom(self):
        center=self.view.mapToScene(self.view.available_rect().center().toPoint())
        self.view.resetTransform();self.view._center_available(center);self.view.view_changed.emit()

    def _library_add(self, item):
        if item is None or item.isHidden():
            return
        choice = item.data(0, QtCore.Qt.UserRole)
        if not choice:return
        group, value = choice
        center = self.view.mapToScene(self.view.available_rect().center().toPoint())
        self._insert_choice({'group': group, 'value': value}, center - QtCore.QPointF(134, 90))

    def _make_node(self, group, value):
        if group in ('app', 'standard', 'rh_llm'):
            app = copy.deepcopy(self.apps[value])
            cached_page = (getattr(self.owner, '_rh_app_pages', {}) or {}).get(value)
            parsed = getattr(cached_page, '_rh_parsed', None) if cached_page is not None else None
            if parsed is None:
                path = (getattr(self.owner, '_rh_app_paths', {}) or {}).get(value)
                if path and Path(path).is_file():
                    if Path(path).stat().st_size > 8 * 1024 * 1024:
                        raise ValueError('App 参数文件过大')
                    parsed = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if parsed is not None:
                data = parsed if isinstance(parsed, dict) else {}
                current_fields = data.get('nodeInfoList') or (data.get('data') or {}).get('nodeInfoList') or (parsed if isinstance(parsed, list) else [])
                if isinstance(current_fields, dict):
                    current_fields = [current_fields]
                if isinstance(current_fields, list):
                    app['nodes'] = copy.deepcopy(current_fields)
                from aetherloom_core.rh_model_apps import metadata
                app.update(metadata(data))
                app['name'] = str(data.get('title') or data.get('webappName') or data.get('name') or app['name'])
                for key in ('base_url', 'url', 'url_error'):
                    if key in data:
                        app[key] = str(data.get(key) or '')
            if cached_page is not None:
                editors = getattr(cached_page, '_rh_node_widgets', {}) or {}
                timers = []
                for entry in editors.values():
                    timer = getattr(entry.get('te'), '_rh_persist_timer', None)
                    if timer is not None and timer.isActive():
                        timers.append((timer, timer.remainingTime()))
                try:
                    app['nodes'] = collect_node_values(app['nodes'], editors)
                finally:
                    # Snapshotting a canvas node must not cancel the App page's
                    # pending persistence of edits the user already made there.
                    for timer, remaining in timers:
                        timer.start(max(0, remaining))
            if not app.get('url') and not app.get('base_url') and not app.get('url_error'):
                connection = getattr(self.owner, '_rh_connection_settings', None)
                host = getattr(connection, 'host', '')
                host_editor = getattr(self.owner, 'rh_host_combo', None)
                if not host and host_editor is not None:
                    host = host_editor.currentText()
                if host:
                    # Legacy installed definitions did not store their source.
                    # Capture the active site only when creating a new node.
                    app['base_url'] = str(host)
            self.apps[value] = copy.deepcopy(app)
            decode = copy.deepcopy((getattr(self.owner, 'rh_local_decode_settings', {}) or {}).get(value, {}))
            decode = dict({'enabled': False, 'mode': 'grc', 'password': '', 'grid_cols': 32, 'delete_original': True}, **decode)
            if cached_page is not None:
                for key, name, getter in (
                    ('enabled', '_rh_local_decode_cb', 'isChecked'),
                    ('mode', '_rh_local_mode_combo', 'currentData'),
                    ('grid_cols', '_rh_local_grid_spin', 'value'),
                    ('password', '_rh_local_pwd_edit', 'text'),
                    ('delete_original', '_rh_local_delete_original_cb', 'isChecked')):
                    widget = getattr(cached_page, name, None)
                    if widget is not None:
                        decode[key] = getattr(widget, getter)()
            from aetherloom_core.rh_model_apps import supports_local_decode
            if not supports_local_decode(app):decode = {}
            node = model.new_node('app', app['name'], app=app, decode_settings=decode,
                                  params={model.parameter_key(field): copy.deepcopy(field.get('fieldValue', '')) for field in app['nodes']})
            model.normalize_app_urls({'nodes': [node]})
        else:
            if value in model.MODEL_KINDS:
                from .model_nodes import create
                node = create(self.owner, value)
            else:
                node = model.new_node(value)
            if value == 'select':
                node['params']['indices'] = [1]
        return node

    def _search_choices(self, anchor):
        choices = []
        prototypes = [(model.node_category(kind), kind, model.TITLES[kind], model.new_node(kind))
                      for kind in model.LIBRARY_KINDS]
        from aetherloom_core.rh_model_apps import GROUPS, backend
        prototypes += [(GROUPS[backend(app)], key, app['name'], model.new_node('app', app=app)) for key, app in self.apps.items()]
        anchor_node = next((node for node in self.document['nodes'] if anchor and node['id'] == anchor['node_id']), None)
        if anchor and anchor_node is None:
            return choices
        accepted = None
        if anchor and not anchor['output']:
            accepted = next((port['type'] for port in model.input_ports(anchor_node) if port['key'] == anchor['input']), None)
            if accepted is None:
                return choices
        type_names = {'mask': '遮罩', 'bounding': 'Bounding', 'text': '文本', 'image': '图像', 'audio': '音频', 'video': '视频',
                      'int': 'INT', 'float': 'FLOAT', 'boolean': '布尔', 'enum': '枚举', 'number': '数值', 'scalar': '枚举', 'file': '任意', 'any': '任意', 'archive': '压缩文件',
                      'batch': 'Batch', 'image_input': '图像', 'video_input': '视频', 'audio_input': '音频', 'text_input': '文本',
                      'bounding_input': 'Bounding', 'mask_input': '遮罩'}
        for group, value, title, prototype in prototypes:
            prefix = model.NODE_CATEGORIES[group] + ' · '
            choice = {'group': group, 'value': value, 'label': prefix + title,
                      'search': prefix + title + ' ' + str(value)}
            if anchor and anchor['output']:
                for port in model.input_ports(prototype):
                    if any(model.types_compatible(kind, port['type']) for kind in model.connection_output_types(anchor_node, anchor.get('input', 'output'))):
                        label = prefix + title + ' → ' + port['label'] + ' · ' + type_names.get(port['type'], port['type'])
                        choices.append(dict(choice, label=label, port=port['key'], search=label + ' ' + str(value)))
            elif anchor:
                for port in model.output_ports(prototype):
                    if model.types_compatible(port['type'], accepted):
                        label = choice['label'] + (' · ' + port['label'] if prototype['kind'] in ('app', 'list_select') else '')
                        choices.append(dict(choice, label=label, output=port['key']))
            else:
                choices.append(choice)
        return choices

    def _show_node_search(self, position, anchor=None):
        if self._node_search is not None:
            self._node_search.close()
            self._node_search.deleteLater()
        position = QtCore.QPointF(position)
        popup = NodeSearchPopup(self._search_choices(anchor), anchor, self, self.canvas_preferences)
        self._node_search = popup
        popup.chosen.connect(lambda choice: self._insert_choice(choice, position, anchor))
        popup.disconnected.connect(self._disconnect_edge)
        popup.favorite_toggled.connect(self._toggle_favorite)
        point = self.view.viewport().mapToGlobal(self.view.mapFromScene(position))
        popup.show_at(point, self.scene.colors)

    def _insert_choice(self, choice, position, anchor=None):
        try:
            node = self._make_node(choice['group'], choice['value'])
            node['size'] = self.view.initial_node_size(node)
            node['x'], node['y'] = position.x(), position.y()
            candidate = copy.deepcopy(self.document)
            candidate['nodes'].append(node)
            if anchor:
                if anchor.get('remove_edge'):
                    candidate['edges'] = [edge for edge in candidate['edges'] if edge['id'] != anchor['remove_edge']]
                if anchor['output']:
                    model.connect(candidate, anchor['node_id'], node['id'], choice['port'], output=anchor.get('input', 'output'))
                else:
                    candidate['edges'] = [edge for edge in candidate['edges'] if not (edge['target'] == anchor['node_id'] and edge['input'] == anchor['input'])]
                    model.connect(candidate, node['id'], anchor['node_id'], anchor['input'], output=choice.get('output', 'output'))
            model.validate_document(candidate)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            self._message(f'无法添加节点：{error}')
            return
        self._checkpoint()
        self.document = candidate
        self._mark_stale(anchor['node_id'] if anchor and not anchor['output'] else node['id'])
        self._edited(rebuild=True, select=node['id'])
        self.view.reveal_nodes([node['id']])
        self._remember_choice(choice)

    def _prepare_node(self, node, rh_nodes):
        model.app_reference(node.get('app') or {})
        app_id = str(node.get('app', {}).get('webapp_id', ''))
        installed = self.apps.get(app_id)
        if installed is None:
            from .dependencies import notice
            issue = notice(node, self.apps)
            raise ValueError(model.node_title(node) + '：' + (issue['label'] if issue else '缺少依赖') + '，请先一键补齐')
        if (installed.get('model_definition') != node.get('app', {}).get('model_definition')
                or _schema(installed['nodes']) != _schema(node.get('app', {}).get('nodes', []))):
            raise ValueError(f"{node.get('title', 'App')} 的定义已变化，请在节点设置中重新绑定参数")
        if self._prepare is None:
            raise ValueError('共享执行服务尚未就绪')
        return self._prepare(node, rh_nodes)

    def _set_document(self, doc):
        session_edit = doc['id'] in self._session_edits
        if session_edit:
            try:
                doc = self.engine.view_document(doc['id'])
                self._last_runtime_revision = doc.pop('_view_revision')
            except (KeyError, RuntimeError):
                self._session_edits.pop(doc['id'], None)
                session_edit = False
        self._document_epoch = getattr(self, '_document_epoch', 0) + 1
        self._edit_group_timer.stop()
        if self._node_search is not None:
            self._node_search.close()
        self._updating = True
        self.document = doc
        self.engine.set_view_canvas(doc['id'] if self.isVisible() else '')
        if not self._install_busy:
            self._install_summary = ''
        if self.store.path_for(doc['id']).is_file():
            self._persisted_ids.add(doc['id'])
            self._deleted_ids.discard(doc['id'])
        try:
            if self.store.path_for(doc['id']).is_file():
                self.store.set_active(doc['id'])
        except (OSError, ValueError) as error:
            self._message(f'记录当前画布失败：{error}')
        self.name_edit.setText(str(doc.get('name', '未命名画布')))
        self._undo, self._redo = [], []
        self._history_weights = {}
        self._removed_runtime = {}
        self._selection_identity = None
        self.scene.set_document(doc)
        self._empty_inspector()
        view = doc.get('view') or {}
        self.view.restore_view(view)
        zoom = self.view.transform().m11()
        self._updating, self._dirty = False, session_edit
        attached = None if session_edit else self.engine.attach(doc)
        if attached:
            self.document = self.engine.view_document(doc['id'])
            self._last_runtime_revision = self.document.pop('_view_revision')
            self.scene.refresh_nodes(self.document)
        self._sync_actions()
        self._refresh_missing_apps()
        self.save_state.setText('未保存 · 运行时保存' if session_edit else '已保存' if self.store.path_for(doc['id']).exists() else '尚未保存')
        self.save_state.setToolTip(str(self.store.path_for(doc['id'])))
        self.zoom_label.setText(f'{int(zoom * 100)}%')
        self._schedule_selected_recovery()

    def enable_selected_recovery(self):
        """Called after the owner has installed all App/task observers."""
        self._recovery_ready = True
        self._recover_selected(self.document['id'])

    def _schedule_selected_recovery(self):
        if not self._recovery_ready or self._closed:
            return
        canvas_id = self.document['id']
        if canvas_id in self._deleted_ids or canvas_id in self._recovery_scheduled:
            return
        self._recovery_scheduled.add(canvas_id)
        QtCore.QTimer.singleShot(0, lambda: self._recover_selected(canvas_id))

    def _recover_selected(self, canvas_id):
        self._recovery_scheduled.discard(canvas_id)
        if self._closed or self.document['id'] != canvas_id or self._is_deleted(canvas_id):
            return
        try:
            # The selected document has already been checked by store.load.
            # Passing it avoids a second read and never scans other snapshots.
            errors = self.workflow_queue.recover_selected(self.document)
        except (OSError, ValueError, RuntimeError):
            errors = []
        if errors:
            self.status_label.setText('部分结果下载暂未恢复；上次会话的生成与排队任务不会自动续跑。')
        self._sync_actions()

    def _watch_workflow_directory(self):
        wanted = {str(path) for path in (self.store.root, self.store.root.parent) if path.is_dir()}
        watched = set(self._workflow_watcher.directories())
        if wanted - watched:
            self._workflow_watcher.addPaths(sorted(wanted - watched))

    def _workflow_directory_changed(self, *unused):
        if not self._closed:
            self._workflow_cleanup.start()

    def _is_deleted(self, canvas_id):
        return canvas_id in self._deleted_ids or (canvas_id in self._persisted_ids and not self.store.path_for(canvas_id).is_file())

    def _mark_workflow_deleted(self, canvas_id):
        self._session_edits.pop(canvas_id, None)
        if canvas_id not in self._deleted_ids:
            self._deleted_ids.add(canvas_id)
            try:
                self.engine.forget_deleted(canvas_id)
            except (OSError, ValueError, RuntimeError):
                pass
        try:
            self.store.discard_runtime(canvas_id)
        except (OSError, ValueError):
            # Keep deletion authoritative even if an antivirus briefly holds
            # the snapshot open; the watcher/startup cleanup can retry later.
            pass
        if canvas_id == self.document['id']:
            self._edit_group_timer.stop()
            self.save_state.setText('工作流已删除')
            self._sync_actions()

    def _prune_workflows(self):
        if self._closed:
            return
        try:
            removed = set(self.store.prune_orphans())
            removed.update(canvas_id for canvas_id in self._persisted_ids if not self.store.path_for(canvas_id).is_file())
            for canvas_id in removed:
                self._mark_workflow_deleted(canvas_id)
        except (OSError, ValueError, RuntimeError):
            # A transient directory lock must not interrupt editing or recovery.
            pass
        self._watch_workflow_directory()

    def _checkpoint(self, edit_path=None):
        if edit_path is not None and edit_path == self._last_edit_path and self._edit_group_timer.isActive():
            return
        self._last_edit_path = edit_path
        self._undo.append(self._edit_snapshot())
        self._redo.clear()
        self._trim_history()
        self._prune_removed_runtime()

    def _prune_removed_runtime(self):
        retained = {node['id'] for snapshot in self._undo + self._redo for node in snapshot['nodes']}
        self._removed_runtime = {key: value for key, value in self._removed_runtime.items() if key in retained}

    def _edit_snapshot(self):
        # Undo is editing history, never a duplicate of task/result history.
        result = {key: copy.deepcopy(value) for key, value in self.document.items() if key != 'run'}
        result['run'] = {}
        for node in result['nodes']:
            for key in RUNTIME_FIELDS + INPUT_HINT_FIELDS:
                node.pop(key, None)
        return result

    def _edited(self, rebuild=False, select=None, connections=False):
        self._edit_serial = getattr(self, '_edit_serial', 0) + 1
        model.sync_dynamic_inputs(self.document)
        self._dirty = True
        deleted = self._is_deleted(self.document['id'])
        self.save_state.setText('工作流已删除 · 手动保存可重新建立' if deleted else '未保存 · 运行时保存')
        self.save_state.setToolTip('修改仅保留在本次会话，退出后舍弃；点击运行或按 Ctrl+S 保存。')
        if not deleted:
            self._edit_group_timer.start()
        update = getattr(self.engine, 'update_document', None)
        if update and not deleted:
            self.document = update(self.document)
        if not deleted:
            self._remember_session_edit()
        if connections:
            self._updating = True
            try:
                self.scene.sync_connections(self.document)
                if select in self.scene.nodes:
                    self.scene.clearSelection();self.scene.nodes[select].setSelected(True)
            finally:self._updating = False
            self._selection_changed(force=self._inspector is not None)
        elif rebuild:
            self._updating = True
            self.scene.set_document(self.document)
            if select in self.scene.nodes:
                self.scene.nodes[select].setSelected(True)
            self._updating = False
            self._selection_identity = None
            self._selection_changed(force=self._inspector is not None)
        else:
            self.scene.refresh_nodes(self.document)
        self._sync_actions()
        self._refresh_missing_apps()

    def _rename(self):
        name = self.name_edit.text().strip() or '未命名画布'
        if name != self.document['name']:
            self._checkpoint()
            self.document['name'] = name
            self._edited()

    def _move_nodes(self, positions):
        positions = dict(positions)
        locked = {node['id'] for node in self.document['nodes'] if node.get('layout_locked')}
        positions = {key: value for key, value in positions.items() if key not in locked}
        if not positions:return
        self._checkpoint()
        for group in self.document['nodes']:
            if group['id'] in positions and group['kind']=='subgraph':
                dx,dy=positions[group['id']][0]-group.get('x',0),positions[group['id']][1]-group.get('y',0)
                for member in self.document['nodes']:
                    if member['id'] in group.get('members',[]) and member['id'] not in positions and member['id'] not in locked:
                        positions[member['id']]=(member.get('x',0)+dx,member.get('y',0)+dy)
                        if member['id'] in self.scene.nodes:self.scene.nodes[member['id']].setPos(*positions[member['id']])
        for node in self.document['nodes']:
            if node['id'] in positions:
                node['x'], node['y'] = positions[node['id']]
        self._edited()

    def _resize_nodes(self, sizes):
        locked = {node['id'] for node in self.document['nodes'] if node.get('layout_locked')}
        sizes = {key: value for key, value in sizes.items() if key not in locked}
        if not sizes:return
        self._checkpoint()
        for node in self.document['nodes']:
            if node['id'] in sizes:
                node['size'] = list(sizes[node['id']])
        self._edited()

    def _connect_nodes(self, source, target, port, output='output'):
        self._reconnect_nodes('', source, target, port, output)

    def _reconnect_nodes(self, old_id, source, target, port, output='output'):
        candidate = copy.deepcopy(self.document)
        removed = [edge for edge in candidate['edges'] if edge['id'] == old_id or (edge['target'] == target and edge['input'] == port)]
        if len(removed) == 1 and removed[0]['source'] == source and removed[0]['target'] == target and removed[0]['input'] == port and removed[0].get('output', 'output') == output:
            return
        candidate['edges'] = [edge for edge in candidate['edges'] if edge not in removed]
        try:
            model.connect(candidate, source, target, port, output=output)
        except ValueError as error:
            self._message(str(error))
            return
        self._checkpoint()
        self.document = candidate
        for edge in removed:
            self._mark_stale(edge['target'])
        self._mark_stale(target)
        self._edited(connections=True, select=target)

    def _disconnect_edge(self, edge_id):
        edge = next((edge for edge in self.document['edges'] if edge['id'] == edge_id), None)
        if edge is None:
            return
        self._checkpoint()
        self.document['edges'].remove(edge)
        self._mark_stale(edge['target'])
        self._edited(connections=True, select=edge['target'])
        target = next((node for node in self.document['nodes'] if node['id'] == edge['target']), {})
        internal = target.get('kind') in {'app', 'text_file'} | set(model.MODEL_KINDS) or target.get('kind') == 'rename' and edge.get('input') in ('name', 'extension')
        self._message('连接已断开，输入恢复使用节点内部值。' if internal else '连接已断开，请连接上游结果后运行。')

    def _mark_stale(self, node_id):
        self._mark_stale_many([node_id])

    def _mark_stale_many(self, node_ids):
        outgoing={}
        for edge in self.document['edges']:outgoing.setdefault(edge['source'],[]).append(edge['target'])
        pending, found = list(node_ids), set()
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(outgoing.get(current,[]))
        for node in self.document['nodes']:
            if node['id'] in found and (node.get('results') or self.engine.is_running(self.document['id'])):
                node['_ui_stale'] = True

    def _node_changed(self, node_id, path, value):
        if self._closed:return
        node = next((node for node in self.document['nodes'] if node['id'] == node_id), None)
        if node is None:
            return
        keys = path.split('.', 1)
        container = node.setdefault(keys[0], {}) if len(keys) == 2 else node
        key = keys[-1]
        if container.get(key) == value:
            return
        reference = None
        if path == 'app.url':
            try:
                reference = model.app_reference(dict(container, url=value))
            except ValueError as error:
                self._message(str(error))
                return
        if not (node['kind'] in ('text', 'note', 'text_split') and path=='params.text'):
            self._checkpoint(None if node['kind'] == 'text_template' and path == 'params.template' else (node_id, path))
        container[key] = copy.deepcopy(value)
        if node['kind'] == 'text_template' and path == 'params.template':
            from .utility_nodes import template_keys
            valid = {'var:' + item for item in template_keys(value)}
            self.document['edges'] = [edge for edge in self.document['edges']
                                      if edge['target'] != node_id or edge['input'] in valid]
        if path == 'app.url':
            container['url'] = reference['url']
            container['base_url'] = reference['base_url']
            container.pop('url_error', None)
        if path != 'title':
            self._mark_stale(node_id)
        self._edited(rebuild=path == 'params' and node['kind'] in model.collections.KINDS,
                     connections=node['kind'] == 'text_template' and path == 'params.template', select=node_id)

    def _edge_changed(self, edge_id, key, value):
        edge = next((edge for edge in self.document['edges'] if edge['id'] == edge_id), None)
        if edge and edge.get(key) != value:
            self._checkpoint((edge_id, key))
            edge[key] = value
            self._mark_stale(edge['target'])
            self._edited()
            self.scene.edges[edge_id].update_path()

    def _open_settings(self, item_id=None):
        item = self.scene.nodes.get(item_id) or self.scene.edges.get(item_id)
        if item is not None and not item.isSelected():
            with QtCore.QSignalBlocker(self.scene):
                self.scene.clearSelection()
                item.setSelected(True)
        if not self.scene.selectedItems():
            self._message('请先选择节点或连线，再打开设置。')
            return
        self._selection_changed(force=True)

    def _model_settings(self, node_id):
        item = self.scene.nodes.get(node_id)
        if item is None or item.node['kind'] not in model.MODEL_KINDS:return
        # The explicit per-node entry must also work while several nodes are selected.
        with QtCore.QSignalBlocker(self.scene):
            self.scene.clearSelection();item.setSelected(True)
        self._open_settings(node_id)
        inspector = self._inspector
        if isinstance(inspector, Inspector) and getattr(inspector, 'model_fields', None):
            inspector.tabs.setCurrentIndex(1)
            key = 'model' if item.node.get('model_config', {}).get('provider') else 'provider'
            inspector.model_fields[key].setFocus(QtCore.Qt.OtherFocusReason)

    def _close_settings(self):
        if isinstance(self._inspector, Inspector) and not self._inspector.validate():
            return
        with QtCore.QSignalBlocker(self.scene):self.scene.clearSelection()
        self._selection_identity = None
        self._empty_inspector()
        self.view.setFocus()

    def _other_settings(self,node_id):
        item=self.scene.nodes.get(node_id)
        if item:
            if not item.isSelected():
                with QtCore.QSignalBlocker(self.scene):self.scene.clearSelection();item.setSelected(True)
            self._open_settings(node_id)
            if self._inspector:self._inspector.focus_other_settings()

    def _batch_node_changed(self,node_ids,path,value):
        from .selection import options
        ids=set(node_ids);nodes=[n for n in self.document['nodes'] if n['id'] in ids]
        eligible=set(next((o['ids'] for o in options(nodes) if o['path']==path),[]))
        keys=path.split('.',1);changes=[]
        for node in nodes:
            if node['id'] not in eligible:continue
            container=node.setdefault(keys[0],{}) if len(keys)>1 else node
            if bool(container.get(keys[-1],False))!=bool(value):changes.append((node,container))
        if not changes:return
        self._checkpoint()
        for node,container in changes:container[keys[-1]]=bool(value)
        self._mark_stale_many([node['id'] for node,unused in changes]);self._edited()
        self._selection_changed(force=self._inspector is not None)
        self._message(f'已更新 {len(changes)} 个节点，可一次撤销。')

    def _queue_selection_changed(self):
        if not self._updating and not self._closed:self._selection_timer.start(0)

    def _selection_changed(self, force=False):
        if self._closed:
            return
        if self._updating:
            return
        if not force and self.view._rubber_selecting:return
        self._selection_timer.stop()
        selected = self.scene.selectedItems()
        nodes=sorted([item for item in selected if isinstance(item,NodeItem)],key=lambda item:item.node['id'])
        node = next((item for item in selected if isinstance(item, NodeItem)), None)
        edge = next((item for item in selected if isinstance(item, EdgeItem)), None)
        identity = ('batch',tuple(item.node['id'] for item in nodes)) if len(nodes)>1 else ('node', node.node['id']) if node else ('edge', edge.edge['id']) if edge else None
        if not force and nodes:
            if identity != self._selection_identity:
                self._empty_inspector()
                self._selection_identity = identity
            return
        if identity == self._selection_identity and not force:
            return
        self._selection_identity = identity
        self._last_edit_path = None
        if len(nodes)>1:
            from .selection import BatchInspector
            inspector=BatchInspector([item.node for item in nodes], parent=self.inspector_scroll.viewport());inspector.changed.connect(self._batch_node_changed)
        elif node:
            definition = self.apps.get(str(node.node.get('app', {}).get('webapp_id', '')))
            is_app = node.node['kind'] == 'app'
            inspector = Inspector(node.node, self.document['id'], self.document['edges'], self.histories,
                                  parent=self.inspector_scroll.viewport(), model_owner=self.owner,
                                  missing_app=is_app and definition is None,
                                  changed_definition=is_app and definition is not None and _schema(definition['nodes']) != _schema(node.node.get('app', {}).get('nodes', [])))
            inspector.changed.connect(lambda path, value, node_id=node.node['id']: self._node_changed(node_id, path, value))
            inspector.rebind_requested.connect(self._rebind_app)
            inspector.password_requested.connect(self._provide_password)
            inspector.install_requested.connect(self._install_missing_apps)
            if inspector.install_button is not None:
                inspector.install_button.setEnabled(not self._install_busy)
            if is_app:
                open_app = QtWidgets.QPushButton('查看 App 输出卡片')
                open_app.clicked.connect(lambda unused=False, app_id=str(node.node.get('app', {}).get('webapp_id', '')): self._open_app(app_id))
                inspector.tab_forms[-1].insertWidget(0, open_app)
            previous = self._inspector
            if (isinstance(previous, Inspector) and previous.tabs is not None
                    and inspector.tabs is not None and previous.node['id'] == node.node['id']):
                inspector.tabs.setCurrentIndex(previous.tabs.currentIndex())
        elif edge:
            inspector = EdgeInspector(edge.edge, parent=self.inspector_scroll.viewport())
            inspector.changed.connect(lambda key, value, edge_id=edge.edge['id']: self._edge_changed(edge_id, key, value))
        else:
            self._empty_inspector()
            return
        inspector.message.connect(self._message)
        # Wrapped help and long provider/path values must not determine the
        # width of a docked or floating settings panel.
        for label in inspector.findChildren(QtWidgets.QLabel):
            if label.wordWrap():
                policy = label.sizePolicy();policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
                label.setSizePolicy(policy);label.setMinimumWidth(0)
        for control in inspector.findChildren(QtWidgets.QComboBox):
            control.setMinimumWidth(0);control.setMinimumContentsLength(8)
            control.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        close_button = QtWidgets.QToolButton(inspector)
        close_button.setText('×')
        close_button.setObjectName('canvasCloseInspector')
        close_button.setFixedSize(28, 28)
        close_button.setToolTip('收起完整设置；双击节点标题或右键“节点设置”再次打开')
        close_button.setCursor(QtCore.Qt.PointingHandCursor)
        close_button.clicked.connect(self._close_settings)
        header = QtWidgets.QHBoxLayout()
        title_item = inspector.layout().takeAt(0)
        header.addWidget(title_item.widget(), 1)
        header.addWidget(close_button, 0, QtCore.Qt.AlignTop)
        inspector.layout().insertLayout(0, header)
        old = self.inspector_scroll.widget()
        if old:
            old.hide()
            self.inspector_scroll.takeWidget()
            old.deleteLater()
        self._inspector = inspector
        self.inspector_scroll.setWidget(inspector)
        self._refresh_placeholder_palette()
        self._place_inspector()

    def _empty_inspector(self):
        old = self.inspector_scroll.widget()
        if old:
            old.hide()
            self.inspector_scroll.takeWidget()
            old.deleteLater()
        self._inspector = None
        self.inspector_scroll.hide()
        self._place_inspector()

    def _focus_decode(self, node_id):
        item = self.scene.nodes.get(node_id)
        if item is None:
            return
        if not item.isSelected():
            self.scene.clearSelection()
            item.setSelected(True)
        item.ensure_inline()
        if item.inline_proxy is not None:
            item.inline_proxy.show()
            editor = item.inline_proxy.widget()
            group = editor.inspector.decode_group
            if group is not None:
                if hasattr(editor.inspector, 'decode_section'):editor.inspector.decode_section.set_expanded(True)
                editor.ensureWidgetVisible(group)
                group.setFocus()

    def _edit_mask(self, node_id):
        item = self.scene.nodes.get(node_id)
        if item is None or item.node['kind'] != 'image':return
        self.scene.clearSelection();item.setSelected(True)
        item.ensure_inline()
        inspector = item.inline_proxy.widget().inspector if item.inline_proxy is not None else None
        if isinstance(inspector, Inspector):
            files = inspector.findChild(QtWidgets.QListWidget, 'canvasInputFiles')
            if files is not None:inspector._edit_input_mask(files)

    def _focus_result(self, node_id, index):
        item = self.scene.nodes.get(node_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self._open_settings(node_id)
        inspector = self._inspector
        if isinstance(inspector, Inspector) and inspector.results_list is not None:
            if inspector.tabs is not None:
                inspector.tabs.setCurrentIndex(inspector.tabs.count() - 1)
            listing = inspector.results_list
            inspector.result_browser.focus_index(index, 'input' if item.input_preview() else 'results')
            if inspector.tabs is None:
                self.inspector_scroll.ensureWidgetVisible(listing)

    def _open_app(self, app_id):
        button = (getattr(self.owner, '_rh_app_buttons', {}) or {}).get(app_id)
        if button is not None:
            button.click()
        else:
            self._message('请先在 RH App 页面添加此应用。')

    def _provide_password(self, node_id):
        node = next((node for node in self.document['nodes'] if node['id'] == node_id), None)
        if not node:
            return
        password = str(node.get('decode_settings', {}).get('password') or '')
        if not password:
            self._message('请先填写解码密码。')
            return
        try:
            count = self.engine.provide_password(self.document['id'], node_id, password)
            self._message(f'已为 {count} 个任务补充密码，将继续本地解码。' if count else '此节点没有正在等待解码密码的任务。')
        except (OSError, ValueError, RuntimeError) as error:
            self._message(f'补充密码失败：{error}')

    def _rebind_app(self, node_id):
        node = next(node for node in self.document['nodes'] if node['id'] == node_id)
        installed = self.apps.get(str(node.get('app', {}).get('webapp_id', '')))
        if installed is None:
            return
        previous = {model.parameter_key(field): field for field in node.get('app', {}).get('nodes', [])}
        current = {model.parameter_key(field): field for field in installed['nodes']}
        changed = {key for key in previous if key not in current or _schema([previous[key]]) != _schema([current[key]])}
        note = '相同定义的参数保留当前值，新增或变更参数使用 App 当前默认值。受影响的连线将断开，可通过撤销恢复。'
        if QtWidgets.QMessageBox.question(self, '重新绑定 App 参数', note, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        self._checkpoint()
        node['app'] = copy.deepcopy(installed)
        node['params'] = {key: copy.deepcopy(node['params'].get(key, field.get('fieldValue', ''))) if key not in changed else copy.deepcopy(field.get('fieldValue', '')) for key, field in current.items()}
        self.document['edges'] = [edge for edge in self.document['edges'] if not (edge['target'] == node_id and edge['input'] in changed)]
        self._mark_stale(node_id)
        self._edited(rebuild=True, select=node_id)

    def _action(self, action):
        if self._preference_action(action):return
        method = {'copy': self.copy_nodes, 'paste': self.paste_nodes, 'delete': self.delete_selected, 'clear': self.clear_canvas, 'undo': self.undo, 'redo': self.redo,'select_all':self.select_all_nodes}.get(action)
        if method:
            method()

    def select_all_nodes(self):
        with QtCore.QSignalBlocker(self.scene):
            self.scene.clearSelection()
            for item in self.scene.nodes.values():item.setSelected(True)
        self._selection_changed()

    def delete_canvas(self):
        canvas_id=self.document['id'];name=self.document.get('name','未命名画布')
        reply=QtWidgets.QMessageBox.question(self,'删除画布',f'删除“{name}”及其快照、运行临时文件？\n该画布排队和运行中的任务会取消；输入素材和正式输出文件保留。',QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,QtWidgets.QMessageBox.No)
        if reply!=QtWidgets.QMessageBox.Yes:return
        try:
            self.workflow_queue.cancel_canvas(canvas_id)
            self._edit_group_timer.stop();self.store.delete(canvas_id);self._mark_workflow_deleted(canvas_id)
            self._set_document(model.new_document())
            self._message('画布已删除；仍被任务读取的临时文件会在释放后清理。')
        except (OSError,ValueError,RuntimeError) as error:self._message('删除画布失败：'+str(error))

    def clear_canvas(self):
        """Clear the editable graph as one undo step; active runs keep their snapshot."""
        if not self.document['nodes'] and not self.document['edges']:return
        if not self._flush_editors():return
        self._checkpoint()
        count = len(self.document['nodes'])
        for node in self.document['nodes']:
            self._removed_runtime[node['id']] = {key: copy.deepcopy(node[key]) for key in RUNTIME_FIELDS if key in node}
        self.document['nodes'] = []
        self.document['edges'] = []
        self._edited(rebuild=True)
        self._message(f'已清空 {count} 个节点及全部连线，可按 Ctrl+Z 撤销。已发起的任务仍按运行快照执行。')

    def copy_nodes(self):
        ids = {item.node['id'] for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        from .subgraphs import members
        ids=members(self.document,ids)
        if ids:
            self._clipboard = copy.deepcopy({'nodes': [node for node in self.document['nodes'] if node['id'] in ids],
                                             'edges': [edge for edge in self.document['edges'] if edge['source'] in ids and edge['target'] in ids]})
            for node in self._clipboard['nodes']:
                for key in model.RUNTIME_FIELDS:node.pop(key, None)
            self._message(f'已复制 {len(ids)} 个节点。')

    def paste_nodes(self):
        if not self._clipboard:
            return
        self._checkpoint()
        graph = copy.deepcopy(self._clipboard)
        ids = {node['id']: uuid.uuid4().hex for node in graph['nodes']}
        for node in graph['nodes']:
            if node['kind']=='subgraph':node['members']=[ids[key] for key in node.get('members',[]) if key in ids]
            node['id'] = ids[node['id']]
            node['x'] += 40
            node['y'] += 40
            for key in model.RUNTIME_FIELDS:node.pop(key, None)
            node['results'], node['fingerprint'], node['status'] = [], '', 'IDLE'
        for edge in graph['edges']:
            edge.update(id=uuid.uuid4().hex, source=ids[edge['source']], target=ids[edge['target']])
        self.document['nodes'].extend(graph['nodes'])
        self.document['edges'].extend(graph['edges'])
        self._edited(rebuild=True)
        for node in graph['nodes']:
            self.scene.nodes[node['id']].setSelected(True)
        self.view.reveal_nodes(ids.values())

    def delete_selected(self):
        node_ids = {item.node['id'] for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        from .subgraphs import members
        node_ids=members(self.document,node_ids)
        edge_ids = {item.edge['id'] for item in self.scene.selectedItems() if isinstance(item, EdgeItem)}
        if not node_ids and not edge_ids:
            return
        self._checkpoint()
        for node in self.document['nodes']:
            if node['id'] in node_ids:
                self._removed_runtime[node['id']] = {key: copy.deepcopy(node[key]) for key in RUNTIME_FIELDS if key in node}
        for edge in self.document['edges']:
            if edge['id'] in edge_ids or edge['source'] in node_ids:
                self._mark_stale(edge['target'])
        self.document['nodes'] = [node for node in self.document['nodes'] if node['id'] not in node_ids]
        for group in self.document['nodes']:
            if group['kind']=='subgraph':group['members']=[key for key in group.get('members',[]) if key not in node_ids]
        self.document['edges'] = [edge for edge in self.document['edges'] if edge['id'] not in edge_ids and edge['source'] not in node_ids and edge['target'] not in node_ids]
        self._edited(rebuild=True)

    def _restore_edit(self, restored):
        self._last_edit_path = None
        self._edit_group_timer.stop()
        selected={item.node['id'] for item in self.scene.selectedItems() if isinstance(item,NodeItem)}
        live_nodes={node['id']:node for node in self.document['nodes']}
        restored_ids = {node['id'] for node in restored['nodes']}
        for key, node in live_nodes.items():
            if key not in restored_ids:
                self._removed_runtime[key] = {field: copy.deepcopy(node[field]) for field in RUNTIME_FIELDS if field in node}
        runtime = dict(self._removed_runtime)
        runtime.update(live_nodes)
        restored['run'] = copy.deepcopy(self.document.get('run', {}))
        for node in restored['nodes']:
            live=live_nodes.get(node['id'])
            # Revalidate against current hints; Undo must not replay old errors.
            for key in INPUT_HINT_FIELDS:
                node.pop(key, None)
                if live and key in live:
                    node[key] = copy.deepcopy(live[key])
            if node['kind'] in ('text', 'note', 'text_split') and live:
                node.setdefault('params',{})['text']=live.get('params',{}).get('text','')
            if node['id'] in runtime:
                for key in RUNTIME_FIELDS:
                    if key in runtime[node['id']]:
                        node[key] = copy.deepcopy(runtime[node['id']][key])
            # A deleted node's accepted task can finish before Undo restores it.
            # Preserve the latest durable result, not the status at deletion time.
            state = restored['run'].get('nodes', {}).get(node['id'], {})
            for key in RUNTIME_FIELDS:
                if key in state:
                    node[key] = copy.deepcopy(state[key])
        ignored = set(RUNTIME_FIELDS + INPUT_HINT_FIELDS) | {'input_keys'}
        def settings(node):return {key: value for key, value in node.items() if key not in ignored}
        connections_only = (set(live_nodes) == {node['id'] for node in restored['nodes']}
                            and all(settings(node) == settings(live_nodes[node['id']]) for node in restored['nodes']))
        self.document = restored
        self.name_edit.setText(restored['name'])
        self._edited(rebuild=not connections_only, connections=connections_only)
        with QtCore.QSignalBlocker(self.scene):
            for node_id in selected:
                if node_id in self.scene.nodes:self.scene.nodes[node_id].setSelected(True)
        self._selection_changed()

    def undo(self):
        if self._undo:
            self._redo.append(self._edit_snapshot())
            self._restore_edit(self._undo.pop())
            self._trim_history()
            self._prune_removed_runtime()

    def redo(self):
        if self._redo:
            self._undo.append(self._edit_snapshot())
            self._restore_edit(self._redo.pop())
            self._trim_history()
            self._prune_removed_runtime()

    def _drop_files(self, paths, position):
        from .text_files import TEXT_SUFFIXES
        groups = {}
        folders = [path for path in paths if os.path.isdir(path)]
        if folders:
            label, accepted = QtWidgets.QInputDialog.getItem(self, '导入文件夹', '读取文件类型', ['图像', '视频', '音频', '文本'], 0, False)
            if accepted:groups[{'图像': 'image', '视频': 'video', '音频': 'audio', '文本': 'text_file'}[label]] = folders
        for path in paths:
            if not os.path.isfile(path):
                continue
            extension = Path(path).suffix.lower()
            kind = next((kind for kind, suffixes in model.MEDIA_SUFFIXES.items() if extension in suffixes), None)
            if kind is None and extension in TEXT_SUFFIXES:
                kind = 'text_file'
            if kind is None:continue
            groups.setdefault(kind, []).append(path)
        if not groups:
            self._message('请拖入支持的图像、视频、音频、文本文件或素材文件夹。')
            return
        self._checkpoint()
        created, offset = [], 0
        for index, (kind, files) in enumerate(groups.items()):
            node = model.new_node(kind, params={'files': list(dict.fromkeys(files))}, x=position.x() + offset, y=position.y())
            node['size'] = self.view.initial_node_size(node)
            offset += node['size'][0] + 48
            created.append(node['id'])
            self.document['nodes'].append(node)
        self._edited(rebuild=True, select=node['id'])
        self.view.reveal_nodes(created)

    def _view_changed(self):
        zoom = self.view.transform().m11()
        self.zoom_label.setText(f'{int(zoom * 100)}%')
        if self._updating:
            return
        self.document['view'] = self.view.view_state()
        if self.document['nodes']:
            self._edited()

    def save(self, *unused, automatic=False, validation_scope=None):
        if not automatic and not self._flush_editors(validation_scope):return False
        self._edit_group_timer.stop()
        canvas_id = self.document['id']
        if self._is_deleted(canvas_id):
            self._mark_workflow_deleted(canvas_id)
            if automatic:
                return True
        try:
            self.save_state.setText('正在保存…')
            self.document['view'] = self.view.view_state()
            model.normalize_app_urls(self.document)
            self.engine.save_document(self.document, explicit=not automatic)
            current = self.engine.view_document(canvas_id)
            if current is not None:
                self._last_runtime_revision = current.pop('_view_revision')
                self.document = current
                self.scene.refresh_nodes(self.document)
            self._persisted_ids.add(canvas_id)
            self._deleted_ids.discard(canvas_id)
            self._watch_workflow_directory()
            self._dirty = False
            self._session_edits.pop(canvas_id, None)
            self.store.set_active(canvas_id)
            self._refresh_missing_apps()
            self.save_state.setText('已保存 · ' + QtCore.QTime.currentTime().toString('HH:mm:ss'))
            self.save_state.setToolTip(str(self.store.path_for(canvas_id)))
            return True
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            if automatic and self._is_deleted(canvas_id):
                self._mark_workflow_deleted(canvas_id)
                return True
            self.save_state.setText('保存失败')
            self.save_state.setToolTip(str(error))
            self._message(f'保存失败：{error}')
            return False

    def new_canvas(self):
        if not self._save_before_switch():
            return
        self._set_document(model.new_document())

    def open_canvas(self):
        if self._package_busy:
            self._message('正在读取或保存工作流，请等待完成。');return
        if not self._save_before_switch():
            return
        from .workflow_library import WorkflowLibrary
        dialog = WorkflowLibrary(self)
        try:
            if dialog.exec_() == QtWidgets.QDialog.Accepted:self._open_path(dialog.selected_path)
        finally:dialog.deleteLater()

    def _open_path(self, path):
        if not path or self._package_busy:return
        path = Path(path).resolve()
        if not self._save_before_switch():return
        if path.parent == self.store.root and path.stem in self._session_edits:
            try:
                draft = self.engine.view_document(path.stem)
            except (KeyError, RuntimeError):
                # An edit undone back to its saved baseline may be evicted by
                # the idle queue. Fall back to that saved file, not stale metadata.
                self._session_edits.pop(path.stem, None)
            else:
                self._set_document(draft)
                return
        self._start_package('open', lambda: self.store.load(path) if path.parent == self.store.root else self.store.import_workflow(path))

    def save_as(self):
        if self._package_busy:
            self._message('正在处理工作流，请等待完成。')
            return
        if not self._flush_editors():return
        name, accepted = QtWidgets.QInputDialog.getText(self, '保存画布副本', '副本名称（仅复制节点设置与连线，运行快照独立）', text=self.document['name'] + ' 副本')
        if accepted and name.strip():
            snapshot = copy.deepcopy(self.document)
            # A copy can rescue edits even if the original file has a conflict.
            self._start_package('copy', lambda: self.store.duplicate_workflow(snapshot, name))

    def import_canvas(self):
        if self._package_busy:
            self._message('正在处理工作流，请等待完成。')
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '导入工作流 JSON', '', 'AetherLoom 工作流 (*.json)')
        if not path:
            return
        if not self._save_before_switch():
            return
        self._start_package('import', lambda: self.store.import_workflow(path))

    def export_canvas(self):
        if self._package_busy:
            self._message('正在处理工作流，请等待完成。')
            return
        if not self._flush_editors():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出工作流 JSON', self.document['name'] + '.aetherloom.json', 'AetherLoom 工作流 (*.json)')
        if not path:
            return
        if not Path(path).suffix:path += '.json'
        snapshot = copy.deepcopy(self.document)
        self._start_package('export', lambda: self.store.export_workflow(snapshot, path))

    def _start_package(self, kind, operation):
        self._package_busy = True
        self._package_origin = (self.document['id'], getattr(self, '_document_epoch', 0))
        self._package_edit_serial = getattr(self, '_edit_serial', 0)
        self._message('正在读取工作流…' if kind in ('import', 'open') else '正在保存工作流…')
        # A single package operation per page; no Qt widgets enter the worker.
        QtCore.QThreadPool.globalInstance().start(_PackageJob(kind, operation, self._package_signals))

    @QtCore.pyqtSlot(str, object, str)
    def _package_finished(self, kind, result, error):
        self._package_busy = False
        if self._closed:
            return
        if error:
            self._message(('打开失败：' if kind in ('import', 'open') else '保存失败：') + error)
        elif kind in ('import', 'open', 'copy'):
            if self._package_origin != (self.document['id'], getattr(self, '_document_epoch', 0)):
                self._message('工作流处理完成，可从“打开”列表选择；已保留当前画布。');return
            if not self._flush_editors():
                self._message('工作流读取完成；请先修正当前输入，再打开画布。');return
            if kind == 'open' and result['id'] == self.document['id'] and self._package_edit_serial != getattr(self, '_edit_serial', 0):
                self._message('读取期间当前画布已有新编辑，已保留编辑内容。');return
            if kind == 'open' and not self.store.workflow_matches(result):
                self._message('画布文件在读取期间被修改，请重新打开。');return
            # The user may continue editing while the workflow is being read.
            if kind != 'copy' and not self._save_before_switch():
                self._message('工作流已导入到本地，请先保存当前编辑，再从“打开”选择它。')
                return
            if kind == 'copy' and self._dirty:
                self._message('副本已保存到本地，当前编辑继续保留；可从“打开”选择副本。');return
            self._set_document(result)
            missing = sum(node['kind'] == 'app' and str(node.get('app', {}).get('webapp_id', '')) not in self.apps for node in result['nodes'])
            unsupported = sum(model.unknown_node(node) for node in result['nodes'])
            skipped = sum(bool(node.get('_restored_missing_results')) for node in result['nodes'])
            self._message(('画布副本已打开。' if kind == 'copy' else '画布已打开。') + (f' {skipped} 个节点的缺失结果已跳过。' if skipped else '') + (f' {missing} 个节点缺少应用或模型，请一键补齐。' if missing else '')
                          + (f' {unsupported} 个节点类型暂不支持，已保留为占位节点。' if unsupported else ''))
        else:
            self._message('工作流 JSON 已导出，仅含节点设置与连线；不包含输入素材、运行结果或密钥。')

    def _flush_editors(self, scope=None):
        """Commit active controls before explicit saves/switches, not while typing."""
        if not self._validate_inline(scope):return False
        panel = self._inspector
        if isinstance(panel, Inspector) and scope is not None and panel.node['id'] not in scope:panel = None
        if isinstance(panel, Inspector) and not panel.validate():return False
        if panel is not None:
            for editor in panel.findChildren(QtWidgets.QLineEdit):
                if editor.isEnabled() and not editor.isReadOnly() and editor.isModified():
                    editor.editingFinished.emit();editor.setModified(False)
        self._rename()
        return True

    def _remember_session_edit(self):
        # Only lightweight catalog metadata is duplicated; the engine already
        # holds the editable document separately from its saved run baseline.
        import time
        doc = self.document
        self._session_edits[doc['id']] = dict(
            id=doc['id'], name=doc.get('name', '未命名画布'),
            path=str(self.store.path_for(doc['id'])), modified=time.time(),
            nodes=len(doc['nodes']), edges=len(doc['edges']), error='',
            snapshot=bool(doc.get('run')), session_edit=True)

    def _save_before_switch(self):
        if not self._flush_editors():return False
        if self._dirty and self._is_deleted(self.document['id']):
            self._message('当前画布文件已被删除；请先手动保存或保存副本，以保留当前编辑。');return False
        if self._dirty:
            self._remember_session_edit()
        return True

    def _record_run_texts(self, target):
        from .input_requirements import plan
        ids = plan(self.document, target)['scope']
        for node in self.document['nodes']:
            if node['id'] not in ids:
                continue
            texts = {'text': node.get('params', {}).get('text', '')} if node['kind'] == 'text' else {}
            if node['kind'] in model.MODEL_KINDS:
                texts = {key: node.get('params', {}).get(key, '') for key in ('prompt', 'system_prompt')}
            if node['kind'] == 'app':
                for field in node.get('app', {}).get('nodes', []):
                    if model.field_type(field) == 'text':
                        key = model.parameter_key(field)
                        texts[key] = node.get('params', {}).get(key, field.get('fieldValue', ''))
            for key, value in texts.items():
                identity = (self.document['id'], node['id'], key)
                entries = self.histories.setdefault(identity, [])
                active = next((editor._prompt_history for editor in self.findChildren(QtWidgets.QTextEdit)
                               if hasattr(editor, '_prompt_history') and editor._prompt_history.entries is entries), None)
                if active:
                    active.record_run()
                else:
                    entries.append(TextSnapshot(str(value), 'run'))

    def _validate_inline(self, scope=None):
        for item in self.scene.nodes.values():
            if scope is not None and item.node['id'] not in scope:continue
            if item.inline_proxy is not None:
                editor = item.inline_proxy.widget()
                if hasattr(editor, 'validate') and not editor.validate():
                    self.view.ensureVisible(item)
                    self._message('请先修正节点中未完成或无效的数值。')
                    return False
        return True

    def _commit_valid_text_drafts(self, scope=None):
        # InlineText and media editors do not wrap an Inspector. Search the
        # actual widget so every inline editor can participate safely.
        panels = [item.inline_proxy.widget() for key, item in self.scene.nodes.items()
                  if (scope is None or key in scope) and item.inline_proxy is not None]
        if isinstance(self._inspector, Inspector) and (scope is None or self._inspector.node['id'] in scope):
            panels.append(self._inspector)
        for panel in panels:
            for editor in panel.findChildren(QtWidgets.QLineEdit):
                if editor.isEnabled() and not editor.isReadOnly() and editor.isModified() and editor.hasAcceptableInput():
                    editor.editingFinished.emit()
                    editor.setModified(False)

    def run_canvas(self, target=None, force=False):
        from .input_requirements import plan, refresh
        try:
            requested = model.execution_scope(self.document, target)
            # Commit valid text drafts before deciding whether local input is
            # missing, without validating unrelated or pruned numeric editors.
            self._commit_valid_text_drafts(requested)
            input_plan = plan(self.document, target)
            scope = input_plan['scope']
        except ValueError as error:
            self._message(f'无法运行：{error}');return
        refresh(self, validated=input_plan['requested'])
        if not scope:
            self.save(automatic=True)
            self._message('本次没有输入完整的执行分支；已标出缺少输入的节点，保留上次预览。')
            return
        if not self._validate_inline(scope):return
        if isinstance(self._inspector, Inspector) and self._inspector.node['id'] in scope and not self._inspector.validate():
            self._message('请先修正节点中未完成或无效的数值。')
            return
        self._rename()
        self.refresh_apps()
        try:
            self.batch_spin.interpretText()
            if not self.document['nodes']:
                raise ValueError('请先添加节点并设置输入。')
            model.validate_document(self.document)
            input_plan = plan(self.document, target)
            scope = input_plan['scope']
            self._record_run_texts(target)
            if not self.save(validation_scope=scope):
                return
            batches = 1 if target else self.document.get('batch_count', 1)
            self.workflow_queue.enqueue(self.document, target=target, force=force, batch_count=batches,
                                        prepare_app=self._prepare_node, document_saved=True)
            self._sync_actions()
            self._message(f'已加入工作流队列，共 {batches} 批、{len(scope)} 个节点。'
                          + (f' {len(input_plan["issues"])} 个节点缺少输入，对应分支本次跳过，旧预览保留。' if input_plan['issues'] else ''))
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self._message(f'无法运行：{error}')

    def stop_canvas(self):
        if not self.workflow_queue.can_cancel(self.document['id']):
            return
        try:
            errors = self.workflow_queue.cancel_canvas(self.document['id'])
            self._message('已请求取消当前画布的全部运行和排队任务，等待已提交任务确认。'
                          + (' 部分请求暂未确认，将继续重试。' if errors else ''))
        except (OSError, ValueError, RuntimeError) as error:
            self._message(f'终止任务时遇到问题：{error}')
        finally:
            self._sync_actions()

    @QtCore.pyqtSlot(dict)
    def _runtime_changed(self, update):
        if update.get('id') != self.document['id'] or self._closed or update.get('id') in self._deleted_ids:
            return
        revision = update.get('_view_revision')
        if revision is not None:
            if revision <= getattr(self, '_last_runtime_revision', -1):
                return
            self._last_runtime_revision = revision
        # Worker notifications describe the configuration at publication time.
        # A queued completion may arrive after an edit (including an upstream
        # edit); it may refresh old results, but must not validate the new draft.
        edited_nodes = model.changed_execution_nodes(self.document, update)
        self.document['run'] = copy.deepcopy(update.get('run', {}))
        nodes = {node['id']: node for node in update.get('nodes', [])}
        result_changed = False
        for node in self.document['nodes']:
            incoming = nodes.get(node['id'])
            if incoming:
                edited = node['id'] in edited_nodes
                if not edited and incoming.get('status') in ('SUCCESS', 'REUSED') and not incoming.get('stale'):
                    node.pop('_ui_stale', None)
                changed = node.get('results') != incoming.get('results')
                result_changed |= changed
                for key in RUNTIME_FIELDS:
                    if key == 'results' and not changed:continue
                    if edited and key in ('stale', '_runtime_input_issue', '_runtime_missing_ports'):
                        continue
                    if key in incoming:
                        node[key] = copy.deepcopy(incoming[key])
                if edited:
                    node.update(stale=True, _ui_stale=True,
                                _runtime_input_issue='', _runtime_missing_ports=[])
                if changed:
                    available, signatures, missing = model.available_results(node.get('results', []), node.get('result_signatures'))
                    node['results'] = available
                    if signatures is not None:
                        node['result_signatures'] = signatures
                    if missing:
                        node['_restored_missing_results'] = True
        self.scene.refresh_nodes(self.document)
        self._sync_actions()
        if isinstance(self._inspector, Inspector) and self._selection_identity:
            selected = next((node for node in self.document['nodes'] if node['id'] == self._selection_identity[1]), None)
            if selected:
                # Runtime updates must not destroy in-progress numeric/text drafts.
                self._inspector.update_results(selected.get('results', []), node=selected)
        run = self.document.get('run', {})
        if run:
            status = STATUS_NAMES.get(run.get('status', ''), run.get('status', ''))
            states = list(run.get('nodes', {}).values())
            completed = sum(node.get('status') in ('SUCCESS', 'REUSED', 'SKIPPED') for node in states)
            skipped = sum(node.get('status') == 'SKIPPED' for node in states)
            batches = run.get('batch_count', 1)
            batch_index = run.get('batch_index', 0) + 1
            self.status_label.setText(f'第 {batch_index}/{batches} 批 · {completed} / {len(states)} 节点已处理'
                                      + (f' · {len(run["input_issues"])} 个缺少输入的节点及其分支未运行' if run.get('input_issues') else '')
                                      + (f' · {skipped} 个已跳过' if skipped else '') + (f' · {status}' if status else ''))

    def _batch_count_changed(self, value):
        if self._updating:
            return
        if self.document.get('batch_count', 1) == value:
            return
        self._checkpoint('batch_count')
        self.document['batch_count'] = int(value)
        self._edited()

    def _sync_actions(self):
        running = self.engine.is_running(self.document['id'])
        busy = self.workflow_queue.is_busy(self.document['id'])
        canceling = self.workflow_queue.is_canceling(self.document['id'])
        can_cancel = self.workflow_queue.can_cancel(self.document['id'])
        self.run_action.setEnabled(True)
        run = self.document.get('run') or {}
        self.batch_spin.setEnabled(True)
        batch_count = self.document.get('batch_count', 1)
        if self.batch_spin.value() != batch_count:
            blocker = QtCore.QSignalBlocker(self.batch_spin)
            self.batch_spin.setValue(batch_count)
            del blocker
        self.run_action.setText('加入队列' if busy or running else '运行画布')
        self.run_action.setToolTip(f'按输出节点及其所需上游执行，共 {batch_count} 批；独立 App / API 也属于输出节点。')
        self.stop_action.setText('取消中…' if canceling and not can_cancel else '全部终止')
        self.stop_action.setEnabled(can_cancel)
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))
        self.clear_action.setEnabled(bool(self.document['nodes'] or self.document['edges']))
        self._place_inspector()

    def _message(self, text):
        self.status_label.setText(str(text))

    def _place_inspector(self):
        self.page_subtitle.setVisible(self.height() >= 560)
        self._run_layout.setDirection(QtWidgets.QBoxLayout.TopToBottom if self.center.width() < 370
                                      else QtWidgets.QBoxLayout.LeftToRight)
        self.run_panel.adjustSize()
        self.run_panel.move(max(8, self.center.width() - self.run_panel.width() - 12),
                            max(8, self.center.height() - self.run_panel.height() - 12))
        mode = 'compact' if self.width() < 760 else 'overlay' if self.width() < 1080 else 'wide'
        previous = self._responsive_mode
        if mode != previous:
            self._responsive_mode = mode
            if mode != 'wide' and previous in (None, 'wide'):
                self._wide_library_visible = self.palette_action.isChecked()
            if mode == 'compact':
                self.palette_action.setChecked(False)
            elif previous == 'compact':
                self.palette_action.setChecked(self._wide_library_visible)
            if mode == 'wide' and previous not in (None, 'wide'):
                self.palette_action.setChecked(self._wide_library_visible)
        narrow = mode != 'wide'
        if narrow:
            if self.inspector_scroll.parent() is self.splitter:
                self.inspector_scroll.setParent(self.center)
            width = min(315, max(230, self.center.width() - 36))
            self.inspector_scroll.setGeometry(max(0, self.center.width() - width - 10), 10, width,
                                               max(100, self.center.height() - self.run_panel.height() - 38))
            self.inspector_scroll.raise_()
        elif self.inspector_scroll.parent() is not self.splitter:
            self.splitter.addWidget(self.inspector_scroll)
            self.splitter.setSizes([210, max(400, self.width() - 550), 300])
        self.inspector_scroll.setVisible(self._inspector is not None)
        self.view.overlay_exclusion = self.inspector_scroll.width() + 20 if narrow and self.inspector_scroll.isVisible() else 0
        self.view.bottom_exclusion = self.run_panel.height() + 12
        self.view.schedule_adapt()
        self.run_panel.raise_()

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'center', None) and event.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(0, self._place_inspector)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_inspector()

    def showEvent(self, event):
        super().showEvent(event)
        self.engine.set_view_canvas(self.document['id'])
        try:
            current = self.engine.view_document(self.document['id'])
        except (KeyError, RuntimeError):
            current = None
        if current is not None:
            self._runtime_changed(current)
        self.refresh_apps()
        self.refresh_theme()
        QtCore.QTimer.singleShot(0, self._place_inspector)

    def hideEvent(self, event):
        if getattr(self.engine, '_view_canvas', None) == self.document['id']:
            self.engine.set_view_canvas('')
        super().hideEvent(event)

    def refresh_theme(self):
        mode = getattr(self.owner, '_theme_mode', 'dark')
        from .appearance import canvas_palette
        p = canvas_palette(palette(mode))
        arrow_root = Path(resource_path('icons'))
        up_arrow = (arrow_root / f'ui-chevron-up-{mode}.svg').as_posix()
        down_arrow = (arrow_root / f'ui-chevron-down-{mode}.svg').as_posix()
        check_icon = (arrow_root / 'ui-check.svg').as_posix()
        self.scene.colors = p
        self.scene.refresh_ports()
        self.scene.update()
        theme_palette = QtGui.QPalette(self.palette())
        for role, color in ((QtGui.QPalette.Window, p['canvas']), (QtGui.QPalette.WindowText, p['text']),
                            (QtGui.QPalette.Base, p['input']), (QtGui.QPalette.Text, p['text']),
                            (QtGui.QPalette.ButtonText, p['text']), (QtGui.QPalette.PlaceholderText, p['muted'])):
            theme_palette.setColor(role, QtGui.QColor(color))
        theme_palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(p['muted']))
        self.setPalette(theme_palette)
        self.setStyleSheet(f'''
            QWidget#aetherloomCanvasPage {{ background: {p['canvas']}; color: {p['text']}; }}
            QWidget#aetherloomCanvasPage QWidget {{ color: {p['text']}; font-family: 'Microsoft YaHei UI'; font-size: 13px; }}
            QWidget#aetherloomCanvasPage QLabel {{ background: transparent; border: none; }}
            QWidget#aetherloomCanvasPage QLabel#canvasPageTitle {{ font-size: 23px; font-weight: 700; padding-right: 12px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasSectionTitle {{ font-size: 14px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QScrollArea#canvasInspector QWidget {{ font-size: 12px; }}
            QWidget#aetherloomCanvasPage QScrollArea#canvasInspector QLabel#canvasSectionTitle {{ font-size: 14px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasCollectionRoute {{ color: {p['accent']}; font-weight: 600; padding: 8px; background: {p['accent_soft']}; border-radius: 6px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasCollectionExample {{ color: {p['muted']}; padding: 9px; background: {p['input']}; border-radius: 6px; }}
            QWidget#canvasBatchInspector QCheckBox {{ background: transparent; padding: 8px 4px; spacing: 8px; border: none; border-radius: 6px; }}
            QWidget#canvasBatchInspector QCheckBox:hover {{ background: {p['hover']}; }}
            QWidget#canvasBatchInspector QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {p['muted']}; border-radius: 4px; background: {p['input']}; image: none; }}
            QWidget#canvasBatchInspector QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; image: url("{check_icon}"); }}
            QWidget#canvasBatchInspector QCheckBox::indicator:indeterminate {{ background: {p['accent_soft']}; border: 4px solid {p['accent']}; width: 10px; height: 10px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasMuted {{ color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QLabel#canvasBuiltinReuseHint {{ color: {p['muted']}; font-size: 11px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasWarning {{ color: {p['warning']}; }}
            QFrame#canvasMissingApps {{ background: {p['surface']}; border: 1px solid {p['warning']}; border-radius: 8px; }}
            QFrame#canvasPanel, QScrollArea#canvasInspector {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; }}
            QScrollArea#canvasInspector > QWidget > QWidget {{ background: {p['surface']}; }}
            QFrame#canvasSurface {{ border: 1px solid {p['border']}; border-radius: 10px; }}
            QWidget#aetherloomCanvasPage QListWidget {{ background: transparent; border: none; outline: none; }}
            QWidget#aetherloomCanvasPage QListWidget::item {{ border-radius: 8px; padding: 8px 6px; }}
            QWidget#aetherloomCanvasPage QListWidget::item:hover {{ background: {p['hover']}; }}
            QWidget#aetherloomCanvasPage QListWidget::item:selected {{ background: {p['accent_soft']}; color: {p['accent']}; }}
            QWidget#aetherloomCanvasPage QLineEdit, QWidget#aetherloomCanvasPage QTextEdit,
            QWidget#aetherloomCanvasPage QPlainTextEdit, QWidget#aetherloomCanvasPage QAbstractSpinBox,
            QWidget#aetherloomCanvasPage QComboBox {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 7px; selection-background-color: {p['accent']}; }}
            QWidget#aetherloomCanvasPage QLineEdit:focus, QWidget#aetherloomCanvasPage QTextEdit:focus,
            QWidget#aetherloomCanvasPage QComboBox:focus {{ border-color: {p['accent']}; }}
            QWidget#aetherloomCanvasPage QAbstractSpinBox QLineEdit {{ border: none; background: transparent; padding: 0; }}
            QWidget#aetherloomCanvasPage QSpinBox#canvasBatchCount {{ padding: 3px 5px; }}
            QWidget#aetherloomCanvasPage QSpinBox#canvasBatchCount:focus {{ border-color: {p['accent']}; }}
            QSpinBox#canvasBatchCount::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
                width: 23px; height: 17px; border: none; border-left: 1px solid {p['border']}; background: transparent; }}
            QSpinBox#canvasBatchCount::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
                width: 23px; height: 17px; border: none; border-left: 1px solid {p['border']}; background: transparent; }}
            QSpinBox#canvasBatchCount::up-button:hover, QSpinBox#canvasBatchCount::down-button:hover {{ background: {p['hover']}; }}
            QSpinBox#canvasBatchCount::up-arrow {{ image: url("{up_arrow}"); width: 12px; height: 12px; }}
            QSpinBox#canvasBatchCount::down-arrow {{ image: url("{down_arrow}"); width: 12px; height: 12px; }}
            QWidget#aetherloomCanvasPage QPushButton, QWidget#aetherloomCanvasPage QToolButton {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 7px 9px; }}
            QWidget#aetherloomCanvasPage QPushButton:hover, QWidget#aetherloomCanvasPage QToolButton:hover {{ background: {p['hover']}; border-color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QToolButton:checked {{ background: {p['accent_soft']}; color: {p['accent']}; }}
            QWidget#aetherloomCanvasPage QFrame#canvasRunPanel {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasRunButton {{ background: {p['accent']}; color: #ffffff; border-color: {p['accent']}; padding: 7px 14px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasRunButton:disabled {{ background: {p['hover']}; color: {p['muted']}; border-color: {p['border']}; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasStopButton {{ padding: 7px 10px; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasStopButton:enabled {{ color: {p['danger']}; border-color: {p['danger']}; }}
            QWidget#aetherloomCanvasPage QPushButton:disabled, QWidget#aetherloomCanvasPage QToolButton:disabled {{ color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QToolBar {{ border: 1px solid {p['border']}; border-radius: 10px; background: {p['surface']}; spacing: 5px; padding: 5px; }}
            QWidget#aetherloomCanvasPage QToolBar QToolButton {{ background: transparent; border-color: transparent; }}
            QWidget#aetherloomCanvasPage QToolBar QToolButton:hover {{ background: {p['hover']}; }}
            QWidget#aetherloomCanvasPage QWidget#canvasToolbarSpacer {{ background: transparent; border: none; }}
            QWidget#aetherloomCanvasPage QToolBar QToolButton:checked {{ background: {p['accent_soft']}; color: {p['accent']}; }}
            QWidget#aetherloomCanvasPage QToolBar QToolButton#canvasAddNodeButton {{ background: {p['accent_soft']}; color: {p['accent']}; padding: 7px 13px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QToolButton#qt_toolbar_ext_button {{ min-width: 24px; min-height: 28px; padding: 2px; }}
            QWidget#aetherloomCanvasPage QGroupBox {{ border: 1px solid {p['border']}; border-radius: 7px; margin-top: 10px; padding-top: 12px; }}
            QWidget#aetherloomCanvasPage QGroupBox::title {{ subcontrol-origin: margin; left: 9px; padding: 0 4px; }}
            QTabWidget#canvasNodeSettingsTabs::pane {{ border: none; }}
            QScrollArea#canvasNodeTabScroll, QWidget#canvasNodeTabContent {{ background: {p['surface']}; border: none; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar {{ font-size: 11px; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar::tab {{ background: transparent; color: {p['muted']};
                border: 1px solid transparent; border-radius: 6px; padding: 7px 5px; margin: 2px; font-size: 12px; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar::tab:selected {{ color: {p['accent']};
                background: {p['accent_soft']}; border-color: {p['border']}; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar::tab:hover {{ background: {p['hover']}; }}
            QWidget#aetherloomCanvasPage QLabel#canvasInspectorTitle {{ font-size: 15px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QWidget#canvasInspectorHeader {{ background: transparent; border: none; }}
            QTabWidget#canvasNodeSettingsTabs, QTabWidget#canvasNodeSettingsTabs QTabBar {{ background: transparent; border: none; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasTextHistoryButton {{ padding: 0; background: transparent; border: none; font-size: 16px; color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasTextHistoryButton:hover {{ color: {p['text']}; background: {p['hover']}; }}
            QWidget#aetherloomCanvasPage QLabel#canvasInspectorSubtitle {{ font-size: 11px; color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasCloseInspector {{ border: none; background: transparent; padding: 0; font-size: 18px; color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasCloseInspector:hover {{ background: {p['hover']}; color: {p['text']}; }}
            QWidget#aetherloomCanvasPage QLineEdit#canvasNodeName {{ background: transparent; border: none; border-bottom: 1px solid transparent; border-radius: 0; padding: 2px 0; color: {p['text']}; font-size: 15px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QLineEdit#canvasNodeName:focus {{ color: {p['text']}; border-color: {p['accent']}; }}
            QScrollArea#canvasInspector QCheckBox {{ spacing: 8px; padding: 5px 0; }}
            QScrollArea#canvasInspector QLabel#canvasMuted {{ background: {p['input']}; border-radius: 6px; padding: 8px; font-size: 11px; }}
            QScrollArea#canvasInspector QComboBox {{ padding-right: 24px; }}
            QScrollArea#canvasInspector QComboBox::drop-down {{ width: 24px; border: none; background: transparent; }}
            QScrollArea#canvasInspector QComboBox::down-arrow {{ image: url("{down_arrow}"); width: 12px; height: 12px; }}
            QScrollArea#canvasInspector QAbstractSpinBox {{ padding-right: 24px; }}
            QScrollArea#canvasInspector QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 23px; border: none; background: transparent; }}
            QScrollArea#canvasInspector QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 23px; border: none; background: transparent; }}
            QScrollArea#canvasInspector QAbstractSpinBox::up-arrow {{ image: url("{up_arrow}"); width: 10px; height: 10px; }}
            QScrollArea#canvasInspector QAbstractSpinBox::down-arrow {{ image: url("{down_arrow}"); width: 10px; height: 10px; }}
            QTreeWidget#canvasNodeLibrary {{ background: transparent; border: none; outline: none; font-size: 12px; }}
            QTreeWidget#canvasNodeLibrary::item {{ height: 30px; border: none; border-radius: 4px; padding: 0 4px; }}
            QTreeWidget#canvasNodeLibrary::item:hover {{ background: {p['hover']}; }}
            QTreeWidget#canvasNodeLibrary::item:selected {{ background: {p['accent_soft']}; color: {p['accent']}; }}
            QTreeWidget#canvasNodeLibrary::branch {{ background: transparent; }}
            QWidget#aetherloomCanvasPage QSplitter::handle {{ background: transparent; width: 8px; }}
            QWidget#aetherloomCanvasPage QScrollBar:vertical {{ width: 8px; background: transparent; }}
            QWidget#aetherloomCanvasPage QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 4px; min-height: 26px; }}
        ''')
        self._refresh_placeholder_palette()

        for item in self.scene.nodes.values():
            if item.inline_proxy is not None:item.inline_proxy.widget().refresh()

        queue_panel = getattr(self.owner, '_canvas_workflow_queue_panel', None)
        if queue_panel is not None:
            queue_panel.refresh_theme()
            if queue_panel.isVisible():
                queue_panel.refresh()

    def _refresh_placeholder_palette(self):
        p = self.scene.colors
        for editor in self.findChildren(QtWidgets.QLineEdit):
            value = editor.palette()
            value.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor(p['muted']))
            editor.setPalette(value)

    def shutdown(self):
        if self._closed:
            return
        self._commit_valid_text_drafts()
        self._rename()
        # Editing stays in memory. Only Run and explicit Save commit it.
        self._prune_workflows()
        self._closed = True
        self._selection_timer.stop()
        if getattr(self.engine, '_view_canvas', None) == self.document['id']:
            self.engine.set_view_canvas('')
        self._edit_group_timer.stop()
        self._workflow_cleanup.stop()
        self.scene.thumbnails.close()
        # The owner holds the execution FIFO and engine. Closing an editing
        # surface must not stop accepted or queued workflow snapshots.
        for entries in self.histories.values():
            if isinstance(entries,list):entries.clear()
            elif isinstance(entries,QtGui.QTextDocument):entries.clearUndoRedoStacks()
        self.histories.clear()
