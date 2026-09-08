"""Local decoding workspace; existing preview and worker handlers remain shared."""
from functools import partial

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_parameters import RhEnumComboBox, RhNumberSpinBox
from aetherloom_core.paths import current_dir
from pathlib import Path
from aetherloom_core.ui.responsive import make_responsive
from aetherloom_core.ui.widgets import DropLabel, DropListWidget


class LogEmitter(QtCore.QObject):
    sig = QtCore.pyqtSignal(str)


class DecodePage(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.running = False
        self.canceling = False
        self.setObjectName('decodePage')
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(14)
        heading = QtWidgets.QHBoxLayout()
        title_column = QtWidgets.QVBoxLayout()
        title_column.addWidget(self.label('本地解码', 'decodeTitle'))
        title_column.addWidget(self.label('导入素材，选择解码方式，对比并保存结果。', 'decodeMuted'))
        heading.addLayout(title_column, 1)
        self.import_button = self.button('导入素材', self.import_files)
        self.import_button.setObjectName('decodePrimary')
        heading.addWidget(self.import_button)
        outer.addLayout(heading)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        sidebar = QtWidgets.QWidget()
        sidebar.setObjectName('decodeSidebar')
        sidebar.setMinimumWidth(290)
        left = QtWidgets.QVBoxLayout(sidebar)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(12)
        card, layout = self.card('待解码素材')
        self.count_label = self.label('共 0 项 · 已选 0 项', 'decodeMuted')
        layout.addWidget(self.count_label)
        owner.file_list = DropListWidget()
        owner.file_list.setIconSize(QtCore.QSize(88, 88))
        owner._file_list_icon_base = 88
        owner.file_list.setSpacing(5)
        owner.file_list.setUniformItemSizes(True)
        owner.file_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        owner.file_list.setMinimumSize(0, 190)
        owner.file_list.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        owner.file_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        owner.file_list.setTextElideMode(QtCore.Qt.ElideMiddle)
        owner.file_list.verticalScrollBar().valueChanged.connect(partial(owner._on_list_scrolled, owner.file_list))
        owner.file_list.viewport().installEventFilter(owner)
        owner.file_list.itemSelectionChanged.connect(partial(owner._on_list_selection_changed, owner.file_list))
        layout.addWidget(owner.file_list, 1)
        self.empty_label = self.label('拖入图片或视频，也可点击右上角导入素材。', 'decodeMuted')
        layout.addWidget(self.empty_label)
        folder_row = QtWidgets.QHBoxLayout()
        folder_row.addWidget(self.button('打开素材目录', lambda: owner._open_folder_path(owner.local_decode_dir, create=True)))
        folder_row.addWidget(self.button('刷新', self.refresh))
        layout.addLayout(folder_row)
        left.addWidget(card, 1)

        controls, settings = self.card('解码设置')
        owner.decode_control_group = controls
        owner._control_group_css_template = None
        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        self.mode_combo = RhEnumComboBox()
        self.mode_combo.addItem('GRC · 网格还原', 'grc')
        self.mode_combo.addItem('SSTool · 密码解码', 'sst')
        mode_label = self.label('方式')
        mode_label.setWordWrap(False)
        form.addWidget(mode_label, 0, 0)
        form.addWidget(self.mode_combo, 0, 1)
        owner.grid_label = self.label('网格列数')
        owner.grid_label.setWordWrap(False)
        owner.grid_spin = RhNumberSpinBox(integer=True)
        owner.grid_spin.configure({'min': 4, 'max': 256})
        owner.grid_spin.setValue(32)
        form.addWidget(owner.grid_label, 1, 0)
        form.addWidget(owner.grid_spin, 1, 1)
        owner.sst_pwd_label = self.label('解码密码')
        owner.sst_pwd_label.setWordWrap(False)
        owner.sst_pwd_edit = QtWidgets.QLineEdit()
        owner.sst_pwd_edit.setPlaceholderText('未加密时可留空')
        owner.sst_pwd_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_toggle = QtWidgets.QAction('显示密码', owner.sst_pwd_edit)
        owner.sst_pwd_edit.addAction(self.password_toggle, QtWidgets.QLineEdit.TrailingPosition)
        self.password_toggle.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView))
        self.password_toggle.setCheckable(True)
        self.password_toggle.toggled.connect(self.show_password)
        form.addWidget(owner.sst_pwd_label, 2, 0)
        form.addWidget(owner.sst_pwd_edit, 2, 1)
        form.setColumnStretch(1, 1)
        settings.addLayout(form)
        owner.show_grid_cb = QtWidgets.QCheckBox('预览显示网格线')
        owner.show_grid_cb.toggled.connect(owner.on_file_selected)
        owner.overwrite_cb = QtWidgets.QCheckBox('覆盖已有解码结果')
        owner.overwrite_cb.setToolTip('关闭时跳过已有结果；原始素材始终保留。')
        settings.addWidget(owner.show_grid_cb)
        settings.addWidget(owner.overwrite_cb)
        settings.addWidget(self.label('结果自动保存，原始素材保留。', 'decodeMuted'))
        left.addWidget(controls)
        self.splitter.addWidget(sidebar)

        workspace = QtWidgets.QWidget()
        workspace.setObjectName('decodeWorkspace')
        right = QtWidgets.QVBoxLayout(workspace)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)
        self.previews = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.previews.setChildrenCollapsible(False)
        self.previews.setHandleWidth(10)
        original, original_layout = self.card('原始素材')
        owner.preview_tabs = QtWidgets.QTabWidget()
        owner.preview_tabs.tabBar().hide()
        owner.orig_view_grc = self.preview('选择素材或拖入图片 / 视频')
        owner.orig_view_sst = self.preview('选择素材或拖入图片 / 视频')
        owner._grc_tab_index = owner.preview_tabs.addTab(owner.orig_view_grc, 'GRC')
        owner._sst_tab_index = owner.preview_tabs.addTab(owner.orig_view_sst, 'SSTool')
        owner.orig_view = owner.orig_view_grc
        original_layout.addWidget(owner.preview_tabs, 1)
        owner.orig_info_label = self.label('', 'fileInfoLabel')
        original_layout.addWidget(owner.orig_info_label)
        self.previews.addWidget(original)

        result, result_layout = self.card('解码结果')
        owner.output_container = QtWidgets.QFrame()
        stack = QtWidgets.QGridLayout(owner.output_container)
        stack.setContentsMargins(0, 0, 0, 0)
        owner.output_view = self.preview('解码后的结果将在这里展示')
        stack.addWidget(owner.output_view, 0, 0)
        owner.output_play_btn = QtWidgets.QToolButton(owner.output_container)
        owner.output_play_btn.setObjectName('outputPlayButton')
        owner.output_play_btn.setCursor(QtCore.Qt.PointingHandCursor)
        owner.output_play_btn.setToolTip('解码当前选中的素材')
        owner.output_play_btn.setIcon(owner._get_play_icon(64))
        owner.output_play_btn.setIconSize(QtCore.QSize(64, 64))
        owner.output_play_btn.setFixedSize(92, 92)
        owner._play_btn_size_base = 92
        owner._play_icon_px_base = 64
        owner.output_play_btn.hide()
        stack.addWidget(owner.output_play_btn, 0, 0, QtCore.Qt.AlignCenter)
        result_layout.addWidget(owner.output_container, 1)
        owner.file_info_label = self.label('', 'fileInfoLabel')
        result_layout.addWidget(owner.file_info_label)
        self.previews.addWidget(result)
        self.previews.setSizes([450, 450])
        right.addWidget(self.previews, 1)
        owner._info_labels = {'orig': owner.orig_info_label, 'output': owner.file_info_label}
        owner._current_pixmaps = {'orig': None, 'output': None}
        owner._current_paths = {'orig': None, 'output': None}
        owner._orig_pixmaps_by_mode = {'grc': None, 'sst': None}
        owner._orig_paths_by_mode = {'grc': None, 'sst': None}

        execution, execution_layout = self.card()
        status_row = QtWidgets.QHBoxLayout()
        self.status_label = self.label('等待选择素材', 'decodeStatus')
        status_row.addWidget(self.status_label, 1)
        owner.elapsed_label = self.label('解码用时: 0.00s', 'decodeMuted')
        status_row.addWidget(owner.elapsed_label)
        execution_layout.addLayout(status_row)
        owner.progress = QtWidgets.QProgressBar()
        owner.progress.setRange(0, 100)
        owner.progress.setValue(0)
        owner.progress.setFixedHeight(8)
        owner.progress.setTextVisible(False)
        execution_layout.addWidget(owner.progress)
        actions = QtWidgets.QHBoxLayout()
        owner.preview_btn = self.button('解码选中')
        owner.preview_btn.setObjectName('decodePrimary')
        owner.batch_btn = self.button('解码全部')
        owner.batch_btn.setToolTip('处理当前素材目录中的全部图片和视频')
        owner.cancel_btn = self.button('停止解码')
        actions.addWidget(owner.preview_btn)
        actions.addWidget(owner.batch_btn)
        actions.addWidget(owner.cancel_btn)
        actions.addStretch(1)
        actions.addWidget(self.button('打开结果目录', lambda: owner._open_folder_path(owner.output_dir, create=True)))
        execution_layout.addLayout(actions)
        right.addWidget(execution)

        self.log_toggle = QtWidgets.QToolButton()
        self.log_toggle.setText('处理日志')
        self.log_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.log_toggle.setCheckable(True)
        self.log_toggle.setArrowType(QtCore.Qt.RightArrow)
        self.log_toggle.setCursor(QtCore.Qt.PointingHandCursor)
        right.addWidget(self.log_toggle, 0, QtCore.Qt.AlignLeft)
        owner.log_text = QtWidgets.QTextEdit()
        owner.log_text.setReadOnly(True)
        owner.log_text.document().setMaximumBlockCount(1500)
        owner.log_text.setFixedHeight(110)
        owner.log_text.hide()
        self.log_toggle.toggled.connect(self.toggle_log)
        right.addWidget(owner.log_text)
        owner._log_emitter = LogEmitter(owner)
        owner._log_emitter.sig.connect(owner.log_text.append)
        owner._elapsed_timer = QtCore.QTimer(owner)
        owner._elapsed_timer.setInterval(200)
        owner._elapsed_timer.timeout.connect(owner._update_elapsed_label)
        owner._progress_start_time = None
        self.splitter.addWidget(workspace)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([285, 950])
        outer.addWidget(self.splitter, 1)

        self.mode_combo.currentIndexChanged.connect(owner.preview_tabs.setCurrentIndex)
        owner.preview_tabs.currentChanged.connect(self.mode_changed)
        self.mode_changed(0, save=False)
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(40)
        self.update_timer.timeout.connect(self.update_selection)
        for signal in (owner.file_list.itemSelectionChanged, owner.file_list.model().rowsInserted,
                       owner.file_list.model().rowsRemoved, owner.file_list.model().modelReset):
            signal.connect(self.queue_selection_update)
        self.update_selection()
        make_responsive(self, rows=((heading, 650), (actions, 800), (status_row, 650)),
                        splitters=((self.splitter, 1050), (self.previews, 800)))
        self._responsive_scroll.widget().setObjectName('decodeContent')
        self.apply_theme()

    @staticmethod
    def label(text, name=''):
        label = QtWidgets.QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        return label

    @staticmethod
    def card(title=''):
        frame = QtWidgets.QFrame()
        frame.setObjectName('decodeCard')
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        if title:
            layout.addWidget(DecodePage.label(title, 'decodeSection'))
        return frame, layout

    @staticmethod
    def button(text, callback=None):
        button = QtWidgets.QPushButton(text)
        button.setCursor(QtCore.Qt.PointingHandCursor)
        if callback:
            button.clicked.connect(callback)
        return button

    @staticmethod
    def preview(text):
        label = DropLabel(alignment=QtCore.Qt.AlignCenter)
        label.setObjectName('previewLabel')
        label.setText(text)
        label.setWordWrap(True)
        label.setMinimumSize(240, 230)
        label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        return label

    def import_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, '导入待解码素材', self.owner.local_decode_dir,
            '图片和视频 (*.png *.jpg *.jpeg *.bmp *.tiff *.webp *.mp4 *.mov *.avi *.mkv *.gif *.webm)')
        if files:
            self.owner._on_files_dropped(files)

    def refresh(self):
        self.owner.load_folder(self.owner.local_decode_dir)

    def show_password(self, visible):
        self.owner.sst_pwd_edit.setEchoMode(QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password)
        self.password_toggle.setText('隐藏密码' if visible else '显示密码')

    def toggle_log(self, visible):
        self.owner.log_text.setVisible(visible)
        self.log_toggle.setArrowType(QtCore.Qt.DownArrow if visible else QtCore.Qt.RightArrow)

    def mode_changed(self, index, save=True):
        owner = self.owner
        sst = index == owner._sst_tab_index
        mode = 'sst' if sst else 'grc'
        blocker = QtCore.QSignalBlocker(self.mode_combo)
        self.mode_combo.setCurrentIndex(index)
        del blocker
        owner.grid_label.setVisible(not sst)
        owner.grid_spin.setVisible(not sst)
        owner.show_grid_cb.setVisible(not sst)
        owner.sst_pwd_label.setVisible(sst)
        owner.sst_pwd_edit.setVisible(sst)
        owner.orig_view = owner.orig_view_sst if sst else owner.orig_view_grc
        pixmap = owner._orig_pixmaps_by_mode.get(mode)
        path = owner._orig_paths_by_mode.get(mode)
        owner._current_pixmaps['orig'] = pixmap
        owner._current_paths['orig'] = path
        if pixmap is not None:
            owner.orig_view.set_base_pixmap(pixmap)
            if path:
                owner._set_file_info(path, 'orig')
        if save:
            owner._save_settings()
            QtCore.QTimer.singleShot(0, owner.on_file_selected)

    def update_selection(self):
        owner = self.owner
        count = owner.file_list.count()
        selected = len(owner.file_list.selectedItems())
        self.count_label.setText(f'共 {count} 项 · 已选 {selected} 项')
        self.empty_label.setVisible(count == 0)
        owner.preview_btn.setEnabled(not self.running and selected > 0)
        owner.batch_btn.setEnabled(not self.running and count > 0)
        owner.cancel_btn.setEnabled(self.running and not self.canceling)
        owner.preview_btn.setText(f'解码选中 · {selected}' if selected else '解码选中')
        if not self.running and not getattr(self, 'last_status', None):
            self.status_label.setText(f'已选择 {selected} 项素材' if selected else '等待选择素材')

    @QtCore.pyqtSlot()
    def queue_selection_update(self):
        if not getattr(self.owner, '_closing', False):
            self.update_timer.start()

    def set_running(self, running, *, canceled=False):
        self.running = running
        self.canceling = False
        self.owner.decode_control_group.setEnabled(not running)
        self.import_button.setEnabled(not running)
        self.owner.output_play_btn.setEnabled(not running)
        if running:
            self.last_status = None
            self.status_label.setText('正在解码 · ' + ('SSTool' if self.owner._current_decode_mode() == 'sst' else 'GRC'))
        else:
            self.last_status = '已停止解码' if canceled else '处理结束 · 详情见日志'
            self.status_label.setText(self.last_status)
        self.update_selection()

    def set_canceling(self):
        self.canceling = True
        self.status_label.setText('正在停止解码…')
        self.update_selection()

    def apply_theme(self):
        dark = getattr(self.owner, '_theme_mode', 'dark') == 'dark'
        surface, border, muted, preview = ('#151e2b', '#2c3b50', '#9caec4', '#0e1520') if dark else ('#ffffff', '#d6e0eb', '#65768c', '#f4f7fb')
        foreground = '#e4edf8' if dark else '#23364e'
        background = '#0b101a' if dark else '#f4f7fc'
        selected = '#193951' if dark else '#dcebf9'
        check = (Path(current_dir) / 'icons' / 'ui-check.svg').as_posix()
        self.setStyleSheet(f'''
            QWidget#decodeContent {{ background: {background}; }}
            QWidget#decodeSidebar, QWidget#decodeWorkspace, QLabel, QCheckBox {{ background: transparent; }}
            QFrame#decodeCard {{ background: {surface}; border: 1px solid {border}; border-radius: 12px; }}
            QLabel#decodeTitle {{ color: {foreground}; font-size: 26px; font-weight: 700; }}
            QLabel#decodeSection, QLabel#decodeStatus {{ color: {foreground}; font-weight: 600; }}
            QLabel#decodeMuted, QLabel#fileInfoLabel {{ color: {muted}; font-size: 12px; }}
            QLabel#previewLabel {{ background: {preview}; color: {muted}; border: 1px dashed {border}; border-radius: 8px; }}
            QTabWidget::pane {{ border: 0; }}
            QListWidget {{ border: 0; background: {preview}; border-radius: 8px; }}
            QListWidget::item:selected {{ background: {selected}; color: {foreground}; border-radius: 6px; }}
            QComboBox, QAbstractSpinBox, QLineEdit {{ background: {preview}; color: {foreground}; border: 1px solid {border}; border-radius: 6px; padding: 4px; }}
            QAbstractSpinBox QLineEdit {{ border: 0; padding: 0; }}
            QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {muted}; border-radius: 3px; background: {preview}; }}
            QCheckBox::indicator:checked {{ background: #2b8bd5; border-color: #2b8bd5; image: url("{check}"); }}
            QPushButton {{ padding: 7px 10px; border-radius: 7px; background: {surface}; color: {foreground}; border: 1px solid {border}; }}
            QPushButton:hover {{ background: {selected}; }}
            QPushButton:disabled {{ color: {muted}; background: {preview}; }}
            QPushButton#decodePrimary {{ background: #2b8bd5; color: white; border: 1px solid #2b8bd5; font-weight: 600; }}
            QPushButton#decodePrimary:hover {{ background: #369be5; }}
            QPushButton#decodePrimary:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
            QProgressBar {{ border: 0; border-radius: 4px; background: {border}; }}
            QProgressBar::chunk {{ background: #2b8bd5; border-radius: 4px; }}
            QToolButton#outputPlayButton {{ background: {surface}; border: 1px solid {border}; border-radius: 46px; }}
            QSplitter::handle {{ background: transparent; }}
            QSplitter::handle:hover {{ background: {border}; }}
        ''')
