"""Native, persistent canvas page. Execution belongs to the shared RH service."""
import copy
import json
import os
import uuid
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.paths import current_dir
from aetherloom_core.prompt_history import TextSnapshot
from aetherloom_core.rh_ui import palette
from aetherloom_core.rh_parameters import collect_node_values
from . import model
from .storage import CanvasStore
from .engine import CanvasEngine
from .workflow_queue import ensure_workflow_queue
from .workflow_queue_panel import show_workflow_queue
from .graphics import CanvasScene, CanvasView, NodeItem, EdgeItem, KIND_NAMES, STATUS_NAMES
from .editors import Inspector, EdgeInspector


RUNTIME_FIELDS = ('results', 'result_signatures', 'fingerprint', 'status', 'progress', 'node_progress', 'message', 'error', 'generation', 'cached', 'stale', 'activated', '_restored_missing_results', '_restored_positions_ambiguous')


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

    def __init__(self, choices, anchor=None, parent=None):
        super().__init__(parent, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        self.setObjectName('canvasNodeSearch')
        self.choices = choices
        self.setMinimumWidth(280)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(8)
        title = QtWidgets.QLabel('添加兼容节点' if anchor else '添加节点')
        title.setObjectName('canvasSearchTitle')
        layout.addWidget(title)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索节点名称、App 或输入类型…')
        self.search.installEventFilter(self)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.listing = QtWidgets.QListWidget()
        self.listing.setUniformItemSizes(True)
        self.listing.setSpacing(2)
        self.listing.itemClicked.connect(self._choose)
        self.listing.itemActivated.connect(self._choose)
        layout.addWidget(self.listing, 1)
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
        for choice in self.choices:
            if text and text not in choice.get('search', choice['label']).casefold():
                continue
            item = QtWidgets.QListWidgetItem(choice['label'])
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


class CanvasPage(QtWidgets.QWidget):
    """One editing surface; the engine keeps other open/running canvases alive."""

    def __init__(self, owner, service, prepare_app=None, store=None):
        super().__init__(owner)
        self.owner, self.service = owner, service
        self.store = store or CanvasStore()
        self.settings = getattr(owner, 'settings', {})
        self._prepare = prepare_app or getattr(owner, '_canvas_prepare_app', None)
        existing_queue = getattr(owner, '_canvas_workflow_queue', None)
        engine = existing_queue.engine if existing_queue is not None else CanvasEngine(service, self._prepare_node, self.store, owner)
        self.workflow_queue = ensure_workflow_queue(owner, engine=engine, store=self.store)
        self.engine = self.workflow_queue.engine
        self.store = self.engine.store
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
        self._wide_panel_preferences = (True, True)
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
        self._autosave = QtCore.QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(700)
        self._autosave.timeout.connect(lambda: self.save(automatic=True))
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

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('画布')
        title.setObjectName('canvasPageTitle')
        heading.addWidget(title)
        self.name_edit = QtWidgets.QLineEdit(self.document['name'])
        self.name_edit.setMaximumWidth(360)
        self.name_edit.setMinimumWidth(90)
        self.name_edit.editingFinished.connect(self._rename)
        heading.addWidget(self.name_edit, 1)
        heading.addStretch(1)
        self.save_state = QtWidgets.QLabel('本地画布')
        self.save_state.setObjectName('canvasMuted')
        heading.addWidget(self.save_state)
        layout.addLayout(heading)
        toolbar = QtWidgets.QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(QtCore.QSize(16, 16))
        toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.palette_action = toolbar.addAction('节点库')
        self.palette_action.setCheckable(True)
        self.palette_action.setChecked(True)
        self.palette_action.toggled.connect(lambda checked: self.library.setVisible(checked))
        package_button = QtWidgets.QToolButton()
        package_button.setText('文件')
        package_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        package_menu = QtWidgets.QMenu(package_button)
        for label, callback in [('新建画布', self.new_canvas), ('打开画布…', self.open_canvas), ('保存', self.save), ('另存为…', self.save_as)]:
            package_menu.addAction(label, callback)
        package_menu.addSeparator()
        package_menu.addAction('导入工作流 JSON…', self.import_canvas)
        package_menu.addAction('导出工作流 JSON…', self.export_canvas)
        package_button.setMenu(package_menu)
        toolbar.addWidget(package_button)
        toolbar.addAction('保存', self.save)
        toolbar.addSeparator()
        self.run_action = QtWidgets.QAction('运行画布', self)
        self.run_action.triggered.connect(lambda: self.run_canvas())
        self.stop_action = QtWidgets.QAction('全部终止', self)
        self.stop_action.setToolTip('取消当前画布正在运行与排队的全部工作流组；保留已完成结果')
        self.stop_action.triggered.connect(self.stop_canvas)
        self.inspector_action = toolbar.addAction('设置')
        self.inspector_action.setCheckable(True)
        self.inspector_action.setChecked(True)
        self.inspector_action.toggled.connect(self._toggle_inspector)
        toolbar.addAction('适应全部', lambda: self.view.fit_nodes())
        self.connection_action = toolbar.addAction('RH 连接', self._connection_settings)
        self.connection_action.setToolTip('与 RH App 主页共享站点和 API key 设置')
        self.queue_action = toolbar.addAction('工作流队列')
        self.queue_action.setCheckable(True)
        self.queue_action.triggered.connect(lambda checked: self._show_workflow_queue(checked))
        self.queue_action.setToolTip('显示 / 隐藏所有画布的工作流队列，可展开任务查看 App')
        package_menu.addSeparator()
        self.undo_action = package_menu.addAction('撤销画布编辑', self.undo)
        self.undo_action.setToolTip('撤销画布编辑 · Ctrl+Z')
        self.redo_action = package_menu.addAction('重做画布编辑', self.redo)
        self.redo_action.setToolTip('重做画布编辑 · Ctrl+Y')
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
        self.install_missing_button = QtWidgets.QPushButton('添加缺失 App')
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
        library_title = QtWidgets.QLabel('添加节点')
        library_title.setObjectName('canvasSectionTitle')
        library_layout.addWidget(library_title)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索 App 或基础节点')
        self.search.textChanged.connect(self._filter_library)
        library_layout.addWidget(self.search)
        self.library_list = QtWidgets.QListWidget()
        self.library_list.setWordWrap(True)
        self.library_list.setSpacing(3)
        self.library_list.itemDoubleClicked.connect(self._library_add)
        library_layout.addWidget(self.library_list, 1)
        add = QtWidgets.QPushButton('添加所选节点')
        add.clicked.connect(lambda: self._library_add(self.library_list.currentItem()))
        library_layout.addWidget(add)
        refresh = QtWidgets.QPushButton('刷新已添加 App')
        refresh.clicked.connect(self.refresh_apps)
        library_layout.addWidget(refresh)
        self.splitter.addWidget(self.library)
        self.center = QtWidgets.QFrame()
        self.center.installEventFilter(self)
        self.center.setObjectName('canvasSurface')
        center_layout = QtWidgets.QVBoxLayout(self.center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        self.scene = CanvasScene(self)
        self.view = CanvasView(self.scene)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.scene.nodes_moved.connect(self._move_nodes)
        self.scene.nodes_resized.connect(self._resize_nodes)
        self.scene.result_requested.connect(self._focus_result)
        self.scene.connect_requested.connect(self._connect_nodes)
        self.scene.reconnect_requested.connect(self._reconnect_nodes)
        self.scene.disconnect_requested.connect(self._disconnect_edge)
        self.scene.add_requested.connect(self._show_node_search)
        self.scene.run_requested.connect(lambda node_id, force: self.run_canvas(target=node_id, force=force))
        self.scene.decode_requested.connect(self._focus_decode)
        self.scene.action_requested.connect(self._action)
        self.view.files_dropped.connect(self._drop_files)
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
        self.inspector_scroll.setMinimumWidth(245)
        self.inspector_scroll.setMaximumWidth(430)
        self.inspector_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.splitter.addWidget(self.inspector_scroll)
        self.splitter.setSizes([215, 850, 290])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        layout.addWidget(self.splitter, 1)
        footer = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel('双击空白 / Tab 添加 · 拖线到空白添加兼容节点 · 中键 / 空格拖动画布')
        self.status_label.setObjectName('canvasMuted')
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.zoom_label = QtWidgets.QLabel('100%')
        self.zoom_label.setObjectName('canvasMuted')
        footer.addWidget(self.zoom_label)
        layout.addLayout(footer)
        self._empty_inspector()
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
                saved = self.store.list(lightweight=True)
                if saved:
                    self.document = self.store.load(saved[0]['id'])
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
                if 'url' in data:
                    apps[str(app_id)]['url'] = str(data.get('url') or '')
                if data.get('url_error'):
                    apps[str(app_id)]['url_error'] = str(data['url_error'])
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        self.apps = apps
        self.library_list.clear()
        for kind in ('image', 'text', 'video', 'audio', 'select', 'preview'):
            item = QtWidgets.QListWidgetItem('＋  ' + model.TITLES[kind])
            item.setData(QtCore.Qt.UserRole, ('base', kind))
            item.setSizeHint(QtCore.QSize(150, 36))
            self.library_list.addItem(item)
        for app_id, app in sorted(apps.items(), key=lambda pair: pair[1]['name'].lower()):
            item = QtWidgets.QListWidgetItem('APP  ' + app['name'])
            item.setData(QtCore.Qt.UserRole, ('app', app_id))
            item.setToolTip(app['name'] + '\n' + app_id)
            item.setSizeHint(QtCore.QSize(170, 42))
            self.library_list.addItem(item)
        self._filter_library()
        self._refresh_missing_apps()
        if self._selection_identity:
            self._selection_changed(force=True)

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
                requests.setdefault(reference['webapp_id'], reference)
        return list(requests.values()), issues, missing_nodes

    def _refresh_missing_apps(self):
        requests, issues, count = self._app_requirements()
        pieces = []
        if requests:
            pieces.append(f'此画布缺少 {len(requests)} 个 App，涉及 {count} 个节点。添加后保留各节点参数。')
        if issues:
            pieces.append(f'{len(issues)} 个节点的 App 引用无效，请重新添加对应 App 节点。')
        if self._install_summary:
            pieces.append(self._install_summary)
        self.missing_label.setText(' '.join(pieces))
        self.missing_label.setToolTip('\n'.join(issues))
        self.install_missing_button.setText('正在添加…' if self._install_busy else '添加缺失 App')
        self.install_missing_button.setEnabled(bool(requests) and not self._install_busy)
        self.missing_banner.setVisible(bool(pieces) or self._install_busy)
        if isinstance(self._inspector, Inspector) and getattr(self._inspector, 'install_button', None):
            self._inspector.install_button.setEnabled(not self._install_busy)

    def _install_missing_apps(self, node_id=None):
        if self._closed or self._install_busy:
            return
        self.refresh_apps()
        references, issues, unused = self._app_requirements({node_id} if node_id else None)
        if not references:
            self._message(issues[0] if issues else '所需 App 已添加到本机。')
            return
        install = getattr(self.owner, '_rh_install_apps', None)
        if install is None:
            self._message('App 添加服务尚未就绪，请重新打开客户端。')
            return
        self._install_busy = True
        self._install_summary = f'准备添加 {len(references)} 个 App…'
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
        self._install_summary = f'已处理 {index} / {total} 个 App' + ('，部分添加失败。' if error else '…')
        self._refresh_missing_apps()

    def _install_finished(self, report):
        self._install_job = None
        self._install_busy = False
        if self._closed:
            return
        failures = report.get('failed') or []
        self._install_summary = (f"已添加 {len(report.get('added') or [])} 个 App。"
                                 + (f'{len(failures)} 个添加失败，可检查 RH 连接设置后重试。' if failures else ''))
        # The installer has refreshed the owner's catalog. Existing canvas node
        # definitions/values remain independent; changed schemas require rebind.
        self.refresh_apps()
        if failures:
            details = '\n'.join(str(item.get('webapp_id') or 'App') + '：' + str(item.get('error') or '添加失败') for item in failures)
            self.missing_label.setToolTip(details)
        else:
            self._message(self._install_summary)
            self._install_summary = ''
            self._refresh_missing_apps()

    def _filter_library(self):
        text = self.search.text().strip().lower()
        for index in range(self.library_list.count()):
            item = self.library_list.item(index)
            item.setHidden(text not in (item.text() + item.toolTip()).lower())

    def _library_add(self, item):
        if item is None:
            return
        group, value = item.data(QtCore.Qt.UserRole)
        center = self.view.mapToScene(self.view.viewport().rect().center())
        self._insert_choice({'group': group, 'value': value}, center - QtCore.QPointF(134, 90))

    def _make_node(self, group, value):
        if group == 'app':
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
            node = model.new_node('app', app['name'], app=app, decode_settings=decode,
                                  params={model.parameter_key(field): copy.deepcopy(field.get('fieldValue', '')) for field in app['nodes']})
            model.normalize_app_urls({'nodes': [node]})
        else:
            node = model.new_node(value)
            if value == 'select':
                node['params']['indices'] = [1]
        return node

    def _search_choices(self, anchor):
        choices = []
        prototypes = [('base', kind, model.TITLES[kind], model.new_node(kind))
                      for kind in ('image', 'text', 'video', 'audio', 'select', 'preview')]
        prototypes += [('app', key, app['name'], model.new_node('app', app=app)) for key, app in self.apps.items()]
        anchor_node = next((node for node in self.document['nodes'] if anchor and node['id'] == anchor['node_id']), None)
        if anchor and anchor_node is None:
            return choices
        accepted = None
        if anchor and not anchor['output']:
            accepted = next((port['type'] for port in model.input_ports(anchor_node) if port['key'] == anchor['input']), None)
            if accepted is None:
                return choices
        type_names = {'text': '文本', 'image': '图像', 'audio': '音频', 'video': '视频',
                      'number': '数值', 'scalar': '枚举', 'file': '文件', 'any': '任意结果'}
        for group, value, title, prototype in prototypes:
            prefix = 'APP · ' if group == 'app' else ''
            choice = {'group': group, 'value': value, 'label': prefix + title,
                      'search': title + ' ' + str(value)}
            if anchor and anchor['output']:
                for port in model.input_ports(prototype):
                    if any(model.types_compatible(kind, port['type']) for kind in model.output_types(anchor_node)):
                        label = prefix + title + ' → ' + port['label'] + ' · ' + type_names.get(port['type'], port['type'])
                        choices.append(dict(choice, label=label, port=port['key'], search=label + ' ' + str(value)))
            elif anchor:
                if any(model.types_compatible(kind, accepted) for kind in model.output_types(prototype)):
                    choices.append(choice)
            else:
                choices.append(choice)
        return choices

    def _show_node_search(self, position, anchor=None):
        if self._node_search is not None:
            self._node_search.close()
            self._node_search.deleteLater()
        position = QtCore.QPointF(position)
        popup = NodeSearchPopup(self._search_choices(anchor), anchor, self)
        self._node_search = popup
        popup.chosen.connect(lambda choice: self._insert_choice(choice, position, anchor))
        popup.disconnected.connect(self._disconnect_edge)
        point = self.view.viewport().mapToGlobal(self.view.mapFromScene(position))
        popup.show_at(point, self.scene.colors)

    def _insert_choice(self, choice, position, anchor=None):
        try:
            node = self._make_node(choice['group'], choice['value'])
            node['x'], node['y'] = position.x(), position.y()
            candidate = copy.deepcopy(self.document)
            candidate['nodes'].append(node)
            if anchor:
                if anchor.get('remove_edge'):
                    candidate['edges'] = [edge for edge in candidate['edges'] if edge['id'] != anchor['remove_edge']]
                if anchor['output']:
                    model.connect(candidate, anchor['node_id'], node['id'], choice['port'])
                else:
                    candidate['edges'] = [edge for edge in candidate['edges'] if not (edge['target'] == anchor['node_id'] and edge['input'] == anchor['input'])]
                    model.connect(candidate, node['id'], anchor['node_id'], anchor['input'])
            model.validate_document(candidate)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            self._message(f'无法添加节点：{error}')
            return
        self._checkpoint()
        self.document = candidate
        self._mark_stale(anchor['node_id'] if anchor and not anchor['output'] else node['id'])
        self._edited(rebuild=True, select=node['id'])

    def _prepare_node(self, node, rh_nodes):
        model.app_reference(node.get('app') or {})
        app_id = str(node.get('app', {}).get('webapp_id', ''))
        installed = self.apps.get(app_id)
        if installed is None:
            raise ValueError(f"请先添加 App：{node.get('title', app_id)}")
        if _schema(installed['nodes']) != _schema(node.get('app', {}).get('nodes', [])):
            raise ValueError(f"{node.get('title', 'App')} 的定义已变化，请在节点设置中重新绑定参数")
        if self._prepare is None:
            raise ValueError('共享执行服务尚未就绪')
        return self._prepare(node, rh_nodes)

    def _set_document(self, doc):
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
            self.store.set_active(doc['id'])
        except (OSError, ValueError) as error:
            self._message(f'记录当前画布失败：{error}')
        self.name_edit.setText(str(doc.get('name', '未命名画布')))
        self._undo, self._redo = [], []
        self._removed_runtime = {}
        self._selection_identity = None
        self.scene.set_document(doc)
        self._empty_inspector()
        view = doc.get('view') or {}
        self.view.resetTransform()
        zoom = min(3.5, max(.12, float(view.get('zoom', 1))))
        self.view.scale(zoom, zoom)
        self.view.centerOn(float(view.get('x', 0)), float(view.get('y', 0)))
        self._updating, self._dirty = False, False
        attached = self.engine.attach(doc)
        if attached:
            self.document = attached
            self.scene.refresh_nodes(self.document)
        self._sync_actions()
        self._refresh_missing_apps()
        self.save_state.setText('已保存' if self.store.path_for(doc['id']).exists() else '本地画布')
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
            self._autosave.stop()
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
        if edit_path is not None and edit_path == self._last_edit_path and self._autosave.isActive():
            return
        self._last_edit_path = edit_path
        self._undo.append(self._edit_snapshot())
        del self._undo[:-40]
        self._redo.clear()

    def _edit_snapshot(self):
        # Undo is editing history, never a duplicate of task/result history.
        result = {key: copy.deepcopy(value) for key, value in self.document.items() if key != 'run'}
        result['run'] = {}
        for node in result['nodes']:
            for key in RUNTIME_FIELDS:
                node.pop(key, None)
        return result

    def _edited(self, rebuild=False, select=None):
        self._dirty = True
        deleted = self._is_deleted(self.document['id'])
        self.save_state.setText('工作流已删除 · 手动保存可重新建立' if deleted else '正在保存…')
        if not deleted:
            self._autosave.start()
        update = getattr(self.engine, 'update_document', None)
        if update and not deleted:
            self.document = update(self.document)
        if rebuild:
            self._updating = True
            self.scene.set_document(self.document)
            if select in self.scene.nodes:
                self.scene.nodes[select].setSelected(True)
            self._updating = False
            self._selection_identity = None
            self._selection_changed(force=True)
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
        self._checkpoint()
        for node in self.document['nodes']:
            if node['id'] in positions:
                node['x'], node['y'] = positions[node['id']]
        self._edited()

    def _resize_nodes(self, sizes):
        self._checkpoint()
        for node in self.document['nodes']:
            if node['id'] in sizes:
                node['size'] = list(sizes[node['id']])
        self._edited()

    def _connect_nodes(self, source, target, port):
        self._reconnect_nodes('', source, target, port)

    def _reconnect_nodes(self, old_id, source, target, port):
        candidate = copy.deepcopy(self.document)
        removed = [edge for edge in candidate['edges'] if edge['id'] == old_id or (edge['target'] == target and edge['input'] == port)]
        if len(removed) == 1 and removed[0]['source'] == source and removed[0]['target'] == target and removed[0]['input'] == port:
            return
        candidate['edges'] = [edge for edge in candidate['edges'] if edge not in removed]
        try:
            model.connect(candidate, source, target, port)
        except ValueError as error:
            self._message(str(error))
            return
        self._checkpoint()
        self.document = candidate
        for edge in removed:
            self._mark_stale(edge['target'])
        self._mark_stale(target)
        self._edited(rebuild=True, select=target)

    def _disconnect_edge(self, edge_id):
        edge = next((edge for edge in self.document['edges'] if edge['id'] == edge_id), None)
        if edge is None:
            return
        self._checkpoint()
        self.document['edges'].remove(edge)
        self._mark_stale(edge['target'])
        self._edited(rebuild=True, select=edge['target'])
        target = next((node for node in self.document['nodes'] if node['id'] == edge['target']), {})
        self._message('连接已断开，输入恢复使用节点内部值。' if target.get('kind') == 'app' else '连接已断开，请连接上游结果后运行。')

    def _mark_stale(self, node_id):
        pending, found = [node_id], set()
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(edge['target'] for edge in self.document['edges'] if edge['source'] == current)
        for node in self.document['nodes']:
            if node['id'] in found and (node.get('results') or self.engine.is_running(self.document['id'])):
                node['_ui_stale'] = True

    def _node_changed(self, node_id, path, value):
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
        self._checkpoint((node_id, path))
        container[key] = copy.deepcopy(value)
        if path == 'app.url':
            container['url'] = reference['url']
            container['base_url'] = reference['base_url']
            container.pop('url_error', None)
        if path != 'title':
            self._mark_stale(node_id)
        self._edited()

    def _edge_changed(self, edge_id, key, value):
        edge = next((edge for edge in self.document['edges'] if edge['id'] == edge_id), None)
        if edge and edge.get(key) != value:
            self._checkpoint((edge_id, key))
            edge[key] = value
            self._mark_stale(edge['target'])
            self._edited()
            self.scene.edges[edge_id].update_path()

    def _selection_changed(self, force=False):
        if self._updating:
            return
        selected = self.scene.selectedItems()
        node = next((item for item in selected if isinstance(item, NodeItem)), None)
        edge = next((item for item in selected if isinstance(item, EdgeItem)), None)
        identity = ('node', node.node['id']) if node else ('edge', edge.edge['id']) if edge else None
        if identity == self._selection_identity and not force:
            return
        self._selection_identity = identity
        self._last_edit_path = None
        if node:
            definition = self.apps.get(str(node.node.get('app', {}).get('webapp_id', '')))
            is_app = node.node['kind'] == 'app'
            inspector = Inspector(node.node, self.document['id'], self.document['edges'], self.histories,
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
                inspector.tab_forms[3].insertWidget(0, open_app)
                previous = self._inspector
                if (isinstance(previous, Inspector) and previous.tabs is not None
                        and previous.node['id'] == node.node['id']):
                    inspector.tabs.setCurrentIndex(previous.tabs.currentIndex())
        elif edge:
            inspector = EdgeInspector(edge.edge)
            inspector.changed.connect(lambda key, value, edge_id=edge.edge['id']: self._edge_changed(edge_id, key, value))
        else:
            self._empty_inspector()
            return
        inspector.message.connect(self._message)
        old = self.inspector_scroll.takeWidget()
        if old:
            old.deleteLater()
        self._inspector = inspector
        self.inspector_scroll.setWidget(inspector)
        self._refresh_placeholder_palette()

    def _empty_inspector(self):
        old = self.inspector_scroll.takeWidget()
        if old:
            old.deleteLater()
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(18, 22, 18, 22)
        title = QtWidgets.QLabel('在画布中连接想法')
        title.setObjectName('canvasSectionTitle')
        layout.addWidget(title)
        hint = QtWidgets.QLabel('从左侧添加已保存的 App 或素材节点。\n\n选中节点可独立设置参数；选中连线可选择结果。\n\n画布任务与 App 页面共享队列、进度和输出。')
        hint.setWordWrap(True)
        hint.setObjectName('canvasMuted')
        layout.addWidget(hint)
        layout.addStretch()
        self._inspector = None
        self.inspector_scroll.setWidget(widget)

    def _focus_decode(self, node_id):
        self.inspector_action.setChecked(True)
        self._selection_changed(force=True)
        if isinstance(self._inspector, Inspector) and self._inspector.decode_group:
            self._inspector.tabs.setCurrentIndex(1)
            self._inspector.tabs.widget(1).ensureWidgetVisible(self._inspector.decode_group)
            self._inspector.decode_group.setFocus()

    def _focus_result(self, node_id, index):
        item = self.scene.nodes.get(node_id)
        if item is None:
            return
        self.inspector_action.setChecked(True)
        self.scene.clearSelection()
        item.setSelected(True)
        inspector = self._inspector
        if isinstance(inspector, Inspector) and inspector.results_list is not None:
            if inspector.tabs is not None:
                inspector.tabs.setCurrentIndex(3)
            listing = inspector.results_list
            if 0 <= index < listing.count():
                listing.setCurrentRow(index)
                listing.scrollToItem(listing.item(index))
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
        method = {'copy': self.copy_nodes, 'paste': self.paste_nodes, 'delete': self.delete_selected, 'undo': self.undo, 'redo': self.redo}.get(action)
        if method:
            method()

    def copy_nodes(self):
        ids = {item.node['id'] for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
        if ids:
            self._clipboard = copy.deepcopy({'nodes': [node for node in self.document['nodes'] if node['id'] in ids],
                                             'edges': [edge for edge in self.document['edges'] if edge['source'] in ids and edge['target'] in ids]})
            self._message(f'已复制 {len(ids)} 个节点。')

    def paste_nodes(self):
        if not self._clipboard:
            return
        self._checkpoint()
        graph = copy.deepcopy(self._clipboard)
        ids = {node['id']: uuid.uuid4().hex for node in graph['nodes']}
        for node in graph['nodes']:
            node['id'] = ids[node['id']]
            node['x'] += 40
            node['y'] += 40
            node['results'], node['fingerprint'], node['status'] = [], '', 'IDLE'
            node.pop('_ui_stale', None)
        for edge in graph['edges']:
            edge.update(id=uuid.uuid4().hex, source=ids[edge['source']], target=ids[edge['target']])
        self.document['nodes'].extend(graph['nodes'])
        self.document['edges'].extend(graph['edges'])
        self._edited(rebuild=True)
        for node in graph['nodes']:
            self.scene.nodes[node['id']].setSelected(True)

    def delete_selected(self):
        node_ids = {item.node['id'] for item in self.scene.selectedItems() if isinstance(item, NodeItem)}
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
        self.document['edges'] = [edge for edge in self.document['edges'] if edge['id'] not in edge_ids and edge['source'] not in node_ids and edge['target'] not in node_ids]
        self._edited(rebuild=True)

    def _restore_edit(self, restored):
        runtime = dict(self._removed_runtime)
        runtime.update({node['id']: node for node in self.document['nodes']})
        restored['run'] = copy.deepcopy(self.document.get('run', {}))
        for node in restored['nodes']:
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
        self.document = restored
        self.name_edit.setText(restored['name'])
        self._edited(rebuild=True)

    def undo(self):
        if self._undo:
            self._redo.append(self._edit_snapshot())
            self._restore_edit(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._edit_snapshot())
            self._restore_edit(self._redo.pop())

    def _drop_files(self, paths, position):
        groups = {}
        for path in paths:
            if not os.path.isfile(path):
                continue
            extension = Path(path).suffix.lower()
            if extension in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff'):
                kind = 'image'
            elif extension in ('.mp4', '.webm', '.mov', '.mkv', '.avi'):
                kind = 'video'
            elif extension in ('.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac'):
                kind = 'audio'
            else:
                continue
            groups.setdefault(kind, []).append(path)
        if not groups:
            self._message('请拖入图像、视频或音频文件。')
            return
        self._checkpoint()
        for index, (kind, files) in enumerate(groups.items()):
            node = model.new_node(kind, params={'files': files}, x=position.x() + index * 300, y=position.y())
            self.document['nodes'].append(node)
        self._edited(rebuild=True, select=node['id'])

    def _view_changed(self):
        zoom = self.view.transform().m11()
        self.zoom_label.setText(f'{int(zoom * 100)}%')
        if self._updating:
            return
        point = self.view.mapToScene(self.view.viewport().rect().center())
        self.document['view'] = {'zoom': zoom, 'x': point.x(), 'y': point.y()}
        if self.document['nodes']:
            self._edited()

    def save(self, *unused, automatic=False):
        self._autosave.stop()
        canvas_id = self.document['id']
        if self._is_deleted(canvas_id):
            self._mark_workflow_deleted(canvas_id)
            if automatic:
                return True
        try:
            model.normalize_app_urls(self.document)
            self.engine.save_document(self.document, explicit=not automatic)
            current = self.engine.document(canvas_id)
            if current is not None:
                self.document = current
                self.scene.refresh_nodes(self.document)
            self._persisted_ids.add(canvas_id)
            self._deleted_ids.discard(canvas_id)
            self._watch_workflow_directory()
            self._dirty = False
            self._refresh_missing_apps()
            self.save_state.setText('已保存到本地')
            return True
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            if automatic and self._is_deleted(canvas_id):
                self._mark_workflow_deleted(canvas_id)
                return True
            self.save_state.setText('保存失败')
            self._message(f'保存失败：{error}')
            return False

    def new_canvas(self):
        if self._dirty and not self.save(automatic=True):
            return
        self._set_document(model.new_document())

    def open_canvas(self):
        if self._dirty and not self.save(automatic=True):
            return
        try:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '打开工作流 JSON', str(self.store.root), 'AetherLoom 工作流 (*.json)')
            if not path:
                return
            doc = self.store.load(path) if Path(path).resolve().parent == self.store.root else self.store.import_workflow(path)
            self._set_document(doc)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._message(f'打开工作流失败：{error}')

    def save_as(self):
        if self._package_busy:
            self._message('正在处理工作流，请等待完成。')
            return
        if not self.save():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '工作流另存为', self.document['name'] + '.aetherloom.json', 'AetherLoom 工作流 (*.json)')
        if path:
            snapshot = copy.deepcopy(self.document)
            self._start_package('export', lambda: self.store.export_workflow(snapshot, path))

    def import_canvas(self):
        if self._package_busy:
            self._message('正在处理工作流，请等待完成。')
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '导入工作流 JSON', '', 'AetherLoom 工作流 (*.json)')
        if not path:
            return
        if self._dirty and not self.save(automatic=True):
            return
        self._start_package('import', lambda: self.store.import_workflow(path))

    def export_canvas(self):
        if self._package_busy:
            self._message('正在处理工作流，请等待完成。')
            return
        if not self.save(automatic=True):
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出工作流 JSON', self.document['name'] + '.aetherloom.json', 'AetherLoom 工作流 (*.json)')
        if not path:
            return
        snapshot = copy.deepcopy(self.document)
        self._start_package('export', lambda: self.store.export_workflow(snapshot, path))

    def _start_package(self, kind, operation):
        self._package_busy = True
        self._message('正在导入工作流…' if kind == 'import' else '正在保存工作流 JSON…')
        # A single package operation per page; no Qt widgets enter the worker.
        QtCore.QThreadPool.globalInstance().start(_PackageJob(kind, operation, self._package_signals))

    @QtCore.pyqtSlot(str, object, str)
    def _package_finished(self, kind, result, error):
        self._package_busy = False
        if self._closed:
            return
        if error:
            self._message(('导入失败：' if kind == 'import' else '导出失败：') + error)
        elif kind == 'import':
            # The user may continue editing while the workflow is being read.
            if self._dirty and not self.save(automatic=True):
                self._message('工作流已导入到本地，请先保存当前编辑，再从“打开”选择它。')
                return
            self._set_document(result)
            missing = sum(node['kind'] == 'app' and str(node.get('app', {}).get('webapp_id', '')) not in self.apps for node in result['nodes'])
            self._message('工作流已导入。' + (f' {missing} 个 App 尚未添加到本机，请补齐后运行。' if missing else ''))
        else:
            self._message('工作流 JSON 已保存。输入素材和运行结果保留在独立恢复快照中。')

    def _record_run_texts(self, target):
        ids = model.ancestors(self.document, target) if target else {node['id'] for node in self.document['nodes']}
        for node in self.document['nodes']:
            if node['id'] not in ids:
                continue
            texts = {'text': node.get('params', {}).get('text', '')} if node['kind'] == 'text' else {}
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

    def run_canvas(self, target=None, force=False):
        if isinstance(self._inspector, Inspector) and not self._inspector.validate():
            self._message('请先修正节点中未完成或无效的数值。')
            return
        self._rename()
        self.refresh_apps()
        try:
            self.batch_spin.interpretText()
            if not self.document['nodes']:
                raise ValueError('请先添加节点并设置输入。')
            model.validate_document(self.document)
            self._record_run_texts(target)
            if not self.save():
                return
            batches = 1 if target else self.document.get('batch_count', 1)
            self.workflow_queue.enqueue(self.document, target=target, force=force, batch_count=batches,
                                        prepare_app=self._prepare_node)
            self._sync_actions()
            self._message(f'已加入工作流队列，共 {batches} 批；将按加入顺序运行并同步 App 输出卡片。')
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
        self.document['run'] = copy.deepcopy(update.get('run', {}))
        nodes = {node['id']: node for node in update.get('nodes', [])}
        result_changed = False
        for node in self.document['nodes']:
            incoming = nodes.get(node['id'])
            if incoming:
                changed = node.get('results') != incoming.get('results')
                result_changed |= changed
                for key in RUNTIME_FIELDS:
                    if key in incoming:
                        node[key] = copy.deepcopy(incoming[key])
                if changed:
                    available, signatures, missing = model.available_results(node.get('results', []), node.get('result_signatures'))
                    node['results'] = available
                    if signatures is not None:
                        node['result_signatures'] = signatures
                    if missing:
                        node['_restored_missing_results'] = True
        self.scene.refresh_nodes(self.document)
        self._sync_actions()
        if result_changed and isinstance(self._inspector, Inspector) and self._selection_identity:
            selected = next((node for node in self.document['nodes'] if node['id'] == self._selection_identity[1]), None)
            if selected:
                # Runtime updates must not destroy in-progress numeric/text drafts.
                self._inspector.update_results(selected.get('results', []))
        run = self.document.get('run', {})
        if run:
            status = STATUS_NAMES.get(run.get('status', ''), run.get('status', ''))
            completed = sum(node.get('status') in ('SUCCESS', 'REUSED', 'SKIPPED') for node in self.document['nodes'])
            skipped = sum(node.get('status') == 'SKIPPED' for node in self.document['nodes'])
            batches = run.get('batch_count', 1)
            batch_index = run.get('batch_index', 0) + 1
            self.status_label.setText(f'第 {batch_index}/{batches} 批 · {completed} / {len(self.document["nodes"])} 节点已处理'
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
        self.run_action.setToolTip(f'将当前设置固化为新的工作流组并加入队尾，共 {batch_count} 批')
        self.stop_action.setText('取消中…' if canceling and not can_cancel else '全部终止')
        self.stop_action.setEnabled(can_cancel)
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))
        self._place_inspector()

    def _message(self, text):
        self.status_label.setText(str(text))
        toast = getattr(self.owner, '_show_toast', None)
        if toast:
            toast(str(text), 4000)

    def _toggle_inspector(self, checked):
        self.inspector_scroll.setVisible(checked)
        self._place_inspector()

    def _place_inspector(self):
        self._run_layout.setDirection(QtWidgets.QBoxLayout.TopToBottom if self.center.width() < 370
                                      else QtWidgets.QBoxLayout.LeftToRight)
        self.run_panel.adjustSize()
        self.run_panel.move(max(8, self.center.width() - self.run_panel.width() - 12),
                            max(8, self.center.height() - self.run_panel.height() - 12))
        mode = 'compact' if self.width() < 800 else 'overlay' if self.width() < 1020 else 'wide'
        previous = self._responsive_mode
        if mode != previous:
            self._responsive_mode = mode
            if mode != 'wide' and previous in (None, 'wide'):
                self._wide_panel_preferences = (self.palette_action.isChecked(), self.inspector_action.isChecked())
                self.inspector_action.setChecked(False)
            if mode == 'compact':
                self.palette_action.setChecked(False)
            elif previous == 'compact':
                self.palette_action.setChecked(self._wide_panel_preferences[0])
            if mode == 'wide' and previous not in (None, 'wide'):
                self.palette_action.setChecked(self._wide_panel_preferences[0])
                self.inspector_action.setChecked(self._wide_panel_preferences[1])
        narrow = mode != 'wide'
        if narrow:
            if self.inspector_scroll.parent() is self.splitter:
                self.inspector_scroll.setParent(self.center)
            width = min(315, max(230, self.center.width() - 36))
            self.inspector_scroll.setGeometry(max(0, self.center.width() - width - 10), 10, width,
                                               max(100, self.center.height() - self.run_panel.height() - 38))
            self.inspector_scroll.setVisible(self.inspector_action.isChecked())
            self.inspector_scroll.raise_()
        elif self.inspector_scroll.parent() is not self.splitter:
            self.splitter.addWidget(self.inspector_scroll)
            self.inspector_scroll.setVisible(self.inspector_action.isChecked())
            self.splitter.setSizes([210, max(400, self.width() - 550), 300])
        self.view.overlay_exclusion = self.inspector_scroll.width() + 20 if narrow and self.inspector_scroll.isVisible() else 0
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
            current = self.engine.document(self.document['id'])
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
        p = palette(mode)
        arrow_root = Path(current_dir) / 'icons'
        up_arrow = (arrow_root / f'ui-chevron-up-{mode}.svg').as_posix()
        down_arrow = (arrow_root / f'ui-chevron-down-{mode}.svg').as_posix()
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
            QWidget#aetherloomCanvasPage QWidget {{ color: {p['text']}; font-size: 12px; }}
            QWidget#aetherloomCanvasPage QLabel {{ background: transparent; border: none; }}
            QWidget#aetherloomCanvasPage QLabel#canvasPageTitle {{ font-size: 23px; font-weight: 700; padding-right: 12px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasSectionTitle {{ font-size: 14px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QLabel#canvasMuted {{ color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QLabel#canvasBuiltinReuseHint {{ color: {p['muted']}; font-size: 11px; }}
            QWidget#aetherloomCanvasPage QLabel#canvasWarning {{ color: {p['warning']}; }}
            QFrame#canvasMissingApps {{ background: {p['surface']}; border: 1px solid {p['warning']}; border-radius: 8px; }}
            QFrame#canvasPanel, QScrollArea#canvasInspector {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 11px; }}
            QScrollArea#canvasInspector > QWidget > QWidget {{ background: {p['surface']}; }}
            QFrame#canvasSurface {{ border: 1px solid {p['border']}; border-radius: 11px; }}
            QWidget#aetherloomCanvasPage QListWidget {{ background: transparent; border: none; outline: none; }}
            QWidget#aetherloomCanvasPage QListWidget::item {{ border-radius: 6px; padding: 5px; }}
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
            QWidget#aetherloomCanvasPage QToolButton#canvasRunButton {{ background: {p['accent']}; color: #ffffff; border-color: {p['accent']}; padding: 10px 19px; font-weight: 600; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasRunButton:disabled {{ background: {p['hover']}; color: {p['muted']}; border-color: {p['border']}; }}
            QWidget#aetherloomCanvasPage QToolButton#canvasStopButton {{ padding: 10px 14px; }}
            QWidget#aetherloomCanvasPage QPushButton:disabled, QWidget#aetherloomCanvasPage QToolButton:disabled {{ color: {p['muted']}; }}
            QWidget#aetherloomCanvasPage QToolBar {{ border: none; background: transparent; spacing: 5px; }}
            QWidget#aetherloomCanvasPage QToolButton#qt_toolbar_ext_button {{ min-width: 24px; min-height: 28px; padding: 2px; }}
            QWidget#aetherloomCanvasPage QGroupBox {{ border: 1px solid {p['border']}; border-radius: 7px; margin-top: 10px; padding-top: 12px; }}
            QWidget#aetherloomCanvasPage QGroupBox::title {{ subcontrol-origin: margin; left: 9px; padding: 0 4px; }}
            QTabWidget#canvasNodeSettingsTabs::pane {{ border: none; border-top: 1px solid {p['border']}; }}
            QScrollArea#canvasNodeTabScroll, QWidget#canvasNodeTabContent {{ background: {p['surface']}; border: none; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar {{ font-size: 11px; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar::tab {{ background: transparent; color: {p['muted']};
                border: none; border-bottom: 2px solid transparent; padding: 8px 2px; margin: 0; font-size: 11px; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar::tab:selected {{ color: {p['accent']};
                background: {p['accent_soft']}; border-bottom-color: {p['accent']}; }}
            QTabWidget#canvasNodeSettingsTabs QTabBar::tab:hover {{ background: {p['hover']}; }}
            QWidget#aetherloomCanvasPage QSplitter::handle {{ background: transparent; width: 8px; }}
            QWidget#aetherloomCanvasPage QScrollBar:vertical {{ width: 8px; background: transparent; }}
            QWidget#aetherloomCanvasPage QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 4px; min-height: 26px; }}
        ''')
        self._refresh_placeholder_palette()

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
        if self._dirty:
            self.save(automatic=True)
        self._prune_workflows()
        self._closed = True
        if getattr(self.engine, '_view_canvas', None) == self.document['id']:
            self.engine.set_view_canvas('')
        self._autosave.stop()
        self._workflow_cleanup.stop()
        self.scene.thumbnails.close()
        # The owner holds the execution FIFO and engine. Closing an editing
        # surface must not stop accepted or queued workflow snapshots.
        for entries in self.histories.values():
            entries.clear()
        self.histories.clear()
