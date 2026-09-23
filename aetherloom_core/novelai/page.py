"""Independent illustration workspace; every request uses a frozen GUI snapshot."""
import copy
import os
import uuid
from collections import OrderedDict
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.paths import current_dir
from .styles import page_stylesheet
from . import catalog, storage
from .controls import DrawingControls
from .dialogs import AccountDialog, TagsDialog, ChunksDialog, token_for
from .credentials import tokens_for, tokens_from_record
from .history import HistoryPanel
from .task_panel import TaskPanel
from .queue import QueueService
from .queue_dialog import QueueDialog
from .preview import ImagePreview
from .account_status import AccountMonitor, AccountStrip
from .preferences import normalize_suggestion_preferences, SuggestionPreferencesDialog


class _PreviewStack(QtWidgets.QStackedWidget):
    """The hidden editor must not raise the normal preview's minimum height."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout().setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        self.currentChanged.connect(self.updateGeometry)

    def minimumSizeHint(self):
        current = self.currentWidget()
        return (current.minimumSizeHint().expandedTo(current.minimumSize())
                if current is not None else QtCore.QSize(0, 0))

    def sizeHint(self):
        current = self.currentWidget()
        return current.sizeHint() if current is not None else QtCore.QSize(0, 0)


class NovelAIPage(QtWidgets.QWidget):
    def __init__(self, owner, parent=None, *, data_dir=None):
        super().__init__(parent)
        self.owner = owner
        self.data_dir = str(data_dir or current_dir)
        saved = storage.load_settings(self.data_dir)
        self._saved_settings = copy.deepcopy(saved)
        self.prompt_suggestion_preferences = normalize_suggestion_preferences(saved.get('prompt_suggestions'))
        self.setObjectName('novelaiPage')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self._job = None
        self._closing = False
        self._submitting = False
        self._position_editing = False
        self._position_syncing = False
        self._position_baseline = None
        self._position_state = None
        self._submit_cooldown = QtCore.QTimer(self)
        self._submit_cooldown.setSingleShot(True)
        self._submit_cooldown.setTimerType(QtCore.Qt.PreciseTimer)
        self._submit_cooldown.setInterval(500)
        self._submit_cooldown.timeout.connect(self._update_submit_button)
        self._queue = QueueService(self)
        self._queue_dialog = None
        self._editor_revision = 0
        self._enqueued_revisions = {}
        self._refreshed_tasks = set()
        self._follow_queue_preview = True
        self._preview_task_id = None
        self._displayed_task_id = None
        self._displayed_task_state = None
        self._stream_previews = OrderedDict()
        self._task_results = []
        self._task_result_index = 0
        self._result_view_context = None
        self._history_ids = set()
        self._input = ''
        self._mask = None
        self._saved_mask = None
        self._selected = None
        self._chunks = {}
        self._enhancement_options = {}
        self._panel_override = False
        self._previewing_input = False
        self._account_monitor = AccountMonitor(self)
        self._account_monitor.changed.connect(self._account_changed)
        self._quota_timer = QtCore.QTimer(self)
        self._quota_timer.setInterval(60000)
        self._quota_timer.timeout.connect(self._refresh_account)
        self._billing_timer = QtCore.QTimer(self)
        self._billing_timer.setSingleShot(True)
        self._billing_timer.timeout.connect(self._refresh_billing_hint)
        self._build()
        from .suggestions import TagSuggestions
        self._tag_suggestions = TagSuggestions(self)
        self.controls.tagSuggestionsRequested.connect(self.tags)
        self.controls.changed.connect(self._editor_changed)
        self.controls.positionEditRequested.connect(self.toggle_position_editor)
        self.focused.toggled.connect(self._editor_changed)
        self.padding.valueChanged.connect(self._editor_changed)
        self._queue.changed.connect(self._queue_changed)
        self._queue.progress.connect(self._queue_progress)
        self._queue.preview.connect(self._queue_preview)
        self._queue.completed.connect(self._queue_completed)
        self._queue.failed.connect(self._queue_failed)
        queue_settings = saved.get('queue')
        queue_settings = queue_settings if isinstance(queue_settings, dict) else {}
        # The old paid-only limit does not define the new unified request policy.
        try:
            self._queue.set_concurrency(queue_settings.get('concurrency', 3))
        except (ValueError, TypeError, OverflowError):
            self._queue.set_concurrency(3)
        try:
            self._queue.set_retry_interval(queue_settings.get('retry_interval', 5))
        except (ValueError, TypeError, OverflowError):
            self._queue.set_retry_interval(5)
        try:
            self._apply_settings(saved.get('options', catalog.default_options()))
            self._mask = self._saved_mask = saved.get('mask') if isinstance(saved.get('mask'), dict) else None
        except (ValueError, TypeError, OverflowError):
            self._apply_settings(catalog.default_options())
            self.status.setText('已忽略无法读取的旧参数，请重新设置。')
        self.history.set_items(storage.load_history(self.data_dir))
        self._history_ids = {(v.get('id') or v.get('path')) for v in self.history.items()}
        from aetherloom_core.api_manager_ui import credential_events
        credential_events().saved.connect(self._credentials_changed)
        self.apply_theme()
        self._refresh_input_label()
        self._queue_changed()
        self._refresh_billing_hint()

    def _button(self, text, slot, tip='', checkable=False):
        button = QtWidgets.QToolButton(self)
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.clicked.connect(slot)
        return button

    def _build(self):
        from .references import guard_wheel
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(2)
        self._splitter_user_sized = False
        self.splitter.splitterMoved.connect(lambda *_: setattr(self, '_splitter_user_sized', True))
        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setObjectName('novelaiSidebar')
        self.sidebar.setMinimumWidth(300)
        self.sidebar.setMaximumWidth(560)
        left = QtWidgets.QVBoxLayout(self.sidebar)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        heading = QtWidgets.QWidget()
        top = QtWidgets.QHBoxLayout(heading)
        top.setContentsMargins(12, 6, 8, 2)
        brand = QtWidgets.QLabel('NovelAI')
        brand.setObjectName('novelaiBrand')
        top.addWidget(brand)
        top.addStretch()
        self.account_btn = self._button('连接', self.connection, '管理 API 密钥与账户')
        top.addWidget(self.account_btn)
        self.settings_btn = self._button('设置', self.open_preferences, '设置 NovelAI 提示词候选')
        self.settings_btn.setAccessibleName('NovelAI 设置')
        top.addWidget(self.settings_btn)
        more = self._button('☰', lambda: None, '参数文件、提示词工具与界面设置')
        more.setAccessibleName('NovelAI 页面菜单')
        menu = QtWidgets.QMenu(more)
        menu.addAction('导入参数 JSON…', self.import_preset)
        menu.addAction('导出参数 JSON…', self.export_preset)
        menu.addAction('从图片读取参数…', self.import_metadata)
        menu.addSeparator()
        menu.addAction('提示词片段…', self.edit_chunks)
        menu.addAction('标签建议…', self.tags)
        menu.addAction('历史作品…', self.show_history)
        menu.addSeparator()
        menu.addAction('收起参数栏', lambda: self._toggle_panel(self.controls, False))
        menu.addAction('显示 / 收起历史栏', lambda: self._toggle_panel(self.task_panel, self.task_panel.isHidden()))
        menu.addAction('打开 NovelAI 绘图说明', lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl('https://docs.novelai.net/en/image/')))
        more.setMenu(menu)
        more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.more_btn = more
        top.addWidget(more)
        left.addWidget(heading)
        self.account_strip = AccountStrip(self)
        self.account_strip.set_sidebar_compact(True)
        self.account_strip.refreshRequested.connect(lambda: self._refresh_account(force=True))
        left.addWidget(self.account_strip)
        self.controls = DrawingControls(self.owner)
        self.controls.setMinimumWidth(0)
        self.controls.setMaximumWidth(16777215)
        self.controls.action.currentIndexChanged.connect(self._mode_changed)
        self.controls.model.currentIndexChanged.connect(self._sync_result_actions)
        left.addWidget(self.controls, 1)
        self.splitter.addWidget(self.sidebar)
        self.workspace = QtWidgets.QFrame()
        self.workspace.setObjectName('novelaiWorkspace')
        self.workspace.installEventFilter(self)
        self.workspace.setMinimumWidth(180)
        self.workspace.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        center = QtWidgets.QVBoxLayout(self.workspace)
        center.setContentsMargins(6, 4, 6, 4)
        center.setSpacing(4)
        self.workspace_switchbar = QtWidgets.QWidget()
        switch = QtWidgets.QHBoxLayout(self.workspace_switchbar)
        switch.setContentsMargins(2, 0, 2, 0)
        self.parameters_btn = self._button('参数', lambda flag: self._toggle_panel(self.controls, flag), '显示或收起左侧参数', True)
        self.parameters_btn.setChecked(True)
        self.parameters_btn.setObjectName('novelaiPanelToggle')
        self.history_btn = self._button('结果栏', lambda flag: self._toggle_panel(self.task_panel, flag), '显示或收起右侧任务图片', True)
        self.history_btn.setChecked(True)
        self.history_btn.setObjectName('novelaiPanelToggle')
        switch.addWidget(self.parameters_btn)
        self.result_view_btn = self._button('结果', self.show_result, '返回刚才查看的结果，保留多图浏览位置', True)
        self.input_view_btn = self._button('底图', self.show_input, '查看本次生成使用的底图与遮罩', True)
        self.result_view_btn.setObjectName('novelaiViewTab')
        self.input_view_btn.setObjectName('novelaiViewTab')
        switch.addSpacing(10)
        switch.addWidget(self.result_view_btn)
        switch.addWidget(self.input_view_btn)
        switch.addStretch()
        switch.addWidget(self.history_btn)
        center.addWidget(self.workspace_switchbar)
        toolbar = QtWidgets.QHBoxLayout()
        self.input_btn = self._button('导入图像', self.choose_input, '拖入、粘贴或选择图像；无本地路径的图像会存入输入目录')
        toolbar.addWidget(self.input_btn)
        self.edit_btn = self._button('遮罩', self.edit_mask)
        toolbar.addWidget(self.edit_btn)
        self.geometry_btn = self._button('裁剪', self.geometry)
        toolbar.addWidget(self.geometry_btn)
        toolbar.addStretch()
        self.fit_btn = self._button('适应', lambda: self.preview.fit())
        toolbar.addWidget(self.fit_btn)
        tools = self._button('更多', lambda: None)
        tools_menu = QtWidgets.QMenu(tools)
        tools_menu.addAction('显示底图与遮罩', self.show_input)
        tools_menu.addAction('显示最近结果', self.show_result)
        tools_menu.addAction('历史作品…', self.show_history)
        tools_menu.addAction('固定当前图像用于对比', self.pin_result)
        tools_menu.addAction('关闭对比', self.close_comparison)
        tools_menu.addAction('绘制 / 遮罩…', self.edit_mask)
        tools_menu.addAction('裁剪 / 扩图…', self.geometry)
        preview_scale = tools_menu.addAction('预览图 100%', lambda: self.preview.actual())
        preview_scale.setToolTip('预览图最长边为 2200 像素；双击图片可用本地工具查看原图。')
        tools_menu.addSeparator()
        tools_menu.addAction('将当前图像作为底图', self.result_to_input)
        tools_menu.addAction('增强当前图像（图生图）', self.enhance)
        tools_menu.addAction('复用当前结果的实际提示词', self.reuse_actual)
        tools_menu.addSeparator()
        tools_menu.addAction('清除底图与遮罩', self.clear_input)
        tools.setMenu(tools_menu)
        tools.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.tools_btn = tools
        toolbar.addWidget(tools)
        self.preview_toolbar = QtWidgets.QWidget()
        toolbar.setContentsMargins(0, 0, 0, 0)
        self.preview_toolbar.setLayout(toolbar)
        self.source_label = QtWidgets.QLabel('底图：未选择')
        self.source_label.setObjectName('novelaiMuted')
        self.source_label.setTextFormat(QtCore.Qt.PlainText)
        self.source_label.setMinimumWidth(0)
        self.source_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.preview = ImagePreview(self)
        self.preview.setMinimumHeight(120)
        self.preview.filesDropped.connect(self.import_paths)
        self.preview.statusChanged.connect(self.status_message)
        self.comparison = ImagePreview(self)
        self.comparison.setMinimumHeight(120)
        self.comparison.hide()
        self.view_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.view_splitter.addWidget(self.preview)
        self.view_splitter.addWidget(self.comparison)
        self.view_splitter.setChildrenCollapsible(False)
        from .position_canvas import CharacterPositionEditor
        self.position_editor = CharacterPositionEditor(self)
        self.position_editor.positionEdited.connect(self._position_edited)
        self.position_editor.finishRequested.connect(self.finish_position_editor)
        self.position_editor.cancelRequested.connect(self.cancel_position_editor)
        self.preview_stack = _PreviewStack()
        self.preview_stack.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        self.preview_stack.addWidget(self.view_splitter)
        self.preview_stack.addWidget(self.position_editor)
        self.preview.imageChanged.connect(self._refresh_position_background)
        self.result_actions = QtWidgets.QToolBar('结果操作', self)
        self.result_actions.setObjectName('novelaiResultActions')
        self.result_actions.setMovable(False)
        self.result_actions.setFloatable(False)
        self.result_actions.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.result_actions.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.result_actions.setMinimumWidth(0)
        for title, slot, tip in (
                ('图生图', self.result_to_input, '将当前预览图像用于图生图；调整参数后点击生成才会提交'),
                ('增强', self.enhance, '设置增强幅度、放大倍数或独立强度与噪声'),
                ('局部重绘', lambda: self.prepare_result_action('infill'), '使用当前预览图像并绘制重绘遮罩'),
                ('2× 放大', lambda: self.prepare_result_action('upscale'), '使用独立超分模型；点击生成后提交'),
                ('图像工具', lambda: self.prepare_result_action('augment'), '使用当前预览图像进行上色、去背或其他图像处理'),
                ('打开原图', lambda: self.history.open(self._preview_image_record()), '使用本地默认工具打开当前图像'),
                ('文件位置', lambda: self.history.reveal(self._preview_image_record()), '在文件夹中选中当前图像')):
            action = self.result_actions.addAction(title, slot)
            action.setToolTip(tip)
            action.setProperty('normalToolTip', tip)
            action.triggered.connect(self._sync_result_actions)
            if title in ('图生图', '增强', '局部重绘', '2× 放大', '图像工具'):
                action.setCheckable(True)
                action.setData({'图生图': 'img2img', '增强': 'enhance', '局部重绘': 'infill',
                                '2× 放大': 'upscale', '图像工具': 'augment'}[title])
        center.addWidget(self.result_actions)
        center.addWidget(self.preview_stack, 1)
        self.focus_bar = QtWidgets.QWidget()
        focus_layout = QtWidgets.QHBoxLayout(self.focus_bar)
        focus_layout.setContentsMargins(0, 0, 0, 0)
        self.focused = QtWidgets.QCheckBox('聚焦遮罩区域重绘')
        self.focused.setToolTip('只发送遮罩周围的裁剪区域，完成后按遮罩合回原图；保留区域外像素。')
        focus_layout.addWidget(self.focused)
        focus_layout.addStretch()
        focus_layout.addWidget(QtWidgets.QLabel('边距'))
        from .references import guard_wheel
        self.padding = guard_wheel(QtWidgets.QSpinBox())
        self.padding.setRange(0, 2048)
        self.padding.setValue(64)
        self.padding.setSuffix(' px')
        focus_layout.addWidget(self.padding)
        center.addWidget(self.focus_bar)
        self.result_label = QtWidgets.QLabel('结果将自动保存到输出目录 / NovelAI')
        self.result_label.setObjectName('novelaiMuted')
        self.result_label.setTextFormat(QtCore.Qt.PlainText)
        self.result_label.setWordWrap(True)
        self.result_label.setMinimumWidth(0)
        self.result_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        result_bar = QtWidgets.QHBoxLayout()
        result_bar.addWidget(self.result_label, 1)
        self.result_previous = self._button('‹', lambda: self._turn_task_result(-1), '同一任务的上一张结果')
        self.result_next = self._button('›', lambda: self._turn_task_result(1), '同一任务的下一张结果')
        self.result_position = QtWidgets.QLabel()
        self.result_position.setObjectName('novelaiMuted')
        for widget in (self.result_previous, self.result_position, self.result_next):
            result_bar.addWidget(widget)
            widget.hide()
        self.seed_btn = self._button('应用种子', self.apply_selected_seed, '将此结果的实际种子回填到参数区')
        self.seed_btn.setAccessibleName('应用当前结果种子')
        self.seed_btn.hide()
        result_bar.addWidget(self.seed_btn)
        self.pin_btn = self._button('对比', self.toggle_comparison, '固定当前结果用于对比；再次点击关闭', True)
        self.pin_btn.hide()
        result_bar.addWidget(self.pin_btn)
        center.addLayout(result_bar)
        center.addWidget(self.source_label)
        center.addWidget(self.preview_toolbar)
        self.splitter.addWidget(self.workspace)
        self._history_dialog = QtWidgets.QDialog(self)
        self._history_dialog.setWindowTitle('NovelAI · 历史作品')
        self._history_dialog.resize(560, 620)
        history_layout = QtWidgets.QVBoxLayout(self._history_dialog)
        history_layout.setContentsMargins(0, 0, 0, 0)
        self.history = HistoryPanel(self.owner, self._history_dialog)
        history_layout.addWidget(self.history)
        self.history.selected.connect(self.select_result)
        self.history.selected.connect(self._history_dialog.hide)
        self.history.reuseRequested.connect(self.reuse_result)
        self.history.reuseRequested.connect(self._history_dialog.hide)
        self.history.useImageRequested.connect(lambda record: self.set_input(record.get('path', '')))
        self.history.useImageRequested.connect(self._history_dialog.hide)
        self.history.itemsChanged.connect(self._save_history)
        self.task_panel = TaskPanel(self.owner, self)
        self.task_panel.set_compact(True)
        self.task_panel.taskSelected.connect(self.select_task)
        self.task_panel.queueRequested.connect(self.show_queue)
        self.task_panel.copyRequested.connect(self.copy_task_to_page)
        self.task_panel.resultSelected.connect(self.select_result)
        self.task_panel.useImageRequested.connect(lambda record: self.set_input(record.get('path', '')))
        for panel in (self.history, self.task_panel):
            panel.reuseSettingsRequested.connect(self.reuse_parameters)
            panel.reuseSeedRequested.connect(self.reuse_seed)
            panel.reuseAllRequested.connect(lambda record: self.reuse_parameters(record, include_seed=True))
        self.splitter.addWidget(self.task_panel)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([400, 800, 140])
        outer.addWidget(self.splitter, 1)
        action_bar = QtWidgets.QFrame()
        action_bar.setObjectName('novelaiActionBar')
        footer = QtWidgets.QVBoxLayout(action_bar)
        footer.setContentsMargins(10, 7, 10, 8)
        footer.setSpacing(5)
        self.controls.quick_settings.hide()
        utilities = QtWidgets.QHBoxLayout()
        utilities.setSpacing(4)
        self.billing_hint = QtWidgets.QLabel('自动轮试')
        self.billing_hint.setTextFormat(QtCore.Qt.PlainText)
        self.billing_hint.setObjectName('novelaiMuted')
        self.billing_hint.setMinimumWidth(0)
        self.billing_hint.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        utilities.addWidget(self.billing_hint, 1)
        self.queue_btn = self._button('任务队列', self.show_queue, '查看本次会话的任务、结果与取消操作')
        utilities.addWidget(self.queue_btn)
        self.stop_btn = QtWidgets.QPushButton('停止')
        self.stop_btn.setObjectName('novelaiStop')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setToolTip('取消等待任务并停止本地等待；已提交的云端请求仍可能执行和计费。')
        utilities.addWidget(self.stop_btn)
        footer.addLayout(utilities)
        self.status = QtWidgets.QLabel('输入提示词后生成。' if token_for(self.owner) else '请先在连接设置中添加 API Token。')
        self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        self.status.setMaximumHeight(32)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.status.setObjectName('novelaiMuted')
        footer.addWidget(self.status)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setObjectName('novelaiGenerationProgress')
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        footer.addWidget(self.progress)
        self.resave_btn = QtWidgets.QPushButton('重新保存结果')
        self.resave_btn.clicked.connect(self.resave)
        self.resave_btn.hide()
        footer.addWidget(self.resave_btn)
        generate_row = QtWidgets.QHBoxLayout()
        generate_row.setSpacing(6)
        self.task_count = guard_wheel(QtWidgets.QSpinBox())
        self.task_count.setObjectName('novelaiTaskCount')
        self.task_count.setRange(1, 10)
        self.task_count.setValue(1)
        self.task_count.setPrefix('任务 × ')
        self.task_count.setAccessibleName('一次提交任务数量')
        self.task_count.setToolTip('一次加入 1–10 个独立任务；每个任务的出图张数由绘图参数决定。')
        self.task_count.setFixedWidth(103)
        generate_row.addWidget(self.task_count)
        self.run_btn = QtWidgets.QPushButton('生成图像')
        self.run_btn.setObjectName('novelaiPrimary')
        self.run_btn.setMinimumWidth(110)
        self.run_btn.setToolTip('按所选任务数量加入队列；点击后冷却 0.5 秒。')
        self.run_btn.clicked.connect(self.generate)
        generate_row.addWidget(self.run_btn, 1)
        footer.addLayout(generate_row)
        left.addWidget(action_bar)

    def _sync_panel_switch(self):
        self.parameters_btn.setChecked(not self.sidebar.isHidden())
        self.history_btn.setChecked(not self.task_panel.isHidden())
        self.workspace_switchbar.show()

    def _toggle_panel(self, panel, visible):
        self._panel_override = True
        target = self.sidebar if panel is self.controls else panel
        if visible and self.width() < 820:
            other = self.task_panel if target is self.sidebar else self.sidebar
            other.hide()
        target.setVisible(visible)
        self._sync_panel_switch()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, 'task_panel'):
            return
        if not self._panel_override:
            self.task_panel.setVisible(self.width() >= 900)
            self.sidebar.setVisible(self.width() >= 680)
        elif self.width() < 680 and not self.sidebar.isHidden() and not self.task_panel.isHidden():
            self.task_panel.hide()
        self._sync_panel_switch()
        if not self._splitter_user_sized:
            side = 400 if self.width() >= 1280 else 340 if self.width() >= 980 else 300
            self.splitter.setSizes([side, max(180, self.width() - side - 144), 140])
        self._update_workspace_layout()

    def eventFilter(self, watched, event):
        if (watched is getattr(self, 'workspace', None) and event.type() == QtCore.QEvent.Resize
                and hasattr(self, 'source_label')):
            self._update_workspace_layout()
        return super().eventFilter(watched, event)

    def _update_workspace_layout(self):
        compact = self.workspace.width() < 520
        self.edit_btn.setVisible(not compact)
        self.geometry_btn.setVisible(not compact)
        self._refresh_input_label()

    def status_message(self, text):
        if not self._closing:
            self.status.setText(str(text))
            self.status.setToolTip(str(text))

    def _mode_changed(self, *_):
        if hasattr(self, 'focus_bar'):
            self.focus_bar.setVisible(self.controls.action.currentData() == 'infill' and not self._position_editing)
            names = {'upscale': '放大图像', 'augment': '执行图像工具', 'infill': '局部重绘', 'img2img': '图生图'}
            queued = any(t['state'] not in ('succeeded', 'failed', 'cancelled') for t in self._queue.tasks)
            self.run_btn.setText('加入队列' if queued else names.get(self.controls.action.currentData(), '生成图像'))
            self._sync_result_actions()

    def _options(self):
        options = self.controls.settings()
        options.pop('prompt_suggestions', None)
        if options.get('action') != 'img2img':
            options.update(enhancement=False, upscaled_enhance=False)
        options.update(image_path=self._input, focused=self.focused.isChecked(),
                       focus_padding=self.padding.value(), chunks=copy.deepcopy(self._chunks))
        return options

    def _apply_settings(self, options):
        if not isinstance(options, dict):
            raise ValueError('参数必须是 JSON 对象。')
        for name, maximum in (('characters', catalog.MAX_STORED_CHARACTERS), ('references', 16)):
            values = options.get(name, [])
            if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, dict) for v in values):
                raise ValueError(f'{name} 格式无效或超过 {maximum} 项。')
        chunks = options.get('chunks', {})
        if not isinstance(chunks, dict) or len(chunks) > 500 or any(not isinstance(v, str) or len(v) > 30000 for v in chunks.values()):
            raise ValueError('提示词片段格式无效或数量过多。')
        options = copy.deepcopy(options)
        options.pop('prompt_suggestions', None)
        if options.get('seed') is None:
            options['seed'] = options.get('request_seed', -1)
        self.controls.set_settings(options)
        self._input = str(options.get('image_path') or '')
        self._chunks = options.get('chunks', {}) if isinstance(options.get('chunks', {}), dict) else {}
        self.focused.setChecked(bool(options.get('focused', False)))
        self.padding.setValue(int(options.get('focus_padding', 64)))
        self._mode_changed()
        self._editor_changed()

    def _editor_changed(self, *_):
        self._editor_revision += 1
        if not self._closing:
            self._billing_timer.start(120)
            if self._position_editing and not self._position_syncing:
                self._sync_position_editor()

    def _refresh_billing_hint(self):
        # Retain the former slot name for existing connections; no cost classification.
        if self._closing or not hasattr(self, 'billing_hint'):
            return
        keys = tokens_for(self.owner)
        self.billing_hint.setText(f'自动轮试 · {len(keys)} 个密钥' if keys else '请配置连接密钥')
        self.billing_hint.setToolTip('仅队首按连接顺序轮试密钥；请求被接受后，下一项才提交。已接受的任务可并行接收结果。')
        self.account_btn.setToolTip(f'管理 {len(keys)} 个密钥，查看全部账户与额度')
        self.account_strip.setToolTip('主页仅显示首个密钥的账户额度；连接设置可查看全部账户。')

    def _save_settings(self, *, prompt_suggestions=None):
        # Preserve unrelated settings, including values added since this page
        # was opened. Suggestion preferences never enter drawing snapshots.
        saved = copy.deepcopy(self._saved_settings)
        saved.update(storage.load_settings(self.data_dir))
        queue = saved.get('queue')
        queue = copy.deepcopy(queue) if isinstance(queue, dict) else {}
        queue.update(concurrency=self._queue.concurrency, retry_interval=self._queue.retry_interval)
        saved.update(options=self._options(), mask=self._saved_mask,
            queue=queue,
            prompt_suggestions=normalize_suggestion_preferences(
                self.prompt_suggestion_preferences if prompt_suggestions is None else prompt_suggestions))
        storage.save_settings(self.data_dir, saved)
        self._saved_settings = saved

    def open_preferences(self):
        SuggestionPreferencesDialog(self).exec_()

    def save_prompt_suggestion_preferences(self, values):
        values = normalize_suggestion_preferences(values)
        self._save_settings(prompt_suggestions=values)
        self.prompt_suggestion_preferences = values
        self._tag_suggestions.preferences_changed()

    def _save_history(self, items):
        try:
            current = {(v.get('id') or v.get('path')) for v in items}
            additions = [v for v in items if (v.get('id') or v.get('path')) not in self._history_ids]
            storage.update_history(self.data_dir, additions=additions, removed=self._history_ids - current)
            self._history_ids = current
        except (OSError, ValueError) as error:
            self.status_message('历史索引未能保存：' + str(error))

    def _queue_configuration_changed(self, *_):
        try:
            self._save_settings()
        except (OSError, ValueError) as error:
            self.status_message('队列设置本次会话已生效，但未能保存：' + str(error))

    def connection(self):
        if AccountDialog(self.owner, self).exec_():
            self._refresh_billing_hint()
            self._refresh_account(force=True)

    def _refresh_account(self, *, force=False):
        if not self._closing:
            self._account_monitor.refresh(token_for(self.owner), force=force)

    def _account_changed(self):
        if self._closing:
            return
        state = self._account_monitor
        token = token_for(self.owner)
        self._refresh_billing_hint()
        self.account_strip.render(state.data, status=state.status, busy=state.busy,
                                  updated_at=state.updated_at, error=state.error)
        if token:
            self.account_strip.tier.setText('默认账户 · ' + self.account_strip.tier.text())

    def _credentials_changed(self, path, records):
        expected = getattr(self.owner, '_apikeys_file', None) or os.path.join(current_dir, 'apikeys.json')
        if self._closing or os.path.normcase(os.path.abspath(str(path))) != os.path.normcase(os.path.abspath(str(expected))):
            return
        record = records.get('novelai', {}) if isinstance(records, dict) else {}
        # Save events may precede the dialog's owner update. Keep both views in sync.
        if not hasattr(self.owner, '_apikeys'):
            self.owner._apikeys = {}
        if tokens_from_record(record):
            self.owner._apikeys['novelai'] = copy.deepcopy(record)
        else:
            self.owner._apikeys.pop('novelai', None)
        self._refresh_billing_hint()
        self._account_monitor.refresh(token_for(self.owner))

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closing:
            self._quota_timer.start()
            QtCore.QTimer.singleShot(0, self._refresh_account)

    def hideEvent(self, event):
        self._quota_timer.stop()
        super().hideEvent(event)

    def choose_input(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '导入图像', '', '图像 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)')
        self.import_paths(paths)

    def import_paths(self, paths):
        if paths:
            self.import_image(paths[0], additional_paths=paths[1:])

    def import_image(self, path, *, parameters_only=False, additional_paths=()):
        try:
            values = storage.read_image_settings(path)
            if not values and parameters_only:
                self.status_message('图片没有可读取的 NovelAI 参数元数据。')
                return
            caps = catalog.capabilities(self.controls.model.currentData())
            action = self.controls.action.currentData()
            reference_caps = {}
            for family in ('vibe', 'precise'):
                allowed = bool(caps.get(family)) and action in ('generate', 'img2img', 'infill')
                if family == 'vibe' and action == 'infill':
                    allowed = False
                reference_caps[family] = allowed
                reference_caps[family + '_reason'] = ('请先选择 V4.5 模型' if not caps.get(family) else
                    '当前模式不支持此参考方式，请先切换为文生图或图生图')
            from .import_dialog import ImageImportDialog
            dialog = ImageImportDialog(path, values, self,
                mode=getattr(self.owner, '_theme_mode', 'dark'), actual_options=self._actual_options(values) if values else None,
                reference_capabilities=reference_caps, parameters_only=parameters_only,
                image_count=1 + len(additional_paths))
            if not dialog.exec_():
                return
            if dialog.result_action == 'image':
                self.set_input(path)
                return
            if dialog.result_action in ('vibe', 'precise'):
                self._import_references([path, *additional_paths], dialog.result_action)
                return
            options = self._options()
            options.update(dialog.selected_options())
            self._apply_settings(options)
            self._refresh_input_label()
            self.status_message('已导入选中的图片参数；当前底图与参考图保持不变。')
        except (OSError, ValueError, TypeError, OverflowError) as error:
            self.status_message('图片无法导入：' + str(error))

    def _import_references(self, paths, family):
        references = self.controls.references
        previous = references.family.currentData()
        if previous != family and references.value():
            result = QtWidgets.QMessageBox.question(self, '切换参考方式',
                '现有参考图将一并切换为' + ('氛围参考（Vibe）' if family == 'vibe' else '精确参考（Precise）') +
                '，保留图像和可用设置。是否继续？', QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if result != QtWidgets.QMessageBox.Yes:
                return
        references.family.setCurrentIndex(references.family.findData(family))
        count = len(references.value())
        references.add_paths(paths)
        self._toggle_panel(self.controls, True)
        self.controls.focus_section('references')
        added = len(references.value()) - count
        self.status_message(f'已添加 {added} 张参考图；底图保持不变，可在左侧调整参考强度。' if added else '没有添加新的参考图。')

    def set_input(self, path):
        if not path or not os.path.isfile(path):
            self.status_message('底图不存在，请重新选择。')
            return False
        try:
            from PIL import Image, ImageOps
            from .client import MAX_INPUT, MAX_PIXELS
            if Path(path).stat().st_size > MAX_INPUT:
                raise ValueError('输入图像超过 32 MB')
            with Image.open(path) as image:
                if image.width * image.height > MAX_PIXELS or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('请选择不超过 16777216 像素的静态图像。')
                width, height = image.size
                if image.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
                normalize = image.format not in ('PNG', 'JPEG', 'WEBP', 'BMP')
            with Image.open(path) as image:
                image.verify()
            if normalize:
                from aetherloom_core.mask_assets import store_asset
                with Image.open(path) as source:
                    normalized = ImageOps.exif_transpose(source).convert('RGBA')
                    try:
                        path = store_asset(normalized, Path(getattr(self.owner, 'input_dir', os.path.join(self.data_dir, 'input'))) / 'novelai')
                    finally:
                        normalized.close()
            self._editor_changed()
            self._input = os.path.abspath(path)
            self.controls._raw.update(enhancement=False, upscaled_enhance=False)
            self._mask = self._saved_mask = None
            self.controls._raw['mask_path'] = ''
            if self.controls.action.currentData() == 'generate':
                self.controls.action.setCurrentIndex(self.controls.action.findData('img2img'))
            # Keep the ratio and use legal 64px units without changing the source.
            factor = min(1, (1_048_576 / (width * height)) ** .5, 4096 / max(width, height))
            self.controls.width.setValue(max(64, round(width * factor / 64) * 64))
            self.controls.height.setValue(max(64, round(height * factor / 64) * 64))
            self._refresh_input_label()
            self.show_input()
            return True
        except (OSError, ValueError) as error:
            self.status_message(str(error))
            return False

    def _refresh_input_label(self):
        title = '底图：' + (Path(self._input).name if self._input else '未选择')
        self.source_label.setText(self.source_label.fontMetrics().elidedText(
            title, QtCore.Qt.ElideMiddle, max(120, self.workspace.width() - 16)))
        self.source_label.setToolTip(self._input)
        self.source_label.setVisible(bool(self._input) and not self._position_editing)
        path = str(self._preview_image_record().get('path', ''))
        enabled = bool(path and os.path.isfile(path)) and not self._position_editing
        self.edit_btn.setEnabled(enabled)
        self.geometry_btn.setEnabled(enabled)

    def toggle_position_editor(self):
        if self._position_editing:
            self.finish_position_editor()
        else:
            self.open_position_editor()

    def open_position_editor(self):
        if self._closing or self._position_editing:
            return
        state = self.controls.position_editor_state()
        if not state['enabled']:
            return
        self._position_baseline = copy.deepcopy(state)
        self._position_state = copy.deepcopy(state)
        self._position_editing = True
        self.controls.set_position_editing(True)
        self.position_editor.configure(state['characters'], state['size'], state['free_coordinates'])
        self._refresh_position_background()
        self.preview_stack.setCurrentWidget(self.position_editor)
        self._update_position_chrome()
        self.position_editor.focus_canvas()
        self.status_message('在中央画面拖动角色编号；坐标实时生效，完成定位后返回预览。')

    def _sync_position_editor(self):
        if not self._position_editing or self._position_syncing:
            return
        state = self.controls.position_editor_state()
        if not state['enabled']:
            self.finish_position_editor()
            return
        previous = self._position_state or {}
        index = self.position_editor.selected_index()
        previous_ids = previous.get('ids', [])
        selected_id = previous_ids[index] if 0 <= index < len(previous_ids) else None
        self._position_state = copy.deepcopy(state)
        self.position_editor.configure(state['characters'], state['size'], state['free_coordinates'])
        if selected_id in state['ids']:
            self.position_editor.select_character(state['ids'].index(selected_id))

    def _position_edited(self, index, x, y):
        if not self._position_editing or self._closing:
            return
        indices = (self._position_state or {}).get('indices', [])
        if not 0 <= index < len(indices):
            return
        self._position_syncing = True
        try:
            self.controls.set_character_position(indices[index], x, y)
            self._position_state = self.controls.position_editor_state()
        finally:
            self._position_syncing = False

    def _refresh_position_background(self):
        if self._position_editing and not self._closing:
            self.position_editor.set_background(self.preview.snapshot_pixmap())

    def _update_position_chrome(self):
        self.preview_toolbar.setVisible(not self._position_editing)
        self.source_label.setVisible(bool(self._input) and not self._position_editing)
        self.fit_btn.setEnabled(not self._position_editing)
        self.tools_btn.setEnabled(not self._position_editing)
        self.result_label.setVisible(not self._position_editing)
        self._refresh_input_label()
        self._mode_changed()
        self._update_result_navigation()

    def finish_position_editor(self):
        if not self._position_editing:
            return
        self._position_editing = False
        self._position_baseline = self._position_state = None
        self.controls.set_position_editing(False)
        self.preview_stack.setCurrentWidget(self.view_splitter)
        self.position_editor.set_background(QtGui.QPixmap())
        self._update_position_chrome()
        self.status_message('已结束角色定位，当前坐标用于下一次生成。')

    def cancel_position_editor(self):
        if not self._position_editing:
            return
        baseline = self._position_baseline
        self.finish_position_editor()
        # Roles may have been reordered, removed or replaced while editing.
        # Only restore coordinates on the same surviving role objects.
        self.controls.restore_character_positions(baseline)
        self.status_message('已取消本次定位；提示词和其他参数修改保持不变。')

    def show_history(self):
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def show_input(self):
        if not self._previewing_input:
            self._result_view_context = {
                'record': copy.deepcopy(self._selected), 'results': copy.deepcopy(self._task_results),
                'index': self._task_result_index,
                'task_id': self._preview_task_id if self._follow_queue_preview else None,
            }
        self._follow_queue_preview = False
        self.task_panel.select_task(None)
        self._previewing_input = True
        self._update_result_navigation()
        if self._input:
            self.result_label.setText('当前底图')
            self.result_label.setToolTip(self._input)
            self.preview.load_path(self._input, self._mask)
        else:
            self.result_label.setText('结果将自动保存到输出目录 / NovelAI')
            self.result_label.setToolTip('')
            self.preview.set_empty()

    def pin_result(self):
        path = str(self._preview_image_record().get('path', ''))
        if os.path.isfile(path):
            self.comparison.show()
            self.comparison.load_path(path)
            self.pin_btn.setChecked(True)
            self.view_splitter.setSizes([500, 500])

    def close_comparison(self):
        self.comparison.hide()
        self.pin_btn.setChecked(False)
        self._sync_result_actions()

    def toggle_comparison(self, checked):
        if checked:
            self.pin_result()
        else:
            self.close_comparison()

    @staticmethod
    def _record_seed(record):
        value = record.get('seed')
        if value is None and isinstance(record.get('settings'), dict):
            value = record['settings'].get('seed')
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4294967295 else None

    def _preview_image_record(self):
        """Image actions follow the displayed input/result, not a stale result."""
        if self._previewing_input:
            return {'path': self._input} if self._input else {}
        return self._selected or {}

    def _use_preview_input(self, path):
        if not path or not os.path.isfile(path):
            self.status_message('当前图像不存在，请重新选择。')
            return False
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(self._input or '.')):
            return self.set_input(path)
        # Changing the processing mode must not erase this input's mask,
        # painting, or user-selected output size.
        self.show_input()
        return True

    def _sync_result_actions(self):
        context = self._result_view_context or {}
        has_result = bool(self._selected or context.get('record') or context.get('task_id')
                          or self._follow_queue_preview and self._preview_task_id)
        self.result_view_btn.setEnabled(has_result and not self._position_editing)
        self.input_view_btn.setEnabled(bool(self._input and os.path.isfile(self._input)) and not self._position_editing)
        self.result_view_btn.setChecked(not self._previewing_input)
        self.input_view_btn.setChecked(self._previewing_input)
        if self._position_editing:
            self.result_actions.hide()
            self.seed_btn.hide()
            self.pin_btn.hide()
            return
        record = self._selected if not self._previewing_input else None
        seed = self._record_seed(record) if record else None
        self.seed_btn.setVisible(seed is not None)
        if seed is not None:
            self.seed_btn.setText(f'种子 {seed} ↗')
        image = self._preview_image_record()
        available = bool(image and os.path.isfile(str(image.get('path', ''))))
        self.result_actions.show()
        mode = self.controls.action.currentData()
        enhanced = mode == 'img2img' and bool(self.controls._raw.get('enhancement'))
        for action in self.result_actions.actions():
            supported = action.data() != 'infill' or bool(self.controls._caps.get('infill'))
            action.setEnabled(available and supported)
            action.setToolTip(action.property('normalToolTip') if supported else
                              '当前模型不支持局部重绘，请选择 V5 Full 或 V4.5 模型。')
            if action.isCheckable():
                selected = (action.data() == ('enhance' if enhanced else mode)) and self._previewing_input
                action.setChecked(selected and available)
        self.pin_btn.setVisible(available or not self.comparison.isHidden())
        self.pin_btn.setEnabled(available or self.pin_btn.isChecked())
        self._refresh_input_label()

    def apply_selected_seed(self):
        if self._selected and not self._previewing_input:
            self.reuse_seed(self._selected)


    def show_result(self):
        context = self._result_view_context if self._previewing_input else None
        if context:
            task = self._queue.get_task(context.get('task_id')) if context.get('task_id') else None
            index = context.get('index', 0)
            if task and task.get('accepted') and task['state'] not in ('failed', 'cancelled'):
                self.select_task(task['id'])
                if self._task_results:
                    self._turn_task_result(index - self._task_result_index)
                return
            if context.get('record'):
                self._follow_queue_preview = False
                self._task_results = copy.deepcopy(context.get('results') or [])
                self._task_result_index = max(0, min(index, len(self._task_results) - 1))
                self._display_record(context['record'])
                self._update_result_navigation()
                self._result_view_context = None
                return
            self._result_view_context = None
            self.status_message('之前查看的任务已停止；可在任务队列中查看详情。')
        if self._selected:
            self.select_result(self._selected)
        else:
            self._sync_result_actions()

    def clear_input(self):
        self._editor_changed()
        self._input = ''
        self._mask = self._saved_mask = None
        self.controls._raw['mask_path'] = ''
        self.controls._raw.update(enhancement=False, upscaled_enhance=False)
        self.controls.action.setCurrentIndex(self.controls.action.findData('generate'))
        self._refresh_input_label()
        if self._previewing_input:
            self.show_result()
            if self._previewing_input:
                self.show_input()
        self._sync_result_actions()

    def edit_mask(self):
        path = str(self._preview_image_record().get('path', ''))
        if not path:
            return
        from aetherloom_core.mask_editor import edit_mask
        from aetherloom_core import mask_assets
        try:
            draft = self._mask if mask_assets.matches(self._mask, path) else None
            result = edit_mask(path, self, draft)
            if result is not None:
                if not self._use_preview_input(path):
                    return
                self._mask = result
                self._editor_changed()
                self.show_input()
                with mask_assets.read(result) as mask:
                    has_mask = mask.getbbox() is not None
                if has_mask and catalog.capabilities(self.controls.model.currentData())['infill']:
                    self.controls.action.setCurrentIndex(self.controls.action.findData('infill'))
                    self.status_message('遮罩已暂存，已选择局部重绘；点击生成后才会提交。')
                elif has_mask:
                    self.status_message('遮罩已暂存；当前模型不支持局部重绘，请选择 V5 Full 或 V4.5 模型。')
                else:
                    self.status_message('绘制已暂存，点击生成后保存输入素材并提交。')
        except (OSError, ValueError) as error:
            self.status_message(str(error))

    def geometry(self):
        path = str(self._preview_image_record().get('path', ''))
        if not path:
            return
        from .image_tools import GeometryDialog
        try:
            dialog = GeometryDialog(path, getattr(self.owner, 'input_dir', os.path.join(self.data_dir, 'input')), self)
            if dialog.exec_() and dialog.result_path:
                if not self.set_input(dialog.result_path):
                    return
                if dialog.mode.currentData() == 'expand':
                    if catalog.capabilities(self.controls.model.currentData())['infill']:
                        self.controls.action.setCurrentIndex(self.controls.action.findData('infill'))
                        self.status_message('已切换到局部重绘，透明扩展区域会作为重绘遮罩。')
                    else:
                        self.status_message('已载入扩展图像；当前模型不支持局部重绘，请先选择 V5 Full 或 V4.5 模型。')
        except (OSError, ValueError) as error:
            self.status_message(str(error))

    def select_result(self, record):
        self._result_view_context = None
        self._follow_queue_preview = False
        self.task_panel.select_task(None)
        self._task_results = []
        self._update_result_navigation()
        self._display_record(record)

    def _display_record(self, record):
        self._selected = copy.deepcopy(record)
        self._previewing_input = False
        path = str(record.get('path', ''))
        # Never leave another task's image visible while opening a missing result.
        self.preview.set_empty('正在读取已保存结果…' if os.path.isfile(path) else '此结果文件已移动或删除，可选择此任务的其他结果。')
        if os.path.isfile(path):
            self.preview.load_path(path)
        self.result_label.setText(f'{record.get("width", "?")} × {record.get("height", "?")}')
        self.result_label.setToolTip(path)
        self._sync_result_actions()

    def _update_result_navigation(self):
        self._sync_result_actions()
        count = len(self._task_results)
        for widget in (self.result_previous, self.result_position, self.result_next):
            widget.setVisible(count > 1 and not self._position_editing and not self._previewing_input)
        self.result_position.setText(f'{self._task_result_index + 1} / {count}' if count else '')
        self.result_previous.setEnabled(self._task_result_index > 0)
        self.result_next.setEnabled(self._task_result_index + 1 < count)

    def _turn_task_result(self, direction):
        if not self._task_results:
            return
        self._task_result_index = max(0, min(self._task_result_index + direction, len(self._task_results) - 1))
        self._display_record(self._task_results[self._task_result_index])
        self._update_result_navigation()

    def select_task(self, task_id):
        task = self._queue.get_task(task_id)
        if not task or not task.get('accepted') or task['state'] in ('failed', 'cancelled'):
            return
        self._result_view_context = None
        self._follow_queue_preview = True
        self._preview_task_id = self._displayed_task_id = task_id
        self._displayed_task_state = task['state']
        self.task_panel.select_task(task_id)
        self._previewing_input = False
        self._task_results = copy.deepcopy(task.get('results') or [])
        self._task_result_index = next((i for i, record in enumerate(self._task_results)
                                        if os.path.isfile(str(record.get('path', '')))), 0)
        if self._task_results:
            self._display_record(self._task_results[self._task_result_index])
        else:
            self._selected = None
            message = task.get('message') or '任务已受理，正在生成…'
            self.preview.set_empty(message)
            raw = self._stream_previews.get(task_id) if task['state'] == 'running' else None
            if raw:
                self.preview.set_bytes(raw)
            self.result_label.setText(f'任务 #{task["index"]} · {message}')
            self.result_label.setToolTip('')
        self._update_result_navigation()

    def reuse_seed(self, record):
        seed = self._record_seed(record)
        if seed is None:
            self.status_message('此结果未提供可复用的实际种子。')
            return
        self.controls.seed.setText(str(seed))
        self._editor_changed()
        self._history_dialog.hide()
        self.status_message(f'已应用种子 {seed}，其他参数保持不变。')

    def reuse_parameters(self, record, *, include_seed=False):
        try:
            values = storage.import_options(record.get('settings', {}))
            # Official quick reuse keeps the current input assets and local execution preferences.
            protected = {'seed', 'request_seed', 'action', 'image_path', 'mask_path', 'references',
                         'focused', 'focus_padding', 'timeout', 'stream'}
            patch = {key: value for key, value in values.items()
                     if key not in protected and not key.startswith('resolved_')}
            options = self._options()
            options.update(patch)
            seed = self._record_seed(record)
            if include_seed and seed is not None:
                options['seed'] = seed
            self._apply_settings(options)
            self._refresh_input_label()
            self._history_dialog.hide()
            message = '已复用参数与种子。' if include_seed and seed is not None else '已复用参数，保留当前种子。'
            self.status_message(message + '当前底图与参考图保持不变。')
        except (ValueError, TypeError, OverflowError) as error:
            self.status_message('参数无法复用：' + str(error))

    def reuse_result(self, record):
        try:
            self._apply_settings(record.get('settings', {}))
            self._mask = self._saved_mask = record.get('settings', {}).get('mask_reference')
            self._refresh_input_label()
            self.select_result(record)
            self.status_message('已复用参数；随机词将在下次生成时重新抽取，可从“更多”复用实际提示词。')
        except (ValueError, TypeError) as error:
            self.status_message('参数无法复用：' + str(error))

    def result_to_input(self):
        self.prepare_result_action('img2img')

    def prepare_result_action(self, action):
        path = str(self._preview_image_record().get('path', ''))
        if not path or self._position_editing:
            return
        if action == 'infill' and not catalog.capabilities(self.controls.model.currentData())['infill']:
            self.status_message('当前模型不支持局部重绘，请先选择 V5 Full 或 V4.5 模型。')
            return
        if not self._use_preview_input(path):
            return
        self.controls._raw.update(enhancement=False, upscaled_enhance=False)
        self.controls.action.setCurrentIndex(self.controls.action.findData(action))
        self._editor_changed()
        self._sync_result_actions()
        self._toggle_panel(self.controls, True)
        if action == 'infill':
            self.edit_mask()
        self.status_message('已准备当前图像；调整左侧设置并点击生成后提交。')

    def enhance(self):
        path = str(self._preview_image_record().get('path', ''))
        if not path or self._position_editing:
            return
        from .enhancement import EnhancementDialog
        try:
            dialog = EnhancementDialog(path, self.controls.model.currentData(),
                                       self, values=self._enhancement_options,
                                       mode=getattr(self.owner, '_theme_mode', 'dark'))
            if not dialog.exec_():
                return
            changes = dialog.options()
            options = self._options()
            if not self._use_preview_input(path):
                return
            self._enhancement_options = dialog.preferences()
            options.update(changes)
            options.update(image_path=self._input, mask_path='', seed=-1, n_samples=1,
                           action='img2img', enhancement=True)
            self._apply_settings(options)
            self._toggle_panel(self.controls, True)
            self.controls.focus_section('image')
            self.status_message('已准备增强参数，保留当前提示词与参考图；点击图生图后提交。')
        except (OSError, ValueError) as error:
            self.status_message('无法增强此结果：' + str(error))

    def reuse_actual(self):
        if not self._selected:
            return
        values = self._actual_options(self._selected.get('settings', {}))
        options = self._options()
        reset_positions = False
        for key in ('prompt', 'negative_prompt', 'quality_preset', 'uc_preset', 'chunks'):
            if key in values:
                options[key] = copy.deepcopy(values[key])
        if 'characters' in values:
            current = options.get('characters', [])
            manual = self.controls.position_mode.mode()
            if manual is None:
                manual = bool(current and current[0].get('use_coords'))
            characters = []
            for index, source in enumerate(values['characters']):
                character = copy.deepcopy(current[index]) if index < len(current) else dict(x=.5, y=.5, use_coords=manual)
                character.update(prompt=source.get('prompt', ''), negative_prompt=source.get('negative_prompt', ''))
                character['enabled'] = source.get('enabled', True)
                character['name'] = source.get('name', '')
                character['use_coords'] = bool(manual)
                if (manual and character['enabled'] and index < len(current)
                        and not current[index].get('enabled', True)
                        and not catalog.capabilities(options['model'])['free_coordinates']):
                    # A disabled V5 draft may retain coordinates unavailable to
                    # V4.5. Reusing another result must not enable that invalid
                    # position silently; newly enabled roles start at the center.
                    for axis in ('x', 'y'):
                        if not any(abs(float(character.get(axis, .5)) - value) < 1e-6
                                   for value in (.1, .3, .5, .7, .9)):
                            character[axis] = .5
                            reset_positions = True
                characters.append(character)
            options['characters'] = characters
        self._apply_settings(options)
        self._refresh_input_label()
        message = '已复用实际提示词；模型、尺寸、种子和输入素材保持不变，自动质量与负面预设已关闭。'
        if reset_positions:
            message += ' 新启用角色中不适用于当前模型的位置已设为居中。'
        self.status_message(message)

    @staticmethod
    def _actual_options(settings):
        values = copy.deepcopy(settings)
        for name in ('prompt', 'negative_prompt'):
            if 'resolved_' + name in settings:
                values[name] = settings['resolved_' + name]
        raw_characters = settings.get('resolved_characters')
        if isinstance(raw_characters, list):
            from .task_records import merge_resolved_characters
            values['characters'] = merge_resolved_characters(
                values.get('characters', []), raw_characters,
                settings.get('resolved_negative_characters', []))
        keep_chunks = any(isinstance(c, dict) and not c.get('enabled', True)
                          for c in values.get('characters', []))
        values.update(quality_preset='none', uc_preset='none',
                      chunks=values.get('chunks', {}) if keep_chunks else {})
        return values

    def edit_chunks(self):
        dialog = ChunksDialog(self._chunks, self)
        if dialog.exec_():
            self._chunks = dialog.chunks
            self._editor_changed()

    def tags(self, editor=None):
        from PyQt5 import sip
        if not isinstance(editor, QtWidgets.QTextEdit):
            editor = self.controls.active_prompt_editor()
        if editor is None or sip.isdeleted(editor):
            return
        if not self.prompt_suggestion_preferences['online']:
            if not self.prompt_suggestion_preferences['local']:
                self.status_message('提示词候选已全部关闭，可在 NovelAI「设置」中重新开启。')
                return
            editor.setFocus(QtCore.Qt.OtherFocusReason)
            editor.show_suggestions()
            return
        # Capture the target before the modal takes focus. Negative and character
        # editors must receive their own tags; QTextCursor handles UTF-16 offsets.
        cursor = editor.textCursor()
        query = cursor.selectedText() or editor._get_prefix_before_cursor()
        token = token_for(self.owner)
        if not token:
            self.connection()
            token = token_for(self.owner)
        if not token:
            return
        dialog = TagsDialog(token, self.controls.model.currentData(), self,
                            dataset_mode=self.controls.dataset_mode.currentData())
        dialog.query.setText(query)
        if dialog.exec_() and dialog.selected_tag and not sip.isdeleted(editor):
            editor.setTextCursor(cursor)
            editor.insert_tag(dialog.selected_tag)

    def import_preset(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '导入绘图参数', '', 'JSON (*.json)')
        if not path:
            return
        try:
            values = storage.import_options(storage.read_json(path, None))
            self._apply_settings(values)
            self._mask = self._saved_mask = None
            self._refresh_input_label()
            self.status_message('已导入参数；底图、参考图需重新选择，避免上传文件中指定的本地路径。')
        except (OSError, ValueError, TypeError) as error:
            self.status_message(str(error))

    def export_preset(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出绘图参数', 'NovelAI-settings.json', 'JSON (*.json)')
        if path:
            try:
                storage.atomic_json(path, dict(format='aetherloom-novelai', version=1, settings=storage.import_options(self._options())))
            except (OSError, ValueError) as error:
                self.status_message(str(error))

    def import_metadata(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '从图片读取参数', '', '图像 (*.png *.webp *.jpg *.jpeg)')
        if path:
            self.import_image(path, parameters_only=True)

    def _update_submit_button(self):
        self.run_btn.setEnabled(not self._closing and not self._submitting
                                and not self._submit_cooldown.isActive())

    def generate(self):
        # Guard the handler as well as the button, including modal event loops.
        if self._closing or self._submitting or self._submit_cooldown.isActive():
            return
        self._submitting = True
        self._submit_cooldown.start()
        self._update_submit_button()
        try:
            keys = tokens_for(self.owner)
            if not keys:
                self.connection()
                keys = tokens_for(self.owner)
            if self._closing or not keys:
                return
            submitted = []
            error_text = ''
            try:
                self.task_count.interpretText()
                count = self.task_count.value()
                snapshot = copy.deepcopy(self._options())
                draft = copy.deepcopy(self._mask)
                revision = self._editor_revision
                output_dir = str(getattr(self.owner, 'output_dir', os.path.join(self.data_dir, 'output')))
                input_dir = str(getattr(self.owner, 'input_dir', os.path.join(self.data_dir, 'input')))
                batch_id = uuid.uuid4().hex
                for batch_index in range(1, count + 1):
                    task_id = self._queue.enqueue(snapshot, draft, keys, output_dir, input_dir, self.data_dir,
                        batch={'id': batch_id, 'index': batch_index, 'count': count},
                        source={'type': 'novelai_page', 'name': 'NovelAI'})
                    submitted.append(task_id)
                    self._enqueued_revisions[task_id] = revision
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                error_text = str(error)
            if not submitted:
                self.status_message('未能加入队列：' + error_text)
                return
            self._follow_queue_preview = True
            self._preview_task_id = submitted[0]
            self._queue_changed()
            first = self._queue.get_task(submitted[0])['index']
            last = self._queue.get_task(submitted[-1])['index']
            label = f'任务 #{first}' if len(submitted) == 1 else f'{len(submitted)} 个任务（#{first}–#{last}）'
            message = label + ' 已加入队列，参数与输入将独立保留。'
            if error_text:
                message += '其余任务未入队：' + error_text
            try:
                self._save_settings()
            except (OSError, ValueError) as error:
                message += '界面参数未能保存：' + str(error)
            self.status_message(message)
        finally:
            self._submitting = False
            self._update_submit_button()

    def show_queue(self):
        if self._queue_dialog is None:
            self._queue_dialog = QueueDialog(self._queue, self,
                mode=getattr(self.owner, '_theme_mode', 'dark'))
            self._queue_dialog.selected.connect(self.select_result)
            self._queue_dialog.copyRequested.connect(self.copy_task_to_page)
            self._queue_dialog.configurationChanged.connect(self._queue_configuration_changed)
        self._queue_dialog.show()
        self._queue_dialog.raise_()
        self._queue_dialog.activateWindow()

    def copy_task_to_page(self, task_id):
        try:
            copied = self._queue.copy_task(task_id)
            options = copied.get('options')
            if not isinstance(options, dict):
                raise ValueError('任务没有可复用的绘图参数。')
            self._apply_settings(options)
            self._mask = copy.deepcopy(copied.get('mask'))
            self._saved_mask = (copy.deepcopy(self._mask) if isinstance(self._mask, dict)
                                and not any(self._mask.get(key) for key in ('png', 'paint_png')) else None)
            self.task_count.setValue(1)
            self._follow_queue_preview = False
            self.task_panel.select_task(None)
            self._task_results = []
            self._update_result_navigation()
            self._refresh_input_label()
            task = self._queue.get_task(task_id) or {}
            result = next((record for record in task.get('results', [])
                           if isinstance(record, dict) and os.path.isfile(record.get('path') or '')), None)
            if self._input and os.path.isfile(self._input):
                self.show_input()
            elif result is not None:
                self.select_result(result)
            else:
                self._selected = None
                self._sync_result_actions()
                self.preview.set_empty('已复制任务参数；点击生成后再次请求')
                self.result_label.setText('结果将自动保存到输出目录 / NovelAI')
                self.result_label.setToolTip('')
            self.parameters_btn.setChecked(True)
            self._toggle_panel(self.controls, True)
            if self._queue_dialog is not None:
                self._queue_dialog.hide()
            navigation = getattr(self.owner, 'novelai_btn', None)
            if isinstance(navigation, QtWidgets.QAbstractButton):
                navigation.click()
            self.controls.prompt.setFocus(QtCore.Qt.OtherFocusReason)
            missing = []
            paths = [self._input, options.get('mask_path', '')]
            paths.extend(item.get('path', '') for item in options.get('references', []) if isinstance(item, dict) and item.get('enabled', True))
            if isinstance(self._mask, dict):
                paths.extend(self._mask.get(field, '') for field, inline in
                             (('path', 'png'), ('paint_path', 'paint_png')) if not self._mask.get(inline))
            for path in paths:
                if path and not os.path.isfile(path):
                    missing.append(path)
            message = '已将此任务复制到主页，任务数量设为 1；点击生成后才会再次提交。'
            if missing:
                message += f'有 {len(set(missing))} 个输入文件已丢失，请重新选择。'
            self.status_message(message)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self.status_message('任务无法复制到主页：' + str(error))

    def _queue_changed(self):
        if self._closing:
            return
        self._job = self._queue.active_job
        tasks = self._queue.tasks
        active = [t for t in tasks if t['state'] not in ('succeeded', 'failed', 'cancelled')]
        waiting = sum(t['state'] in ('queued', 'preparing', 'ready', 'retry_wait')
                      or (t['state'] == 'running' and not t.get('accepted')) for t in active)
        self.queue_btn.setText(f'任务队列 · {len(active)}' if active else '任务队列')
        self.queue_btn.setToolTip(f'执行 {self._queue.active_count} 项 · 等待 {waiting} 项 · 共 {len(tasks)} 项；点击查看轮试状态或设置并发')
        self.stop_btn.setEnabled(any(t['state'] not in ('save_failed', 'saving', 'canceling') for t in active))
        self.progress.setVisible(self._queue.active_job is not None or any(t['state'] == 'preparing' for t in tasks))
        self.resave_btn.setVisible(self._queue.has_unsaved)
        self.resave_btn.setEnabled(self._queue.active_job is None)
        self._update_submit_button()
        self._mode_changed()
        known = {t['id'] for t in tasks}
        self._refreshed_tasks.intersection_update(known)
        unfinished = {t['id'] for t in active}
        self.task_panel.set_tasks(tasks)
        for identity in list(self._stream_previews):
            task = self._queue.get_task(identity)
            if not task or task['state'] != 'running':
                self._stream_previews.pop(identity, None)
        # A clicked task remains selected after completion, even while others run.
        if self._follow_queue_preview:
            if self._preview_task_id is None:
                self._preview_task_id = next((t['id'] for t in tasks
                    if t.get('accepted') and t['state'] in ('running', 'saving')), None)
            task = self._queue.get_task(self._preview_task_id)
            if task and task.get('accepted') and task['state'] not in ('failed', 'cancelled'):
                if (self._displayed_task_id != task['id']
                        or (self._displayed_task_state != task['state']
                            and task['state'] in ('saving', 'save_failed', 'canceling'))):
                    self.select_task(task['id'])
            elif task and task['state'] in ('failed', 'cancelled'):
                if self._displayed_task_id != task['id'] or self._displayed_task_state != task['state']:
                    self._displayed_task_id, self._displayed_task_state = task['id'], task['state']
                    self._selected = None
                    self._task_results = []
                    self._update_result_navigation()
                    self.preview.set_empty(task.get('message') or '此任务已停止，可在任务队列中查看详情。')
                    self.result_label.setText(f'任务 #{task["index"]} · 已停止')
                    self.result_label.setToolTip('')
        # Completed handlers run before the terminal changed notification.
        for task_id in list(self._enqueued_revisions):
            if task_id not in unfinished:
                self._enqueued_revisions.pop(task_id, None)

    def _queue_progress(self, task_id, value):
        if self._closing:
            return
        task = self._queue.get_task(task_id)
        if task and task['state'] in ('preparing', 'running', 'saving') and (
                self._preview_task_id in (None, task_id)):
            message = value.get('message', '正在生成…') if isinstance(value, dict) else str(value)
            self.status_message(f'任务 #{task["index"]} · {message}')
            if self._follow_queue_preview and self._preview_task_id == task_id and not self._task_results:
                self.result_label.setText(f'任务 #{task["index"]} · {message}')

    def _queue_preview(self, task_id, value):
        task = self._queue.get_task(task_id)
        if (self._closing or not task or not task.get('accepted')
                or task['state'] != 'running' or not isinstance(value, dict)):
            return
        raw = value.get('bytes')
        if not isinstance(raw, bytes) or not raw:
            return
        # Keep only recent stream frames; completed results are read from disk.
        self._stream_previews.pop(task_id, None)
        if len(raw) <= 8 * 1024 * 1024:
            self._stream_previews[task_id] = raw
        while len(self._stream_previews) > 8 or sum(map(len, self._stream_previews.values())) > 16 * 1024 * 1024:
            self._stream_previews.popitem(last=False)
        self.task_panel.set_preview(task_id, value)
        if self._preview_task_id == task_id and self._follow_queue_preview:
            self._selected = None
            self._previewing_input = False
            self._sync_result_actions()
            self.preview.set_bytes(raw)

    def _refresh_after_task(self, task_id):
        task = self._queue.get_task(task_id)
        if task and task.get('submitted') and task_id not in self._refreshed_tasks:
            self._refreshed_tasks.add(task_id)
            self._refresh_account(force=True)

    def _queue_completed(self, task_id, value):
        if self._closing:
            return
        warning = value.get('warning', '')
        mask = value.get('mask')
        if mask and self._enqueued_revisions.get(task_id) == self._editor_revision:
            self._mask = self._saved_mask = mask
            try:
                self._save_settings()
            except (OSError, ValueError) as error:
                warning = (warning + '\n' if warning else '') + '图片已保存，但遮罩设置未能保存：' + str(error)
        records = value.get('records', [])
        self.history.add_items(records)
        if records and self._follow_queue_preview and self._preview_task_id in (None, task_id):
            self.select_task(task_id)
        task = self._queue.get_task(task_id)
        self.status_message(warning or f'任务 #{task["index"]} 已保存 {len(records)} 张图片。')
        self._refresh_after_task(task_id)

    def _queue_failed(self, task_id, error):
        if self._closing:
            return
        if isinstance(error, storage.SaveError):
            self.history.add_items(error.saved)
            if self._follow_queue_preview and self._preview_task_id == task_id:
                self.select_task(task_id)
            self.status_message('图片保存失败，队列已暂停；请重新保存结果：' + str(error))
        else:
            task = self._queue.get_task(task_id)
            self.status_message(f'任务 #{task["index"]} · {error}')
        self._refresh_after_task(task_id)

    def stop(self):
        self._queue.cancel_all()
        self.status_message('已取消等待任务，并请求停止当前本地等待；已提交的云端任务仍可能执行和计费。')

    def resave(self):
        task = next((t for t in self._queue.tasks if t['state'] == 'save_failed'), None)
        if task is None:
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, '重新保存已生成图片')
        if directory:
            try:
                self._queue.resave(task['id'], directory)
            except (OSError, ValueError, RuntimeError) as error:
                self.status_message('重新保存未能开始：' + str(error))

    def apply_theme(self):
        mode = getattr(self.owner, '_theme_mode', 'dark')
        self.setStyleSheet(page_stylesheet(mode))
        self.account_strip.apply_theme(mode)
        self.controls.apply_theme(mode)
        self.position_editor.apply_theme(mode)
        self.preview.apply_theme(mode)
        self.comparison.apply_theme(mode)
        self.history.apply_theme(mode)
        self.task_panel.apply_theme(mode)
        self._history_dialog.setStyleSheet(page_stylesheet(mode))
        if self._queue_dialog is not None:
            self._queue_dialog.apply_theme(mode)

    def can_close(self):
        if not self._queue.has_unsaved:
            return True
        prompt = QtWidgets.QMessageBox(self)
        prompt.setWindowTitle('还有尚未保存的 NovelAI 图片')
        prompt.setIcon(QtWidgets.QMessageBox.Warning)
        prompt.setText('部分图片已生成，但尚未成功保存。')
        prompt.setInformativeText('退出会丢失这些图片。可以返回任务队列，重新选择目录保存，无需再次生成。')
        keep = prompt.addButton('返回保存', QtWidgets.QMessageBox.RejectRole)
        discard = prompt.addButton('放弃图片并退出', QtWidgets.QMessageBox.DestructiveRole)
        prompt.setDefaultButton(keep)
        prompt.exec_()
        if prompt.clickedButton() is discard:
            return True
        self.show_queue()
        return False

    def shutdown(self):
        self._closing = True
        self._tag_suggestions.close()
        self._position_editing = False
        self._position_baseline = self._position_state = None
        self.position_editor.set_background(QtGui.QPixmap())
        self._submit_cooldown.stop()
        self._update_submit_button()
        self._quota_timer.stop()
        self._account_monitor.shutdown()
        self._billing_timer.stop()
        self._queue.shutdown()
        try:
            self._save_settings()
        except (OSError, ValueError):
            pass
        if self._queue_dialog is not None:
            self._queue_dialog.close()
        self._history_dialog.close()
        self.history.shutdown()
        self.task_panel.shutdown()
        self._stream_previews.clear()
        self.preview.shutdown()
        self.comparison.shutdown()
