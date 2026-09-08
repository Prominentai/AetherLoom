"""Page construction and existing RH callback wiring."""
from aetherloom_core.ui.widgets import ClickableSlider
from aetherloom_core.ui.widgets import CompletionTextEdit
from aetherloom_core.resources import DEFAULT_EXPAND_SYSTEM_PROMPT
from aetherloom_core.resources import DEFAULT_IMAGE_REVERSE_PROMPT
from aetherloom_core.ui.widgets import DropLabel
from aetherloom_core.ui.widgets import DropListWidget
from aetherloom_core.resources import HOME_ICON_SVG
from aetherloom_core.resources import IMAGE_EXTS
from PIL import Image
from aetherloom_core.resources import PLAY_BUTTON_SVG
from aetherloom_core.prompt_history import PromptHistory
from PyQt5.QtCore import Qt
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_parameters import RhEnumComboBox
from aetherloom_core.rh_parameters import RhNumberSpinBox
from aetherloom_core.paths import current_dir, SOURCE_ROOT
from aetherloom_core.ui.widgets import ThumbnailDelegate
from aetherloom_core.ui.responsive import make_responsive, SidebarScroll
from moviepy.editor import VideoFileClip
from aetherloom_core.platform_utils import _api_debug
from aetherloom_core.platform_utils import _move_to_trash
from aetherloom_core.platform_utils import _set_native_titlebar_dark
from aetherloom_core.platform_utils import _svg_to_icon
from aetherloom_core import api_manager
from aetherloom_core.rh_parameters import collect_node_values
import cv2
from aetherloom_core.services.decoding import grc
from aetherloom_core.prompt_history import input_history_entries
import json
import os
from functools import partial
from aetherloom_core.prompt_history import record_run_inputs
import shutil
import subprocess
import sys
import threading
import weakref
import webbrowser


class MainLayoutMixin:
    def _setup_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        h = QtWidgets.QHBoxLayout(central)

        # Left: vertical sidebar (compact icon+label buttons)
        self.sidebar_frame = QtWidgets.QFrame()
        self.sidebar_frame.setObjectName('sidebarFrame')
        # set an initial width based on base width and current scale, capped by sensible limits
        try:
            scale = getattr(self, '_ui_scale_factor', 1.0)
            # determine available width (window preferred, fallback to primary screen)
            try:
                win_w = int(self.width() or 0)
            except Exception:
                win_w = 0
            if not win_w or win_w < 200:
                try:
                    screen = QtWidgets.QApplication.primaryScreen()
                    win_w = int(screen.availableGeometry().width()) if screen is not None else 1200
                except Exception:
                    win_w = 1200
            base_frac = float(getattr(self, '_sidebar_base_frac', 0.12))
            min_frac = float(getattr(self, '_sidebar_min_frac', 0.06))
            cap_frac = float(getattr(self, '_sidebar_max_fraction', 0.18))
            # compute widths proportionally
            base_w = max(int(win_w * min_frac), int(win_w * base_frac))
            cap_w = max(int(win_w * 0.12), int(win_w * cap_frac))
            # scale by UI scale and clamp
            initial_w = int(max(base_w * scale, min(base_w, cap_w)))
            # absolute px safety bounds
            initial_w = max(int(getattr(self, '_sidebar_base_width', 160)), min(initial_w, int(getattr(self, '_sidebar_max_px', 420))))
            self.sidebar_frame.setFixedWidth(initial_w)
        except Exception:
            try:
                self.sidebar_frame.setFixedWidth(self._sidebar_base_width)
            except Exception:
                pass
        sidebar_layout = QtWidgets.QVBoxLayout(self.sidebar_frame)
        sidebar_layout.setContentsMargins(14, 16, 14, 14)
        sidebar_layout.setSpacing(10)

        # self.sidebar_brand_label = QtWidgets.QLabel('GRC 工具台')
        # self.sidebar_brand_label.setObjectName('sidebarBrand')
        # self.sidebar_brand_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # sidebar_layout.addWidget(self.sidebar_brand_label)

        sidebar_base_icon = getattr(self, '_sidebar_icon_px_base', 28)
        sidebar_base_height = getattr(self, '_sidebar_button_height_base', 56)

        def _make_sidebar_button(text, tooltip, icon):
            btn = QtWidgets.QToolButton()
            btn.setObjectName('sidebarButton')
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setAutoRaise(True)
            btn.setCheckable(True)
            btn.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            btn.setFixedHeight(sidebar_base_height)
            btn.setIcon(icon)
            btn.setIconSize(QtCore.QSize(sidebar_base_icon, sidebar_base_icon))
            btn.setText(text)
            btn.setToolTip(tooltip)
            btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            return btn

        decode_icon = self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
        settings_icon = self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView)
        folder_icon = self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon)
        api_icon = self.style().standardIcon(QtWidgets.QStyle.SP_DriveNetIcon)
        runninghub_icon = self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton)

        # try loading custom sidebar icons from ./icons
        try:
            icon_dir = os.path.join(current_dir, 'icons')
            os.makedirs(icon_dir, exist_ok=True)
            # persist inline SVGs into icons/ so they behave like other icons and can be customized
            try:
                emblem_path = os.path.join(icon_dir, 'home_emblem.svg')
                icon_path = os.path.join(icon_dir, 'home_icon.svg')
                if not os.path.exists(emblem_path):
                    with open(emblem_path, 'w', encoding='utf-8') as wf:
                        wf.write(PLAY_BUTTON_SVG)
                if not os.path.exists(icon_path):
                    with open(icon_path, 'w', encoding='utf-8') as wf:
                        wf.write(HOME_ICON_SVG)
            except Exception:
                pass
            # prefer SVG, fallback to provided JPEGs if present
            candidates = {
                'decode': [
                    os.path.join(icon_dir, 'local_decoding.svg'),
                    os.path.join(icon_dir, 'local_decoding.jpeg'),
                ],
                'local': [
                    os.path.join(icon_dir, 'local_files.svg'),
                    os.path.join(icon_dir, 'local_files.jpeg'),
                ],
                'settings': [
                    os.path.join(icon_dir, 'setting.svg'),
                    os.path.join(icon_dir, 'setting.jpeg'),
                ],
                'api': [
                    os.path.join(icon_dir, 'api.svg'),
                    os.path.join(icon_dir, 'api.jpeg'),
                ],
                'runninghub': [
                    os.path.join(icon_dir, 'runninghub.svg'),
                    os.path.join(icon_dir, 'runninghub.jpeg'),
                ],
            }

            def _pick_icon(paths, fallback):
                for p in paths:
                    if os.path.exists(p):
                        return QtGui.QIcon(p)
                return fallback

            decode_icon = _pick_icon(candidates['decode'], decode_icon)
            folder_icon = _pick_icon(candidates['local'], folder_icon)
            settings_icon = _pick_icon(candidates['settings'], settings_icon)
            api_icon = _pick_icon(candidates['api'], api_icon)
            runninghub_icon = _pick_icon(candidates.get('runninghub', []), runninghub_icon)
        except Exception:
            pass

        try:
            # prefer an on-disk icon if present (icons/home_icon.svg), else render from inline SVG
            try:
                icon_file = os.path.join(current_dir, 'icons', 'home_icon.svg')
            except Exception:
                icon_file = None
            home_svg_icon = None
            try:
                if icon_file and os.path.exists(icon_file):
                    home_svg_icon = QtGui.QIcon(icon_file)
                else:
                    home_svg_icon = _svg_to_icon(HOME_ICON_SVG, sidebar_base_icon)
            except Exception:
                home_svg_icon = None
        except Exception:
            home_svg_icon = None
        home_icon = home_svg_icon or self.style().standardIcon(QtWidgets.QStyle.SP_DirHomeIcon)
        self.home_btn = _make_sidebar_button('主页', '应用首页', home_icon)
        self.decode_btn = _make_sidebar_button('本地解码', '解码与预览', decode_icon)
        self.local_btn = _make_sidebar_button('本地文件', '浏览输入/输出素材', folder_icon)
        self.api_btn = _make_sidebar_button('API管理', '模型与接口管理', api_icon)
        self.runninghub_btn = _make_sidebar_button('RH应用', 'RunningHub 应用', runninghub_icon)
        canvas_icon = QtGui.QIcon(os.path.join(current_dir, 'icons', 'canvas.svg'))
        self.canvas_btn = _make_sidebar_button('画布', '画布与应用工作流', canvas_icon)
        self.settings_btn = _make_sidebar_button('设置中心', '参数与目录管理', settings_icon)

        # make Home the default selected sidebar entry
        try:
            self.home_btn.setChecked(True)
        except Exception:
            pass
        # order: Home, Runninghub (moved), then other pages
        sidebar_layout.addWidget(self.home_btn)
        sidebar_layout.addWidget(self.runninghub_btn)
        sidebar_layout.addWidget(self.canvas_btn)
        sidebar_layout.addWidget(self.decode_btn)
        sidebar_layout.addWidget(self.local_btn)
        sidebar_layout.addWidget(self.api_btn)
        sidebar_layout.addWidget(self.settings_btn)
        self._sidebar_buttons = [self.home_btn, self.runninghub_btn, self.canvas_btn, self.decode_btn, self.local_btn, self.api_btn, self.settings_btn]
        sidebar_layout.addStretch(1)

        theme_row = QtWidgets.QHBoxLayout()
        theme_row.setContentsMargins(0, 0, 0, 0)
        theme_row.setSpacing(8)
        self.theme_toggle_btn = QtWidgets.QPushButton('')
        self.theme_toggle_btn.setObjectName('themeToggleButton')
        self.theme_toggle_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        try:
            base_sz = getattr(self, '_theme_toggle_size_base', 52)
            self.theme_toggle_btn.setFixedSize(QtCore.QSize(base_sz, base_sz))
        except Exception:
            pass
        try:
            theme_icon_dir = os.path.join(current_dir, 'icons')
            theme_icon = None
            for candidate in (
                os.path.join(theme_icon_dir, 'theme_toggle.svg'),
                os.path.join(theme_icon_dir, 'theme_toggle.jpeg'),
            ):
                if os.path.exists(candidate):
                    theme_icon = QtGui.QIcon(candidate)
                    break
            if theme_icon:
                self.theme_toggle_btn.setIcon(theme_icon)
                base_icon = getattr(self, '_theme_toggle_icon_px_base', 32)
                self.theme_toggle_btn.setIconSize(QtCore.QSize(base_icon, base_icon))
        except Exception:
            pass
        theme_row.addWidget(self.theme_toggle_btn)
        sidebar_layout.addLayout(theme_row)
        self.sidebar_scroll = SidebarScroll(self.sidebar_frame)
        h.addWidget(self.sidebar_scroll)
        # ensure collapsed flag default
        try:
            self._sidebar_collapsed = False
        except Exception:
            pass

        # collapse toggle button to the right of sidebar (shows only an arrow)
        try:
            self.sidebar_toggle_btn = QtWidgets.QToolButton()
            self.sidebar_toggle_btn.setObjectName('sidebarToggle')
            self.sidebar_toggle_btn.setText('\u203A')  # single character arrow '›'
            self.sidebar_toggle_btn.setFixedWidth(28)
            self.sidebar_toggle_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            self.sidebar_toggle_btn.setToolTip('折叠/展开侧边栏')
            # minimal styling so it looks like a splitter button
            try:
                self.sidebar_toggle_btn.setStyleSheet('QToolButton#sidebarToggle { border: none; background: transparent; font-weight: 700; }')
            except Exception:
                pass
            h.addWidget(self.sidebar_toggle_btn)
        except Exception:
            self.sidebar_toggle_btn = None

        # Right: stacked pages (decode page + settings page)
        self.pages = QtWidgets.QStackedWidget()
        h.addWidget(self.pages, 1)

        # --- Page: 主页 (home) ---
        from aetherloom_core.ui.home import HomePage
        home_page = HomePage(self, current_dir)
        self.home_page = home_page
        self.home_readme = home_page.readme
        self.home_subtitle = home_page.subtitle
        make_responsive(home_page)
        self.pages.addWidget(home_page)

        # --- Page: 本地解码 (decode) ---
        from aetherloom_core.ui.decode import DecodePage
        decode_page = DecodePage(self)
        self._decode_page = decode_page
        self.pages.addWidget(decode_page)

        # --- Page: 本地文件 (local files) ---
        local_page = QtWidgets.QWidget()
        local_layout = QtWidgets.QVBoxLayout(local_page)

        # toggle row: show input / output / both
        toggle_row = QtWidgets.QHBoxLayout()
        self.local_mode_group = QtWidgets.QButtonGroup(self)
        rb_in = QtWidgets.QRadioButton('仅输入')
        rb_out = QtWidgets.QRadioButton('仅输出')
        rb_both = QtWidgets.QRadioButton('同时显示')
        rb_both.setChecked(True)
        self.local_mode_group.addButton(rb_in, 0)
        self.local_mode_group.addButton(rb_out, 1)
        self.local_mode_group.addButton(rb_both, 2)
        toggle_row.addWidget(rb_in)
        toggle_row.addWidget(rb_out)
        toggle_row.addWidget(rb_both)
        # right-side: thumbnail size slider + numeric input
        slider_layout = QtWidgets.QHBoxLayout()
        # prefix label, numeric spin box and unit label so user can type exact thumbnail size
        self.thumb_size_label = QtWidgets.QLabel('缩略图:')
        self.thumb_size_spin = QtWidgets.QSpinBox()
        self.thumb_size_spin.setRange(80, 2000)
        self.thumb_size_spin.setSingleStep(1)
        self.thumb_size_spin.setValue(200)
        # make spinbox wider so the last digit isn't clipped; allow preferred horizontal sizing
        try:
            # widen the spinbox and use a fixed horizontal size so digits are never clipped
            self.thumb_size_spin.setFixedWidth(140)
            self.thumb_size_spin.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            # reduce internal padding to give more room for digits where platform stylesheet allows
            try:
                self.thumb_size_spin.setStyleSheet('QSpinBox { padding-right: 6px; padding-left: 6px; }')
            except Exception:
                pass
        except Exception:
            pass
        self.thumb_size_unit = QtWidgets.QLabel('px')
        self.thumb_size_slider = ClickableSlider(Qt.Horizontal)
        self.thumb_size_slider.setRange(80, 2000)
        self.thumb_size_slider.setSingleStep(10)
        self.thumb_size_slider.setTickInterval(10)
        self.thumb_size_slider.setValue(200)
        # Make the slider easier to operate and visually larger
        self.thumb_size_slider.setFixedWidth(420)
        try:
            self.thumb_size_slider.setFixedHeight(28)
            self.thumb_size_slider.setStyleSheet('QSlider::handle:horizontal { width: 20px; height: 20px; margin: -6px 0px; border-radius: 10px; background: #2b8bd5; }')
        except Exception:
            pass
        # add widgets: prefix label, spinbox, unit label, then slider
        slider_layout.addWidget(self.thumb_size_label)
        slider_layout.addWidget(self.thumb_size_spin)
        slider_layout.addWidget(self.thumb_size_unit)
        slider_layout.addWidget(self.thumb_size_slider)
        toggle_row.addStretch(1)
        toggle_row.addLayout(slider_layout)
        # sorting controls row (separate sort for input and output lists)
        try:
            sort_row = QtWidgets.QHBoxLayout()
            # input sort
            lbl_in_sort = QtWidgets.QLabel('输入 排序:')
            self.local_sort_in_combo = QtWidgets.QComboBox()
            self.local_sort_in_combo.setToolTip('为输入格子选择排序方式 (会被记住)')
            # output sort
            lbl_out_sort = QtWidgets.QLabel('输出 排序:')
            self.local_sort_out_combo = QtWidgets.QComboBox()
            self.local_sort_out_combo.setToolTip('为输出格子选择排序方式 (会被记住)')

            # populate with options (data keys used for persistence)
            sort_items = [
                ('名称 ↑', 'name_asc'),
                ('名称 ↓', 'name_desc'),
                ('修改时间 ↑', 'mtime_asc'),
                ('修改时间 ↓', 'mtime_desc'),
                ('文件大小 ↑', 'size_asc'),
                ('文件大小 ↓', 'size_desc'),
                ('扩展名 ↑', 'ext_asc'),
                ('扩展名 ↓', 'ext_desc'),
            ]
            for lbl, key in sort_items:
                self.local_sort_in_combo.addItem(lbl, key)
                self.local_sort_out_combo.addItem(lbl, key)

            # default selection from settings if available (applied later in _apply_settings)
            sort_row.addWidget(lbl_in_sort)
            sort_row.addWidget(self.local_sort_in_combo)
            sort_row.addSpacing(12)
            sort_row.addWidget(lbl_out_sort)
            sort_row.addWidget(self.local_sort_out_combo)
            sort_row.addStretch(1)
            # add search box next to sort controls
            try:
                lbl_search = QtWidgets.QLabel('搜索:')
                self.local_search_edit = QtWidgets.QLineEdit()
                self.local_search_edit.setPlaceholderText('按文件名搜索 · Ctrl+F')
                self.local_search_edit.setMinimumWidth(220)
                self.local_search_edit.setClearButtonEnabled(True)
                self.local_search_edit.setToolTip('按文件名筛选；Enter 立即搜索，Esc 清空')
                self.local_search_edit.returnPressed.connect(self._apply_local_search)
                find_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Find, local_page)
                find_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
                find_shortcut.activated.connect(lambda: (self.local_search_edit.setFocus(), self.local_search_edit.selectAll()))
                clear_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self.local_search_edit)
                clear_shortcut.setContext(QtCore.Qt.WidgetShortcut)
                clear_shortcut.activated.connect(self.local_search_edit.clear)
                # debounce timer for search
                try:
                    self._local_search_timer = QtCore.QTimer(self)
                    self._local_search_timer.setSingleShot(True)
                    self._local_search_timer.setInterval(180)
                    self._local_search_timer.timeout.connect(self._apply_local_search)
                except Exception:
                    self._local_search_timer = None
                try:
                    if getattr(self, '_local_search_timer', None) is not None:
                        self.local_search_edit.textChanged.connect(lambda _t: self._local_search_timer.start())
                    else:
                        self.local_search_edit.textChanged.connect(lambda _t: self._apply_local_search())
                except Exception:
                    try:
                        self.local_search_edit.textChanged.connect(self._apply_local_search)
                    except Exception:
                        pass
                sort_row.addWidget(lbl_search)
                sort_row.addWidget(self.local_search_edit)
            except Exception:
                self.local_search_edit = None
            # toolbar visibility toggle and wrapper
            controls_toggle_row = QtWidgets.QHBoxLayout()
            controls_toggle_row.addStretch(1)
            self.local_controls_toggle_btn = QtWidgets.QPushButton('隐藏排序/筛选栏')
            try:
                self.local_controls_toggle_btn.setCheckable(False)
            except Exception:
                pass
            controls_toggle_row.addWidget(self.local_controls_toggle_btn)
            local_layout.addLayout(controls_toggle_row)
            try:
                self.local_controls_toggle_btn.clicked.connect(self._toggle_local_controls)
            except Exception:
                pass
            self.local_controls_wrapper = QtWidgets.QWidget()
            controls_column = QtWidgets.QVBoxLayout(self.local_controls_wrapper)
            controls_column.setContentsMargins(0, 0, 0, 0)
            controls_column.setSpacing(8)
            controls_column.addLayout(toggle_row)
            controls_column.addLayout(sort_row)
            # dynamic filter rows with add button
            try:
                filter_header = QtWidgets.QHBoxLayout()
                lbl_filter = QtWidgets.QLabel('筛选:')
                filter_header.addWidget(lbl_filter)
                self.local_filter_add_btn = QtWidgets.QPushButton('+ 添加筛选')
                self.local_filter_add_btn.setObjectName('localFilterAddButton')
                self.local_filter_add_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                self.local_filter_add_btn.setFixedHeight(36)
                self.local_filter_add_btn.setMinimumWidth(126)
                self.local_filter_add_btn.setStyleSheet('QPushButton#localFilterAddButton { border-radius: 16px; padding: 6px 16px; background: #2265d8; color: #ffffff; font-weight: 600; } QPushButton#localFilterAddButton:hover { background: #1b52b5; }')
                self.local_filter_add_btn.setToolTip('添加新的筛选条件')
                filter_header.addWidget(self.local_filter_add_btn)
                self.local_filter_clear_btn = QtWidgets.QPushButton('全部删除')
                self.local_filter_clear_btn.setObjectName('localFilterClearButton')
                self.local_filter_clear_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                self.local_filter_clear_btn.setFixedHeight(36)
                self.local_filter_clear_btn.setMinimumWidth(112)
                self.local_filter_clear_btn.setStyleSheet('QPushButton#localFilterClearButton { border-radius: 14px; padding: 6px 16px; background: rgba(244,67,54,0.85); color: #ffffff; font-weight: 600; } QPushButton#localFilterClearButton:hover { background: rgba(229,57,53,0.95); }')
                self.local_filter_clear_btn.setToolTip('删除全部筛选条件')
                filter_header.addWidget(self.local_filter_clear_btn)
                self.local_filter_dropdown_label = QtWidgets.QLabel('更多:')
                self.local_filter_dropdown_label.setVisible(False)
                filter_header.addWidget(self.local_filter_dropdown_label)
                self.local_filter_dropdown = QtWidgets.QComboBox()
                try:
                    self.local_filter_dropdown.setMinimumWidth(220)
                    self.local_filter_dropdown.setFixedHeight(36)
                    self.local_filter_dropdown.setFrame(True)
                    self.local_filter_dropdown.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
                    self.local_filter_dropdown.setStyleSheet('QComboBox { border: 1px solid #5c6bc0; border-radius: 12px; padding: 4px 12px; background: rgba(17,17,19,0.4); } QComboBox:focus { border-color: #2265d8; } QComboBox::drop-down { width: 28px; border-left: 1px solid rgba(255,255,255,0.08); }')
                except Exception:
                    pass
                self.local_filter_dropdown.setVisible(False)
                self.local_filter_dropdown.setPlaceholderText('展开更多筛选')
                filter_header.addWidget(self.local_filter_dropdown)
                filter_header.addStretch(1)
                controls_column.addLayout(filter_header)
                self.local_filter_container = QtWidgets.QVBoxLayout()
                self.local_filter_container.setSpacing(6)
                controls_column.addLayout(self.local_filter_container)
                try:
                    self.local_filter_add_btn.clicked.connect(lambda: self._add_local_filter_row())
                    self.local_filter_clear_btn.clicked.connect(self._clear_all_filter_rows)
                    self.local_filter_dropdown.currentIndexChanged.connect(self._on_filter_dropdown_changed)
                except Exception:
                    pass
                try:
                    self._refresh_filter_dropdown()
                    self._apply_filter_controls_scale(getattr(self, '_ui_scale_factor', 1.0))
                    self._update_filter_clear_visibility()
                except Exception:
                    pass
            except Exception:
                self.local_filter_add_btn = None
                self.local_filter_clear_btn = None
                self.local_filter_container = None
                self.local_filter_dropdown = None
                self.local_filter_dropdown_label = None
            local_layout.addWidget(self.local_controls_wrapper)
            self.local_controls_wrapper.setVisible(True)
        except Exception:
            # fallback: add the toggle row only
            local_layout.addLayout(toggle_row)
            self.local_sort_in_combo = None
            self.local_sort_out_combo = None
            self.local_controls_toggle_btn = None
            self.local_controls_wrapper = None

        # thumbnail grids: separate lists for input and output so we can show side-by-side when needed
        lists_row = QtWidgets.QHBoxLayout()
        self.local_list_in = QtWidgets.QListWidget()
        self.local_list_out = QtWidgets.QListWidget()
        for lw in (self.local_list_in, self.local_list_out):
            lw.setViewMode(QtWidgets.QListView.IconMode)
            lw.setIconSize(QtCore.QSize(200, 200))
            lw.setResizeMode(QtWidgets.QListView.Adjust)
            lw.setMovement(QtWidgets.QListView.Static)
            lw.setSpacing(20)
            lw.setWrapping(True)
            try:
                lw.setUniformItemSizes(True)
            except Exception:
                pass
            lw.setContextMenuPolicy(Qt.CustomContextMenu)
            lw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
            # avoid default selection background that darkens the whole item
            try:
                lw.setStyleSheet('''
                    QListWidget::item:selected { background: transparent; }
                    QListView::item:selected { background: transparent; }
                ''')
            except Exception:
                pass
            try:
                delegate = ThumbnailDelegate(lw)
                lw.setItemDelegate(delegate)
            except Exception:
                pass
        # show full file info for selection in a floating bottom-right frame
        try:
            self._selection_info_frame = QtWidgets.QFrame(self)
            self._selection_info_frame.setObjectName('selectionInfoFrame')
            self._selection_info_frame.setStyleSheet('QFrame#selectionInfoFrame { background: rgba(16,16,20,230); border: 1px solid #2b8bd5; border-radius: 8px; color: white; }')
            self._selection_info_frame.setVisible(False)
            self._selection_info_label = QtWidgets.QLabel('', self._selection_info_frame)
            self._selection_info_label.setWordWrap(True)
            self._selection_info_label.setStyleSheet('color: white; padding: 8px;')
            lay = QtWidgets.QVBoxLayout(self._selection_info_frame)
            lay.setContentsMargins(6,6,6,6)
            lay.addWidget(self._selection_info_label)
            self._selection_info_frame.setLayout(lay)
            # do not force a fixed width here; we'll size dynamically when showing selection
            try:
                self._selection_info_frame.setMaximumWidth(1000)
            except Exception:
                pass
        except Exception:
            self._selection_info_frame = None
            self._selection_info_label = None
        lists_row.addWidget(self.local_list_in, 1)
        lists_row.addWidget(self.local_list_out, 1)
        local_layout.addLayout(lists_row, 1)

        # add small footnote labels under each list to show folder file counts
        try:
            counts_row = QtWidgets.QHBoxLayout()
            self.local_count_label_in = QtWidgets.QLabel('', alignment=Qt.AlignLeft)
            self.local_count_label_out = QtWidgets.QLabel('', alignment=Qt.AlignRight)
            # muted style
            try:
                self.local_count_label_in.setStyleSheet('color: #9aa0a6; padding:4px;')
                self.local_count_label_out.setStyleSheet('color: #9aa0a6; padding:4px;')
            except Exception:
                pass
            counts_row.addWidget(self.local_count_label_in, 1)
            counts_row.addWidget(self.local_count_label_out, 1)
            local_layout.addLayout(counts_row)
        except Exception:
            self.local_count_label_in = None
            self.local_count_label_out = None

        # wire events
        self.local_list_in.customContextMenuRequested.connect(lambda pos: self.on_local_context_menu(pos, self.local_list_in))
        self.local_list_out.customContextMenuRequested.connect(lambda pos: self.on_local_context_menu(pos, self.local_list_out))
        self.local_list_in.itemActivated.connect(lambda it: self._open_path_item(it))
        self.local_list_out.itemActivated.connect(lambda it: self._open_path_item(it))
        # show file info when selection changes
        try:
            self.local_list_in.itemSelectionChanged.connect(partial(self._on_list_selection_changed, self.local_list_in))
            self.local_list_out.itemSelectionChanged.connect(partial(self._on_list_selection_changed, self.local_list_out))
        except Exception:
            pass
        # enqueue visible thumbnails when scrolling or resizing viewport
        try:
            self.local_list_in.verticalScrollBar().valueChanged.connect(partial(self._on_list_scrolled, self.local_list_in))
            self.local_list_out.verticalScrollBar().valueChanged.connect(partial(self._on_list_scrolled, self.local_list_out))
            self.local_list_in.viewport().installEventFilter(self)
            self.local_list_out.viewport().installEventFilter(self)
        except Exception:
            pass
        def _on_local_mode_changed():
            try:
                self._save_settings()
            except Exception:
                pass
            try:
                self._refresh_local_list()
            except Exception:
                pass
        self.local_mode_group.buttonClicked.connect(lambda _: _on_local_mode_changed())
        # connect thumb size slider with debounce to avoid rebuilding on every small movement
        try:
            # timer used to debounce heavy thumbnail regeneration
            self._thumb_slider_timer = QtCore.QTimer(self)
            self._thumb_slider_timer.setSingleShot(True)
            self._thumb_slider_timer.setInterval(180)
            self._thumb_slider_timer.timeout.connect(self._on_local_thumb_slider_changed)
            # quick-moving handler updates label immediately, syncs spinbox and restarts debounce timer
            def _thumb_moved(val):
                # spinbox is the source of truth for the numeric value; keep it synced
                try:
                    v = int(val)
                    if hasattr(self, 'thumb_size_spin') and self.thumb_size_spin.value() != v:
                        self.thumb_size_spin.blockSignals(True)
                        self.thumb_size_spin.setValue(v)
                        self.thumb_size_spin.blockSignals(False)
                except Exception:
                    pass
                try:
                    # sync spinbox without causing recursive signals
                    if hasattr(self, 'thumb_size_spin'):
                        try:
                            v = int(val)
                            if self.thumb_size_spin.value() != v:
                                self.thumb_size_spin.blockSignals(True)
                                self.thumb_size_spin.setValue(v)
                                self.thumb_size_spin.blockSignals(False)
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                        try:
                            # centralized interaction start (shows low-res placeholders)
                            try:
                                self._start_thumb_interaction()
                            except Exception:
                                pass
                            self._thumb_slider_timer.start()
                        except Exception:
                            pass
                except Exception:
                    pass
            self.thumb_size_slider.valueChanged.connect(_thumb_moved)
            # sync spinbox -> slider
            def _spin_changed(v):
                try:
                    if self.thumb_size_slider.value() != int(v):
                        self.thumb_size_slider.blockSignals(True)
                        self.thumb_size_slider.setValue(int(v))
                        self.thumb_size_slider.blockSignals(False)
                    try:
                        self._thumb_slider_timer.start()
                    except Exception:
                        pass
                except Exception:
                    pass
            self.thumb_size_spin.valueChanged.connect(_spin_changed)
            # install event filter on pages so mouse wheel anywhere on local page can adjust slider
            try:
                self.pages.installEventFilter(self)
            except Exception:
                pass
        except Exception:
            pass

        # interaction debounce timer: while interacting (scroll/slider/tab) we show low-res placeholders
        try:
            self._thumb_interaction_timer = QtCore.QTimer(self)
            self._thumb_interaction_timer.setSingleShot(True)
            self._thumb_interaction_timer.setInterval(360)
            self._thumb_interaction_timer.timeout.connect(self._end_thumb_interaction)
        except Exception:
            self._thumb_interaction_timer = None

        from aetherloom_core.local_browser_ui import configure as configure_local_browser
        configure_local_browser(self, local_page)
        self.pages.addWidget(local_page)

        # --- Page: API 管理 ---
        api_page = QtWidgets.QWidget()
        api_page.setObjectName('api_page_root')
        api_layout = QtWidgets.QVBoxLayout(api_page)
        try:
            api_layout.setContentsMargins(0, 0, 0, 0)
            api_layout.setSpacing(0)
        except Exception:
            pass
        self.api_page = api_page

        api_scroll = QtWidgets.QScrollArea()
        api_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        api_scroll.setWidgetResizable(True)
        api_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._api_scroll = api_scroll
        api_layout.addWidget(api_scroll)

        api_container = QtWidgets.QWidget()
        api_scroll.setWidget(api_container)
        api_inner = QtWidgets.QVBoxLayout(api_container)
        try:
            api_inner.setContentsMargins(24, 24, 24, 24)
            api_inner.setSpacing(16)
        except Exception:
            pass

        api_page.setStyleSheet(
            """
            #api_page_root QLabel { font-size: 11.5pt; }
            #api_page_root QLineEdit, #api_page_root QSpinBox { font-size: 11pt; }
            #api_page_root #settingsCard QLabel { font-size: 10.5pt; }
            """
        )

        api_hero = QtWidgets.QFrame()
        api_hero.setObjectName('settingsHeroFrame')
        api_hero_layout = QtWidgets.QVBoxLayout(api_hero)
        api_hero_layout.setSpacing(6)
        api_title = QtWidgets.QLabel('API 管理中心')
        api_title.setObjectName('settingsHeroTitle')
        api_desc = QtWidgets.QLabel('配置视觉、翻译和大语言模型，测试响应并获取供应商提供的模型列表。')
        api_desc.setObjectName('settingsHint')
        api_desc.setWordWrap(True)
        api_hero_layout.addWidget(api_title)
        api_hero_layout.addWidget(api_desc)
        api_inner.addWidget(api_hero)

        # --- API Key 管理 折叠面板 ---
        from aetherloom_core.api_credentials import get_credentials as api_credentials_for
        try:
            apikey_panel = QtWidgets.QFrame()
            apikey_panel.setObjectName('apikeyPanel')
            apikey_layout = QtWidgets.QVBoxLayout(apikey_panel)
            apikey_layout.setContentsMargins(18, 12, 18, 12)
            apikey_layout.setSpacing(6)

            apikey_toggle = QtWidgets.QToolButton()
            apikey_toggle.setText('API 密钥管理')
            apikey_toggle.setObjectName('apiCardToggle')
            apikey_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            apikey_toggle.setArrowType(Qt.RightArrow)
            apikey_toggle.toggled.connect(lambda checked: apikey_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow))
            self._api_keys_toggle = apikey_toggle
            apikey_toggle.setCheckable(True)
            apikey_toggle.setChecked(False)
            apikey_toggle.setMinimumHeight(44)
            apikey_toggle.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            # header row: toggle left, 保存 button right
            apikey_header = QtWidgets.QHBoxLayout()
            apikey_header.setContentsMargins(0, 0, 0, 0)
            apikey_header.addWidget(apikey_toggle)
            apikey_header.addStretch(1)
            apikey_save_btn = QtWidgets.QPushButton('保存密钥')
            apikey_save_btn.setObjectName('apiPrimaryButton')
            apikey_save_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            apikey_save_btn.setMinimumHeight(36)
            apikey_save_btn.setVisible(False)
            apikey_header.addWidget(apikey_save_btn)
            apikey_layout.addLayout(apikey_header)

            # apikeys store (in-memory) and file path
            try:
                self._apikeys_file = os.path.join(current_dir, 'apikeys.json')
                try:
                    with open(self._apikeys_file, 'r', encoding='utf-8') as f:
                        self._apikeys = json.load(f) or {}
                except Exception:
                    self._apikeys = {}
            except Exception:
                self._apikeys_file = None
                self._apikeys = {}

            # map provider_key -> widgets for apikey panel rows
            self.apikey_rows = {}

            apikey_expand = QtWidgets.QWidget()
            apikey_expand_layout = QtWidgets.QVBoxLayout(apikey_expand)
            apikey_expand_layout.setContentsMargins(8, 8, 8, 8)
            apikey_expand_layout.setSpacing(8)
            apikey_expand.setVisible(False)

            # container for provider key rows (one per provider)
            keys_container = QtWidgets.QVBoxLayout()
            keys_container.setSpacing(8)
            keys_holder = QtWidgets.QWidget()
            keys_holder.setLayout(keys_container)
            apikey_expand_layout.addWidget(keys_holder)

            def _get_all_providers():
                # return a list of unique providers (display_name, provider_key), excluding 'custom'
                items = []
                try:
                    if api_manager and hasattr(api_manager, 'PROVIDERS'):
                        provs = getattr(api_manager, 'PROVIDERS') or {}
                        for pk, pv in provs.items():
                            if pk == 'custom':
                                continue
                            name = pv.get('name', pk) if isinstance(pv, dict) else pk
                            items.append((name, pk))
                    else:
                        seen_keys = set()
                        if api_manager and hasattr(api_manager, 'get_providers'):
                            for cat in getattr(self, 'api_categories', []):
                                cat_key = cat[0]
                                for p in (api_manager.get_providers(cat_key) or []):
                                    pk = p.get('key')
                                    if not pk or pk in seen_keys or pk == 'custom':
                                        continue
                                    seen_keys.add(pk)
                                    name = p.get('name', pk)
                                    items.append((name, pk))
                        elif isinstance(getattr(self, 'api_catalog', None), dict):
                            seen_keys = set()
                            for cat_key, provs in (self.api_catalog or {}).items():
                                for p in provs:
                                    pk = p.get('key')
                                    if not pk or pk in seen_keys or pk == 'custom':
                                        continue
                                    seen_keys.add(pk)
                                    name = p.get('name', pk)
                                    items.append((name, pk))
                except Exception:
                    pass
                try:
                    items = sorted(items, key=lambda x: x[0].lower())
                except Exception:
                    pass
                return items

            def _make_provider_row(display, prov_key):
                row = QtWidgets.QWidget(keys_holder)
                h = QtWidgets.QGridLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(8)
                label = QtWidgets.QLabel(display, row)
                label.setWordWrap(True)
                label.setMinimumWidth(0)

                get_btn = QtWidgets.QPushButton('获取密钥', row)
                get_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                btn_holder = QtWidgets.QWidget(row)
                btn_layout = QtWidgets.QHBoxLayout(btn_holder)
                btn_layout.setContentsMargins(0, 0, 0, 0)
                btn_layout.addStretch()
                btn_layout.addWidget(get_btn)

                # Inputs: for Baidu we need AppID + Secret, otherwise a single API Key field
                baidu_appid = QtWidgets.QLineEdit(row)
                baidu_appid.setPlaceholderText('请输入 AppID')
                baidu_secret = QtWidgets.QLineEdit(row)
                baidu_secret.setPlaceholderText('请输入 Secret')
                baidu_secret.setEchoMode(QtWidgets.QLineEdit.Password)
                baidu_appid.setVisible(prov_key == 'baidu_translate')
                baidu_secret.setVisible(prov_key == 'baidu_translate')

                key_edit = QtWidgets.QLineEdit(row)
                key_edit.setPlaceholderText('请输入 API Key，仅保存在本地')
                key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
                key_edit.setMinimumWidth(0)
                # hide API key input for Baidu rows (we show AppID/Secret instead)
                key_edit.setVisible(prov_key != 'baidu_translate')

                # Keep inputs on their own row so provider names never force overflow.
                h.addWidget(label, 0, 0)
                h.addWidget(btn_holder, 0, 1, Qt.AlignRight)
                h.setColumnStretch(0, 1)
                h.setColumnStretch(1, 1)
                if prov_key == 'baidu_translate':
                    baidu_appid.setMinimumWidth(0)
                    baidu_secret.setMinimumWidth(0)
                    h.addWidget(baidu_appid, 1, 0)
                    h.addWidget(baidu_secret, 1, 1)
                else:
                    h.addWidget(key_edit, 1, 0, 1, 2)

                # prefill stored values
                try:
                    filled = False
                    for cat in getattr(self, 'api_categories', []):
                        cat_key = cat[0]
                        providers = api_manager.get_providers(cat_key) if api_manager and hasattr(api_manager, 'get_providers') else self.api_catalog.get(cat_key, []) if isinstance(getattr(self, 'api_catalog', None), dict) else []
                        for p in (providers or []):
                            if p.get('key') == prov_key:
                                prof = self._get_api_provider_profile(cat_key, prov_key)
                                if isinstance(prof, dict):
                                    key_edit.setText(str(prof.get('api_key','')))
                                    baidu_appid.setText(str(prof.get('appid','')))
                                    baidu_secret.setText(str(prof.get('secret','')))
                                    filled = True
                                    break
                        if filled:
                            break
                except Exception:
                    pass

                # also prefill from apikeys.json (self._apikeys) if present
                try:
                    if isinstance(getattr(self, '_apikeys', None), dict):
                        rec = api_credentials_for(self._apikeys, prov_key)
                        if prov_key == 'baidu_translate':
                            if isinstance(rec, dict):
                                baidu_appid.setText(rec.get('appid', ''))
                                baidu_secret.setText(rec.get('secret', ''))
                        else:
                            if isinstance(rec, dict):
                                key_edit.setText(rec.get('api_key', ''))
                except Exception:
                    pass

                def _on_get():
                    try:
                        url = api_manager.get_api_key_portal(prov_key) if api_manager and hasattr(api_manager, 'get_api_key_portal') else ''
                        if url:
                            webbrowser.open(url)
                    except Exception:
                        pass

                def _save():
                    try:
                        val = key_edit.text().strip()
                        appid = baidu_appid.text().strip()
                        secret = baidu_secret.text().strip()

                        # update in-memory apikeys store only; file write happens on 顶部 保存 click
                        try:
                            if prov_key == 'baidu_translate':
                                if appid or secret:
                                    self._apikeys[prov_key] = {'appid': appid, 'secret': secret}
                                elif prov_key in self._apikeys:
                                    del self._apikeys[prov_key]
                            else:
                                if val:
                                    self._apikeys[prov_key] = {'api_key': val}
                                elif prov_key in self._apikeys:
                                    del self._apikeys[prov_key]
                        except Exception:
                            pass
                    except Exception:
                        pass

                get_btn.clicked.connect(_on_get)
                key_edit.editingFinished.connect(_save)
                baidu_appid.editingFinished.connect(_save)
                baidu_secret.editingFinished.connect(_save)

                # register widgets so header 保存 can write them to apikeys.json
                try:
                    self.apikey_rows[prov_key] = {'key_edit': key_edit, 'appid': baidu_appid, 'secret': baidu_secret}
                except Exception:
                    pass

                return row

            # build provider rows once
            try:
                providers = _get_all_providers()
                for display, pk in providers:
                    row = _make_provider_row(display, pk)
                    keys_container.addWidget(row)
            except Exception:
                pass

            # write apikeys.json when 保存 clicked
            def _write_apikeys_file(_checked=False, *, show_feedback=True):
                try:
                    if not getattr(self, '_apikeys_file', None):
                        return
                    managed_keys = set((getattr(self, 'apikey_rows', {}) or {}).keys())
                    # ensure in-memory store reflects current inputs
                    try:
                        for pk, widgets in (getattr(self, 'apikey_rows', {}) or {}).items():
                            try:
                                if pk == 'baidu_translate':
                                    appid = (widgets.get('appid') and widgets['appid'].text().strip()) or ''
                                    secret = (widgets.get('secret') and widgets['secret'].text().strip()) or ''
                                    if appid or secret:
                                        self._apikeys[pk] = {'appid': appid, 'secret': secret}
                                    elif pk in self._apikeys:
                                        del self._apikeys[pk]
                                else:
                                    ak = (widgets.get('key_edit') and widgets['key_edit'].text().strip()) or ''
                                    if ak:
                                        self._apikeys[pk] = {'api_key': ak}
                                    elif pk in self._apikeys:
                                        del self._apikeys[pk]
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Also collect any custom per-category API keys (saved when user typed a custom provider and entered a key)
                    try:
                        for cat_key, fields in (getattr(self, 'api_config_fields', {}) or {}).items():
                            try:
                                prov_combo = fields.get('provider')
                                if prov_combo is None:
                                    continue
                                prov_val = prov_combo.currentData()
                                if prov_val == 'custom':
                                    managed_keys.add(f'custom_{cat_key}')
                                    try:
                                        akw = fields.get('api_key')
                                        if akw is not None:
                                            ak = akw.text().strip() or ''
                                            if ak:
                                                self._apikeys[f'custom_{cat_key}'] = {'api_key': ak}
                                            elif f'custom_{cat_key}' in self._apikeys:
                                                del self._apikeys[f'custom_{cat_key}']
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Explicitly cleared managed keys stay deleted; unrelated keys survive.
                    from aetherloom_core.api_manager_ui import persist_credentials
                    self._apikeys = persist_credentials(self._apikeys_file, self._apikeys or {}, managed_keys)

                    # refresh per-category UI fields by applying values from self._apikeys
                    try:
                        for k, v in (getattr(self, 'api_config_fields', {}) or {}).items():
                            try:
                                combo = v.get('provider')
                                if combo is None:
                                    continue
                                prov = combo.currentData()
                                api_key_widget = v.get('api_key')
                                baidu_appid_w = v.get('baidu_appid')
                                baidu_secret_w = v.get('baidu_secret')
                                stack = v.get('api_key_stack')
                                # If provider is custom, prefer per-category saved key (custom_<cat>)
                                if not prov or prov == 'custom':
                                    try:
                                        rec = api_credentials_for(self._apikeys, 'custom', k)
                                        ak = rec.get('api_key', '')
                                        if api_key_widget is not None:
                                            api_key_widget._api_key_dirty = False
                                        if ak and api_key_widget is not None:
                                            api_key_widget.setText(ak)
                                            api_key_widget.setReadOnly(False)
                                        else:
                                            if api_key_widget is not None:
                                                api_key_widget.setText('')
                                                api_key_widget.setReadOnly(False)
                                                api_key_widget.setPlaceholderText('可输入自定义密钥')
                                    except Exception:
                                        try:
                                            if api_key_widget is not None:
                                                api_key_widget.setReadOnly(False)
                                        except Exception:
                                            pass
                                else:
                                    if prov == 'baidu_translate':
                                        rec = api_credentials_for(self._apikeys, 'baidu_translate')
                                        appid = rec.get('appid', '')
                                        secret = rec.get('secret', '')
                                        if baidu_appid_w is not None:
                                            baidu_appid_w.setText(appid)
                                            baidu_appid_w.setEnabled(False if not appid else False)
                                        if baidu_secret_w is not None:
                                            baidu_secret_w.setText(secret)
                                            baidu_secret_w.setEnabled(False if not secret else False)
                                        if api_key_widget is not None:
                                            api_key_widget.setReadOnly(True)
                                        if stack is not None:
                                            try:
                                                stack.setCurrentIndex(1)
                                            except Exception:
                                                pass
                                    else:
                                        rec = api_credentials_for(self._apikeys, prov)
                                        ak = rec.get('api_key', '')
                                        if api_key_widget is not None:
                                            api_key_widget.setText(ak if ak else '')
                                            if not ak:
                                                api_key_widget.setPlaceholderText('请打开上方apikey管理输入密钥并保存')
                                            api_key_widget.setReadOnly(True)
                                            if stack is not None:
                                                try:
                                                    stack.setCurrentIndex(0)
                                                except Exception:
                                                    pass
                            except Exception:
                                pass
                    except Exception:
                        pass

                    if show_feedback:
                        QtWidgets.QMessageBox.information(self, '保存完成', 'API 密钥已保存。')
                except Exception:
                    try:
                        QtWidgets.QMessageBox.warning(self, '保存失败', '写入 apikeys.json 时出现错误，查看日志。')
                    except Exception:
                        pass
                    _api_debug('failed to write apikeys.json')

            self._write_apikeys_file = _write_apikeys_file

            try:
                apikey_save_btn.clicked.connect(_write_apikeys_file)
            except Exception:
                pass

            apikey_layout.addWidget(apikey_expand)
            api_inner.addWidget(apikey_panel)

            apikey_toggle.toggled.connect(lambda v: apikey_expand.setVisible(v))
            apikey_toggle.toggled.connect(apikey_save_btn.setVisible)
        except Exception:
            pass

        api_cards = QtWidgets.QVBoxLayout()
        api_cards.setSpacing(14)
        api_inner.addLayout(api_cards)

        from aetherloom_core.api_manager_ui import CollapsibleApiCard, ApiProbeController, apply_theme as apply_api_theme
        self.api_config_fields = {}
        self._api_model_cards = {}
        self._api_probe_controllers = {}
        self._ensure_api_provider_profile_store()

        visible_categories = ([(entry['key'], entry.get('name', entry['key']), entry.get('desc', ''))
                               for entry in api_manager.get_visible_api_categories()]
                              if api_manager and hasattr(api_manager, 'get_visible_api_categories')
                              else [entry for entry in getattr(self, 'api_categories', [])
                                    if entry[0] in ('vision', 'llm', 'translator')])
        for key, title, subtitle in visible_categories:
            card = CollapsibleApiCard(title, subtitle)
            card_layout = card.body_layout
            self._api_model_cards[key] = card

            form = QtWidgets.QFormLayout()
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
            form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
            try:
                form.setHorizontalSpacing(18)
                form.setVerticalSpacing(12)
            except Exception:
                pass

            cfg = self.api_settings.get(key, {}) if isinstance(self.api_settings, dict) else {}

            provider_combo = QtWidgets.QComboBox()
            provider_combo.setEditable(False)
            provider_combo.setMinimumWidth(0)
            provider_combo.setMinimumContentsLength(16)
            provider_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            provider_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            try:
                provider_combo.setMinimumHeight(36)
                provider_combo.setStyleSheet("QComboBox::drop-down { width: 36px; } QComboBox::down-arrow { width: 18px; height: 18px; }")
            except Exception:
                pass
            self._install_combo_wheel_blocker(provider_combo)
            provider_items = self._provider_items_for_category(key)
            for text, val in provider_items:
                provider_combo.addItem(text, val)

            saved_provider = str(cfg.get('provider', '') or '')
            provider_found = False
            for i in range(provider_combo.count()):
                candidate = provider_combo.itemData(i)
                if candidate == saved_provider or (candidate == 'custom' and saved_provider.startswith('custom_')):
                    provider_combo.setCurrentIndex(i)
                    provider_found = True
                    break
            if not provider_found:
                try:
                    if provider_combo.count() > 0:
                        provider_combo.setCurrentIndex(0)
                        saved_provider = provider_combo.itemData(0)
                    else:
                        provider_combo.addItem('自定义', 'custom')
                        provider_combo.setCurrentIndex(0)
                except Exception:
                    pass

            endpoint = QtWidgets.QLineEdit(str(cfg.get('endpoint', '')))
            endpoint.setPlaceholderText('https://api.example.com/v1')
            api_key = QtWidgets.QLineEdit(str(cfg.get('api_key', '')))
            api_key.setEchoMode(QtWidgets.QLineEdit.Password)
            api_key.setPlaceholderText('密钥仅保存在本地')
            baidu_appid = QtWidgets.QLineEdit()
            baidu_appid.setPlaceholderText('Baidu AppID')
            baidu_secret = QtWidgets.QLineEdit()
            baidu_secret.setPlaceholderText('Baidu Secret')
            baidu_secret.setEchoMode(QtWidgets.QLineEdit.Password)
            api_key_btn = QtWidgets.QPushButton('获取密钥')
            api_key_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            api_key_btn.setEnabled(False)
            model_combo = None
            model_docs_btn = None
            model_row_widget = None
            if key != 'runninghub':
                model_combo = QtWidgets.QComboBox()
                model_combo.setEditable(True)
                model_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
                model_combo.setMinimumWidth(0)
                model_combo.setMinimumContentsLength(16)
                model_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                try:
                    model_combo.setMinimumHeight(36)
                    model_combo.setStyleSheet("QComboBox::drop-down { width: 36px; } QComboBox::down-arrow { width: 18px; height: 18px; }")
                except Exception:
                    pass
                self._install_combo_wheel_blocker(model_combo)
                try:
                    model_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
                except Exception:
                    pass
                model_docs_btn = QtWidgets.QPushButton('模型文档')
                model_docs_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                model_docs_btn.setEnabled(False)
                model_row = QtWidgets.QHBoxLayout()
                model_row.setContentsMargins(0, 0, 0, 0)
                model_row.setSpacing(8)
                model_row.addWidget(model_combo, 1)
                model_row.addWidget(model_docs_btn, 0, Qt.AlignTop)
                model_row_widget = QtWidgets.QWidget()
                model_row_widget.setLayout(model_row)

            timeout = QtWidgets.QSpinBox()
            timeout.setMaximumWidth(160)
            timeout.setRange(5, 600)
            timeout.setSingleStep(5)
            try:
                timeout.setValue(int(cfg.get('timeout', 30) or 30))
            except Exception:
                timeout.setValue(30)
            self._install_combo_wheel_blocker(timeout)

            for le in (endpoint, api_key, baidu_appid, baidu_secret):
                try:
                    le.setMinimumWidth(0)
                    le.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                    le.setClearButtonEnabled(True)
                except Exception:
                    pass

            api_key_stack = QtWidgets.QStackedWidget()
            try:
                api_key_stack.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
            except Exception:
                pass
            api_key_stack.addWidget(api_key)

            baidu_widget = QtWidgets.QWidget()
            baidu_form = QtWidgets.QFormLayout(baidu_widget)
            baidu_form.setContentsMargins(0, 0, 0, 0)
            try:
                baidu_form.setHorizontalSpacing(8)
                baidu_form.setVerticalSpacing(6)
            except Exception:
                pass
            baidu_form.addRow('AppID', baidu_appid)
            baidu_form.addRow('Secret', baidu_secret)
            api_key_stack.addWidget(baidu_widget)
            api_key_stack.currentChanged.connect(
                lambda _index, stack=api_key_stack: stack.setFixedHeight(stack.currentWidget().sizeHint().height()))

            api_key_row = QtWidgets.QHBoxLayout()
            api_key_row.setContentsMargins(0, 0, 0, 0)
            api_key_row.setSpacing(8)
            api_key_row.addWidget(api_key_stack, 1)
            custom_key_save_btn = QtWidgets.QPushButton('保存密钥')
            custom_key_save_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            api_key_row.addWidget(custom_key_save_btn)
            custom_key_save_btn.setVisible(provider_combo.currentData() == 'custom')
            provider_combo.currentIndexChanged.connect(
                lambda _index, combo=provider_combo, button=custom_key_save_btn:
                button.setVisible(combo.currentData() == 'custom'))
            custom_key_save_btn.clicked.connect(_write_apikeys_file)
            def _sync_custom_key_memory(_text=None, combo=provider_combo, widget=api_key, cat=key):
                if combo.currentData() != 'custom':
                    return
                widget._api_key_dirty = True
                value = widget.text().strip()
                if value:
                    self._apikeys[f'custom_{cat}'] = {'api_key': value}
                else:
                    self._apikeys.pop(f'custom_{cat}', None)
            def _save_custom_key_edit(combo=provider_combo, widget=api_key, sync=_sync_custom_key_memory):
                if combo.currentData() == 'custom' and getattr(widget, '_api_key_dirty', False):
                    sync()
                    _write_apikeys_file(show_feedback=False)
            api_key.textEdited.connect(_sync_custom_key_memory)
            api_key.editingFinished.connect(_save_custom_key_edit)
            api_key.setToolTip('自定义密钥修改后自动保存；其他密钥请在上方 API 密钥管理中修改。')
            api_key_row_widget = QtWidgets.QWidget()
            api_key_row_widget.setLayout(api_key_row)

            def _update_baidu_combined(*_args, appid_widget=baidu_appid,
                                       secret_widget=baidu_secret, key_widget=api_key):
                appid_val = appid_widget.text().strip()
                secret_val = secret_widget.text().strip()
                combined = appid_val
                if secret_val:
                    combined = f"{appid_val}:{secret_val}" if appid_val else f":{secret_val}"
                key_widget.setText(combined)

            try:
                baidu_appid.textEdited.connect(_update_baidu_combined)
                baidu_secret.textEdited.connect(_update_baidu_combined)
            except Exception:
                pass

            def _make_apply_provider_change(cat_key, provider_widget, endpoint_widget, model_widget, api_btn, cfg_snapshot, api_key_widget, timeout_widget, api_key_stack_widget=None, baidu_widgets=None, model_docs_btn=None, update_baidu_combined=_update_baidu_combined):
                def _apply(saved_model=None):
                    sel = provider_widget.currentData()
                    profile = self._get_api_provider_profile(cat_key, sel)
                    entry = self._find_provider_entry(cat_key, sel)
                    # adjust api_key placeholder per provider
                    try:
                        default_ph = '密钥仅保存在本地'
                        if sel == 'baidu_translate':
                            ph = 'Baidu: appid:secret（推荐）；或仅填 appid 并在 extra 中提供 secret'
                        elif sel == 'google_translate':
                            ph = 'Google: API key'
                        else:
                            ph = default_ph
                        api_key_widget.setPlaceholderText(ph)
                        api_key_widget.setToolTip(ph)
                    except Exception:
                        pass
                    is_baidu = sel == 'baidu_translate'
                    if api_key_stack_widget is not None:
                        try:
                            api_key_stack_widget.setCurrentIndex(1 if is_baidu else 0)
                        except Exception:
                            pass
                    if is_baidu and baidu_widgets:
                        appid_val = (profile.get('appid') if isinstance(profile, dict) else None) or ''
                        secret_val = (profile.get('secret') if isinstance(profile, dict) else None) or ''
                        if not appid_val and not secret_val:
                            combined = api_key_widget.text().strip()
                            appid_val = combined
                            secret_val = ''
                            if ':' in combined:
                                parts = combined.split(':', 1)
                                appid_val = parts[0].strip()
                                secret_val = parts[1].strip()
                        appid_widget = baidu_widgets.get('appid') if isinstance(baidu_widgets, dict) else None
                        secret_widget = baidu_widgets.get('secret') if isinstance(baidu_widgets, dict) else None
                        try:
                            if appid_widget is not None:
                                appid_widget.setText(appid_val)
                        except Exception:
                            pass
                        try:
                            if secret_widget is not None:
                                secret_widget.setText(secret_val)
                        except Exception:
                            pass
                        try:
                            update_baidu_combined()
                        except Exception:
                            pass
                    try:
                        url = ''
                        if api_manager and hasattr(api_manager, 'get_api_key_portal'):
                            url = api_manager.get_api_key_portal(sel) or ''
                        api_btn.setProperty('api_url', url)
                        api_btn.setEnabled(bool(url))
                    except Exception:
                        pass
                    custom_cache = self.api_custom_cache if isinstance(getattr(self, 'api_custom_cache', None), dict) else {}
                    cached = custom_cache.get(cat_key, {}) if sel == 'custom' and isinstance(custom_cache, dict) else {}
                    docs_url = ''
                    if api_manager and hasattr(api_manager, 'get_model_list_url'):
                        try:
                            docs_url = api_manager.get_model_list_url(sel) or ''
                        except Exception:
                            docs_url = ''
                    if model_docs_btn is not None:
                        try:
                            if sel == 'custom':
                                docs_url = ''
                            model_docs_btn.setEnabled(bool(docs_url))
                            model_docs_btn.setProperty('model_docs_url', docs_url)
                        except Exception:
                            pass
                    if sel == 'custom':
                        endpoint_widget.setReadOnly(False)
                        base_endpoint = ''
                        base_api_key = ''
                        base_model = ''
                        base_timeout = 30
                        if cached:
                            base_endpoint = cached.get('endpoint', '') or ''
                            base_api_key = cached.get('api_key', '') or ''
                            base_model = cached.get('model', '') or ''
                            try:
                                base_timeout = int(cached.get('timeout', base_timeout) or base_timeout)
                            except Exception:
                                base_timeout = base_timeout
                        elif isinstance(cfg_snapshot.get('provider'), str) and cfg_snapshot.get('provider').startswith('custom'):
                            base_endpoint = cfg_snapshot.get('endpoint', '') or ''
                            base_api_key = cfg_snapshot.get('api_key', '') or ''
                            base_model = cfg_snapshot.get('model', '') or ''
                            try:
                                base_timeout = int(cfg_snapshot.get('timeout', base_timeout) or base_timeout)
                            except Exception:
                                base_timeout = base_timeout
                        if isinstance(profile, dict) and profile:
                            base_endpoint = profile.get('endpoint', base_endpoint) or base_endpoint
                            base_api_key = profile.get('api_key', base_api_key) or base_api_key
                            base_model = profile.get('model', base_model) or base_model
                            try:
                                base_timeout = int(profile.get('timeout', base_timeout) or base_timeout)
                            except Exception:
                                pass
                        try:
                            endpoint_widget.setText(str(base_endpoint))
                        except Exception:
                            pass
                        try:
                            api_key_widget.setText(str(base_api_key))
                        except Exception:
                            pass
                        if timeout_widget is not None:
                            try:
                                timeout_widget.setValue(int(base_timeout or 30))
                            except Exception:
                                try:
                                    timeout_widget.setValue(30)
                                except Exception:
                                    pass
                        if model_widget is not None:
                            try:
                                model_widget.blockSignals(True)
                                model_widget.clear()
                                model_val = saved_model if saved_model is not None else base_model
                                model_widget.setEditText(str(model_val or ''))
                            except Exception:
                                pass
                            finally:
                                try:
                                    model_widget.blockSignals(False)
                                except Exception:
                                    pass
                        try:
                            api_btn.setProperty('api_url', '')
                            api_btn.setEnabled(False)
                        except Exception:
                            pass
                        return
                    if entry is None:
                        endpoint_widget.setReadOnly(False)
                    else:
                        endpoint_widget.setReadOnly(sel != 'ollama')
                        try:
                            saved_endpoint = (cfg_snapshot.get('endpoint')
                                              if sel == cfg_snapshot.get('provider') else '')
                            profile_endpoint = profile.get('endpoint') if isinstance(profile, dict) else ''
                            endpoint_widget.setText(str(profile_endpoint or saved_endpoint or entry.get('endpoint', '')))
                        except Exception:
                            pass
                    if model_widget is not None:
                        models = []
                        if entry is not None:
                            try:
                                models = entry.get('models', []) or []
                            except Exception:
                                models = []
                        try:
                            model_widget.blockSignals(True)
                            model_widget.clear()
                            for m in models:
                                model_widget.addItem(str(m))
                            if saved_model:
                                model_widget.setEditText(str(saved_model))
                            elif models:
                                model_widget.setCurrentIndex(0)
                            else:
                                model_widget.setEditText(str(cfg_snapshot.get('model', '')))
                            if isinstance(profile, dict):
                                prof_model = profile.get('model')
                                if prof_model:
                                    model_widget.setEditText(str(prof_model))
                        except Exception:
                            pass
                        finally:
                            try:
                                model_widget.blockSignals(False)
                            except Exception:
                                pass
                        try:
                            placeholder = '输入或选择模型名称'
                            if cat_key == 'translator':
                                if sel == 'baidu_translate':
                                    placeholder = '百度翻译模型无需填写，留空即可'
                                elif sel == 'google_translate':
                                    placeholder = 'Google 翻译仅支持 nmt（留空即为 nmt）'
                                else:
                                    placeholder = '翻译模型可选项'
                            editor = model_widget.lineEdit()
                            if editor is not None:
                                editor.setPlaceholderText(placeholder)
                                editor.setToolTip(placeholder)
                        except Exception:
                            pass
                    desired_api_key = None
                    if isinstance(profile, dict) and profile.get('api_key') is not None:
                        desired_api_key = profile.get('api_key')
                    elif sel != cfg_snapshot.get('provider'):
                        desired_api_key = ''
                    if desired_api_key is not None:
                        try:
                            api_key_widget.setText(str(desired_api_key))
                        except Exception:
                            pass
                    if timeout_widget is not None and isinstance(profile, dict) and profile.get('timeout') is not None:
                        try:
                            timeout_widget.setValue(int(profile.get('timeout')))
                        except Exception:
                            pass
                return _apply

            baidu_field_map = {'appid': baidu_appid, 'secret': baidu_secret}
            apply_provider_change = _make_apply_provider_change(
                key,
                provider_combo,
                endpoint,
                model_combo,
                api_key_btn,
                cfg,
                api_key,
                timeout,
                api_key_stack,
                baidu_field_map,
                model_docs_btn,
            )
            apply_provider_change(saved_model=str(cfg.get('model', '')))
            try:
                provider_combo._current_provider_key = provider_combo.currentData()
            except Exception:
                provider_combo._current_provider_key = None

            def _on_provider_changed(_=None, handler=apply_provider_change, combo=provider_combo, cat_key=key):
                try:
                    prev = getattr(combo, '_current_provider_key', None)
                    if prev:
                        self._snapshot_api_provider_fields(cat_key, prev)
                except Exception:
                    pass
                handler()
                try:
                    combo._current_provider_key = combo.currentData()
                except Exception:
                    pass

            try:
                provider_combo.currentIndexChanged.connect(_on_provider_changed)
            except Exception:
                pass

            try:
                api_key_btn.clicked.connect(lambda _=None, btn=api_key_btn: self._open_api_portal(btn))
            except Exception:
                pass
            if model_docs_btn is not None:
                try:
                    model_docs_btn.clicked.connect(lambda _=None, btn=model_docs_btn: self._open_model_docs(btn))
                except Exception:
                    pass

            form.addRow('服务提供方', provider_combo)
            form.addRow('接口地址', endpoint)

            form.addRow('API 密钥', api_key_row_widget)
            if model_combo is not None:
                form.addRow('模型名称', model_row_widget or model_combo)
            form.addRow('超时 (s)', timeout)
            card_layout.addLayout(form)

            self.api_config_fields[key] = {
                'provider': provider_combo,
                'endpoint': endpoint,
                'api_key': api_key,
                'model': model_combo,
                'model_docs_btn': model_docs_btn,
                'timeout': timeout,
                'api_key_stack': api_key_stack,
                'baidu_appid': baidu_appid,
                'baidu_secret': baidu_secret,
            }

            # Sync UI changes immediately into in-memory settings and persist
            try:
                def _sync_api_settings_now(*_args):
                    try:
                        if hasattr(self, '_collect_api_settings_from_ui'):
                            try:
                                self._collect_api_settings_from_ui()
                            except Exception:
                                pass
                        try:
                            # ensure in-memory settings mirror the collected api_settings
                            try:
                                if isinstance(getattr(self, 'api_settings', None), dict) and isinstance(getattr(self, 'settings', None), dict):
                                    self.settings['api_settings'] = self.api_settings
                            except Exception:
                                pass
                            self._save_settings()
                        except Exception:
                            pass
                    except Exception:
                        pass

                try:
                    provider_combo.currentIndexChanged.connect(_sync_api_settings_now)
                except Exception:
                    pass
                try:
                    endpoint.editingFinished.connect(_sync_api_settings_now)
                except Exception:
                    pass
                try:
                    api_key.editingFinished.connect(_sync_api_settings_now)
                except Exception:
                    pass
                try:
                    timeout.valueChanged.connect(_sync_api_settings_now)
                except Exception:
                    pass
                if model_combo is not None:
                    try:
                        model_combo.currentTextChanged.connect(_sync_api_settings_now)
                    except Exception:
                        try:
                            model_combo.editTextChanged.connect(_sync_api_settings_now)
                        except Exception:
                            pass
            except Exception:
                pass

            # sync apikeys from apikeys.json into these fields; non-custom providers are read-only
            def _sync_apikey_from_store(_=None, prov_combo=provider_combo, api_key_widget=api_key, baidu_appid_widget=baidu_appid, baidu_secret_widget=baidu_secret, key_stack=api_key_stack, cat_key=key, endpoint_widget=endpoint):
                try:
                    _api_debug(f"_sync_apikey_from_store called for combo {getattr(prov_combo, 'objectName', lambda: '')()} currentData={prov_combo.currentData() if prov_combo is not None else 'n/a'} cat={cat_key}")
                    prov = prov_combo.currentData()
                    # treat explicit 'custom' and forms like 'custom_<category>' as custom providers
                    if (not prov) or prov == 'custom' or (isinstance(prov, str) and prov.startswith('custom_')):
                        # for custom provider: if a saved per-category key exists, populate and lock it;
                        # otherwise allow typing. endpoint remains editable for custom.
                        try:
                            # prefer explicit provider key if provided (e.g. 'custom_llm'), else fall back to f'custom_{cat_key}'
                            lookup_key = prov if isinstance(prov, str) and prov.startswith('custom_') else f'custom_{cat_key}'
                            rec = api_credentials_for(self._apikeys, lookup_key)
                            ak = rec.get('api_key', '')
                            if ak:
                                api_key_widget.setText(ak)
                                api_key_widget.setReadOnly(False)
                            else:
                                api_key_widget.setText('')
                                api_key_widget.setReadOnly(False)
                                api_key_widget.setPlaceholderText('可输入自定义密钥')
                        except Exception:
                            try:
                                api_key_widget.setReadOnly(False)
                            except Exception:
                                pass
                        try:
                            baidu_appid_widget.setEnabled(True)
                            baidu_secret_widget.setEnabled(True)
                        except Exception:
                            pass
                        try:
                            endpoint_widget.setReadOnly(False)
                        except Exception:
                            pass
                        return

                    # non-custom providers: read-only and populated from store
                    if prov == 'baidu_translate':
                        rec = api_credentials_for(self._apikeys, 'baidu_translate')
                        appid = rec.get('appid', '')
                        secret = rec.get('secret', '')
                        if appid or secret:
                            baidu_appid_widget.setText(appid)
                            baidu_secret_widget.setText(secret)
                            baidu_appid_widget.setEnabled(False)
                            baidu_secret_widget.setEnabled(False)
                        else:
                            baidu_appid_widget.setText('')
                            baidu_secret_widget.setText('')
                            baidu_appid_widget.setPlaceholderText('请打开上方apikey管理输入密钥并保存')
                            baidu_appid_widget.setEnabled(False)
                            baidu_secret_widget.setEnabled(False)
                        key_stack.setCurrentIndex(1)
                        api_key_widget.setReadOnly(True)
                    elif prov == 'ollama':
                        # Ollama runs locally; no API key required. Endpoint should remain editable.
                        try:
                            api_key_widget.setText('')
                            api_key_widget.setReadOnly(True)
                        except Exception:
                            pass
                        try:
                            endpoint_widget.setReadOnly(False)
                        except Exception:
                            pass
                        if key_stack is not None:
                            try:
                                key_stack.setCurrentIndex(0)
                            except Exception:
                                pass
                    else:
                        rec = api_credentials_for(self._apikeys, prov)
                        ak = rec.get('api_key', '')
                        if ak:
                            api_key_widget.setText(ak)
                        else:
                            api_key_widget.setText('')
                            api_key_widget.setPlaceholderText('请打开上方apikey管理输入密钥并保存')
                        api_key_widget.setReadOnly(True)
                        key_stack.setCurrentIndex(0)
                except Exception:
                    pass

            try:
                provider_combo.currentIndexChanged.connect(_sync_apikey_from_store)
                # initial sync
                _sync_apikey_from_store()
            except Exception:
                pass

            self._api_probe_controllers[key] = ApiProbeController(self, key, self.api_config_fields[key], card)
            api_cards.addWidget(card)

        # API 按钮 行 已移除 - API 配置（不含 apikeys）将在关闭应用时自动保存

        def _collect_api_settings_from_ui():
            # Hidden categories keep their settings; only visible cards are merged.
            import copy
            collected = copy.deepcopy(self.api_settings) if isinstance(self.api_settings, dict) else {}
            custom_cache = self.api_custom_cache if isinstance(getattr(self, 'api_custom_cache', None), dict) else {}
            if not isinstance(custom_cache, dict):
                custom_cache = {}
            try:
                for k, fields in getattr(self, 'api_config_fields', {}).items():
                    current_provider = (fields['provider'].currentData() if hasattr(fields['provider'], 'currentData') else '') or str(fields['provider'].currentText()).strip()
                    # normalize custom provider to per-category key so it can be persisted and looked up
                    try:
                            if current_provider == 'custom':
                                current_provider = f'custom_{k}'
                            elif isinstance(current_provider, str) and current_provider.startswith('custom_'):
                                # already in desired form
                                pass
                    except Exception:
                        pass
                    try:
                        self._snapshot_api_provider_fields(k, current_provider)
                    except Exception:
                        pass
                    # Do NOT persist apikeys into settings.json — omit the api_key field entirely
                    collected[k] = {
                        'provider': current_provider,
                        'endpoint': fields['endpoint'].text().strip(),
                        'model': ((fields['model'].currentText() if hasattr(fields['model'], 'currentText') else str(fields['model'].text())).strip()) if fields.get('model') is not None else '',
                        'timeout': int(fields['timeout'].value()),
                    }
                    # for custom provider (either 'custom' or 'custom_<category>'), persist endpoint/model/timeout (no api_key)
                    try:
                        prov = str(collected[k].get('provider', '') or '')
                        if prov == 'custom' or prov.startswith('custom_'):
                            # store under the category key (k)
                            custom_cache[k] = {
                                'endpoint': collected[k]['endpoint'],
                                'model': collected[k]['model'],
                                'timeout': collected[k]['timeout'],
                            }
                    except Exception:
                        pass
            except Exception:
                pass
            self.api_settings = collected
            self.api_custom_cache = custom_cache
            return collected

        self._collect_api_settings_from_ui = _collect_api_settings_from_ui

        # Ensure apikeys from apikeys.json populate API cards on startup
        try:
            def _sync_all_apikeys_on_start():
                try:
                    # ensure in-memory apikeys loaded
                    if not isinstance(getattr(self, '_apikeys', None), dict):
                        try:
                            if getattr(self, '_apikeys_file', None) and os.path.exists(self._apikeys_file):
                                with open(self._apikeys_file, 'r', encoding='utf-8') as f:
                                    self._apikeys = json.load(f) or {}
                            else:
                                self._apikeys = {}
                        except Exception:
                            self._apikeys = {}
                    for fields in (getattr(self, 'api_config_fields', {}) or {}).values():
                        try:
                            combo = fields.get('provider')
                            if combo is None:
                                continue
                            # trigger the combo's currentIndexChanged to force sync handler
                            try:
                                combo.currentIndexChanged.emit(combo.currentIndex())
                            except Exception:
                                try:
                                    combo.setCurrentIndex(combo.currentIndex())
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
            # run once after UI setup to populate fields
            QtCore.QTimer.singleShot(60, _sync_all_apikeys_on_start)
        except Exception:
            pass

        def _save_api_settings_now():
            try:
                _collect_api_settings_from_ui()
                self._save_settings()
                msg = 'API 配置已保存到 settings.json'
                try:
                    if hasattr(self, 'log'):
                        self.log(msg)
                except Exception:
                    pass
                try:
                    QtWidgets.QMessageBox.information(self, '已保存', msg)
                except Exception:
                    pass
            except Exception:
                pass

        def _reload_api_settings_now():
            try:
                loaded = self._load_settings() or {}
                self._apply_settings(loaded, apply_window_geometry=False, apply_page_index=False)
                try:
                    QtWidgets.QMessageBox.information(self, '已同步', '已从 settings.json 重新加载。')
                except Exception:
                    pass
            except Exception:
                pass

        # 保存/重新加载 按钮已移除; use app close to persist API config (sans apikeys)

        api_inner.addStretch(1)

        from aetherloom_core.ui.preferences import configure_api
        configure_api(self, api_inner, api_hero)
        apply_api_theme(self, getattr(self, '_theme_mode', 'dark'))
        self.pages.addWidget(api_page)

        # --- Page: Runninghub 应用 (placeholder empty page) ---
        from aetherloom_core.rh_dashboard import Dashboard, AppCard, TaskPanel
        self._rh_dashboard = Dashboard(self)
        runninghub_page = QtWidgets.QWidget()
        runninghub_layout = QtWidgets.QVBoxLayout(runninghub_page)
        try:
            runninghub_layout.setContentsMargins(32, 32, 32, 32)
            runninghub_layout.setSpacing(8)
        except Exception:
            pass
        # Top controls: host selector + apikey input
        try:
            # First row: label + host dropdown
            top_row = QtWidgets.QHBoxLayout()
            top_row.setSpacing(8)
            host_label = QtWidgets.QLabel('选择Runninghub网址：')
            try:
                host_label.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass
            self.rh_host_combo = QtWidgets.QComboBox()
            self.rh_host_combo.addItems(['www.runninghub.cn', 'www.runninghub.ai'])
            try:
                self.rh_host_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                self.rh_host_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
            except Exception:
                pass
            # initialize host selection from settings if available
            try:
                sel = None
                if isinstance(getattr(self, 'settings', None), dict):
                    sel = self.settings.get('runninghub_host')
                if sel:
                    # find matching index
                    for i in range(self.rh_host_combo.count()):
                        if self.rh_host_combo.itemText(i) == sel:
                            self.rh_host_combo.setCurrentIndex(i)
                            break
            except Exception:
                pass
            top_row.addWidget(host_label)
            top_row.addWidget(self.rh_host_combo)
            try:
                open_host_btn = QtWidgets.QPushButton('打开网页')
                open_host_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                open_host_btn.setMaximumWidth(180)
                def _open_host():
                    try:
                        host = self.rh_host_combo.currentText() or 'www.runninghub.cn'
                        hostn = host if host.startswith('http') else f'https://{host}'
                        import webbrowser as _wb
                        _wb.open(hostn+'/?inviteCode=rh-v1380')
                    except Exception:
                        pass
                open_host_btn.clicked.connect(_open_host)
                top_row.addWidget(open_host_btn)
            except Exception:
                pass
            try:
                cancel_all_btn = QtWidgets.QPushButton('取消全部任务')
                cancel_all_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                cancel_all_btn.setMaximumWidth(260)
                def _on_cancel_all():
                    self._refresh_rh_task_credentials()
                    workflow_queue = getattr(self, '_canvas_workflow_queue', None)
                    if workflow_queue is not None:
                        workflow_queue.cancel_all()
                    else:
                        canvas = getattr(self, 'canvas_page', None)
                        if canvas is not None:
                            canvas.engine.stop_all()
                    shared_tasks = set()
                    shared = getattr(self, '_rh_execution_service', None)
                    if shared is not None:
                        for record in shared.record_headers():
                            if record.get('status') not in ('SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN'):
                                if record.get('task_id'):
                                    shared_tasks.add(record['task_id'])
                                shared.cancel(record['run_id'])
                    lifecycle = getattr(self, '_rh_task_lifecycle', None)
                    if lifecycle is None:
                        return
                    from aetherloom_core.rh_submission_queue import get_submission_queue
                    submission_queue = get_submission_queue(self)
                    queued = submission_queue.cancel_all()
                    for item in queued:
                        card = item.get('card') if isinstance(item, dict) else None
                        if card is not None:
                            card._rh_cancelled = True
                    for card in list(getattr(self, '_rh_running_cards', ())):
                        if not getattr(card, '_task_id', None):
                            card._rh_cancelled = True
                    submission_queue.wake()
                    with self._rh_task_runtime_lock:
                        task_ids = set(self._rh_live_task_ids)
                        for ids in self._rh_running_tasks.values():
                            task_ids.update(ids)
                    persisted = lifecycle.store.read()
                    task_ids.update(persisted)
                    # Record every cancellation now. The lifecycle's bounded
                    # status pool retries and confirms cloud cancellation; a
                    # slow first RPC must not delay intent for all later tasks.
                    for task_id in task_ids - shared_tasks:
                        context = lifecycle.context(task_id, persisted=persisted.get(task_id))
                        if not context.get('webapp_id'):
                            context['webapp_id'] = getattr(self, '_rh_task_to_wid', {}).get(task_id)
                        if not context.get('webapp_id'):
                            continue
                        if shared is not None:
                            context = shared.adopt_task(task_id, context)
                            shared.cancel(context['run_id'])
                        else:
                            lifecycle.cancel_task(task_id, context['webapp_id'])
                cancel_all_btn.clicked.connect(_on_cancel_all)
            except Exception:
                pass
            try:
                one_click_btn = QtWidgets.QPushButton('一键添加/更新推荐应用')
                try:
                    one_click_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                    # one_click_btn.setFixedHeight(40)
                    one_click_btn.setMaximumWidth(500)
                    f = one_click_btn.font()
                    f.setPointSize(f.pointSize() + 2)
                    f.setBold(True)
                    one_click_btn.setFont(f)
                except Exception:
                    pass
                top_row.addWidget(one_click_btn)
            except Exception:
                pass
            top_row.addStretch(1)
            runninghub_layout.addLayout(top_row)

            # handler for one-click add/update recommended apps
            try:
                def _one_click_add_recommended():
                    # runs in main thread: start a QThread to perform network work
                    try:
                        connection = self._rh_connection_snapshot()
                    except (OSError, ValueError, TypeError):
                        self._show_toast('连接设置保存失败，请检查 apikeys.json 是否可写。', 5000)
                        return
                    try:
                        import get_apps as _get_apps
                    except Exception:
                        _get_apps = None

                    batch_result = {'errors': [], 'warnings': []}
                    def _worker_wrap():
                        added = 0
                        try:
                            import get_apps as _get_apps
                            from api_calls import call_rh
                            base_url = connection['base_url']
                            api_key = connection['api_key']
                            apps = _get_apps.get_runninghub_apps(user_id='1911823721911500801', page=1, page_size=30, n=30, base_url=base_url)
                        except Exception as exc:
                            batch_result['errors'].append(f'获取应用列表失败: {exc}')
                            return 0
                        for app in (apps or []):
                            wid = app.get('webappId') or app.get('id')
                            if not wid:
                                batch_result['errors'].append('应用缺少 webappId')
                                continue
                            try:
                                raw = call_rh.get_nodeinfo(wid, api_key, base_url=base_url, timeout=25)
                                nodes = json.loads(raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else str(raw))
                                if not isinstance(nodes, list) or not nodes:
                                    raise ValueError('未返回可用节点')
                            except Exception as exc:
                                batch_result['errors'].append(f'应用 {wid}: {exc}')
                                continue
                            page_title = app.get('webappName') or ''
                            description = app.get('description') or ''
                            thumbnail_uri = ''
                            try:
                                detail = _get_apps.scrape_runninghub_detail(app.get('url') or f'{base_url}/webapp/{wid}', timeout=10, api_base=base_url)
                                page_title = detail.get('name') or page_title
                                description = detail.get('description') or description
                                covers = detail.get('covers') or []
                                if isinstance(covers, list) and covers:
                                    thumbnail_uri = covers[0].get('thumbnailUri') or covers[0].get('url') or ''
                            except Exception as exc:
                                batch_result['warnings'].append(f'应用 {wid} 详情: {exc}')
                            try:
                                dest_dir = os.path.join(current_dir, 'RH_apps', str(wid))
                                os.makedirs(dest_dir, exist_ok=True)
                                path = os.path.join(dest_dir, f'{wid}.json')
                                data_to_save = {'webappId': wid, 'title': page_title, 'description': description,
                                                'url': app.get('url') or f'{base_url}/webapp/{wid}', 'base_url': base_url,
                                                'thumbnail_uri': thumbnail_uri, 'nodeInfoList': nodes}
                                with open(path, 'wb') as file:
                                    file.write(json.dumps(data_to_save, ensure_ascii=False).encode('utf-8'))
                                added += 1
                            except Exception as exc:
                                batch_result['errors'].append(f'保存应用 {wid} 失败: {exc}')
                        return added

                    try:
                        t = QtCore.QThread()
                        class _Runner(QtCore.QObject):
                            finished = QtCore.pyqtSignal(int)
                            @QtCore.pyqtSlot()
                            def run(self):
                                try:
                                    count = _worker_wrap()
                                except Exception as exc:
                                    batch_result['errors'].append(f'批量导入失败: {exc}')
                                    count = 0
                                try:
                                    self.finished.emit(count)
                                except Exception:
                                    pass

                        runner = _Runner()
                        runner.moveToThread(t)
                        t.started.connect(runner.run)
                        def _on_done(count):
                            try:
                                _load_rh_apps()
                            except Exception:
                                pass
                            failures = batch_result['errors']
                            warnings = batch_result['warnings']
                            message = f'已添加/更新 {count} 个应用'
                            if failures:
                                message += f'，{len(failures)} 项失败：{failures[0]}'
                            if warnings:
                                message += f'；{len(warnings)} 项详情未获取'
                                if not failures:
                                    message += f'：{warnings[0]}'
                            try:
                                self._show_toast(message, 5000 if failures or warnings else 3000)
                            except Exception:
                                pass
                        runner.finished.connect(_on_done)
                        runner.finished.connect(runner.deleteLater)
                        runner.finished.connect(t.quit)
                        t.finished.connect(t.deleteLater)
                        if not hasattr(self, '_rh_worker_refs'):
                            self._rh_worker_refs = []
                        self._rh_worker_refs.append((t, runner))
                        t.start()
                    except Exception:
                        try:
                            self._show_toast('一键添加操作无法启动，请检查日志', 3000)
                        except Exception:
                            pass

                # connect to button if present
                try:
                    one_click_btn.clicked.connect(_one_click_add_recommended)
                except Exception:
                    pass
            except Exception:
                pass

            # Second row: apikey label + input (full width)
            apikey_row = QtWidgets.QHBoxLayout()
            apikey_row.setSpacing(8)
            apikey_label = QtWidgets.QLabel('输入apikey:')
            self.rh_apikey_input = QtWidgets.QLineEdit()
            self.rh_apikey_input.setPlaceholderText('读取本地 apikeys.json 中 runninghub_cn 或 runninghub_ai')
            self.rh_apikey_input.setClearButtonEnabled(True)
            try:
                self.rh_apikey_input.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                self.rh_apikey_input.setEchoMode(QtWidgets.QLineEdit.Password)
            except Exception:
                pass
            apikey_row.addWidget(apikey_label)
            apikey_row.addWidget(self.rh_apikey_input, 1)
            manage_keys_btn = QtWidgets.QPushButton('管理 API keys')
            from aetherloom_core.rh_connections import show_connection_dialog
            manage_keys_btn.clicked.connect(lambda: show_connection_dialog(self))
            apikey_row.addWidget(manage_keys_btn)
            try:
                get_key_btn = QtWidgets.QPushButton('获取密钥')
                try:
                    get_key_btn.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed)
                    get_key_btn.setMaximumWidth(180)
                    get_key_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                except Exception:
                    pass
                apikey_row.addWidget(get_key_btn)
                def _on_get_key():
                    try:
                        host = self.rh_host_combo.currentText() or 'www.runninghub.cn'
                        url = f"https://{host}/enterprise-api/sharedApi"
                        webbrowser.open(url)
                    except Exception:
                        pass
                try:
                    get_key_btn.clicked.connect(_on_get_key)
                except Exception:
                    pass
            except Exception:
                pass
            apikey_row.addStretch(1)
            runninghub_layout.addLayout(apikey_row)

            # placeholder area below
            rh_label = QtWidgets.QLabel('Runninghub 应用')
            rh_label.setObjectName('runninghubPlaceholder')
            rh_label.setAlignment(Qt.AlignCenter)
            runninghub_layout.addWidget(rh_label)

            # --- 工作流区域: scrollable grid of large square app buttons ---
            rh_flow_scroll = QtWidgets.QScrollArea()
            rh_flow_scroll.setWidgetResizable(True)
            # disable horizontal scrolling; force wrapping into rows and allow vertical scroll when needed
            rh_flow_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            rh_flow_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            rh_flow_widget = QtWidgets.QWidget()
            rh_flow_widget.setObjectName('runninghubFlow')
            rh_flow_scroll.setWidget(rh_flow_widget)
            rh_hbox = QtWidgets.QWidget()
            rh_hbox_l = QtWidgets.QHBoxLayout(rh_hbox)
            rh_hbox_l.setContentsMargins(0, 0, 0, 0)
            rh_hbox_l.setSpacing(16)
            rh_flow_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            rh_flow_scroll.setMinimumHeight(260)
            rh_hbox_l.addWidget(rh_flow_scroll, 3)
            task_panel = TaskPanel(self._rh_dashboard)
            task_panel.setMinimumWidth(260)
            task_panel.setMaximumWidth(380)
            task_panel.layout().addWidget(cancel_all_btn)
            self._rh_task_panel = task_panel
            rh_hbox_l.addWidget(task_panel, 1)
            runninghub_layout.addWidget(rh_hbox, 1)

            rh_flow_layout = QtWidgets.QGridLayout(rh_flow_widget)
            rh_flow_layout.setContentsMargins(8, 8, 8, 8)
            rh_flow_layout.setHorizontalSpacing(12)
            rh_flow_layout.setVerticalSpacing(12)
            try:
                rh_flow_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            except Exception:
                pass
            try:
                # ensure columns do not expand and force items to lay out from top-left
                for ci in range(0, 8):
                    rh_flow_layout.setColumnStretch(ci, 0)
            except Exception:
                pass

            # container state
            self.rh_workflow_buttons = []  # type: List[QtWidgets.QPushButton]

            def _rh_screen_width():
                try:
                    scr = None
                    try:
                        wh = self.windowHandle()
                        scr = wh.screen() if wh is not None else None
                    except Exception:
                        scr = None
                    if scr is None:
                        try:
                            scr = rh_flow_widget.screen() if hasattr(rh_flow_widget, 'screen') else None
                        except Exception:
                            scr = None
                    if scr is None:
                        try:
                            scr = QtWidgets.QApplication.primaryScreen()
                        except Exception:
                            scr = None
                    if scr is not None:
                        return scr.size().width()
                except Exception:
                    pass
                return None

            def _create_app_button(title: str) -> QtWidgets.QPushButton:
                return AppCard(title, self._rh_dashboard)

            def _set_button_thumbnail(btn: QtWidgets.QPushButton, url: str, wid: str, force: bool = False):
                try:
                    if not url:
                        return
                    try:
                        from urllib.parse import urlparse
                    except Exception:
                        urlparse = None
                    # prepare per-app dir under RH_apps and deterministic thumb name
                    outdir = os.path.join(current_dir, 'RH_apps', str(wid))
                    os.makedirs(outdir, exist_ok=True)
                    # derive extension from url path if possible
                    try:
                        parsed = urlparse(url) if urlparse else None
                        base = os.path.basename(parsed.path) if parsed and parsed.path else ''
                        ext = os.path.splitext(base)[1] or '.jpg'
                    except Exception:
                        ext = '.jpg'
                    dst = os.path.join(outdir, f"{wid}_thumb" + ext)

                    # if thumbnail already exists and caller didn't request force-refresh,
                    # reuse it without downloading.
                    try:
                        if os.path.exists(dst) and not force:
                            try:
                                btn._has_thumbnail = True
                                btn._thumb_path = dst
                            except Exception:
                                pass
                            try:
                                pix = QtGui.QPixmap(dst)
                                if pix and not pix.isNull():
                                    bw = max(1, btn.width() - 24)
                                    bh = max(1, btn.height() - 24)
                                    scaled = pix.scaled(bw, bh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                                    try:
                                        btn.setIcon(QtGui.QIcon(scaled))
                                        btn.setIconSize(scaled.size())
                                        btn.setStyleSheet('QPushButton { border-radius: 8px; padding: 12px 10px 8px 12px; text-align: left; color: #12c2e9; }')
                                    except Exception:
                                        try:
                                            p = dst.replace('\\', '/')
                                            btn.setStyleSheet(f"QPushButton {{ border-radius: 8px; padding: 12px 10px 8px 12px; text-align: left; color: #12c2e9; background-image: url({p}); background-position: center; background-repeat: no-repeat; }}")
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            return
                    except Exception:
                        pass

                    def _dl():
                        try:
                            import requests as _req
                            r = _req.get(url, timeout=12)
                            if r is not None and r.status_code == 200:
                                try:
                                    tmp = dst + '.tmp'
                                    with open(tmp, 'wb') as wf:
                                        wf.write(r.content)
                                    # try to detect if content is a video or animated gif
                                    try:
                                        ct = (r.headers.get('content-type') or '').lower()
                                    except Exception:
                                        ct = ''
                                    try:
                                        ext_lower = ext.lower()
                                    except Exception:
                                        ext_lower = ''
                                    is_gif = ('gif' in ct) or ext_lower.endswith('.gif')
                                    is_video = (ct.startswith('video')) or ext_lower in ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.flv', '.wmv')
                                    dst_to_use = dst
                                    if is_gif or is_video:
                                        try:
                                            png_tmp = tmp + '.png'
                                            # handle GIF first-frame via PIL
                                            if is_gif:
                                                try:
                                                    im = Image.open(tmp)
                                                    im.seek(0)
                                                    frame = im.convert('RGBA')
                                                    frame.save(png_tmp, 'PNG')
                                                except Exception:
                                                    raise
                                            else:
                                                # try moviepy first, fallback to cv2
                                                try:
                                                    clip = VideoFileClip(tmp)
                                                    frame = clip.get_frame(0)
                                                    clip.reader.close()
                                                    try:
                                                        if clip.audio:
                                                            clip.audio.reader.close_proc()
                                                    except Exception:
                                                        pass
                                                    img = Image.fromarray((frame).astype('uint8'))
                                                    img.save(png_tmp, 'PNG')
                                                except Exception:
                                                    try:
                                                        cap = cv2.VideoCapture(tmp)
                                                        ok, fr = cap.read()
                                                        cap.release()
                                                        if ok and fr is not None:
                                                            fr = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
                                                            img = Image.fromarray(fr)
                                                            img.save(png_tmp, 'PNG')
                                                        else:
                                                            raise RuntimeError('no frame')
                                                    except Exception:
                                                        raise
                                            # move png into final destination path (use .png ext)
                                            try:
                                                newdst = os.path.splitext(dst)[0] + '.png'
                                                os.replace(png_tmp, newdst)
                                                try:
                                                    os.remove(tmp)
                                                except Exception:
                                                    pass
                                                dst_to_use = newdst
                                            except Exception:
                                                # fallback to original tmp->dst
                                                try:
                                                    os.replace(tmp, dst)
                                                    dst_to_use = dst
                                                except Exception:
                                                    dst_to_use = dst
                                        except Exception:
                                            pass
                                    else:
                                        try:
                                            os.replace(tmp, dst)
                                            dst_to_use = dst
                                        except Exception:
                                            try:
                                                os.replace(tmp, dst)
                                                dst_to_use = dst
                                            except Exception:
                                                dst_to_use = dst
                                except Exception:
                                    return
                                self._rh_dashboard.thumbnail_ready.emit(weakref.ref(btn), dst_to_use)
                        except Exception:
                            pass

                    import threading as _thr
                    _thr.Thread(target=_dl, daemon=True).start()
                except Exception:
                    pass

            def _reflow_buttons():
                margins = rh_flow_layout.contentsMargins()
                width = max(1, rh_flow_scroll.viewport().width() - margins.left() - margins.right())
                spacing = 12
                columns = max(1, (width + spacing) // (200 + spacing))
                card_width = max(140, min(260, (width - (columns - 1) * spacing) // columns))
                signature = (columns, card_width, tuple(id(button) for button in self.rh_workflow_buttons))
                if getattr(self, '_rh_grid_signature', None) == signature:
                    return
                self._rh_grid_signature = signature
                while rh_flow_layout.count():
                    rh_flow_layout.takeAt(0)
                for i, button in enumerate(self.rh_workflow_buttons):
                    button.setFixedSize(card_width, int(card_width * 0.62) + 70)
                    rh_flow_layout.addWidget(button, i // columns, i % columns, Qt.AlignTop | Qt.AlignLeft)
                self._rh_dashboard.refresh()

            # make reflow callable from other methods (e.g., resizeEvent)
            try:
                self._reflow_rh_buttons = _reflow_buttons
            except Exception:
                pass

            # 添加应用 按钮 (固定在左上)
            add_wf_btn = _create_app_button('+\n添加应用')
            try:
                add_wf_btn.setFixedHeight(260)
                add_wf_btn.setFixedWidth(260)
                try:
                    add_wf_btn.setStyleSheet('QPushButton { font-weight: 800; font-size: 36px; }')
                except Exception:
                    pass
            except Exception:
                pass
            # hide the small status badge on the Add-App button (not needed)
            try:
                ab = getattr(add_wf_btn, '_rh_badge', None)
                if ab is not None:
                    try:
                        ab.hide()
                    except Exception:
                        pass
                    try:
                        ab._visible = False
                    except Exception:
                        pass
            except Exception:
                pass
            rh_flow_layout.addWidget(add_wf_btn, 0, 0)
            self.rh_workflow_buttons.append(add_wf_btn)

            def _clear_app_buttons():
                try:
                    # keep the first button (add) and remove others
                    for btn in list(self.rh_workflow_buttons[1:]):
                        try:
                            rh_flow_layout.removeWidget(btn)
                            btn.setParent(None)
                            btn.deleteLater()
                        except Exception:
                            pass
                    self.rh_workflow_buttons = [self.rh_workflow_buttons[0]]
                    self._rh_app_buttons = {}
                except Exception:
                    pass

            def _load_rh_apps():
                try:
                    # ensure per-app UI/status tracking and a 1s refresh timer
                    try:
                        if not hasattr(self, '_rh_app_buttons'):
                            self._rh_app_buttons = {}
                        if not hasattr(self, '_rh_app_paths'):
                            self._rh_app_paths = {}
                        if not hasattr(self, '_rh_app_active_count'):
                            self._rh_app_active_count = {}
                        if not hasattr(self, '_rh_running_cards'):
                            try:
                                self._rh_running_cards = weakref.WeakSet()
                            except Exception:
                                self._rh_running_cards = set()
                        if not hasattr(self, '_rh_app_last_result'):
                            self._rh_app_last_result = {}
                        if not hasattr(self, '_rh_running_tasks'):
                            # mapping: wid -> set(task_id)
                            self._rh_running_tasks = {}
                        if not hasattr(self, '_rh_status_entries'):
                            # persistent mapping: task_id -> status (persist until GUI exit)
                            self._rh_status_entries = {}
                        if not hasattr(self, '_rh_task_to_wid'):
                            # mapping: task_id -> webapp id (string)
                            self._rh_task_to_wid = {}
                        # file for persisting running tasks across runs
                        from aetherloom_core import rh_tasks
                        if not hasattr(self, '_rh_task_lifecycle'):
                            task_store = rh_tasks.default_task_store()
                            self._rh_tasks_file = task_store.path
                            self._rh_task_lifecycle = rh_tasks.TaskLifecycle(
                                self, task_store,
                                lambda wid, status: self._rh_status_emitter.sig.emit(wid, status))
                            self._refresh_rh_task_credentials()
                        if not hasattr(self, '_rh_status_emitter'):
                            class _StatusEmitter(QtCore.QObject):
                                sig = QtCore.pyqtSignal(str, str)
                            self._rh_status_emitter = _StatusEmitter()
                            def _on_status_update(wid, st):
                                try:
                                    self._rh_task_lifecycle.handle_event(wid, st)
                                    _update_app_button_styles()
                                except Exception as error:
                                    try:
                                        self.log('RunningHub task state update failed: ' + str(error))
                                    except Exception:
                                        pass
                            self._rh_status_emitter.sig.connect(_on_status_update)
                        from aetherloom_core.rh_execution_ui import ensure_execution_service
                        ensure_execution_service(self)
                        if not hasattr(self, '_rh_app_status_timer'):
                            def _update_app_button_styles():
                                self._rh_dashboard.refresh()

                            self._rh_refresh_app_styles = _update_app_button_styles
                            self._rh_app_status_timer = QtCore.QTimer(self)
                            self._rh_app_status_timer.setInterval(1000)
                            self._rh_app_status_timer.timeout.connect(lambda: _update_app_button_styles())
                            try:
                                self._rh_app_status_timer.start()
                            except Exception:
                                pass
                        if not hasattr(self, '_rh_card_timer'):
                            def _update_running_card_timers():
                                try:
                                    import time as _time
                                except Exception:
                                    _time = None
                                try:
                                    cards = list(self._rh_running_cards) if getattr(self, '_rh_running_cards', None) is not None else []
                                except Exception:
                                    cards = []
                                if not cards:
                                    try:
                                        if getattr(self, '_rh_card_timer', None) is not None:
                                            self._rh_card_timer.stop()
                                    except Exception:
                                        pass
                                    return
                                for _card in cards:
                                    try:
                                        if _card is None:
                                            continue
                                        start_ts = getattr(_card, '_timer_start', None)
                                        if not start_ts:
                                            try:
                                                if hasattr(self._rh_running_cards, 'discard'):
                                                    self._rh_running_cards.discard(_card)
                                            except Exception:
                                                pass
                                            continue
                                        if _time is None:
                                            continue
                                        elapsed = _time.time() - float(start_ts)
                                        lbl_timer = getattr(_card, '_timer_label', None)
                                        if lbl_timer is not None:
                                            try:
                                                lbl_timer.setText(f"{elapsed:.2f}s")
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                            self._rh_card_timer = QtCore.QTimer(self)
                            self._rh_card_timer.setInterval(100)
                            try:
                                self._rh_card_timer.timeout.connect(_update_running_card_timers)
                                # start only when cards are running
                                self._rh_card_timer.stop()
                            except Exception:
                                pass
                            # background resume worker: poll persisted running_tasks.json and update statuses
                            def _resume_tasks_loop():
                                self._rh_task_lifecycle.run()

                            try:
                                import threading as _thr
                                self._rh_resume_thread = _thr.Thread(
                                    target=_resume_tasks_loop, name='rh-task-recovery', daemon=True)
                                # The canvas restores durable results before task queries start.
                                self._rh_start_recovery_worker = self._rh_resume_thread.start
                            except Exception:
                                pass
                    except Exception:
                        pass
                    outdir = os.path.join(current_dir, 'RH_apps')
                    if not os.path.isdir(outdir):
                        return

                    # MIGRATE legacy top-level JSONs and thumbs into per-app directories
                    try:
                        # move any RH_apps/*.json into RH_apps/<id>/<id>.json
                        for fname in [f for f in os.listdir(outdir) if f.lower().endswith('.json')]:
                            try:
                                src = os.path.join(outdir, fname)
                                webapp_id = os.path.splitext(fname)[0]
                                dest_dir = os.path.join(outdir, webapp_id)
                                os.makedirs(dest_dir, exist_ok=True)
                                dest = os.path.join(dest_dir, f"{webapp_id}.json")
                                try:
                                    os.replace(src, dest)
                                except Exception:
                                    try:
                                        shutil.move(src, dest)
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                        # move any old thumbnails from RH_apps/thumbs into per-app dirs and rename to {id}_thumb.ext
                        thumbs_dir = os.path.join(outdir, 'thumbs')
                        if os.path.isdir(thumbs_dir):
                            for tfile in os.listdir(thumbs_dir):
                                try:
                                    # expected legacy name pattern: <id>_something.ext
                                    parts = tfile.split('_', 1)
                                    if not parts:
                                        continue
                                    maybe_id = parts[0]
                                    src = os.path.join(thumbs_dir, tfile)
                                    dest_dir = os.path.join(outdir, maybe_id)
                                    if not os.path.isdir(dest_dir):
                                        # create target dir to keep thumb with its app
                                        os.makedirs(dest_dir, exist_ok=True)
                                    ext = os.path.splitext(tfile)[1] or '.jpg'
                                    dest = os.path.join(dest_dir, f"{maybe_id}_thumb" + ext)
                                    try:
                                        os.replace(src, dest)
                                    except Exception:
                                        try:
                                            shutil.move(src, dest)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            # optional: remove thumbs_dir if empty
                            try:
                                if os.path.isdir(thumbs_dir) and not os.listdir(thumbs_dir):
                                    try:
                                        os.rmdir(thumbs_dir)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # collect per-app directories only (GUI will read only from new per-app locations)
                    entries = sorted(os.listdir(outdir))
                    files = []
                    for ent in entries:
                        full = os.path.join(outdir, ent)
                        try:
                            if os.path.isdir(full):
                                try:
                                    nested = [x for x in os.listdir(full) if x.lower().endswith('.json')]
                                except Exception:
                                    nested = []
                                if nested:
                                    # prefer <id>.json inside dir, otherwise first json
                                    candidate = os.path.join(full, f"{ent}.json")
                                    if os.path.exists(candidate):
                                        chosen = candidate
                                    else:
                                        chosen = os.path.join(full, nested[0])
                                    files.append((ent, chosen))
                        except Exception:
                            pass
                    # sort by numeric webapp id when possible
                    def _id_key(item):
                        wid = item[0]
                        try:
                            return int(wid)
                        except Exception:
                            return wid
                    try:
                        favs = getattr(self, 'rh_favorites', None) or set()
                    except Exception:
                        favs = set()
                    files = sorted(files, key=lambda item: (0 if str(item[0]) in favs else 1, _id_key(item)))
                    try:
                        self._rh_app_paths = {}
                    except Exception:
                        pass
                    _clear_app_buttons()
                    for webapp_id, path in files:
                        title_short = webapp_id
                        try:
                            with open(path, 'rb') as f:
                                txt = f.read().decode('utf-8')
                                parsed = json.loads(txt)
                                if isinstance(parsed, dict):
                                    t = parsed.get('title') or ''
                                    if t:
                                        title_short = t
                        except Exception:
                            pass
                        if not title_short:
                            title_display = '待命名'
                        elif len(title_short) > 20:
                            title_display = title_short[:16] + '…'
                        else:
                            title_display = title_short
                        btn = _create_app_button(title_display)
                        try:
                            # store full title on the widget for wrapping/scaling
                            try:
                                btn._full_title = title_short
                            except Exception:
                                pass
                        except Exception:
                            pass
                        try:
                            is_fav = False
                            try:
                                favs = getattr(self, 'rh_favorites', None)
                                is_fav = bool(favs is not None and str(webapp_id) in favs)
                            except Exception:
                                is_fav = False
                            if hasattr(btn, '_rh_set_fav'):
                                try:
                                    btn._rh_set_fav(is_fav)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            if isinstance(getattr(self, '_rh_app_paths', None), dict):
                                self._rh_app_paths[str(webapp_id)] = path
                        except Exception:
                            pass
                        # Register each app for status refresh and theme changes.
                        try:
                            try:
                                btn._wid = webapp_id
                                if not hasattr(self, '_rh_app_buttons'):
                                    self._rh_app_buttons = {}
                                self._rh_app_buttons[webapp_id] = btn
                            except Exception:
                                pass
                        except Exception:
                            pass
                        try:
                            # A quiet surface keeps long app names readable.
                            btn._has_thumbnail = False
                            btn._thumb_path = None
                            from aetherloom_core.rh_ui import navigation_button_stylesheet
                            btn.setStyleSheet(navigation_button_stylesheet(getattr(self, '_theme_mode', 'dark')))
                        except Exception:
                            pass
                        try:
                            # if local JSON contains thumbnail_uri, initiate download and apply
                            try:
                                if isinstance(parsed, dict):
                                    turl = parsed.get('thumbnail_uri') or parsed.get('thumbnail') or ''
                                else:
                                    turl = ''
                            except Exception:
                                turl = ''
                            if turl:
                                try:
                                    _set_button_thumbnail(btn, turl, webapp_id)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            # Enable left-click to open the app detail subpage
                            def _open_app_detail(fpath=path, wid=webapp_id):
                                try:
                                    # load JSON
                                    with open(fpath, 'rb') as _f:
                                        raw = _f.read().decode('utf-8')
                                    parsed = json.loads(raw) if raw else {}
                                except Exception:
                                    parsed = {}

                                def _ensure_app_nav_container(page):
                                    try:
                                        if getattr(page, '_app_nav_layout', None) is not None:
                                            return
                                        # root container is a horizontal layout: left nav + main content
                                        root_layout = page.layout()
                                        if not isinstance(root_layout, QtWidgets.QHBoxLayout):
                                            root_layout = QtWidgets.QHBoxLayout(page)
                                            root_layout.setContentsMargins(0, 0, 0, 0)
                                            root_layout.setSpacing(12)
                                        nav_scroll = QtWidgets.QScrollArea()
                                        nav_scroll.setWidgetResizable(True)
                                        nav_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
                                        nav_wrap = QtWidgets.QWidget()
                                        nav_layout = QtWidgets.QVBoxLayout(nav_wrap)
                                        nav_layout.setContentsMargins(8, 8, 8, 8)
                                        nav_layout.setSpacing(8)
                                        nav_scroll.setWidget(nav_wrap)
                                        try:
                                            nav_scroll.setObjectName('appNavScroll')
                                        except Exception:
                                            pass
                                        root_layout.insertWidget(0, nav_scroll)
                                        # sidebar toggle button (between nav and main content)
                                        try:
                                            nav_toggle = QtWidgets.QToolButton()
                                            nav_toggle.setObjectName('appNavToggle')
                                            nav_toggle.setText('‹')
                                            nav_toggle.setFixedWidth(24)
                                            nav_toggle.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                            nav_toggle.setToolTip('展开/隐藏应用切换栏')
                                            try:
                                                nav_toggle.setStyleSheet('QToolButton#appNavToggle { border: none; background: transparent; font-weight: 700; }')
                                            except Exception:
                                                pass
                                            root_layout.insertWidget(1, nav_toggle)
                                        except Exception:
                                            nav_toggle = None
                                        try:
                                            page._app_nav_layout = nav_layout
                                            page._app_nav_scroll = nav_scroll
                                            page._app_nav_toggle = nav_toggle
                                        except Exception:
                                            pass
                                        try:
                                            if not hasattr(page, '_app_nav_visible'):
                                                page._app_nav_visible = True
                                        except Exception:
                                            pass
                                        def _apply_nav_visibility(pg=page, visible=None):
                                            try:
                                                if visible is None:
                                                    visible = bool(getattr(pg, '_app_nav_visible', True))
                                                pg._app_nav_visible = bool(visible)
                                            except Exception:
                                                visible = True
                                            try:
                                                nav_scroll.setVisible(bool(visible))
                                            except Exception:
                                                pass
                                            try:
                                                if nav_toggle is not None:
                                                    nav_toggle.setText('‹' if visible else '›')
                                            except Exception:
                                                pass
                                            try:
                                                _update_nav_metrics(pg)
                                            except Exception:
                                                pass
                                        try:
                                            if nav_toggle is not None:
                                                nav_toggle.clicked.connect(lambda *_: _apply_nav_visibility(page, not bool(getattr(page, '_app_nav_visible', True))))
                                        except Exception:
                                            pass
                                        def _update_nav_metrics(pg=page):
                                            try:
                                                nav_visible = bool(getattr(pg, '_app_nav_visible', True))
                                                if not nav_visible:
                                                    nav_scroll.setFixedWidth(0)
                                                    return
                                                nav_w = max(148, min(176, int(max(1, pg.width()) / 7)))
                                            except Exception:
                                                nav_w = 148
                                            try:
                                                nav_scroll.setFixedWidth(nav_w)
                                            except Exception:
                                                pass
                                            try:
                                                btn_w = max(96, nav_w - 28)
                                                icon_px = max(48, int(btn_w * 0.64))
                                                pg._app_nav_btn_w = btn_w
                                                pg._app_nav_icon_px = icon_px
                                            except Exception:
                                                pass
                                            try:
                                                lay = getattr(pg, '_app_nav_layout', None)
                                                if lay is not None:
                                                    for i in range(lay.count()):
                                                        it = lay.itemAt(i)
                                                        if it is None:
                                                            continue
                                                        btn_ref = it.widget()
                                                        if isinstance(btn_ref, QtWidgets.QToolButton):
                                                            try:
                                                                btn_ref.setFixedSize(btn_w, btn_w)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                btn_ref.setIconSize(QtCore.QSize(icon_px, icon_px))
                                                            except Exception:
                                                                pass
                                                            try:
                                                                # scale font with button width (clamp for readability)
                                                                f = btn_ref.font()
                                                                pt = int(max(9, min(14, btn_w * 0.12)))
                                                                f.setPointSize(pt)
                                                                btn_ref.setFont(f)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                base_txt = getattr(btn_ref, '_full_title', btn_ref.text()) or ''
                                                                fm_local = QtGui.QFontMetrics(btn_ref.font())
                                                                max_w = max(10, btn_w - 12)
                                                                def _wrap_text(txt, fm, w):
                                                                    line = ''
                                                                    out = []
                                                                    for ch in txt:
                                                                        if ch == '\n':
                                                                            out.append(line)
                                                                            line = ''
                                                                            continue
                                                                        if line and fm.horizontalAdvance(line + ch) > w:
                                                                            out.append(line)
                                                                            line = ch
                                                                        else:
                                                                            line += ch
                                                                    if line:
                                                                        out.append(line)
                                                                    return '\n'.join(out)
                                                                btn_ref.setText(_wrap_text(base_txt, fm_local, max_w))
                                                            except Exception:
                                                                pass
                                            except Exception:
                                                pass
                                        try:
                                            page._update_app_nav_metrics = _update_nav_metrics
                                        except Exception:
                                            pass
                                        try:
                                            _update_nav_metrics()
                                        except Exception:
                                            pass
                                        try:
                                            _apply_nav_visibility(page, getattr(page, '_app_nav_visible', True))
                                        except Exception:
                                            pass
                                        try:
                                            orig_resize = page.resizeEvent if hasattr(page, 'resizeEvent') else None
                                            def _nav_resize(ev, pg=page, orig=orig_resize):
                                                try:
                                                    _update_nav_metrics(pg)
                                                except Exception:
                                                    pass
                                                try:
                                                    if callable(orig):
                                                        return orig(ev)
                                                except Exception:
                                                    pass
                                            page.resizeEvent = _nav_resize
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass

                                def _populate_app_nav(page, active_wid):
                                    try:
                                        layout_nav = getattr(page, '_app_nav_layout', None)
                                        if layout_nav is None:
                                            return

                                        def _center_to_active(btn_target=None):
                                            try:
                                                scroll_local = getattr(page, '_app_nav_scroll', None)
                                                if scroll_local is None:
                                                    return
                                                if btn_target is None:
                                                    try:
                                                        for i in range(layout_nav.count()):
                                                            it = layout_nav.itemAt(i)
                                                            w = it.widget() if it else None
                                                            if isinstance(w, QtWidgets.QToolButton) and str(getattr(w, '_wid', '')) == str(active_wid):
                                                                btn_target = w
                                                                break
                                                    except Exception:
                                                        btn_target = None
                                                if btn_target is None:
                                                    return
                                                bar = scroll_local.verticalScrollBar()
                                                if bar is None:
                                                    return
                                                try:
                                                    viewport_h = max(1, scroll_local.viewport().height())
                                                except Exception:
                                                    viewport_h = max(1, bar.pageStep())
                                                try:
                                                    pos_y = btn_target.mapTo(scroll_local.widget(), QtCore.QPoint(0, 0)).y()
                                                except Exception:
                                                    pos_y = btn_target.y()
                                                try:
                                                    target = int(max(0, pos_y + btn_target.height() / 2 - viewport_h / 2))
                                                except Exception:
                                                    target = bar.value()
                                                bar.setValue(target)
                                            except Exception:
                                                pass

                                        paths = getattr(self, '_rh_app_paths', {}) if hasattr(self, '_rh_app_paths') else {}
                                        try:
                                            items = sorted(list(paths.items()), key=lambda kv: kv[0])
                                        except Exception:
                                            items = list((paths or {}).items())

                                        # if layout already matches items count, just toggle check state to avoid rebuild/scroll jump
                                        try:
                                            existing_btns = [layout_nav.itemAt(i).widget() for i in range(layout_nav.count()) if isinstance(layout_nav.itemAt(i).widget(), QtWidgets.QToolButton)]
                                        except Exception:
                                            existing_btns = []
                                        if existing_btns and len(existing_btns) == len(items):
                                            try:
                                                for btn_ref in existing_btns:
                                                    wid_tag = getattr(btn_ref, '_wid', None) or btn_ref.text()
                                                    try:
                                                        btn_ref.setChecked(str(wid_tag) == str(active_wid))
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                            _center_to_active()
                                            return

                                        while layout_nav.count():
                                            it = layout_nav.takeAt(0)
                                            try:
                                                w = it.widget()
                                                if w is not None:
                                                    w.setParent(None)
                                            except Exception:
                                                pass

                                        for wid2, pth2 in items:
                                            btn_nav = QtWidgets.QToolButton()
                                            btn_nav.setCheckable(True)
                                            btn_nav.setAutoRaise(True)
                                            btn_nav.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                                            try:
                                                btn_w = int(getattr(page, '_app_nav_btn_w', 112) or 112)
                                                icon_px = int(getattr(page, '_app_nav_icon_px', 72) or 72)
                                                btn_nav.setFixedSize(btn_w, btn_w)
                                                btn_nav.setIconSize(QtCore.QSize(icon_px, icon_px))
                                            except Exception:
                                                pass
                                            try:
                                                f = btn_nav.font()
                                                pt = int(max(9, min(14, btn_w * 0.12)))
                                                f.setPointSize(pt)
                                                btn_nav.setFont(f)
                                            except Exception:
                                                pass
                                            label_txt = str(wid2)
                                            try:
                                                meta = (getattr(self, '_rh_app_buttons', {}) or {}).get(wid2)
                                                if meta is not None:
                                                    full = getattr(meta, '_full_title', None) or meta.text() or str(wid2)
                                                    label_txt = full
                                                    thumb = getattr(meta, '_thumb_path', None)
                                                    if thumb and os.path.exists(thumb):
                                                        btn_nav.setIcon(QtGui.QIcon(thumb))
                                            except Exception:
                                                pass
                                            try:
                                                btn_nav._full_title = label_txt
                                            except Exception:
                                                pass
                                            try:
                                                fm_local = QtGui.QFontMetrics(btn_nav.font())
                                                max_w = max(10, btn_w - 12)
                                                def _wrap_text(txt, fm, w):
                                                    line = ''
                                                    out = []
                                                    for ch in txt:
                                                        if ch == '\n':
                                                            out.append(line)
                                                            line = ''
                                                            continue
                                                        if line and fm.horizontalAdvance(line + ch) > w:
                                                            out.append(line)
                                                            line = ch
                                                        else:
                                                            line += ch
                                                    if line:
                                                        out.append(line)
                                                    return '\n'.join(out)
                                                btn_nav.setText(_wrap_text(label_txt, fm_local, max_w))
                                            except Exception:
                                                btn_nav.setText(label_txt)
                                            try:
                                                btn_nav.setStyleSheet('QToolButton { padding: 6px; border-radius: 12px; } QToolButton:checked { background: rgba(46,129,220,0.18); border: 1px solid rgba(46,129,220,0.5); }')
                                            except Exception:
                                                pass
                                            try:
                                                btn_nav._wid = wid2
                                            except Exception:
                                                pass
                                            if str(wid2) == str(active_wid):
                                                try:
                                                    btn_nav.setChecked(True)
                                                except Exception:
                                                    pass
                                            try:
                                                btn_nav.clicked.connect(lambda _=None, fp=pth2, w2=wid2: _open_app_detail(fp, w2))
                                            except Exception:
                                                pass
                                            layout_nav.addWidget(btn_nav)
                                        try:
                                            layout_nav.addStretch(1)
                                        except Exception:
                                            pass
                                        try:
                                            QtCore.QTimer.singleShot(0, _center_to_active)
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass

                                # build app detail page
                                # ensure cache mapping for app pages so state persists across open/close
                                try:
                                    if not hasattr(self, '_rh_app_pages'):
                                        self._rh_app_pages = {}
                                except Exception:
                                    pass
                                # if we've created this app page before, reuse it to preserve right-side previews/logs
                                try:
                                    if wid and isinstance(getattr(self, '_rh_app_pages', None), dict) and wid in self._rh_app_pages:
                                        try:
                                            existing = self._rh_app_pages.get(wid)
                                            if existing is not None:
                                                try:
                                                    _ensure_app_nav_container(existing)
                                                    _populate_app_nav(existing, wid)
                                                except Exception:
                                                    pass
                                                try:
                                                    self._rh_last_app_page = existing
                                                except Exception:
                                                    pass
                                                self.pages.setCurrentWidget(existing)
                                                return
                                        except Exception:
                                            pass
                                    app_page = QtWidgets.QWidget()
                                    from aetherloom_core.rh_ui import app_stylesheet
                                    app_page.setObjectName('rhAppPage')
                                    app_page.setStyleSheet(app_stylesheet(getattr(self, '_theme_mode', 'dark')))
                                    root_layout = QtWidgets.QHBoxLayout(app_page)
                                    root_layout.setContentsMargins(0, 0, 0, 0)
                                    root_layout.setSpacing(12)
                                    _ensure_app_nav_container(app_page)
                                    main_holder = QtWidgets.QWidget()
                                    app_layout = QtWidgets.QVBoxLayout(main_holder)
                                    app_layout.setContentsMargins(12, 14, 16, 14)
                                    app_layout.setSpacing(16)
                                    try:
                                        root_layout.addWidget(main_holder, 1)
                                    except Exception:
                                        pass
                                    try:
                                        self._rh_last_app_page = app_page
                                    except Exception:
                                        pass
                                    # header with back button and title
                                    header = QtWidgets.QHBoxLayout()
                                    back_btn = QtWidgets.QPushButton('← 返回')
                                    back_btn.setObjectName('rhSecondaryButton')
                                    back_btn.setToolTip('返回应用列表 · Esc')
                                    back_btn.setFixedHeight(36)
                                    title_label = QtWidgets.QLabel(parsed.get('title') or wid)
                                    title_label.setObjectName('rhPageTitle')
                                    title_label.setWordWrap(True)
                                    title_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
                                    title_label.setToolTip(str(parsed.get('title') or wid))
                                    header.addWidget(back_btn)
                                    header.addSpacing(8)
                                    heading = QtWidgets.QVBoxLayout()
                                    heading.setSpacing(3)
                                    heading.addWidget(title_label)
                                    page_caption = QtWidgets.QLabel('调整参数，创建新的生成任务')
                                    page_caption.setObjectName('rhSubtitle')
                                    heading.addWidget(page_caption)
                                    header.addLayout(heading, 1)
                                    # reset/update button (acts like '更新应用' in RH context menu)
                                    try:
                                        btn_reset = QtWidgets.QPushButton('重置/更新')
                                        btn_reset.setMaximumWidth(200)
                                        btn_reset.setToolTip('从 RunningHub 拉取最新节点信息并覆盖本地应用文件')
                                    except Exception:
                                        btn_reset = QtWidgets.QPushButton('重置/更新')
                                    btn_reset.setObjectName('rhSecondaryButton')
                                    btn_reset.setFixedHeight(36)
                                    header.addWidget(btn_reset)
                                    app_layout.addLayout(header)

                                    # scroll area for nodes
                                    nodes_scroll = QtWidgets.QScrollArea()
                                    nodes_scroll.setObjectName('rhNodesScroll')
                                    nodes_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
                                    nodes_scroll.setWidgetResizable(True)
                                    try:
                                        nodes_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
                                        nodes_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
                                    except Exception:
                                        pass
                                    nodes_root = QtWidgets.QWidget()
                                    nodes_v = QtWidgets.QVBoxLayout(nodes_root)
                                    nodes_v.setSpacing(10)
                                    nodes_v.setContentsMargins(0, 0, 8, 0)
                                    try:
                                        nodes_v.setAlignment(Qt.AlignTop)
                                    except Exception:
                                        pass

                                    parameter_panel = QtWidgets.QFrame()
                                    parameter_panel.setObjectName('rhParameterPanel')
                                    parameter_layout = QtWidgets.QVBoxLayout(parameter_panel)
                                    parameter_layout.setContentsMargins(14, 14, 6, 10)
                                    parameter_layout.setSpacing(12)
                                    parameter_heading = QtWidgets.QHBoxLayout()
                                    parameter_title = QtWidgets.QLabel('输入参数')
                                    parameter_title.setObjectName('rhSectionTitle')
                                    parameter_heading.addWidget(parameter_title)
                                    parameter_heading.addStretch()
                                    raw_nodes = parsed.get('nodeInfoList') or []
                                    parameter_count = QtWidgets.QLabel(f'{len(raw_nodes) if isinstance(raw_nodes, list) else 1} 项')
                                    parameter_count.setObjectName('rhMuted')
                                    parameter_heading.addWidget(parameter_count)
                                    parameter_layout.addLayout(parameter_heading)
                                    parameter_layout.addWidget(nodes_scroll, 1)
                                    app_page._rh_parameter_panel = parameter_panel
                                    app_page._rh_nodes_scroll = nodes_scroll

                                    # Insert application description as the first left-side card if available
                                    try:
                                        app_description = parsed.get('description') if isinstance(parsed, dict) else ''
                                        if app_description:
                                            desc_box = QtWidgets.QFrame()
                                            desc_box.setFrameShape(QtWidgets.QFrame.StyledPanel)
                                            desc_box.setObjectName('nodeCard')
                                            try:
                                                db_l = QtWidgets.QVBoxLayout(desc_box)
                                                db_l.setContentsMargins(12, 10, 12, 12)
                                                db_l.setSpacing(8)
                                                # Display description prominently (no separate "应用简介" title)
                                                desc_lbl = QtWidgets.QLabel(app_description)
                                                desc_lbl.setWordWrap(True)
                                                desc_lbl.setObjectName('rhNodeSubtitle')
                                                db_l.addWidget(desc_lbl)
                                                nodes_v.addWidget(desc_box)
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass

                                    node_list = parsed.get('nodeInfoList') if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
                                    # ensure list form
                                    if isinstance(node_list, dict):
                                        node_list = [node_list]
                                    # per-node widget map for live updates
                                    node_widgets = {}
                                    app_page._rh_node_cards = []
                                    try:
                                        app_page._rh_node_widgets = node_widgets
                                    except Exception:
                                        pass
                                    try:
                                        # suspend persistence while populating widgets from file so initial set doesn't write back
                                        app_page._rh_suspending_persistence = True
                                    except Exception:
                                        pass
                                    try:
                                        # keep references to the app JSON and path for other components
                                        app_page._rh_fpath = fpath
                                        app_page._rh_wid = wid
                                        app_page._rh_parsed = parsed
                                    except Exception:
                                        pass
                                    for idx, node in enumerate(node_list or []):
                                        try:
                                            box = QtWidgets.QFrame()
                                            box.setObjectName('nodeCard')
                                            box.setProperty('fieldType', (node.get('fieldType') or '').upper())
                                            box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)
                                            bl = QtWidgets.QVBoxLayout(box)
                                            bl.setContentsMargins(14, 12, 14, 12)
                                            bl.setSpacing(10)
                                            field_body = QtWidgets.QWidget()
                                            field_body_layout = QtWidgets.QVBoxLayout(field_body)
                                            field_body_layout.setContentsMargins(0, 0, 0, 0)
                                            field_body_layout.setSpacing(10)
                                            box._rh_body = field_body
                                            app_page._rh_node_cards.append(box)
                                            # header: title (bold) and description underneath, with collapse button
                                            try:
                                                # container to hold title/subtitle and collapse button on the right
                                                hdr_container = QtWidgets.QWidget()
                                                hdr_h = QtWidgets.QHBoxLayout(hdr_container)
                                                hdr_h.setContentsMargins(0, 0, 0, 0)
                                                hdr_h.setSpacing(6)
                                                # vertical layout for title/subtitle
                                                hdr_v = QtWidgets.QVBoxLayout()
                                                desc = str(node.get('description') or node.get('descriptionEn') or '').strip()
                                                field_name = str(node.get('fieldName') or '').strip()
                                                friendly_names = {'prompt': '提示词', 'positive': '正向提示词',
                                                    'negative': '反向提示词', 'seed': '随机种子', 'image': '输入图像',
                                                    'video': '输入视频', 'audio': '输入音频', 'width': '宽度',
                                                    'height': '高度', 'steps': '采样步数', 'strength': '强度'}
                                                title_text = (desc if desc and len(desc) <= 48 and '\n' not in desc
                                                    else friendly_names.get(field_name.lower(), field_name)
                                                    or str(node.get('nodeName') or f'参数 {idx + 1}'))
                                                subtitle_text = desc if desc != title_text else ''

                                                title_lbl = QtWidgets.QLabel(title_text)
                                                title_lbl.setObjectName('rhNodeTitle')
                                                title_lbl.setWordWrap(True)
                                                title_lbl.setToolTip(f"{field_name} · 节点 {node.get('nodeId', '')}")
                                                title_lbl.setProperty('node_title', True)
                                                hdr_v.addWidget(title_lbl)
                                                if subtitle_text:
                                                    sub_lbl = QtWidgets.QLabel(subtitle_text)
                                                    sub_lbl.setObjectName('rhNodeSubtitle')
                                                    sub_lbl.setWordWrap(True)
                                                    sub_lbl.setProperty('node_title', True)
                                                    hdr_v.addWidget(sub_lbl)

                                                hdr_h.addLayout(hdr_v, 1)
                                                kind = str(node.get('fieldType') or '').upper()
                                                type_names = {'STRING': '文本', 'IMAGE': '图像', 'VIDEO': '视频',
                                                    'AUDIO': '音频', 'UPLOAD': '文件', 'INT': '整数', 'FLOAT': '小数',
                                                    'DOUBLE': '小数', 'NUMBER': '数值', 'NUMERIC': '数值',
                                                    'LIST': '选项', 'BOOLEAN': '开关', 'BOOL': '开关'}
                                                type_badge = QtWidgets.QLabel(type_names.get(kind, '参数'))
                                                type_badge.setObjectName('rhTypeBadge')
                                                type_badge.setProperty('fieldType', kind)
                                                hdr_h.addWidget(type_badge, 0, Qt.AlignTop)
                                                # collapse/expand button (prominent down-arrow)
                                                try:
                                                    btn_collapse = QtWidgets.QPushButton('收起')
                                                    btn_collapse.setToolTip('折叠/展开')
                                                    btn_collapse.setFixedSize(48, 28)
                                                    btn_collapse.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                    btn_collapse.setObjectName('rhToolButton')
                                                except Exception:
                                                    btn_collapse = QtWidgets.QPushButton('▾')
                                                hdr_h.addWidget(btn_collapse)
                                                box._rh_collapse = btn_collapse

                                                bl.addWidget(hdr_container)

                                                # toggle function: hide/show non-title widgets inside this card
                                                def _toggle_card(_checked=False, _box=box, _body=field_body, _button=btn_collapse):
                                                    try:
                                                        collapsed = not bool(getattr(_box, '_collapsed', False))
                                                        _box._collapsed = collapsed
                                                        _body.setVisible(not collapsed)
                                                        _button.setText('展开' if collapsed else '收起')
                                                        _button.setToolTip('展开参数' if collapsed else '折叠参数')
                                                    except Exception:
                                                        pass

                                                try:
                                                    btn_collapse.clicked.connect(_toggle_card)
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass

                                            # field info
                                            field_row = QtWidgets.QFormLayout()
                                            field_row.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
                                            field_row.setContentsMargins(0, 0, 0, 0)
                                            field_row.setSpacing(8)
                                            try:
                                                ftype = (node.get('fieldType') or '').upper()
                                            except Exception:
                                                ftype = ''
                                            fname = node.get('fieldName') or ''
                                            fval = node.get('fieldValue') if node.get('fieldValue') is not None else ''
                                            fdata = node.get('fieldData') or ''

                                            # Heuristic: treat certain STRING fields as upload-paths when metadata or names indicate upload
                                            try:
                                                is_upload_field = False
                                                try:
                                                    ft_norm = (ftype or '').strip().upper()
                                                except Exception:
                                                    ft_norm = ftype
                                                if ft_norm == 'UPLOAD':
                                                    is_upload_field = True
                                                else:
                                                    # check field name or node name for upload/zip hints
                                                    try:
                                                        nname = (node.get('fieldName') or '') or (node.get('nodeName') or '')
                                                        if isinstance(nname, str) and ('upload' in nname.lower() or 'zip' in nname.lower()):
                                                            is_upload_field = True
                                                    except Exception:
                                                        pass
                                                    # inspect fieldData for upload hints (e.g. {"image_upload": true})
                                                    try:
                                                        if not is_upload_field and isinstance(fdata, str):
                                                            import json as _json
                                                            try:
                                                                parsed_fd = _json.loads(fdata)
                                                            except Exception:
                                                                parsed_fd = None
                                                        else:
                                                            parsed_fd = fdata if not isinstance(fdata, str) else None
                                                        if isinstance(parsed_fd, dict):
                                                            if parsed_fd.get('image_upload') or parsed_fd.get('upload') or parsed_fd.get('zip'):
                                                                is_upload_field = True
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                is_upload_field = False
                                            # mark the node so upload step can detect heuristic uploads
                                            try:
                                                if is_upload_field:
                                                    try:
                                                        node['_rh_upload'] = True
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass

                                            # helper: persist a fieldValue back to disk
                                            def _persist_and_write(val, index=idx):
                                                try:
                                                    # if UI-driven update is suspended (e.g. during initial load or watcher-driven sync), do not write
                                                    try:
                                                        if getattr(app_page, '_rh_suspending_persistence', False):
                                                            return
                                                    except Exception:
                                                        pass
                                                    # update in-memory structure
                                                    try:
                                                        if isinstance(parsed, dict):
                                                            # prefer nodeInfoList key when present
                                                            lst = parsed.get('nodeInfoList')
                                                            if isinstance(lst, list) and 0 <= index < len(lst):
                                                                lst[index]['fieldValue'] = val
                                                            else:
                                                                # fallback: try to update node_list
                                                                if isinstance(node_list, list) and 0 <= index < len(node_list):
                                                                    node_list[index]['fieldValue'] = val
                                                        elif isinstance(parsed, list):
                                                            if 0 <= index < len(parsed):
                                                                parsed[index]['fieldValue'] = val
                                                    except Exception:
                                                        pass
                                                    # update in-memory references so other code can read changes immediately
                                                    try:
                                                        try:
                                                            app_page._rh_parsed = parsed
                                                        except Exception:
                                                            pass
                                                        if wid:
                                                            try:
                                                                if not hasattr(self, '_rh_app_data'):
                                                                    self._rh_app_data = {}
                                                                self._rh_app_data[wid] = parsed
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                    # atomic write — block the file watcher signals to avoid reacting to our own write
                                                    try:
                                                        watcher = getattr(app_page, '_rh_watcher', None)
                                                        if watcher is not None:
                                                            try:
                                                                watcher.blockSignals(True)
                                                            except Exception:
                                                                pass
                                                        # prepare a copy for dumping where all fieldValue entries are strings
                                                        try:
                                                            import copy as _copy
                                                            to_dump = _copy.deepcopy(parsed)
                                                        except Exception:
                                                            to_dump = parsed
                                                        try:
                                                            def _coerce_fieldvalues(pobj):
                                                                try:
                                                                    nodes = pobj.get('nodeInfoList') if isinstance(pobj, dict) else (pobj if isinstance(pobj, list) else [])
                                                                except Exception:
                                                                    nodes = []
                                                                try:
                                                                    if isinstance(nodes, dict):
                                                                        nodes = [nodes]
                                                                except Exception:
                                                                    pass
                                                                for nd in (nodes or []):
                                                                    try:
                                                                        if isinstance(nd, dict) and 'fieldValue' in nd:
                                                                            v = nd.get('fieldValue')
                                                                            nd['fieldValue'] = '' if v is None else str(v)
                                                                    except Exception:
                                                                        pass
                                                            _coerce_fieldvalues(to_dump)
                                                            try:
                                                                def _strip_internal_flags(pobj):
                                                                    try:
                                                                        nodes = pobj.get('nodeInfoList') if isinstance(pobj, dict) else (pobj if isinstance(pobj, list) else [])
                                                                    except Exception:
                                                                        nodes = []
                                                                    try:
                                                                        if isinstance(nodes, dict):
                                                                            nodes = [nodes]
                                                                    except Exception:
                                                                        pass
                                                                    for nd in (nodes or []):
                                                                        try:
                                                                            if isinstance(nd, dict):
                                                                                keys = list(nd.keys())
                                                                                for k in keys:
                                                                                    try:
                                                                                        if isinstance(k, str) and k.startswith('_rh_'):
                                                                                            try:
                                                                                                del nd[k]
                                                                                            except Exception:
                                                                                                pass
                                                                                    except Exception:
                                                                                        pass
                                                                        except Exception:
                                                                            pass
                                                                _strip_internal_flags(to_dump)
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            os.makedirs(os.path.dirname(fpath), exist_ok=True)
                                                        except Exception:
                                                            pass
                                                        tmp = fpath + '.tmp'
                                                        try:
                                                            with open(tmp, 'w', encoding='utf-8') as wf:
                                                                json.dump(to_dump, wf, ensure_ascii=False, indent=2)
                                                            try:
                                                                os.replace(tmp, fpath)
                                                            except Exception:
                                                                try:
                                                                    os.replace(tmp, fpath)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            try:
                                                                with open(fpath, 'w', encoding='utf-8') as wf:
                                                                    json.dump(to_dump, wf, ensure_ascii=False, indent=2)
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        try:
                                                            try:
                                                                import copy as _copy
                                                                to_dump = _copy.deepcopy(parsed)
                                                            except Exception:
                                                                to_dump = parsed
                                                            try:
                                                                def _coerce_fieldvalues(pobj):
                                                                    try:
                                                                        nodes = pobj.get('nodeInfoList') if isinstance(pobj, dict) else (pobj if isinstance(pobj, list) else [])
                                                                    except Exception:
                                                                        nodes = []
                                                                    try:
                                                                        if isinstance(nodes, dict):
                                                                            nodes = [nodes]
                                                                    except Exception:
                                                                        pass
                                                                    for nd in (nodes or []):
                                                                        try:
                                                                            if isinstance(nd, dict) and 'fieldValue' in nd:
                                                                                v = nd.get('fieldValue')
                                                                                nd['fieldValue'] = '' if v is None else str(v)
                                                                        except Exception:
                                                                            pass
                                                                _coerce_fieldvalues(to_dump)
                                                            except Exception:
                                                                pass
                                                            with open(fpath, 'w', encoding='utf-8') as wf:
                                                                json.dump(to_dump, wf, ensure_ascii=False, indent=2)
                                                        except Exception:
                                                            pass
                                                    finally:
                                                        try:
                                                            watcher = getattr(app_page, '_rh_watcher', None)
                                                            if watcher is not None:
                                                                try:
                                                                    watcher.blockSignals(False)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass

                                            # choose widget by type
                                            if (ftype in ('IMAGE', 'VIDEO', 'AUDIO', 'UPLOAD')) or (locals().get('is_upload_field', False)):
                                                # lineedit + browse
                                                row_w = QtWidgets.QWidget()
                                                row_l = QtWidgets.QHBoxLayout(row_w)
                                                row_l.setContentsMargins(0, 0, 0, 0)
                                                row_l.setSpacing(8)
                                                le = QtWidgets.QLineEdit()
                                                le.setText(str(fval))
                                                le.setPlaceholderText('选择本地文件路径')
                                                btn_b = QtWidgets.QPushButton('浏览')
                                                btn_b.setFixedHeight(36)
                                                btn_b.setObjectName('rhSecondaryButton')
                                                def _browse_and_set(_le=le, ft=ftype, _i=idx):
                                                    try:
                                                        # normalize field-type (strip whitespace, uppercase) to avoid mismatches
                                                        try:
                                                            ft_norm = (ft or '').strip().upper()
                                                        except Exception:
                                                            ft_norm = (ftype or '').upper()
                                                        if ft_norm == 'IMAGE':
                                                            filt = 'Images (*.png *.jpg *.jpeg *.webp);;All Files (*)'
                                                        elif ft_norm == 'VIDEO':
                                                            filt = 'Videos (*.mp4 *.avi *.mov *.mkv *.gif);;All Files (*)'
                                                        elif ft_norm == 'AUDIO':
                                                            filt = 'Audio (*.mp3 *.wav *.flac);;All Files (*)'
                                                        elif ft_norm == 'UPLOAD':
                                                            filt = 'ZIP Files (*.zip);;All Files (*)'
                                                        else:
                                                            filt = 'All Files (*)'
                                                        # prefer last browsed directory if available
                                                        try:
                                                            start_dir = os.path.expanduser('~')
                                                            if isinstance(getattr(self, 'settings', None), dict):
                                                                sd = self.settings.get('last_browse_dir')
                                                                if sd:
                                                                    start_dir = sd
                                                        except Exception:
                                                            start_dir = os.path.expanduser('~')
                                                        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择文件', start_dir, filt)
                                                        if p:
                                                            try:
                                                                p_abs = os.path.abspath(p)
                                                            except Exception:
                                                                p_abs = p
                                                            # persist last browsed directory for next time
                                                            try:
                                                                d = os.path.dirname(p_abs)
                                                                if isinstance(getattr(self, 'settings', None), dict):
                                                                    self.settings['last_browse_dir'] = d
                                                                    try:
                                                                        self._save_settings()
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                            try:
                                                                _le.setText(p_abs)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                _persist_and_write(p_abs, _i)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                # load preview if available
                                                                label = node_widgets.get(_i, {}).get('prev_label')
                                                                if label is not None:
                                                                    label._load_preview(p_abs)
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                # explicitly bind the field type when connecting to avoid late-binding surprises
                                                # pass 'UPLOAD' as the field-type argument when we heuristically detected an upload field
                                                try:
                                                    passed_ft = 'UPLOAD' if locals().get('is_upload_field', False) else ftype
                                                except Exception:
                                                    passed_ft = ftype
                                                btn_b.clicked.connect(lambda _checked=False, _le=le, _ft=passed_ft, _i=idx: _browse_and_set(_le, _ft, _i))
                                                # style input for visibility according to theme
                                                try:
                                                    # use input_bg (darker shade of card) for input backgrounds
                                                    le.setStyleSheet('')
                                                    btn_b.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                row_l.addWidget(le)
                                                row_l.addWidget(btn_b)
                                                try:
                                                    row_w.setToolTip(f"{fname} ({ftype})")
                                                except Exception:
                                                    pass
                                                # persist when user finishes editing path
                                                try:
                                                    def _on_le_finish(_le=le, _i=idx):
                                                        try:
                                                            p = _le.text()
                                                            try:
                                                                p_abs = os.path.abspath(p) if p and os.path.exists(p) else p
                                                            except Exception:
                                                                p_abs = p
                                                            _persist_and_write(p_abs, _i)
                                                            try:
                                                                label = node_widgets.get(_i, {}).get('prev_label')
                                                                if label is not None:
                                                                    label._load_preview(p_abs)
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                    le.editingFinished.connect(_on_le_finish)
                                                except Exception:
                                                    pass
                                                field_row.addRow(row_w)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['le'] = le
                                                except Exception:
                                                    pass
                                                # prev_label is created later for IMAGE/VIDEO fields; set after creation
                                            elif ftype == 'STRING':
                                                te = CompletionTextEdit(current_dir=current_dir)
                                                te.setPlainText(str(fval))
                                                min_te_h = 88
                                                te.setMinimumHeight(min_te_h)
                                                try:
                                                    te.setUndoRedoEnabled(True)
                                                except Exception:
                                                    pass
                                                try:
                                                    te.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                try:
                                                    te.setToolTip(f"{fname} (STRING)")
                                                except Exception:
                                                    pass
                                                try:
                                                    # disable vertical scrollbar; we'll resize to fit content instead
                                                    te.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
                                                    te.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
                                                    te.setWordWrapMode(QtGui.QTextOption.WordWrap)
                                                except Exception:
                                                    pass

                                                def _adjust_te_height(_te, _min=min_te_h):
                                                    try:
                                                        doc = _te.document()
                                                        # ensure document width matches the viewport so height calculation is accurate
                                                        try:
                                                            w = max(10, _te.viewport().width())
                                                            doc.setTextWidth(w)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            h = int(doc.size().height() + _te.frameWidth() * 2 + 8)
                                                        except Exception:
                                                            h = _min
                                                        h = max(_min, h)
                                                        try:
                                                            _te.setFixedHeight(min(h, 220))
                                                        except Exception:
                                                            pass
                                                    except Exception:
                                                        pass

                                                class _AutoHeightWatcher(QtCore.QObject):
                                                    def __init__(self, _te, _min_h):
                                                        super().__init__(_te)
                                                        self._te = _te
                                                        self._min_h = _min_h

                                                    def eventFilter(self, watched, event):
                                                        try:
                                                            if event.type() == QtCore.QEvent.Resize:
                                                                _adjust_te_height(self._te, self._min_h)
                                                        except Exception:
                                                            pass
                                                        return False

                                                # connect persistence and resizing on content change
                                                try:
                                                    # debounce persistence to avoid writing on every keystroke
                                                    try:
                                                        _deb_timer = QtCore.QTimer(te)
                                                        _deb_timer.setSingleShot(True)
                                                        te._rh_persist_timer = _deb_timer
                                                        _deb_interval = 600
                                                    except Exception:
                                                        _deb_timer = None
                                                        _deb_interval = 600

                                                    def _do_persist_timer(_t=_deb_timer, _edit=te, _i=idx):
                                                        try:
                                                            if _t is not None:
                                                                _t.stop()
                                                        except Exception:
                                                            pass
                                                        try:
                                                            _persist_and_write(_edit.toPlainText(), _i)
                                                        except Exception:
                                                            pass

                                                    def _on_te_changed(_te=te, _i=idx, _t=_deb_timer):
                                                        try:
                                                            # restart timer
                                                            if _t is not None:
                                                                try:
                                                                    _t.stop()
                                                                    _t.start(_deb_interval)
                                                                except Exception:
                                                                    pass
                                                            else:
                                                                try:
                                                                    _persist_and_write(_te.toPlainText(), _i)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            _adjust_te_height(_te, min_te_h)
                                                        except Exception:
                                                            pass

                                                    try:
                                                        if _deb_timer is not None:
                                                            _deb_timer.timeout.connect(lambda _te=te, _i=idx: _do_persist_timer(None, _te, _i))
                                                    except Exception:
                                                        pass
                                                    try:
                                                        te.textChanged.connect(_on_te_changed)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass

                                                # install watcher to react to width changes
                                                try:
                                                    watcher = _AutoHeightWatcher(te, min_te_h)
                                                    te.installEventFilter(watcher)
                                                except Exception:
                                                    pass

                                                # initial sizing
                                                try:
                                                    _adjust_te_height(te, min_te_h)
                                                except Exception:
                                                    pass

                                                # container to hold textedit and bottom-right control buttons
                                                try:
                                                    te_container = QtWidgets.QWidget()
                                                    te_v = QtWidgets.QVBoxLayout(te_container)
                                                    te_v.setContentsMargins(0, 0, 0, 0)
                                                    te_v.setSpacing(6)
                                                    te_v.addWidget(te)

                                                    btn_row = QtWidgets.QHBoxLayout()
                                                    btn_row.addStretch(1)

                                                    # translator & expand buttons
                                                    try:
                                                        btn_expand = QtWidgets.QPushButton('扩写')
                                                        btn_zh = QtWidgets.QPushButton('中文')
                                                        btn_en = QtWidgets.QPushButton('英文')
                                                        for b in (btn_expand, btn_zh, btn_en):
                                                            b.setFixedSize(48, 30)
                                                            b.setObjectName('rhToolButton')
                                                            b.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                            try:
                                                                b.setStyleSheet('')
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        btn_expand = QtWidgets.QPushButton('扩写')
                                                        btn_zh = QtWidgets.QPushButton('中 ')
                                                        btn_en = QtWidgets.QPushButton('英')
                                                        try:
                                                            for b in (btn_expand, btn_zh, btn_en):
                                                                b.setFixedSize(40, 40)
                                                                b.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                                try:
                                                                    b.setStyleSheet('font-size:25px; padding:0px; border-radius:4px;')
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass

                                                    # undo/return button (with arrow icon if available)
                                                    try:
                                                        undo_btn = QtWidgets.QPushButton()
                                                        undo_btn.setFixedSize(30, 30)
                                                        undo_btn.setObjectName('rhToolButton')
                                                        try:
                                                            icon = self.style().standardIcon(QtWidgets.QStyle.SP_ArrowBack)
                                                            undo_btn.setIcon(icon)
                                                        except Exception:
                                                            undo_btn.setText('←')
                                                        undo_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                        try:
                                                            undo_btn.setStyleSheet('')
                                                        except Exception:
                                                            pass
                                                    except Exception:
                                                        undo_btn = QtWidgets.QPushButton('←')
                                                        undo_btn.setFixedSize(36, 36)

                                                    forward_btn = QtWidgets.QPushButton()
                                                    forward_btn.setFixedSize(30, 30)
                                                    forward_btn.setObjectName('rhToolButton')
                                                    forward_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowForward))
                                                    forward_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

                                                    # add expand button to the left of zh
                                                    btn_row.addWidget(btn_expand)
                                                    btn_row.addWidget(btn_zh)
                                                    btn_row.addWidget(btn_en)
                                                    btn_row.addWidget(undo_btn)
                                                    btn_row.addWidget(forward_btn)
                                                    te_v.addLayout(btn_row)
                                                except Exception:
                                                    te_container = te

                                                field_row.addRow(te_container)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['te'] = te
                                                except Exception:
                                                    pass

                                                prompt_history = PromptHistory(te, undo_btn, forward_btn,
                                                    input_history_entries(self, wid, node, idx))
                                                node_widgets[idx]['_prompt_history'] = prompt_history

                                                # helper: perform translation via api_calls/translators.translate_auto
                                                def _do_translate(target_lang, _te=te, _idx=idx, _btns=None, _history=prompt_history):
                                                    try:
                                                        cur = _te.toPlainText()
                                                        _history.record(cur, 'before_translation')
                                                        # disable buttons immediately
                                                        try:
                                                            if _btns:
                                                                for b in _btns:
                                                                    try:
                                                                        b.setEnabled(False)
                                                                    except Exception:
                                                                        pass
                                                        except Exception:
                                                            pass

                                                        class _TranslateSignals(QtCore.QObject):
                                                            finished = QtCore.pyqtSignal(object)

                                                        class _TranslateJob(QtCore.QRunnable):
                                                            def __init__(self, text, tlang):
                                                                super().__init__()
                                                                self.text = text
                                                                self.tlang = tlang
                                                                self.signals = _TranslateSignals()

                                                            # capture owner (MainWindow) into closure for worker thread
                                                            pass
                                                            
                                                            def run(self):
                                                                res = None
                                                                timeout = 30
                                                                # attempt configured translator API first (from UI settings/apikeys)
                                                                try:
                                                                    owner = getattr(self, '_owner_ref', None) or None
                                                                except Exception:
                                                                    owner = None
                                                                try:
                                                                    if owner is None:
                                                                        owner = locals().get('self_owner', None)
                                                                except Exception:
                                                                    owner = owner

                                                                tried_api = False
                                                                try:
                                                                    if owner is not None:
                                                                        try:
                                                                            api_cfg = getattr(owner, 'api_settings', None) or (owner.settings.get('api_settings') if isinstance(getattr(owner, 'settings', None), dict) else {})
                                                                        except Exception:
                                                                            api_cfg = {}
                                                                        try:
                                                                            tcfg = (api_cfg or {}).get('translator') or {}
                                                                        except Exception:
                                                                            tcfg = {}
                                                                        api_url = tcfg.get('endpoint') or tcfg.get('api_url') or ''
                                                                        model = tcfg.get('model') or ''
                                                                        provider = tcfg.get('provider')
                                                                        try:
                                                                            timeout = int(tcfg.get('timeout') or 30)
                                                                        except Exception:
                                                                            timeout = 30

                                                                        # Resolve only the selected translation provider's credentials.
                                                                        from aetherloom_core.api_credentials import get_credentials
                                                                        credentials = get_credentials(getattr(owner, '_apikeys', None), provider, 'translator')
                                                                        api_key = credentials.get('api_key', '')
                                                                        has_credentials = bool(api_key or (credentials.get('appid') and credentials.get('secret')))

                                                                        if api_url and has_credentials:
                                                                            tried_api = True
                                                                            try:
                                                                                from api_calls.call_translate import translate_text
                                                                                alt = translate_text(api_url, api_key, model or '', self.text, self.tlang or 'en', timeout=timeout, provider=provider, extra={name: credentials[name] for name in ('appid', 'secret') if credentials.get(name)})
                                                                                if alt:
                                                                                    res = alt
                                                                            except Exception:
                                                                                pass
                                                                except Exception:
                                                                    pass

                                                                # if configured API did not produce result, fall back to internal translators.translate_auto
                                                                if not res:
                                                                    try:
                                                                        from api_calls.translators import translate_auto
                                                                        res = translate_auto(self.text, self.tlang, verbose=False, timeout=timeout)
                                                                    except Exception:
                                                                        res = None

                                                                try:
                                                                    self.signals.finished.emit(res)
                                                                except Exception:
                                                                    pass

                                                        try:
                                                            job = _TranslateJob(cur, target_lang)
                                                            try:
                                                                # attach reference to MainWindow so worker can read settings/apikeys
                                                                job._owner_ref = self
                                                            except Exception:
                                                                pass
                                                            def _on_done(res):
                                                                try:
                                                                    if res is not None:
                                                                        try:
                                                                            _history.apply_result(res, 'translation:' + target_lang)
                                                                        except Exception:
                                                                            pass
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    def _reenable():
                                                                        try:
                                                                            if _btns:
                                                                                for b in _btns:
                                                                                    try:
                                                                                        b.setEnabled(True)
                                                                                    except Exception:
                                                                                        pass
                                                                        except Exception:
                                                                            pass
                                                                    try:
                                                                        QtCore.QTimer.singleShot(200, _reenable)
                                                                    except Exception:
                                                                        _reenable()
                                                                except Exception:
                                                                    pass

                                                            try:
                                                                job.signals.finished.connect(_on_done)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                pool = getattr(self, '_thumb_pool', None) or QtCore.QThreadPool.globalInstance()
                                                            except Exception:
                                                                pool = QtCore.QThreadPool.globalInstance()
                                                            try:
                                                                pool.start(job)
                                                            except Exception:
                                                                # fallback: run synchronously
                                                                job.run()
                                                        except Exception:
                                                            pass
                                                    except Exception:
                                                        pass

                                                # helper: perform expansion via api_calls.call_llm.call_llm
                                                def _do_expand(_te=te, _idx=idx, _btns=None, _history=prompt_history):
                                                    try:
                                                        cur = _te.toPlainText()
                                                        _history.record(cur, 'before_expansion')
                                                        try:
                                                            if _btns:
                                                                for b in _btns:
                                                                    try:
                                                                        b.setEnabled(False)
                                                                    except Exception:
                                                                        pass
                                                        except Exception:
                                                            pass

                                                        class _ExpandSignals(QtCore.QObject):
                                                            finished = QtCore.pyqtSignal(object)

                                                        class _ExpandWorker(QtCore.QRunnable):
                                                            def __init__(self, func):
                                                                super().__init__()
                                                                self.func = func
                                                                self.signals = _ExpandSignals()

                                                            def run(self):
                                                                try:
                                                                    res = self.func()
                                                                except Exception:
                                                                    res = None
                                                                try:
                                                                    self.signals.finished.emit(res)
                                                                except Exception:
                                                                    pass

                                                        try:
                                                            # prefer the live `self.api_settings` updated by the UI; fallback to persisted `self.settings`
                                                            api_settings_live = getattr(self, 'api_settings', None)
                                                            if not isinstance(api_settings_live, dict):
                                                                api_settings_live = (getattr(self, 'settings', {}) or {}).get('api_settings', {})
                                                            api_cfg = api_settings_live.get('llm', {}) if isinstance(api_settings_live, dict) else {}
                                                            provider = api_cfg.get('provider') if isinstance(api_cfg, dict) else None
                                                            endpoint = api_cfg.get('endpoint') if isinstance(api_cfg, dict) else None
                                                            model = api_cfg.get('model') if isinstance(api_cfg, dict) else None
                                                            timeout = int(api_cfg.get('timeout', 30)) if isinstance(api_cfg, dict) else 30
                                                        except Exception:
                                                            provider = None
                                                            endpoint = None
                                                            model = None
                                                            timeout = 30
                                                        from aetherloom_core.api_credentials import get_credentials
                                                        api_key = get_credentials(getattr(self, '_apikeys', {}), provider, 'llm').get('api_key', '')
                                                        if not api_key:
                                                            try:
                                                                fpath = getattr(self, '_apikeys_file', None)
                                                                if fpath and os.path.isfile(fpath):
                                                                    with open(fpath, 'r', encoding='utf-8') as key_file:
                                                                        api_key = get_credentials(json.load(key_file), provider, 'llm').get('api_key', '')
                                                            except (OSError, ValueError, TypeError):
                                                                pass

                                                        def _worker_call():
                                                            try:
                                                                if endpoint and (api_key or provider == 'ollama') and model:
                                                                    from api_calls.call_llm import call_llm
                                                                    try:
                                                                        sys_prompt = '扩写提示词'
                                                                        try:
                                                                            sys_prompt = (getattr(self, 'settings', {}) or {}).get('expand_system_prompt', sys_prompt)
                                                                        except Exception:
                                                                            pass
                                                                        return call_llm(endpoint, api_key or '', model, sys_prompt, cur, temperature=0.6, timeout=timeout, provider=provider)
                                                                    except Exception:
                                                                        return None
                                                                return None
                                                            except Exception:
                                                                return None

                                                        try:
                                                            job = _ExpandWorker(_worker_call)
                                                            def _on_done_expand(res):
                                                                try:
                                                                    if res is not None:
                                                                        try:
                                                                            _history.apply_result(res, 'expansion')
                                                                        except Exception:
                                                                            pass
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    def _reenable_ex():
                                                                        try:
                                                                            if _btns:
                                                                                for b in _btns:
                                                                                    try:
                                                                                        b.setEnabled(True)
                                                                                    except Exception:
                                                                                        pass
                                                                        except Exception:
                                                                            pass
                                                                    try:
                                                                        QtCore.QTimer.singleShot(200, _reenable_ex)
                                                                    except Exception:
                                                                        _reenable_ex()
                                                                except Exception:
                                                                    pass

                                                            try:
                                                                job.signals.finished.connect(_on_done_expand)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                pool = getattr(self, '_thumb_pool', None) or QtCore.QThreadPool.globalInstance()
                                                            except Exception:
                                                                pool = QtCore.QThreadPool.globalInstance()
                                                            try:
                                                                pool.start(job)
                                                            except Exception:
                                                                job.run()
                                                        except Exception:
                                                            pass
                                                    except Exception:
                                                        pass

                                                # Snapshot navigation is separate from native text-edit undo/redo.
                                                btns_tuple = (btn_expand, btn_zh, btn_en)
                                                try:
                                                    btn_zh.clicked.connect(lambda _checked=False, _f=_do_translate, _te=te, _i=idx, _btns=btns_tuple: _f('zh', _te, _i, _btns))
                                                except Exception:
                                                    pass
                                                try:
                                                    btn_en.clicked.connect(lambda _checked=False, _f=_do_translate, _te=te, _i=idx, _btns=btns_tuple: _f('en', _te, _i, _btns))
                                                except Exception:
                                                    pass
                                                try:
                                                    btn_expand.clicked.connect(lambda _checked=False, _f=_do_expand, _te=te, _i=idx, _btns=btns_tuple: _f(_te, _i, _btns))
                                                except Exception:
                                                    pass

                                            elif ftype in ('FLOAT', 'DOUBLE', 'NUMBER', 'NUMERIC'):
                                                try:
                                                    sv = str(fval)
                                                except Exception:
                                                    sv = '0'
                                                ds = RhNumberSpinBox()
                                                ds.configure(fdata)
                                                try:
                                                    ds.setValue(sv)
                                                except Exception:
                                                    pass
                                                try:
                                                    ds.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                try:
                                                    ds.setToolTip(f"{fname} (FLOAT)")
                                                except Exception:
                                                    pass
                                                try:
                                                    ds.installEventFilter(self)
                                                except Exception:
                                                    pass
                                                try:
                                                    ds.valueChanged.connect(lambda v, _i=idx: _persist_and_write(v, _i))
                                                except Exception:
                                                    pass
                                                field_row.addRow(ds)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['ds'] = ds
                                                except Exception:
                                                    pass
                                            elif ftype == 'INT':
                                                try:
                                                    sv = int(fval)
                                                except Exception:
                                                    sv = 0
                                                sb = RhNumberSpinBox(integer=True)
                                                sb.configure(fdata)
                                                sb.setValue(sv)
                                                try:
                                                    sb.installEventFilter(self)
                                                except Exception:
                                                    pass
                                                try:
                                                    sb.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                try:
                                                    sb.setToolTip(f"{fname} (INT)")
                                                except Exception:
                                                    pass
                                                try:
                                                    sb.valueChanged.connect(lambda v, _i=idx: _persist_and_write(v, _i))
                                                except Exception:
                                                    pass
                                                field_row.addRow(sb)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['sb'] = sb
                                                except Exception:
                                                    pass
                                            elif ftype == 'BOOLEAN':
                                                combo_bool = RhEnumComboBox()
                                                try:
                                                    combo_bool.addItem('true')
                                                    combo_bool.addItem('false')
                                                    # determine current index from fval (common stored as string)
                                                    try:
                                                        cur = 0 if str(fval).lower() in ('true', '1', 'yes') else 1
                                                    except Exception:
                                                        cur = 1
                                                    combo_bool.setCurrentIndex(cur)
                                                except Exception:
                                                    pass
                                                try:
                                                    combo_bool.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                try:
                                                    combo_bool.setToolTip(f"{fname} (BOOLEAN)")
                                                except Exception:
                                                    pass
                                                try:
                                                    def _on_bool_changed(i, _i=idx, _combo=combo_bool):
                                                        try:
                                                            val = 'true' if i == 0 else 'false'
                                                            _persist_and_write(val, _i)
                                                        except Exception:
                                                            try:
                                                                _persist_and_write(_combo.currentText(), _i)
                                                            except Exception:
                                                                pass
                                                    combo_bool.currentIndexChanged.connect(_on_bool_changed)
                                                except Exception:
                                                    pass
                                                field_row.addRow(combo_bool)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['combo_bool'] = combo_bool
                                                except Exception:
                                                    pass

                                            elif ftype == 'LIST':
                                                from aetherloom_core.rh_parameters import configure_list_combo
                                                combo = RhEnumComboBox()
                                                configure_list_combo(combo, fdata, fval)
                                                try:
                                                    combo.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                try:
                                                    combo.setToolTip(f"{fname} (LIST)")
                                                except Exception:
                                                    pass
                                                try:
                                                    def _on_combo_changed(i, _i=idx, _combo=combo):
                                                        try:
                                                            opts = getattr(_combo, '_rh_options', [])
                                                            if 0 <= i < len(opts):
                                                                val = opts[i]
                                                            else:
                                                                val = _combo.currentText()
                                                            _persist_and_write(val, _i)
                                                        except Exception:
                                                            try:
                                                                _persist_and_write(_combo.currentText(), _i)
                                                            except Exception:
                                                                pass
                                                    combo.currentIndexChanged.connect(_on_combo_changed)
                                                except Exception:
                                                    pass
                                                field_row.addRow(combo)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['combo'] = combo
                                                except Exception:
                                                    pass
                                            else:
                                                le2 = QtWidgets.QLineEdit()
                                                le2.setText(str(fval))
                                                try:
                                                    le2.setStyleSheet('')
                                                except Exception:
                                                    pass
                                                try:
                                                    le2.setToolTip(f"{fname} ({ftype})")
                                                except Exception:
                                                    pass
                                                try:
                                                    le2.editingFinished.connect(lambda _le=le2, _i=idx: _persist_and_write(_le.text(), _i))
                                                except Exception:
                                                    pass
                                                field_row.addRow(le2)
                                                try:
                                                    node_widgets[idx] = node_widgets.get(idx, {})
                                                except Exception:
                                                    try:
                                                        node_widgets[idx] = {}
                                                    except Exception:
                                                        pass
                                                try:
                                                    node_widgets[idx]['le2'] = le2
                                                except Exception:
                                                    pass

                                            for editor in node_widgets.get(idx, {}).values():
                                                if isinstance(editor, QtWidgets.QComboBox):
                                                    editor.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
                                                    editor.setMinimumContentsLength(10)
                                                if isinstance(editor, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)):
                                                    editor.setMinimumWidth(0)
                                                    editor.setMinimumHeight(36)
                                                    editor.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

                                            # Show user-facing constraints, keeping API metadata out of the form.
                                            try:
                                                field_body_layout.addLayout(field_row)
                                                import json as _metadata_json
                                                metadata = fdata
                                                if isinstance(metadata, str):
                                                    try:
                                                        metadata = _metadata_json.loads(metadata)
                                                    except (ValueError, TypeError):
                                                        pass
                                                if isinstance(metadata, list) and metadata and isinstance(metadata[-1], dict):
                                                    metadata = metadata[-1]
                                                hints = []
                                                if ftype in ('INT', 'FLOAT', 'DOUBLE', 'NUMBER', 'NUMERIC') and isinstance(metadata, dict):
                                                    if metadata.get('min') is not None and metadata.get('max') is not None:
                                                        hints.append(f"范围 {metadata['min']} – {metadata['max']}")
                                                    elif metadata.get('min') is not None:
                                                        hints.append(f"最小值 {metadata['min']}")
                                                    elif metadata.get('max') is not None:
                                                        hints.append(f"最大值 {metadata['max']}")
                                                    if metadata.get('step') is not None:
                                                        hints.append(f"步长 {metadata['step']}")
                                                elif isinstance(metadata, str) and ftype != 'LIST' and not metadata.lstrip().startswith(('{', '[')):
                                                    hints.append(metadata)
                                                if hints:
                                                    fd_lbl = QtWidgets.QLabel(' · '.join(hints))
                                                    fd_lbl.setObjectName('rhMuted')
                                                    fd_lbl.setWordWrap(True)
                                                    fd_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                                                    field_body_layout.addWidget(fd_lbl)
                                            except Exception:
                                                pass
                                            bl.addWidget(field_body)

                                            nodes_v.addWidget(box)
                                            # for IMAGE/VIDEO fields, add a separate preview card under the node card
                                            try:
                                                if ftype in ('IMAGE', 'VIDEO'):
                                                    prev_box = QtWidgets.QFrame()
                                                    prev_box.setFrameShape(QtWidgets.QFrame.StyledPanel)
                                                    prev_box.setObjectName('nodePreviewCard')
                                                    prev_layout = QtWidgets.QVBoxLayout(prev_box)
                                                    prev_layout.setContentsMargins(12, 8, 12, 12)
                                                    prev_layout.setSpacing(6)
                                                    prev_label = QtWidgets.QLabel()
                                                    prev_label.setObjectName('rhInputPreview')
                                                    prev_label.setProperty('rh_compact_input', True)
                                                    prev_label.setAlignment(Qt.AlignCenter)
                                                    prev_label.setMinimumWidth(50)
                                                    prev_label.setMinimumHeight(30)
                                                    prev_label.setMaximumHeight(9999)
                                                    prev_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                    prev_label.setProperty('rh_preview', True)
                                                    # store original pixmap for rescaling on resize
                                                    prev_label._orig_pixmap = None
                                                    prev_label.setAcceptDrops(True)
                                                    # (previously showed file path here; removed per request)

                                                    def _set_placeholder(lbl=prev_label):
                                                        try:
                                                            # delegate to shared helper to ensure consistent theme behavior
                                                            try:
                                                                self._refresh_preview_placeholder(lbl)
                                                            except Exception:
                                                                # fallback inline if helper not available
                                                                target_w = max(120, (lbl.width() - 8) or 200)
                                                                ph = QtGui.QPixmap(target_w, target_w)
                                                                bg = QtGui.QColor('#f0f0f0') if getattr(self, '_theme_mode', 'dark') == 'light' else QtGui.QColor('#121417')
                                                                ph.fill(bg)
                                                                painter = QtGui.QPainter(ph)
                                                                pen = QtGui.QPen(QtGui.QColor(150, 150, 150, 80))
                                                                pen.setStyle(QtCore.Qt.DashLine)
                                                                painter.setPen(pen)
                                                                painter.drawRect(4, 4, target_w-8, target_w-8)
                                                                try:
                                                                    painter.end()
                                                                except Exception:
                                                                    pass
                                                                lbl._orig_pixmap = ph
                                                                try:
                                                                    scaled = ph.scaledToWidth(target_w, QtCore.Qt.SmoothTransformation)
                                                                except Exception:
                                                                    scaled = ph
                                                                lbl.setPixmap(scaled)
                                                                try:
                                                                    lbl.setFixedHeight(scaled.height() + 12)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            try:
                                                                lbl.setText('')
                                                            except Exception:
                                                                pass

                                                    def _update_preview_on_resize(obj, ev):
                                                        try:
                                                            lbl = obj
                                                            if not getattr(lbl, '_last_path', None):
                                                                _set_placeholder(lbl)
                                                                return False
                                                            orig = getattr(lbl, '_orig_pixmap', None)
                                                            if orig is None:
                                                                _set_placeholder(lbl)
                                                                return False
                                                            try:
                                                                target_w = max(120, (lbl.width() - 8) or 200)
                                                                try:
                                                                    scaled = orig.scaled(target_w, 160, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                                                                except Exception:
                                                                    scaled = orig.scaled(target_w, int(target_w * orig.height() / max(1, orig.width())), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                                                                lbl.setPixmap(scaled)
                                                                try:
                                                                    lbl.setFixedHeight(scaled.height() + 12)
                                                                except Exception:
                                                                    pass
                                                            except Exception:
                                                                pass
                                                            return False
                                                        except Exception:
                                                            return False

                                                    class _PrevWatcher(QtCore.QObject):
                                                        def __init__(self, lbl):
                                                            super().__init__(lbl)
                                                            self._lbl = lbl
                                                            # debounce timer for resize events to avoid heavy scaling during splitter drag
                                                            try:
                                                                self._resize_timer = QtCore.QTimer(self)
                                                                self._resize_timer.setSingleShot(True)
                                                                self._resize_timer.setInterval(80)
                                                                self._resize_timer.timeout.connect(self._on_resize_timeout)
                                                            except Exception:
                                                                self._resize_timer = None

                                                        def _on_resize_timeout(self):
                                                            try:
                                                                _update_preview_on_resize(self._lbl, None)
                                                            except Exception:
                                                                pass

                                                        def eventFilter(self, watched, event):
                                                            try:
                                                                if event.type() == QtCore.QEvent.Resize:
                                                                    try:
                                                                        if getattr(self, '_resize_timer', None) is not None:
                                                                            # restart debounce timer
                                                                            self._resize_timer.stop()
                                                                            self._resize_timer.start()
                                                                        else:
                                                                            _update_preview_on_resize(watched, event)
                                                                    except Exception:
                                                                        try:
                                                                            _update_preview_on_resize(watched, event)
                                                                        except Exception:
                                                                            pass
                                                                elif event.type() == QtCore.QEvent.DragEnter:
                                                                    try:
                                                                        md = event.mimeData()
                                                                        if md is not None and (md.hasUrls() or md.hasText()):
                                                                            event.acceptProposedAction()
                                                                            return True
                                                                    except Exception:
                                                                        pass
                                                                elif event.type() == QtCore.QEvent.Drop:
                                                                    try:
                                                                        md = event.mimeData()
                                                                        path = None
                                                                        if md is not None and md.hasUrls():
                                                                            try:
                                                                                url = md.urls()[0]
                                                                                path = url.toLocalFile() or url.toString()
                                                                            except Exception:
                                                                                path = None
                                                                        elif md is not None and md.hasText():
                                                                            path = md.text()
                                                                        if path:
                                                                            handler = getattr(watched, '_on_drop_path', None)
                                                                            if callable(handler):
                                                                                try:
                                                                                    handler(path)
                                                                                except Exception:
                                                                                    pass
                                                                            return True
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                            return False

                                                    watcher = _PrevWatcher(prev_label)
                                                    prev_label.installEventFilter(watcher)

                                                    try:
                                                        prev_label.setContextMenuPolicy(Qt.CustomContextMenu)
                                                        def _on_prev_context(pos, _lbl=prev_label, _le=le, _idx=idx):
                                                            try:
                                                                menu = QtWidgets.QMenu(_lbl)
                                                                act_open = menu.addAction('在默认应用中打开')
                                                                act_open_folder = menu.addAction('在本地文件夹中打开')
                                                                act_copy = menu.addAction('复制到剪贴板')
                                                                act_save = menu.addAction('另存为')
                                                                act_compare = menu.addAction('加入比较')
                                                                menu.addSeparator()
                                                                try:
                                                                    act_image_reverse = menu.addAction('图像反推')
                                                                except Exception:
                                                                    act_image_reverse = None
                                                                act_clear = menu.addAction('清空')
                                                                act_paste = menu.addAction('粘贴')
                                                                

                                                                # clipboard inspection for paste
                                                                cb = QtWidgets.QApplication.clipboard()
                                                                md = cb.mimeData() if cb is not None else None
                                                                ok_enable = False
                                                                candidate = None
                                                                try:
                                                                    if md is not None and md.hasUrls():
                                                                        try:
                                                                            url = md.urls()[0]
                                                                            candidate = url.toLocalFile() or url.toString()
                                                                            if candidate and os.path.exists(candidate):
                                                                                ok_enable = True
                                                                        except Exception:
                                                                            pass
                                                                    elif md is not None and md.hasText():
                                                                        try:
                                                                            txt = md.text().strip()
                                                                            if txt and os.path.exists(txt):
                                                                                candidate = txt
                                                                                ok_enable = True
                                                                        except Exception:
                                                                            pass
                                                                except Exception:
                                                                    pass

                                                                # enable/disable clear/compare based on content
                                                                try:
                                                                    has_content = bool(getattr(_lbl, '_last_path', None))
                                                                except Exception:
                                                                    has_content = False
                                                                try:
                                                                    act_clear.setEnabled(bool(has_content))
                                                                    act_open.setEnabled(bool(has_content))
                                                                    act_open_folder.setEnabled(bool(has_content))
                                                                    act_save.setEnabled(bool(has_content))
                                                                    act_compare.setEnabled(bool(has_content))
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    act_paste.setEnabled(bool(ok_enable))
                                                                except Exception:
                                                                    pass

                                                                action = menu.exec_(QtGui.QCursor.pos())
                                                                if action is None:
                                                                    return

                                                                # helper to resolve path shown in this preview
                                                                try:
                                                                    pth = getattr(_lbl, '_last_path', None)
                                                                except Exception:
                                                                    pth = None

                                                                if action == act_paste and ok_enable and candidate:
                                                                    try:
                                                                        p = os.path.abspath(candidate)
                                                                    except Exception:
                                                                        p = candidate
                                                                    try:
                                                                        _le.setText(p)
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        _persist_and_write(p, _idx)
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        _load_preview_from_path(p)
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_clear:
                                                                    try:
                                                                        try:
                                                                            _le.setText('')
                                                                        except Exception:
                                                                            pass
                                                                        try:
                                                                            _persist_and_write('', _idx)
                                                                        except Exception:
                                                                            pass
                                                                        try:
                                                                            _set_placeholder(_lbl)
                                                                        except Exception:
                                                                            try:
                                                                                self._refresh_preview_placeholder(_lbl)
                                                                            except Exception:
                                                                                pass
                                                                        try:
                                                                            _lbl._last_path = None
                                                                        except Exception:
                                                                            pass
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_open:
                                                                    try:
                                                                        if pth and os.path.exists(pth):
                                                                            if sys.platform.startswith('win'):
                                                                                os.startfile(pth)
                                                                            elif sys.platform == 'darwin':
                                                                                subprocess.Popen(['open', pth])
                                                                            else:
                                                                                subprocess.Popen(['xdg-open', pth])
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_open_folder:
                                                                    try:
                                                                        if pth and os.path.exists(pth):
                                                                            try:
                                                                                try:
                                                                                    folder = os.path.dirname(pth) if pth else pth
                                                                                except Exception:
                                                                                    folder = pth
                                                                                self._reveal_in_explorer(folder)
                                                                            except Exception:
                                                                                pass
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_copy:
                                                                    try:
                                                                        pix = getattr(_lbl, '_orig_pixmap', None)
                                                                        if pix is not None:
                                                                            try:
                                                                                md2 = QtCore.QMimeData()
                                                                                try:
                                                                                    md2.setImageData(pix.toImage())
                                                                                except Exception:
                                                                                    pass
                                                                                try:
                                                                                    ba = QtCore.QByteArray()
                                                                                    buf = QtCore.QBuffer(ba)
                                                                                    buf.open(QtCore.QIODevice.WriteOnly)
                                                                                    pix.save(buf, 'PNG')
                                                                                    buf.close()
                                                                                    md2.setData('image/png', ba)
                                                                                except Exception:
                                                                                    pass
                                                                                if pth and os.path.exists(pth):
                                                                                    try:
                                                                                        md2.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(pth))])
                                                                                    except Exception:
                                                                                        pass
                                                                                QtWidgets.QApplication.clipboard().setMimeData(md2)
                                                                                self.log('已将预览图复制到剪贴板')
                                                                            except Exception:
                                                                                pass
                                                                        else:
                                                                            # copy file path
                                                                            if pth and os.path.exists(pth):
                                                                                try:
                                                                                    md2 = QtCore.QMimeData()
                                                                                    md2.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(pth))])
                                                                                    QtWidgets.QApplication.clipboard().setMimeData(md2)
                                                                                    self.log('已将预览文件复制到剪贴板 (可粘贴到资源管理器)')
                                                                                    return
                                                                                except Exception:
                                                                                    pass
                                                                                try:
                                                                                    QtWidgets.QApplication.clipboard().setText(os.path.abspath(pth))
                                                                                    self.log('已将预览文件路径复制到剪贴板')
                                                                                except Exception:
                                                                                    pass
                                                                            else:
                                                                                self.log('当前无可用文件以复制')
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_save:
                                                                    try:
                                                                        if not pth or not os.path.exists(pth):
                                                                            self.log('当前无可用文件以另存')
                                                                        else:
                                                                            dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存为', os.path.join(self.output_dir, os.path.basename(pth)))
                                                                            if dst:
                                                                                try:
                                                                                    shutil.copy2(pth, dst)
                                                                                    self.log(f'已另存为: {dst}')
                                                                                except Exception:
                                                                                    self.log('另存为失败')
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_compare:
                                                                    try:
                                                                        if pth and os.path.exists(pth):
                                                                            self._add_to_compare([pth])
                                                                        else:
                                                                            self.log('没有可加入比较的文件')
                                                                    except Exception:
                                                                        pass
                                                                elif action == act_image_reverse:
                                                                    try:
                                                                        if pth and os.path.exists(pth):
                                                                            try:
                                                                                self._start_image_reverse([pth])
                                                                            except Exception:
                                                                                pass
                                                                        else:
                                                                            try:
                                                                                self.log('当前无可用文件用于图像反推')
                                                                            except Exception:
                                                                                pass
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                        prev_label.customContextMenuRequested.connect(_on_prev_context)
                                                    except Exception:
                                                        pass

                                                    prev_layout.addWidget(prev_label)
                                                    field_body_layout.addWidget(prev_box)
                                                    try:
                                                        node_widgets[idx] = node_widgets.get(idx, {})
                                                    except Exception:
                                                        try:
                                                            node_widgets[idx] = {}
                                                        except Exception:
                                                            pass
                                                    try:
                                                        node_widgets[idx]['prev_label'] = prev_label
                                                    except Exception:
                                                        pass

                                                    def _load_preview_from_path(pth, lbl=prev_label, ftype_local=ftype):
                                                        try:
                                                            if not pth or not os.path.exists(pth):
                                                                _set_placeholder(lbl)
                                                                return
                                                            if ftype_local == 'IMAGE':
                                                                pix = QtGui.QPixmap(pth)
                                                            else:
                                                                # VIDEO: read first frame via cv2
                                                                pix = None
                                                                try:
                                                                    cap = cv2.VideoCapture(pth)
                                                                    ok, frame = cap.read()
                                                                    cap.release()
                                                                    if ok and frame is not None:
                                                                        # frame is BGR
                                                                        try:
                                                                            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                                                            h, w, ch = frame.shape
                                                                            bytes_per_line = ch * w
                                                                            qimg = QtGui.QImage(frame.data.tobytes(), w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
                                                                            pix = QtGui.QPixmap.fromImage(qimg)
                                                                        except Exception:
                                                                            pix = None
                                                                except Exception:
                                                                    pix = None
                                                            if pix is None or pix.isNull():
                                                                _set_placeholder(lbl)
                                                                return
                                                            # record that this label is showing content from a real file
                                                            try:
                                                                lbl._last_path = pth
                                                            except Exception:
                                                                pass
                                                            lbl._orig_pixmap = pix
                                                            try:
                                                                target_w = max(120, (lbl.width() - 8) or 200)
                                                                try:
                                                                    scaled = pix.scaled(target_w, 160, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                                                                except Exception:
                                                                    scaled = pix.scaled(target_w, int(target_w * pix.height() / max(1, pix.width())), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                                                                lbl.setPixmap(scaled)
                                                                try:
                                                                    lbl.setFixedHeight(scaled.height() + 12)
                                                                except Exception:
                                                                    pass
                                                            except Exception:
                                                                try:
                                                                    lbl.setPixmap(pix)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            try:
                                                                _set_placeholder(lbl)
                                                            except Exception:
                                                                pass

                                                    # attach this loader to the label so watcher-driven updates call the right one
                                                    try:
                                                        prev_label._load_preview = _load_preview_from_path
                                                    except Exception:
                                                        pass

                                                    # expose a drop handler on the label so the watcher can call it
                                                    try:
                                                        def _on_drop(pth, _le=le, _idx=idx, _lbl=prev_label):
                                                            try:
                                                                p = pth
                                                                if isinstance(p, str) and p.startswith('file://'):
                                                                    # strip file:// prefix
                                                                    p = p[len('file://'):]
                                                                p = os.path.abspath(p)
                                                            except Exception:
                                                                p = pth
                                                            try:
                                                                _le.setText(p)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                _persist_and_write(p, _idx)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                _lbl._load_preview(p)
                                                            except Exception:
                                                                pass
                                                        prev_label._on_drop_path = _on_drop
                                                    except Exception:
                                                        pass

                                                    # browse/editing wiring handled above when creating inputs

                                                    # if fval points to existing file, load it now
                                                    try:
                                                        if fval and isinstance(fval, str) and os.path.exists(fval):
                                                            _load_preview_from_path(fval)
                                                        else:
                                                            _set_placeholder(prev_label)
                                                    except Exception:
                                                        _set_placeholder(prev_label)
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass

                                    nodes_v.addStretch(1)
                                    nodes_root.setLayout(nodes_v)
                                    nodes_scroll.setWidget(nodes_root)
                                    # watch the app JSON file for external edits and refresh widgets accordingly
                                    try:
                                        watcher = QtCore.QFileSystemWatcher([fpath])
                                        try:
                                            app_page._rh_watcher = watcher
                                        except Exception:
                                            pass
                                        def _on_appfile_changed(path_changed):
                                            try:
                                                # reload on change
                                                if not os.path.exists(fpath):
                                                    return
                                                with open(fpath, 'rb') as _f:
                                                    raw2 = _f.read().decode('utf-8')
                                                parsed2 = json.loads(raw2) if raw2 else {}
                                            except Exception:
                                                return
                                            try:
                                                nodes2 = parsed2.get('nodeInfoList') if isinstance(parsed2, dict) else (parsed2 if isinstance(parsed2, list) else [])
                                                if isinstance(nodes2, dict):
                                                    nodes2 = [nodes2]
                                            except Exception:
                                                nodes2 = []
                                            try:
                                                # suspend persistence during watcher-driven updates
                                                try:
                                                    app_page._rh_suspending_persistence = True
                                                except Exception:
                                                    pass
                                                nw = getattr(app_page, '_rh_node_widgets', {}) or {}
                                                for i, n in enumerate(nodes2):
                                                    try:
                                                        val = n.get('fieldValue') if n.get('fieldValue') is not None else ''
                                                        entry = nw.get(i) or {}
                                                        try:
                                                            if 'le' in entry and entry.get('le') is not None:
                                                                try:
                                                                    entry.get('le').setText(str(val))
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if 'te' in entry and entry.get('te') is not None:
                                                                try:
                                                                    _te = entry.get('te')
                                                                    # only update the widget if content actually differs to avoid
                                                                    # clobbering the user's current edit/cursor position
                                                                    try:
                                                                        cur_text = _te.toPlainText() if hasattr(_te, 'toPlainText') else ''
                                                                    except Exception:
                                                                        cur_text = None
                                                                    try:
                                                                        new_text = str(val)
                                                                    except Exception:
                                                                        new_text = '' if val is None else str(val)
                                                                    if cur_text is None or cur_text != new_text:
                                                                        try:
                                                                            # attempt to preserve cursor position where sensible
                                                                            try:
                                                                                cursor = _te.textCursor()
                                                                                pos = cursor.position()
                                                                            except Exception:
                                                                                pos = None
                                                                            _te.setPlainText(new_text)
                                                                            try:
                                                                                if pos is not None:
                                                                                    # restore position up to new length
                                                                                    c2 = _te.textCursor()
                                                                                    new_len = len(new_text or '')
                                                                                    c2.setPosition(min(pos, new_len))
                                                                                    _te.setTextCursor(c2)
                                                                            except Exception:
                                                                                pass
                                                                        except Exception:
                                                                            pass
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if 'prev_label' in entry and entry.get('prev_label') is not None:
                                                                    try:
                                                                        pv = entry.get('prev_label')
                                                                        if isinstance(val, str) and os.path.exists(val):
                                                                            try:
                                                                                # prefer the per-label loader to avoid cross-node type overwrite
                                                                                loader = getattr(pv, '_load_preview', None)
                                                                                if callable(loader):
                                                                                    try:
                                                                                        loader(val)
                                                                                    except Exception:
                                                                                        pass
                                                                                else:
                                                                                    try:
                                                                                        _load_preview_from_path(val, lbl=pv)
                                                                                    except Exception:
                                                                                        pass
                                                                            except Exception:
                                                                                pass
                                                                        else:
                                                                            try:
                                                                                _set_placeholder(pv)
                                                                            except Exception:
                                                                                pass
                                                                    except Exception:
                                                                        pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if 'ds' in entry and entry.get('ds') is not None:
                                                                try:
                                                                    entry.get('ds').setExternalValue(val if val != '' else '0')
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if 'sb' in entry and entry.get('sb') is not None:
                                                                try:
                                                                    entry.get('sb').setExternalValue(val if val != '' else '0')
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if 'combo_bool' in entry and entry.get('combo_bool') is not None:
                                                                try:
                                                                    cb = entry.get('combo_bool')
                                                                    ci = 0 if str(val).lower() in ('true', '1', 'yes') else 1
                                                                    cb.setCurrentIndex(ci)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if 'combo' in entry and entry.get('combo') is not None:
                                                                try:
                                                                    c = entry.get('combo')
                                                                    # try to set to matching text
                                                                    idx_match = -1
                                                                    for ii in range(c.count()):
                                                                        try:
                                                                            if c.itemText(ii) == str(val):
                                                                                idx_match = ii
                                                                                break
                                                                        except Exception:
                                                                            pass
                                                                    if idx_match >= 0:
                                                                        c.setCurrentIndex(idx_match)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                            # re-enable persistence after performing updates
                                            try:
                                                app_page._rh_suspending_persistence = False
                                            except Exception:
                                                pass
                                            # some platforms remove the path from watcher on change events; ensure it's watched
                                            try:
                                                if fpath not in watcher.files():
                                                    watcher.addPath(fpath)
                                            except Exception:
                                                pass
                                        try:
                                            watcher.fileChanged.connect(_on_appfile_changed)
                                            try:
                                                app_page._rh_suspending_persistence = False
                                            except Exception:
                                                pass
                                        except Exception:
                                            try:
                                                app_page._rh_suspending_persistence = False
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    # split area: left = nodes list, right = output preview using a draggable QSplitter
                                    try:
                                        parameter_panel.setMinimumWidth(320)
                                        nodes_scroll.setMinimumWidth(0)
                                        nodes_scroll.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
                                    except Exception:
                                        pass

                                    try:
                                        preview_frame = QtWidgets.QFrame()
                                        preview_frame.setObjectName('rhResultPanel')
                                        preview_frame.setFrameShape(QtWidgets.QFrame.NoFrame)
                                        preview_layout = QtWidgets.QVBoxLayout(preview_frame)
                                        preview_layout.setContentsMargins(18, 18, 18, 18)
                                        preview_layout.setSpacing(14)
                                        preview_heading_row = QtWidgets.QHBoxLayout()
                                        preview_heading = QtWidgets.QLabel('生成结果')
                                        preview_heading.setObjectName('rhSectionTitle')
                                        preview_heading_row.addWidget(preview_heading)
                                        preview_heading_row.addStretch(1)
                                        preview_count_label = QtWidgets.QLabel('0 项')
                                        preview_count_label.setObjectName('rhMuted')
                                        preview_heading_row.addWidget(preview_count_label)
                                        preview_layout.addLayout(preview_heading_row)
                                        preview_hint = QtWidgets.QLabel('查看生成内容，双击预览可在本地打开。')
                                        preview_hint.setObjectName('rhMuted')
                                        preview_hint.setWordWrap(True)
                                        preview_layout.addWidget(preview_hint)
                                        from aetherloom_core.rh_output_groups import RhOutputGroups, OutputCard
                                        preview_stack = RhOutputGroups(app_page)
                                        app_page._rh_output_groups = preview_stack
                                        app_page._rh_result_count_label = preview_count_label
                                        _preview_cards = []
                                        app_page._rh_preview_cards = _preview_cards
                                        # OutputCard and its group own responsive preview geometry.

                                        def _reflow_preview_cards():
                                            # The shared bridge owns both current pages and their ordering.
                                            # Card callbacks must not move a card back into a global grid.
                                            callback = getattr(app_page, '_rh_shared_reflow', None)
                                            if callback is not None:
                                                callback()

                                        # Decode settings expand above the results without reducing preview width.
                                        preview_container = QtWidgets.QWidget()
                                        try:
                                            pc_h = QtWidgets.QVBoxLayout(preview_container)
                                            pc_h.setContentsMargins(0, 0, 0, 0)
                                            pc_h.setSpacing(12)
                                        except Exception:
                                            pc_h = QtWidgets.QVBoxLayout(preview_container)

                                        # right-side sidebar (hidden by default) with local-decode options per app
                                        sidebar_frame = QtWidgets.QFrame()
                                        try:
                                            sidebar_frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
                                            sidebar_layout = QtWidgets.QVBoxLayout(sidebar_frame)
                                            sidebar_layout.setContentsMargins(8, 8, 8, 8)
                                            sidebar_layout.setSpacing(6)
                                            decode_scope_hint = QtWidgets.QLabel('以下设置用于之后发起的任务。已提交和等候任务保留发起时的解码配置。')
                                            decode_scope_hint.setObjectName('rhMuted')
                                            decode_scope_hint.setWordWrap(True)
                                            sidebar_layout.addWidget(decode_scope_hint)
                                            # enable checkbox (created here but placed beside the toggle button)
                                            local_cb = QtWidgets.QCheckBox('启用本地解码')
                                            local_cb.setToolTip('用于之后发起的任务，不修改现有任务的解码配置')
                                            local_cb.setChecked(False)
                                            # decode mode selector (GRC/SST)
                                            mode_row = QtWidgets.QHBoxLayout()
                                            mode_row.addWidget(QtWidgets.QLabel('解码方式:'))
                                            local_mode_combo = QtWidgets.QComboBox()
                                            local_mode_combo.addItem('GRC', 'grc')
                                            local_mode_combo.addItem('SST', 'sst')
                                            local_mode_combo.setCurrentIndex(0)
                                            mode_row.addWidget(local_mode_combo)
                                            sidebar_layout.addLayout(mode_row)
                                            # password for SSTool
                                            pwd_row_widget = QtWidgets.QWidget()
                                            pwd_row = QtWidgets.QHBoxLayout(pwd_row_widget)
                                            pwd_row.setContentsMargins(0, 0, 0, 0)
                                            pwd_row.addWidget(QtWidgets.QLabel('密码:'))
                                            local_pwd_edit = QtWidgets.QLineEdit()
                                            local_pwd_edit.setEchoMode(QtWidgets.QLineEdit.Normal)
                                            local_pwd_edit.setPlaceholderText('无')
                                            pwd_row.addWidget(local_pwd_edit)
                                            sidebar_layout.addWidget(pwd_row_widget)
                                            # grid cols
                                            g_row_widget = QtWidgets.QWidget()
                                            g_row = QtWidgets.QHBoxLayout(g_row_widget)
                                            g_row.setContentsMargins(0, 0, 0, 0)
                                            g_row.addWidget(QtWidgets.QLabel('网格列数:'))
                                            local_grid = QtWidgets.QSpinBox()
                                            try:
                                                local_grid.setRange(4, 256)
                                                local_grid.setValue(int(getattr(self, 'grid_spin', None).value() if hasattr(self, 'grid_spin') else 32))
                                            except Exception:
                                                try:
                                                    local_grid.setValue(32)
                                                except Exception:
                                                    pass
                                            g_row.addWidget(local_grid)
                                            sidebar_layout.addWidget(g_row_widget)
                                            # delete original option
                                            delete_orig_cb = QtWidgets.QCheckBox('删除原图像')
                                            delete_orig_cb.setToolTip('解码完成后删除未解码的原输出文件')
                                            delete_orig_cb.setChecked(True)
                                            sidebar_layout.addWidget(delete_orig_cb)
                                            # open local decode folder button
                                            open_row = QtWidgets.QHBoxLayout()
                                            open_btn = QtWidgets.QPushButton('打开本地解码目录')
                                            try:
                                                open_btn.clicked.connect(lambda: self._reveal_in_explorer(self.local_decode_dir) if hasattr(self, '_reveal_in_explorer') else os.startfile(self.local_decode_dir))
                                            except Exception:
                                                try:
                                                    open_btn.clicked.connect(lambda: os.startfile(self.local_decode_dir))
                                                except Exception:
                                                    pass
                                            open_row.addStretch(1)
                                            open_row.addWidget(open_btn)
                                            sidebar_layout.addLayout(open_row)
                                            try:
                                                sidebar_frame.setMinimumWidth(0)
                                                sidebar_frame.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                            except Exception:
                                                pass
                                            # expose widgets on app_page for run-time access
                                            try:
                                                app_page._rh_local_decode_cb = local_cb
                                                app_page._rh_local_grid_spin = local_grid
                                                app_page._rh_local_mode_combo = local_mode_combo
                                                app_page._rh_local_pwd_edit = local_pwd_edit
                                                app_page._rh_local_delete_original_cb = delete_orig_cb
                                                # initial attrs
                                                app_page._rh_local_decode_enabled = bool(local_cb.isChecked())
                                                app_page._rh_local_grid_cols = int(local_grid.value())
                                                app_page._rh_local_decode_mode = local_mode_combo.currentData() or 'grc'
                                                app_page._rh_local_password = local_pwd_edit.text() or ''
                                                app_page._rh_local_delete_original = bool(delete_orig_cb.isChecked())
                                                app_page._rh_local_sidebar_visible = bool(sidebar_frame.isVisible())
                                                def _persist_local_decode_settings(ap=app_page, wid_local=wid):
                                                    try:
                                                        store = getattr(self, 'rh_local_decode_settings', None)
                                                    except Exception:
                                                        store = None
                                                    try:
                                                        if not isinstance(store, dict):
                                                            store = {}
                                                            try:
                                                                self.rh_local_decode_settings = store
                                                            except Exception:
                                                                pass
                                                        cfg = {
                                                            'enabled': bool(getattr(ap, '_rh_local_decode_enabled', False)),
                                                            'mode': str(getattr(ap, '_rh_local_decode_mode', 'grc') or 'grc'),
                                                            'password': str(getattr(ap, '_rh_local_password', '') or ''),
                                                            'grid_cols': int(getattr(ap, '_rh_local_grid_cols', 32) or 32),
                                                            'delete_original': bool(getattr(ap, '_rh_local_delete_original', True)),
                                                            'sidebar_visible': bool(getattr(ap, '_rh_local_sidebar_visible', False))
                                                        }
                                                        store[str(wid_local)] = cfg
                                                        try:
                                                            if isinstance(getattr(self, 'settings', None), dict):
                                                                self.settings['rh_local_decode_settings'] = store
                                                        except Exception:
                                                            pass
                                                        try:
                                                            self._save_settings()
                                                        except Exception:
                                                            pass
                                                    except Exception:
                                                        pass
                                                # wire signals to attributes
                                                try:
                                                    local_cb.toggled.connect(lambda v, ap=app_page: (setattr(ap, '_rh_local_decode_enabled', bool(v)), _persist_local_decode_settings(ap)))
                                                except Exception:
                                                    pass
                                                try:
                                                    local_grid.valueChanged.connect(lambda v, ap=app_page: (setattr(ap, '_rh_local_grid_cols', int(v)), _persist_local_decode_settings(ap)))
                                                except Exception:
                                                    pass
                                                try:
                                                    delete_orig_cb.toggled.connect(lambda v, ap=app_page: (setattr(ap, '_rh_local_delete_original', bool(v)), _persist_local_decode_settings(ap)))
                                                except Exception:
                                                    pass
                                                try:
                                                    local_pwd_edit.textChanged.connect(lambda v, ap=app_page: (setattr(ap, '_rh_local_password', v), _persist_local_decode_settings(ap)))
                                                except Exception:
                                                    pass
                                                try:
                                                    def _update_local_grid_visibility(mode=None, w=g_row_widget):
                                                        try:
                                                            m = mode or (local_mode_combo.currentData() or 'grc')
                                                            w.setVisible(m == 'grc')
                                                            try:
                                                                pwd_row_widget.setVisible(m == 'sst')
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            try:
                                                                w.setVisible(True)
                                                            except Exception:
                                                                pass
                                                    local_mode_combo.currentIndexChanged.connect(lambda _idx, ap=app_page: (setattr(ap, '_rh_local_decode_mode', local_mode_combo.currentData() or 'grc'), _update_local_grid_visibility(), _persist_local_decode_settings(ap)))
                                                    _update_local_grid_visibility(local_mode_combo.currentData())
                                                except Exception:
                                                    pass
                                                try:
                                                    # hydrate from persisted settings if available
                                                    persisted = None
                                                    try:
                                                        if isinstance(getattr(self, 'rh_local_decode_settings', None), dict):
                                                            persisted = self.rh_local_decode_settings.get(str(wid)) or self.rh_local_decode_settings.get(wid)
                                                    except Exception:
                                                        persisted = None
                                                    if isinstance(persisted, dict):
                                                        try:
                                                            local_cb.blockSignals(True)
                                                            local_cb.setChecked(bool(persisted.get('enabled', local_cb.isChecked())))
                                                            local_cb.blockSignals(False)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            mode_val = str(persisted.get('mode', local_mode_combo.currentData() or 'grc') or 'grc')
                                                            idx_mode = local_mode_combo.findData(mode_val)
                                                            if idx_mode < 0:
                                                                idx_mode = 0
                                                            local_mode_combo.blockSignals(True)
                                                            local_mode_combo.setCurrentIndex(idx_mode)
                                                            local_mode_combo.blockSignals(False)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            local_pwd_edit.blockSignals(True)
                                                            local_pwd_edit.setText(persisted.get('password', local_pwd_edit.text()) or '')
                                                            local_pwd_edit.blockSignals(False)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            val_cols = int(persisted.get('grid_cols', local_grid.value()) or local_grid.value())
                                                            local_grid.blockSignals(True)
                                                            local_grid.setValue(val_cols)
                                                            local_grid.blockSignals(False)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            delete_orig_cb.blockSignals(True)
                                                            delete_orig_cb.setChecked(bool(persisted.get('delete_original', delete_orig_cb.isChecked())))
                                                            delete_orig_cb.blockSignals(False)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            app_page._rh_local_decode_enabled = bool(local_cb.isChecked())
                                                            app_page._rh_local_decode_mode = str(mode_val if 'mode_val' in locals() else (local_mode_combo.currentData() or 'grc') or 'grc')
                                                            app_page._rh_local_password = str(local_pwd_edit.text() or '')
                                                            app_page._rh_local_grid_cols = int(local_grid.value())
                                                            app_page._rh_local_delete_original = bool(delete_orig_cb.isChecked())
                                                        except Exception:
                                                            pass
                                                        try:
                                                            # restore sidebar visibility state too
                                                            vis = bool(persisted.get('sidebar_visible', False))
                                                            try:
                                                                sidebar_frame.setVisible(vis)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                app_page._rh_local_sidebar_visible = vis
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            _update_local_grid_visibility(local_mode_combo.currentData())
                                                        except Exception:
                                                            pass
                                                        try:
                                                            _persist_local_decode_settings(app_page)
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass
                                        except Exception:
                                            sidebar_frame = QtWidgets.QFrame()

                                        # hide sidebar by default
                                        try:
                                            sidebar_frame.setVisible(False)
                                        except Exception:
                                            pass

                                        # small toggle button above previews to show/hide sidebar
                                        try:
                                            toggle_row = QtWidgets.QHBoxLayout()
                                            toggle_row.setContentsMargins(0, 0, 0, 0)
                                            toggle_row.setSpacing(6)
                                            toggle_btn = QtWidgets.QToolButton()
                                            toggle_btn.setText('本地解码设置')
                                            toggle_btn.setObjectName('rhSecondaryButton')
                                            toggle_btn.setMinimumHeight(32)
                                            toggle_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                            toggle_btn.setToolTip('展开之后发起任务使用的本地解码选项')
                                            app_page._rh_decode_toggle = toggle_btn
                                            app_page._rh_decode_panel = sidebar_frame
                                            toggle_btn.setCheckable(True)
                                            toggle_btn.setChecked(False)
                                            def _toggle_sidebar(checked):
                                                try:
                                                    sidebar_frame.setVisible(bool(checked))
                                                    try:
                                                        # remember per-page state
                                                        app_page._rh_local_sidebar_visible = bool(checked)
                                                    except Exception:
                                                        pass
                                                    try:
                                                        _persist_local_decode_settings(app_page)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass
                                            try:
                                                toggle_btn.toggled.connect(_toggle_sidebar)
                                            except Exception:
                                                pass
                                            try:
                                                toggle_btn.blockSignals(True)
                                                init_vis = bool(getattr(app_page, '_rh_local_sidebar_visible', False))
                                                toggle_btn.setChecked(init_vis)
                                                sidebar_frame.setVisible(init_vis)
                                                toggle_btn.blockSignals(False)
                                            except Exception:
                                                pass
                                            toggle_row.addStretch(1)
                                            # place the local decode enable checkbox to the left of the toggle
                                            try:
                                                toggle_row.addWidget(local_cb)
                                            except Exception:
                                                pass
                                            toggle_row.addWidget(toggle_btn)
                                            preview_layout.addLayout(toggle_row)
                                        except Exception:
                                            pass

                                        # add the preview scroll area and the sidebar into the container
                                        try:
                                            pc_h.addWidget(sidebar_frame)
                                            pc_h.addWidget(preview_stack, 1)
                                        except Exception:
                                            try:
                                                pc_h.addWidget(preview_stack)
                                            except Exception:
                                                preview_layout.addWidget(preview_stack, 1)

                                        preview_layout.addWidget(preview_container, 1)

                                        # OutputCard scales cached previews after layout changes.

                                        # render preview content for a card (does not modify history)
                                        def _render_preview_content(card, rpath, rtitle=None):
                                            try:
                                                card._preview_size_key = None
                                                lbl = getattr(card, '_img_label', None)
                                                if isinstance(lbl, QtWidgets.QLabel):
                                                    lbl._orig_pixmap = None
                                                # detect file type early so we can switch widget type when needed
                                                try:
                                                    ext = os.path.splitext(rpath)[1].lower() if rpath else ''
                                                except Exception:
                                                    ext = ''
                                                TEXT_EXTS = ('.txt', '.md', '.log', '.json', '.csv')
                                                is_text_ext = ext in TEXT_EXTS
                                                # if previous preview was text but new content is media, replace the widget with a label
                                                try:
                                                    if not is_text_ext and isinstance(lbl, QtWidgets.QTextEdit):
                                                        parent_layout0 = card.layout() if card is not None else None
                                                        insert_at0 = -1
                                                        if parent_layout0 is not None:
                                                            for ii0 in range(parent_layout0.count()):
                                                                try:
                                                                    itw0 = parent_layout0.itemAt(ii0)
                                                                    if itw0 is None:
                                                                        continue
                                                                    wgt0 = itw0.widget()
                                                                    if wgt0 is lbl:
                                                                        insert_at0 = ii0
                                                                        break
                                                                except Exception:
                                                                    pass
                                                        new_lbl0 = QtWidgets.QLabel(os.path.basename(rpath) if rpath else '')
                                                        try:
                                                            new_lbl0.setAlignment(Qt.AlignCenter)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            new_lbl0.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                            new_lbl0.setMinimumWidth(0)
                                                            new_lbl0.setMinimumHeight(30)
                                                        except Exception:
                                                            pass
                                                        try:
                                                            if insert_at0 >= 0 and parent_layout0 is not None:
                                                                parent_layout0.removeWidget(lbl)
                                                                lbl.setParent(None)
                                                                lbl.deleteLater()
                                                                parent_layout0.insertWidget(insert_at0, new_lbl0)
                                                            elif parent_layout0 is not None:
                                                                parent_layout0.addWidget(new_lbl0)
                                                        except Exception:
                                                            pass
                                                        lbl = new_lbl0
                                                        try:
                                                            card._img_label = lbl
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                try:
                                                    if lbl is not None and rpath:
                                                        try:
                                                            lbl._last_path = rpath
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                if lbl is not None and rpath and os.path.exists(rpath):
                                                    pix = None
                                                    try:
                                                        _pix = QtGui.QPixmap(rpath) if not is_text_ext else None
                                                        if _pix is not None and not _pix.isNull():
                                                            pix = _pix
                                                    except Exception:
                                                        pix = None
                                                    try:
                                                        is_text_widget = is_text_ext or (getattr(lbl, '_is_text_preview', False) if is_text_ext else False)
                                                    except Exception:
                                                        is_text_widget = is_text_ext
                                                    if is_text_widget:
                                                        try:
                                                            txt = ''
                                                            try:
                                                                with open(rpath, 'r', encoding='utf-8') as rf:
                                                                    txt = rf.read()
                                                            except Exception:
                                                                try:
                                                                    with open(rpath, 'r', encoding='latin-1') as rf:
                                                                        txt = rf.read()
                                                                except Exception:
                                                                    txt = os.path.basename(rpath)
                                                            try:
                                                                lbl.setPlainText(txt)
                                                            except Exception:
                                                                try:
                                                                    lbl.setText(txt)
                                                                except Exception:
                                                                    pass
                                                            try:
                                                                lbl._last_path = rpath
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if card is not None:
                                                                    card._collapsed = False
                                                            except Exception:
                                                                pass
                                                            try:
                                                                lbl.show()
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                        pix = None
                                                    if (pix is None or pix.isNull()):
                                                        try:
                                                            ext = os.path.splitext(rpath)[1].lower()
                                                            if ext in ('.mp4', '.mov', '.mkv', '.webm', '.avi', '.flv'):
                                                                try:
                                                                    import cv2 as _cv2
                                                                    cap = _cv2.VideoCapture(rpath)
                                                                    ok, frame = cap.read()
                                                                    cap.release()
                                                                    if ok and frame is not None:
                                                                        try:
                                                                            frame = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
                                                                            h, w, ch = frame.shape
                                                                            bytes_per_line = ch * w
                                                                            qimg = QtGui.QImage(frame.data.tobytes(), w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
                                                                            pix = QtGui.QPixmap.fromImage(qimg)
                                                                        except Exception:
                                                                            pix = None
                                                                except Exception:
                                                                    pix = None
                                                        except Exception:
                                                            pix = None
                                                    try:
                                                        try:
                                                            ext = os.path.splitext(rpath)[1].lower()
                                                        except Exception:
                                                            ext = ''
                                                        TEXT_EXTS = ('.txt', '.md', '.log', '.json', '.csv')
                                                        if ext in TEXT_EXTS:
                                                            content = ''
                                                            try:
                                                                with open(rpath, 'r', encoding='utf-8') as rf:
                                                                    content = rf.read()
                                                            except Exception:
                                                                try:
                                                                    with open(rpath, 'r', encoding='latin-1') as rf:
                                                                        content = rf.read()
                                                                except Exception:
                                                                    content = os.path.basename(rpath)
                                                            try:
                                                                if not isinstance(lbl, QtWidgets.QTextEdit):
                                                                    parent_layout = card.layout() if card is not None else None
                                                                    insert_at = -1
                                                                    if parent_layout is not None:
                                                                        for ii in range(parent_layout.count()):
                                                                            try:
                                                                                itw = parent_layout.itemAt(ii)
                                                                                if itw is None:
                                                                                    continue
                                                                                wgt = itw.widget()
                                                                                if wgt is lbl:
                                                                                    insert_at = ii
                                                                                    break
                                                                            except Exception:
                                                                                pass
                                                                    new_txt = QtWidgets.QTextEdit()
                                                                    try:
                                                                        new_txt.setReadOnly(True)
                                                                        new_txt.setAcceptRichText(False)
                                                                        new_txt.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                                        new_txt.setMinimumWidth(0)
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        new_txt.setPlainText(content)
                                                                    except Exception:
                                                                        try:
                                                                            new_txt.setText(content)
                                                                        except Exception:
                                                                            pass
                                                                        try:
                                                                            try:
                                                                                lines2 = max(1, content.count('\n') + 1)
                                                                                fm2 = new_txt.fontMetrics()
                                                                                lh2 = fm2.lineSpacing() or fm2.height()
                                                                                new_h2 = int(min(max(80, lines2 * lh2 + 12), 800))
                                                                                new_txt.setMinimumHeight(new_h2)
                                                                                new_txt.setMaximumHeight(max(new_h2, 400))
                                                                                try:
                                                                                    new_txt.setFixedHeight(new_h2)
                                                                                except Exception:
                                                                                    pass
                                                                            except Exception:
                                                                                new_txt.setMinimumHeight(120)
                                                                        except Exception:
                                                                            pass
                                                                    try:
                                                                        if insert_at >= 0 and parent_layout is not None:
                                                                            parent_layout.removeWidget(lbl)
                                                                            lbl.setParent(None)
                                                                            lbl.deleteLater()
                                                                            parent_layout.insertWidget(insert_at, new_txt)
                                                                        else:
                                                                            try:
                                                                                lbl.hide()
                                                                            except Exception:
                                                                                pass
                                                                            if parent_layout is not None:
                                                                                parent_layout.addWidget(new_txt)
                                                                    except Exception:
                                                                        pass
                                                                    lbl = new_txt
                                                                    try:
                                                                        card._img_label = lbl
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                                        lbl.setMinimumWidth(0)
                                                                    except Exception:
                                                                        pass
                                                                else:
                                                                    try:
                                                                        lbl.setPlainText(content)
                                                                    except Exception:
                                                                        try:
                                                                            lbl.setText(content)
                                                                        except Exception:
                                                                            pass
                                                            except Exception:
                                                                pass
                                                            try:
                                                                lbl._last_path = rpath
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if card is not None:
                                                                    card._collapsed = False
                                                            except Exception:
                                                                pass
                                                            try:
                                                                lbl.show()
                                                            except Exception:
                                                                pass
                                                            pix = None
                                                    except Exception:
                                                        pass

                                                    try:
                                                        if (pix is None or (hasattr(pix, 'isNull') and pix.isNull())):
                                                            try:
                                                                ext2 = os.path.splitext(rpath)[1].lower()
                                                            except Exception:
                                                                ext2 = ''
                                                            if ext2 not in TEXT_EXTS:
                                                                try:
                                                                    if isinstance(lbl, QtWidgets.QTextEdit):
                                                                        parent_layout2 = card.layout() if card is not None else None
                                                                        insert_at2 = -1
                                                                        if parent_layout2 is not None:
                                                                            for ii2 in range(parent_layout2.count()):
                                                                                try:
                                                                                    itw2 = parent_layout2.itemAt(ii2)
                                                                                    if itw2 is None:
                                                                                        continue
                                                                                    wgt2 = itw2.widget()
                                                                                    if wgt2 is lbl:
                                                                                        insert_at2 = ii2
                                                                                        break
                                                                                except Exception:
                                                                                    pass
                                                                        new_lbl2 = QtWidgets.QLabel(os.path.basename(rpath) if rpath else '')
                                                                        try:
                                                                            new_lbl2.setAlignment(Qt.AlignCenter)
                                                                        except Exception:
                                                                            pass
                                                                        try:
                                                                            new_lbl2.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                                            new_lbl2.setMinimumWidth(0)
                                                                            new_lbl2.setMinimumHeight(30)
                                                                        except Exception:
                                                                            pass
                                                                        if insert_at2 >= 0 and parent_layout2 is not None:
                                                                            try:
                                                                                parent_layout2.removeWidget(lbl)
                                                                                lbl.setParent(None)
                                                                                lbl.deleteLater()
                                                                            except Exception:
                                                                                pass
                                                                            try:
                                                                                parent_layout2.insertWidget(insert_at2, new_lbl2)
                                                                            except Exception:
                                                                                parent_layout2.addWidget(new_lbl2)
                                                                        else:
                                                                            try:
                                                                                lbl.hide()
                                                                            except Exception:
                                                                                pass
                                                                            if parent_layout2 is not None:
                                                                                try:
                                                                                    parent_layout2.addWidget(new_lbl2)
                                                                                except Exception:
                                                                                    pass
                                                                        lbl = new_lbl2
                                                                        try:
                                                                            card._img_label = lbl
                                                                        except Exception:
                                                                            pass
                                                                    else:
                                                                        try:
                                                                            lbl.setText(os.path.basename(rpath) if rpath else '')
                                                                        except Exception:
                                                                            pass
                                                                    try:
                                                                        lbl._last_path = rpath
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        if card is not None:
                                                                            card._collapsed = False
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        lbl.show()
                                                                    except Exception:
                                                                        pass
                                                                except Exception:
                                                                    pass
                                                    except Exception:
                                                        pass

                                                    if pix is not None and not pix.isNull():
                                                        lbl._orig_pixmap = pix
                                                        try:
                                                            lbl._last_path = rpath
                                                        except Exception:
                                                            pass
                                                        try:
                                                            try:
                                                                if card is not None:
                                                                    try:
                                                                        card._collapsed = False
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if getattr(card, '_img_label', None) is not None:
                                                                    try:
                                                                        card._img_label.show()
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if getattr(card, '_timer_label', None) is not None:
                                                                    try:
                                                                        card._timer_label.show()
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            target_w = max(1, card.preview_width() - 8)
                                                            try:
                                                                scaled = pix.scaledToWidth(target_w, QtCore.Qt.SmoothTransformation)
                                                            except Exception:
                                                                scaled = pix.scaled(target_w, int(target_w * pix.height() / max(1, pix.width())), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                                                            lbl.setPixmap(scaled)
                                                            try:
                                                                lbl.setFixedHeight(scaled.height() + 12)
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            try:
                                                                lbl.setPixmap(pix)
                                                                try:
                                                                    lbl._last_path = rpath
                                                                except Exception:
                                                                    pass
                                                            except Exception:
                                                                pass
                                                if rtitle:
                                                    t = getattr(card, '_title_label', None)
                                                    if t is None:
                                                        try:
                                                            t = QtWidgets.QLabel(rtitle)
                                                            t.setStyleSheet('font-weight:700;')
                                                            card.layout().insertWidget(0, t)
                                                            card._title_label = t
                                                        except Exception:
                                                            pass
                                                    else:
                                                        try:
                                                            t.setText(rtitle)
                                                        except Exception:
                                                            pass
                                            except Exception:
                                                pass

                                            finally:
                                                if card is not None:
                                                    card.refresh_preview()

                                        def _refresh_nav(card):
                                            try:
                                                wrap = getattr(card, '_nav_wrap', None)
                                                outputs = getattr(card, '_outputs', []) or []
                                                if wrap is not None:
                                                    wrap.setVisible(len(outputs) > 1)
                                            except Exception:
                                                pass

                                        def _switch_output(card, delta):
                                            try:
                                                outputs = getattr(card, '_outputs', []) or []
                                                if len(outputs) <= 1:
                                                    return
                                                try:
                                                    idx = int(getattr(card, '_output_idx', len(outputs) - 1) or 0)
                                                except Exception:
                                                    idx = 0
                                                try:
                                                
                                                    idx = (idx + delta) % len(outputs)
                                                except Exception:
                                                    idx = max(0, min(len(outputs) - 1, idx + delta))
                                                try:
                                                    card._output_idx = idx
                                                except Exception:
                                                    pass
                                                entry = outputs[idx] if idx < len(outputs) else None
                                                if entry:
                                                    _render_preview_content(card, entry.get('path'), entry.get('title'))
                                                _refresh_nav(card)
                                            except Exception:
                                                pass

                                        def _ensure_nav(card):
                                            try:
                                                if getattr(card, '_nav_wrap', None) is not None:
                                                    return
                                                nav_wrap = QtWidgets.QWidget()
                                                nav_layout = QtWidgets.QHBoxLayout(nav_wrap)
                                                nav_layout.setContentsMargins(0, 0, 0, 0)
                                                nav_layout.setSpacing(4)
                                                nav_layout.addStretch(1)
                                                btn_prev = QtWidgets.QToolButton()
                                                try:
                                                    btn_prev.setAutoRaise(True)
                                                    btn_prev.setText('←')
                                                    btn_prev.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                    btn_prev.setToolTip('上一输出')
                                                except Exception:
                                                    pass
                                                btn_next = QtWidgets.QToolButton()
                                                try:
                                                    btn_next.setAutoRaise(True)
                                                    btn_next.setText('→')
                                                    btn_next.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                    btn_next.setToolTip('下一输出')
                                                except Exception:
                                                    pass
                                                try:
                                                    btn_prev.clicked.connect(lambda *_: _switch_output(card, -1))
                                                except Exception:
                                                    pass
                                                try:
                                                    btn_next.clicked.connect(lambda *_: _switch_output(card, 1))
                                                except Exception:
                                                    pass
                                                nav_layout.addWidget(btn_prev)
                                                nav_layout.addWidget(btn_next)
                                                nav_wrap.setVisible(False)
                                                try:
                                                    card._nav_wrap = nav_wrap
                                                    card._nav_prev = btn_prev
                                                    card._nav_next = btn_next
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass

                                        # helper to add a preview card into the scroll area (must be called from main thread)
                                        def _remove_result_card(card):
                                            if card in _preview_cards:
                                                _preview_cards.remove(card)
                                            card.hide()
                                            card.setParent(None)
                                            card.deleteLater()
                                            _reflow_preview_cards()

                                        def _add_preview_card(path, title=None):
                                            try:
                                                card = OutputCard()
                                                card.setFrameShape(QtWidgets.QFrame.StyledPanel)
                                                try:
                                                    # default to collapsed for cards until a preview path is set
                                                    card._collapsed = True
                                                except Exception:
                                                    pass
                                                try:
                                                    card.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                    card.setMinimumWidth(0)
                                                except Exception:
                                                    pass
                                                card.setObjectName('nodePreviewCard')
                                                card_layout = QtWidgets.QVBoxLayout(card)
                                                card_layout.setContentsMargins(12, 8, 12, 12)
                                                card_layout.setSpacing(6)

                                                # create a preview widget: image label or text view for textual files
                                                try:
                                                    ext = (os.path.splitext(path)[1] or '').lower() if path else ''
                                                except Exception:
                                                    ext = ''
                                                TEXT_EXTS = ('.txt', '.md', '.log', '.json', '.csv')
                                                if path and ext in TEXT_EXTS:
                                                    # use a read-only text edit for textual previews
                                                    lbl = QtWidgets.QTextEdit()
                                                    lbl.setReadOnly(True)
                                                    lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                    lbl.setMinimumWidth(0)
                                                    try:
                                                        lbl.setAcceptRichText(False)
                                                    except Exception:
                                                        pass
                                                    try:
                                                        content = ''
                                                        with open(path, 'r', encoding='utf-8') as rf:
                                                            content = rf.read()
                                                    except Exception:
                                                        try:
                                                            with open(path, 'r', encoding='latin-1') as rf:
                                                                content = rf.read()
                                                        except Exception:
                                                            content = os.path.basename(path) if path else ''
                                                    try:
                                                        lbl.setPlainText(content)
                                                    except Exception:
                                                        try:
                                                            lbl.setText(content)
                                                        except Exception:
                                                            pass
                                                    try:
                                                        # auto-size height based on content (similar to left-side text cards)
                                                        try:
                                                            lines = max(1, content.count('\n') + 1)
                                                            fm = lbl.fontMetrics()
                                                            line_h = fm.lineSpacing() or fm.height()
                                                            new_h = int(min(max(80, lines * line_h + 12), 800))
                                                            lbl.setMinimumHeight(new_h)
                                                            lbl.setMaximumHeight(max(new_h, 400))
                                                            try:
                                                                lbl.setFixedHeight(new_h)
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            lbl.setMinimumHeight(120)
                                                    except Exception:
                                                        pass
                                                    # mark as text preview for handlers
                                                    try:
                                                        lbl._is_text_preview = True
                                                    except Exception:
                                                        pass
                                                else:
                                                    lbl = QtWidgets.QLabel()
                                                    lbl.setAlignment(Qt.AlignCenter)
                                                    lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                                    lbl.setMinimumWidth(0)
                                                    lbl.setMinimumHeight(30)
                                                    # ensure label will scale to right-side container width
                                                    try:
                                                        lbl.setMinimumWidth(30)
                                                    except Exception:
                                                        pass
                                                    try:
                                                        pix = QtGui.QPixmap(path)
                                                        if not pix.isNull():
                                                            lbl._orig_pixmap = pix
                                                            try:
                                                                # Initial preview; OutputCard fits it after placement.
                                                                target_w = max(1, card.preview_width() - 8)
                                                                scaled = pix.scaledToWidth(target_w, QtCore.Qt.SmoothTransformation)
                                                                lbl.setPixmap(scaled)
                                                                try:
                                                                    lbl.setFixedHeight(scaled.height() + 12)
                                                                except Exception:
                                                                    pass
                                                            except Exception:
                                                                lbl.setPixmap(pix)
                                                                try:
                                                                    lbl.setFixedHeight(pix.height() + 12)
                                                                except Exception:
                                                                    pass
                                                    except Exception:
                                                        # ensure non-image files at least show filename
                                                        try:
                                                            lbl.setText(os.path.basename(path) if path else '')
                                                            try:
                                                                lbl._last_path = path
                                                            except Exception:
                                                                pass
                                                            try:
                                                                card._collapsed = False
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass

                                                # Keep the result title distinct from timing and actions.
                                                title_lbl = QtWidgets.QLabel('')
                                                try:
                                                    if title:
                                                        title_lbl.setText(title)
                                                except Exception:
                                                    pass
                                                try:
                                                    title_lbl.setObjectName('rhResultTitle')
                                                    title_lbl.setTextFormat(Qt.PlainText)
                                                    title_lbl.setWordWrap(True)
                                                    title_lbl.setMinimumWidth(0)
                                                    title_lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
                                                except Exception:
                                                    pass
                                                
                                                hdr_w = QtWidgets.QWidget()
                                                try:
                                                    hdr_layout = QtWidgets.QHBoxLayout(hdr_w)
                                                    hdr_layout.setContentsMargins(0, 0, 0, 0)
                                                except Exception:
                                                    hdr_layout = QtWidgets.QHBoxLayout(hdr_w)
                                                hdr_layout.addWidget(title_lbl)
                                                timer_lbl = QtWidgets.QLabel('等待结果' if not path else '')
                                                try:
                                                    timer_lbl.setObjectName('rhMuted')
                                                    timer_lbl.setToolTip('任务运行耗时')
                                                except Exception:
                                                    pass
                                                # make the entire header clickable to toggle collapse/expand
                                                try:
                                                    hdr_w.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                    def _toggle_card_header(e, _card=card):
                                                        try:
                                                            if not getattr(_card, '_rh_results_presented', True):
                                                                e.accept()
                                                                return
                                                            # only toggle on left mouse button to avoid interfering with right-click context menu
                                                            try:
                                                                if hasattr(e, 'button') and e.button() != Qt.LeftButton:
                                                                    return
                                                            except Exception:
                                                                pass
                                                            collapsed = getattr(_card, '_collapsed', False)
                                                            img_lbl = getattr(_card, '_img_label', None)
                                                            timer = getattr(_card, '_timer_label', None)
                                                            if collapsed:
                                                                try:
                                                                    if img_lbl is not None:
                                                                        img_lbl.show()
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    if timer is not None:
                                                                        timer.show()
                                                                except Exception:
                                                                    pass
                                                                _card._collapsed = False
                                                            else:
                                                                try:
                                                                    if img_lbl is not None:
                                                                        img_lbl.hide()
                                                                except Exception:
                                                                    pass
                                                                # do not hide timer when collapsing; timer remains visible
                                                                try:
                                                                    if timer is not None:
                                                                        timer.show()
                                                                except Exception:
                                                                    pass
                                                                _card._collapsed = True
                                                        except Exception:
                                                            pass
                                                        try:
                                                            e.accept()
                                                        except Exception:
                                                            pass
                                                    hdr_w.mousePressEvent = _toggle_card_header
                                                except Exception:
                                                    pass
                                                card_layout.addWidget(hdr_w)

                                                card_layout.addWidget(lbl)
                                                card_meta_row = QtWidgets.QHBoxLayout()
                                                card_meta_row.setContentsMargins(0, 0, 0, 0)
                                                card_meta_row.addWidget(timer_lbl)
                                                card_meta_row.addStretch(1)
                                                card_actions_btn = QtWidgets.QToolButton()
                                                card_actions_btn.setText('操作 ···')
                                                card_actions_btn.setObjectName('rhToolButton')
                                                card_actions_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                card_actions_btn.setToolTip('取消运行中的任务，或打开、保存与删除结果')
                                                card_meta_row.addWidget(card_actions_btn)
                                                card_layout.addLayout(card_meta_row)
                                                card._rh_actions_button = card_actions_btn

                                                # initialize output history and attach navigation controls
                                                try:
                                                    card._outputs = []
                                                    card._output_idx = -1
                                                except Exception:
                                                    pass
                                                try:
                                                    _ensure_nav(card)
                                                    if getattr(card, '_nav_wrap', None) is not None:
                                                        try:
                                                            card_layout.addWidget(card._nav_wrap, 0, Qt.AlignRight)
                                                        except Exception:
                                                            card_layout.addWidget(card._nav_wrap)
                                                except Exception:
                                                    pass

                                                # keep references on card for later updates
                                                try:
                                                    card._img_label = lbl
                                                except Exception:
                                                    card._img_label = None
                                                try:
                                                    card._title_label = title_lbl
                                                except Exception:
                                                    card._title_label = None
                                                try:
                                                    card._timer_label = timer_lbl
                                                except Exception:
                                                    card._timer_label = None

                                                # if no preview yet, keep in collapsed state (but show timer)
                                                try:
                                                    if getattr(card, '_collapsed', False):
                                                        try:
                                                            lbl.hide()
                                                        except Exception:
                                                            pass
                                                        try:
                                                            timer_lbl.show()
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                # double-click on the preview label opens the file in default app
                                                try:
                                                    def _on_preview_dbl(e, _lbl=lbl, _pth=path):
                                                        try:
                                                            try:
                                                                p = getattr(_lbl, '_last_path', None) or _pth
                                                            except Exception:
                                                                p = _pth
                                                            if not p:
                                                                return
                                                            if sys.platform.startswith('win'):
                                                                os.startfile(p)
                                                            elif sys.platform == 'darwin':
                                                                subprocess.Popen(['open', p])
                                                            else:
                                                                subprocess.Popen(['xdg-open', p])
                                                        except Exception:
                                                            pass
                                                        try:
                                                            e.accept()
                                                        except Exception:
                                                            pass
                                                    lbl.mouseDoubleClickEvent = _on_preview_dbl
                                                except Exception:
                                                    pass

                                                # store last path on label (may be set later by _update_preview_card)
                                                try:
                                                    lbl._last_path = None
                                                except Exception:
                                                    pass

                                                # context menu on preview card: Cancel task when unfinished, otherwise file actions
                                                try:
                                                    def _on_card_context(pos, _card=card):
                                                        try:
                                                            tid = getattr(_card, '_task_id', None)
                                                            # determine status from persistent map if available
                                                            try:
                                                                entries = getattr(self, '_rh_status_entries', {}) or {}
                                                            except Exception:
                                                                entries = {}
                                                            status = None
                                                            if tid:
                                                                try:
                                                                    status = entries.get(str(tid))
                                                                except Exception:
                                                                    status = None

                                                            # determine whether the card currently has a preview file
                                                            lbl = getattr(_card, '_img_label', None)
                                                            pth = None
                                                            try:
                                                                if lbl is not None:
                                                                    pth = getattr(lbl, '_last_path', None)
                                                            except Exception:
                                                                pth = None

                                                            menu = QtWidgets.QMenu(_card)
                                                            menu.setToolTipsVisible(True)
                                                            task_details = getattr(_card, '_rh_show_task_details', None)
                                                            if task_details is not None:
                                                                act_task_details = menu.addAction('查看本次任务参数')
                                                                act_task_details.triggered.connect(lambda _checked=False: task_details())
                                                                menu.addSeparator()
                                                            # Running tasks retain parameter inspection and cancellation.
                                                            try:
                                                                is_running = False
                                                                try:
                                                                    if tid:
                                                                        if hasattr(self, '_rh_running_tasks') and isinstance(getattr(self, '_rh_running_tasks', None), dict):
                                                                            for _wid, _set in (self._rh_running_tasks.items() if isinstance(self._rh_running_tasks, dict) else []):
                                                                                try:
                                                                                    if tid in (_set or set()):
                                                                                        is_running = True
                                                                                        break
                                                                                except Exception:
                                                                                    pass
                                                                except Exception:
                                                                    is_running = False
                                                                # also treat cards that are currently queued for retry as running
                                                                try:
                                                                    if not is_running:
                                                                        q = getattr(self, '_rh_retry_queue', None) or []
                                                                        for qi in (q if isinstance(q, (list, tuple)) else []):
                                                                            try:
                                                                                if isinstance(qi, dict) and qi.get('card') is _card:
                                                                                    is_running = True
                                                                                    break
                                                                            except Exception:
                                                                                pass
                                                                except Exception:
                                                                    pass
                                                            except Exception:
                                                                is_running = False
                                                            from aetherloom_core.rh_result_actions import associated_paths, card_is_active, delete_card_files, plan_card_deletion
                                                            is_running = is_running or card_is_active(self, _card)
                                                            if is_running:
                                                                cancel_pending = bool(getattr(_card, '_rh_cancel_pending', False))
                                                                act_cancel = menu.addAction('正在取消…' if cancel_pending else '取消任务')
                                                                act_cancel.setEnabled(not cancel_pending)
                                                            else:
                                                                # build actions (delete always available)
                                                                act_open = menu.addAction('在默认应用中打开')
                                                                act_open_folder = menu.addAction('在本地文件夹中打开')
                                                                act_copy = menu.addAction('复制到剪贴板')
                                                                act_save = menu.addAction('另存为')
                                                                act_compare = menu.addAction('加入比较')
                                                                act_image_reverse = menu.addAction('图像反推')
                                                                act_enqueue = menu.addAction('加入本地解码队列')
                                                                act_delete = menu.addAction('删除卡片')

                                                                try:
                                                                    has_content = bool(pth)
                                                                except Exception:
                                                                    has_content = False
                                                                try:
                                                                    act_open.setEnabled(has_content)
                                                                    act_open_folder.setEnabled(has_content)
                                                                    act_copy.setEnabled(has_content)
                                                                    act_save.setEnabled(has_content)
                                                                    act_compare.setEnabled(has_content)
                                                                    try:
                                                                        act_image_reverse.setEnabled(has_content)
                                                                    except Exception:
                                                                        pass
                                                                    act_enqueue.setEnabled(has_content)
                                                                except Exception:
                                                                    pass

                                                            menu.addSeparator()
                                                            act_delete_files = menu.addAction('删除卡片并删除本地文件')
                                                            act_delete_files.setEnabled(not is_running and bool(associated_paths(_card)))
                                                            act_delete_files.setToolTip('请先取消任务并等待取消完成' if is_running else '删除本卡片关联的原始输出及解码文件')
                                                            _card._rh_delete_files_action = act_delete_files

                                                            def _handler_delete_files():
                                                                try:
                                                                    if card_is_active(self, _card):
                                                                        self._show_toast('任务仍在运行，请先取消并等待取消完成', 4000)
                                                                        return
                                                                    fallback_inputs = tuple(
                                                                        node.get('fieldValue') for node in (node_list or [])
                                                                        if isinstance(node, dict)
                                                                        and ((node.get('fieldType') or '').upper() in ('IMAGE', 'VIDEO', 'AUDIO', 'UPLOAD') or node.get('_rh_upload'))
                                                                        and isinstance(node.get('fieldValue'), str)
                                                                        and os.path.isabs(node.get('fieldValue')))
                                                                    options = dict(fallback_roots=(self.output_dir,), fallback_inputs=fallback_inputs)
                                                                    plan = plan_card_deletion(_card, **options)
                                                                    names = [os.path.join(os.path.basename(os.path.dirname(path)), os.path.basename(path)) for path in plan.paths]
                                                                    details = '\n'.join(names[:8])
                                                                    if len(names) > 8:
                                                                        details += f'\n…另有 {len(names) - 8} 个文件'
                                                                    message = (f'将删除此卡片及其关联的 {len(plan.paths)} 个本地输出文件（含原始输出与解码文件）。\n\n{details}'
                                                                               if plan.paths else '此卡片关联的本地文件已不存在，将移除卡片。')
                                                                    answer = QtWidgets.QMessageBox.question(self, '删除卡片与本地文件', message,
                                                                                                           QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                                                                                           QtWidgets.QMessageBox.No)
                                                                    if answer != QtWidgets.QMessageBox.Yes:
                                                                        return
                                                                    result = delete_card_files(_card, _move_to_trash,
                                                                                               is_active=lambda: card_is_active(self, _card), **options)
                                                                    if result.complete:
                                                                        _remove_result_card(_card)
                                                                        self._show_toast(f'已删除卡片及 {len(result.removed)} 个本地文件', 3500)
                                                                    else:
                                                                        _card._rh_delete_errors = result.failures
                                                                        detail = '\n'.join(f'{os.path.basename(path)}: {error}' for path, error in result.failures)
                                                                        self.log('部分输出文件删除失败，卡片已保留:\n' + detail)
                                                                        self._show_toast(f'已删除 {len(result.removed)} 个文件，{len(result.failures)} 个失败；卡片已保留，可稍后重试', 5000)
                                                                except Exception as exc:
                                                                    self.log(f'无法删除卡片关联文件: {exc}')
                                                                    self._show_toast(f'未删除卡片: {exc}', 5000)

                                                            act_delete_files.triggered.connect(_handler_delete_files)

                                                            # connect handlers
                                                            try:
                                                                if not is_running:
                                                                    def _handler_open():
                                                                        try:
                                                                            if pth and os.path.exists(pth):
                                                                                if sys.platform.startswith('win'):
                                                                                    os.startfile(pth)
                                                                                elif sys.platform == 'darwin':
                                                                                    subprocess.Popen(['open', pth])
                                                                                else:
                                                                                    subprocess.Popen(['xdg-open', pth])
                                                                        except Exception:
                                                                            pass
                                                                    act_open.triggered.connect(_handler_open)
                                                            except Exception:
                                                                pass

                                                            try:
                                                                if not is_running:
                                                                    def _handler_open_folder():
                                                                        try:
                                                                            if pth and os.path.exists(pth):
                                                                                try:
                                                                                    folder = os.path.dirname(pth)
                                                                                except Exception:
                                                                                    folder = pth
                                                                                try:
                                                                                    self._reveal_in_explorer(folder)
                                                                                except Exception:
                                                                                    pass
                                                                        except Exception:
                                                                            pass
                                                                    act_open_folder.triggered.connect(_handler_open_folder)
                                                            except Exception:
                                                                pass

                                                            try:
                                                                def _handler_copy(_lbl=lbl, _pth=pth):
                                                                    try:
                                                                        # prefer copying image data when available
                                                                        try:
                                                                            pix = getattr(_lbl, '_orig_pixmap', None)
                                                                        except Exception:
                                                                            pix = None
                                                                        if pix is not None:
                                                                            try:
                                                                                md2 = QtCore.QMimeData()
                                                                                md2.setImageData(pix.toImage())
                                                                                ba = QtCore.QByteArray()
                                                                                buf = QtCore.QBuffer(ba)
                                                                                buf.open(QtCore.QIODevice.WriteOnly)
                                                                                pix.save(buf, 'PNG')
                                                                                buf.close()
                                                                                md2.setData('image/png', ba)
                                                                                QtWidgets.QApplication.clipboard().setMimeData(md2)
                                                                                self.log('已将图片复制到剪贴板')
                                                                                return
                                                                            except Exception:
                                                                                pass

                                                                        # if text preview, copy the text content
                                                                        try:
                                                                            is_text = getattr(_lbl, '_is_text_preview', False)
                                                                        except Exception:
                                                                            is_text = False
                                                                        if is_text:
                                                                            try:
                                                                                txt = None
                                                                                try:
                                                                                    txt = _lbl.toPlainText()
                                                                                except Exception:
                                                                                    txt = None
                                                                                if txt is None:
                                                                                    try:
                                                                                        with open(_pth, 'r', encoding='utf-8') as rf:
                                                                                            txt = rf.read()
                                                                                    except Exception:
                                                                                        try:
                                                                                            with open(_pth, 'r', encoding='latin-1') as rf:
                                                                                                txt = rf.read()
                                                                                        except Exception:
                                                                                            txt = str(_pth or '')
                                                                                QtWidgets.QApplication.clipboard().setText(str(txt or ''))
                                                                                self.log('已将预览文本复制到剪贴板')
                                                                                return
                                                                            except Exception:
                                                                                pass

                                                                        # fallback: copy file URL(s) so Explorer paste works
                                                                        try:
                                                                            if _pth and os.path.exists(_pth):
                                                                                md2 = QtCore.QMimeData()
                                                                                md2.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(_pth))])
                                                                                QtWidgets.QApplication.clipboard().setMimeData(md2)
                                                                                self.log('已将预览文件复制到剪贴板')
                                                                                return
                                                                        except Exception:
                                                                            pass

                                                                        # final fallback: copy path string
                                                                        try:
                                                                            QtWidgets.QApplication.clipboard().setText(str(_pth or ''))
                                                                            self.log('已将预览文件路径复制到剪贴板')
                                                                        except Exception:
                                                                            pass
                                                                    except Exception:
                                                                        pass
                                                                act_copy.triggered.connect(_handler_copy)
                                                            except Exception:
                                                                pass

                                                            try:
                                                                if not is_running:
                                                                    def _handler_save():
                                                                        try:
                                                                            if not pth or not os.path.exists(pth):
                                                                                self.log('当前无可用文件以另存')
                                                                            else:
                                                                                dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存为', os.path.join(self.output_dir, os.path.basename(pth)))
                                                                                if dst:
                                                                                    try:
                                                                                        shutil.copy2(pth, dst)
                                                                                        self.log(f'已另存为: {dst}')
                                                                                    except Exception:
                                                                                        self.log('另存为失败')
                                                                        except Exception:
                                                                            pass
                                                                    act_save.triggered.connect(_handler_save)
                                                            except Exception:
                                                                pass

                                                            try:
                                                                if not is_running:
                                                                    def _handler_compare():
                                                                        try:
                                                                            if pth and os.path.exists(pth):
                                                                                self._add_to_compare([pth])
                                                                            else:
                                                                                self.log('没有可加入比较的文件')
                                                                        except Exception:
                                                                            pass
                                                                    act_compare.triggered.connect(_handler_compare)
                                                            except Exception:
                                                                pass

                                                            try:
                                                                if not is_running:
                                                                    def _handler_image_reverse():
                                                                        try:
                                                                            if not pth or not os.path.exists(pth):
                                                                                try:
                                                                                    self.log('当前无可用文件用于图像反推')
                                                                                except Exception:
                                                                                    pass
                                                                                return
                                                                            try:
                                                                                self._start_image_reverse([pth])
                                                                            except Exception:
                                                                                pass
                                                                        except Exception:
                                                                            pass
                                                                    try:
                                                                        act_image_reverse.triggered.connect(_handler_image_reverse)
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass

                                                            try:
                                                                if not is_running:
                                                                    def _handler_delete():
                                                                        try:
                                                                            if not card_is_active(self, _card):
                                                                                QtCore.QTimer.singleShot(0, lambda: _remove_result_card(_card))
                                                                        except Exception:
                                                                            pass
                                                                    act_delete.triggered.connect(_handler_delete)
                                                            except Exception:
                                                                pass

                                                            try:
                                                                if not is_running:
                                                                    def _handler_enqueue():
                                                                        try:
                                                                            # copy preview file into local decode dir
                                                                            if not pth or not os.path.exists(pth):
                                                                                self.log('当前无可加入解码队列的文件')
                                                                                return
                                                                            decode_dir = getattr(self, 'local_decode_dir', None)
                                                                            if not decode_dir:
                                                                                current_dir = SOURCE_ROOT
                                                                                decode_dir = os.path.join(current_dir, 'decoding')
                                                                            os.makedirs(decode_dir, exist_ok=True)
                                                                            base = os.path.basename(pth)
                                                                            dst = os.path.join(decode_dir, base)
                                                                            if os.path.exists(dst):
                                                                                name, ext = os.path.splitext(base)
                                                                                i = 1
                                                                                while True:
                                                                                    candidate = os.path.join(decode_dir, f"{name}_{i}{ext}")
                                                                                    if not os.path.exists(candidate):
                                                                                        dst = candidate
                                                                                        break
                                                                                    i += 1
                                                                            try:
                                                                                shutil.copy2(pth, dst)
                                                                                self.log(f'已加入本地解码队列: {dst}')
                                                                            except Exception as e:
                                                                                self.log(f'加入解码队列失败: {e}')
                                                                        except Exception as e:
                                                                            try:
                                                                                self.log(f'加入本地解码队列失败: {e}')
                                                                            except Exception:
                                                                                pass
                                                                    act_enqueue.triggered.connect(_handler_enqueue)
                                                            except Exception:
                                                                pass

                                                            # cancel handler for running tasks
                                                            try:
                                                                if is_running:
                                                                    def _handler_cancel():
                                                                        try:
                                                                            import threading as _th
                                                                            self._refresh_rh_task_credentials()
                                                                            shared_run = getattr(_card, '_rh_run_id', None)
                                                                            if shared_run:
                                                                                self._rh_execution_service.cancel(shared_run)
                                                                                current = self._rh_execution_service.get(shared_run) or {}
                                                                                _card._rh_cancel_pending = current.get('status') == 'CANCELING'
                                                                                return

                                                                            # remove any queued retry items for this card or tid so they won't retry
                                                                            try:
                                                                                from aetherloom_core.rh_submission_queue import get_submission_queue
                                                                                submission_queue = get_submission_queue(self)
                                                                                if not tid:
                                                                                    _card._rh_cancelled = True
                                                                                removed = submission_queue.cancel_matching(
                                                                                    lambda qi: isinstance(qi, dict) and
                                                                                    (qi.get('card') is _card or (tid and qi.get('tid') == tid)))
                                                                                submission_queue.wake()
                                                                                # emit retry-canceled for removed items
                                                                                try:
                                                                                    for qi in (removed or []):
                                                                                        try:
                                                                                            wid = qi.get('webapp_id') if isinstance(qi, dict) else None
                                                                                            if hasattr(self, '_rh_status_emitter') and wid:
                                                                                                try:
                                                                                                    self._rh_status_emitter.sig.emit(str(wid), 'RETRY_CANCELED')
                                                                                                except Exception:
                                                                                                    pass
                                                                                        except Exception:
                                                                                            pass
                                                                                except Exception:
                                                                                    pass
                                                                            except Exception:
                                                                                pass

                                                                            def _cancel_worker():
                                                                                if not tid:
                                                                                    _card._rh_cancelled = True
                                                                                    return
                                                                                self._rh_task_lifecycle.cancel_task(
                                                                                    tid, str(getattr(_card, '_webapp_id', '')))
                                                                            _th.Thread(target=_cancel_worker, daemon=True).start()
                                                                        except Exception:
                                                                            pass
                                                                    act_cancel.triggered.connect(_handler_cancel)
                                                            except Exception:
                                                                pass

                                                            # show menu (actions will invoke their connected handlers)
                                                            try:
                                                                menu.exec_(QtGui.QCursor.pos())
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass

                                                    try:
                                                        card.setContextMenuPolicy(Qt.CustomContextMenu)
                                                        card.customContextMenuRequested.connect(_on_card_context)
                                                        card_actions_btn.clicked.connect(lambda _checked=False, _open_menu=_on_card_context: _open_menu(QtCore.QPoint()))
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass

                                                # add to grid-managed list and reflow
                                                if not path:
                                                    from aetherloom_core.rh_progress import update_card_progress
                                                    card._rh_results_ready = False
                                                    card._rh_results_presented = False
                                                    card._rh_pending_outputs = []
                                                    def _present_pending(_card=card):
                                                        pending, _card._rh_pending_outputs = _card._rh_pending_outputs, []
                                                        for output_path, output_title in pending:
                                                            _update_preview_card(_card, output_path, output_title)
                                                    card._rh_present_pending = _present_pending
                                                    update_card_progress(self, card, 'SUBMITTING')
                                                try:
                                                    _preview_cards.append(card)
                                                    _reflow_preview_cards()
                                                except Exception:
                                                    pass
                                                return card
                                            except Exception:
                                                return None

                                        # emitter to queue updates to main thread from worker threads
                                        class _UpdateEmitter(QtCore.QObject):
                                            sig = QtCore.pyqtSignal(object, object, object)

                                        _emitter = _UpdateEmitter()
                                        _emitter.sig.connect(lambda card, path, title: _update_preview_card(card, path, title))

                                        # update a specific placeholder card with image/title (runs on main thread)
                                        def _update_preview_card(card, path=None, title=None):
                                            try:
                                                try:
                                                    if isinstance(title, dict):
                                                        if title.get('task_state') and not getattr(card, '_task_id', None):
                                                            from aetherloom_core.rh_progress import update_card_progress
                                                            state = title['task_state']
                                                            update_card_progress(self, card, state)
                                                            if state in ('FAILED', 'CANCELED'):
                                                                self._rh_app_last_result[str(getattr(card, '_webapp_id', ''))] = state
                                                                detail = getattr(card, '_rh_status_detail', None)
                                                                if detail:
                                                                    card._rh_progress_widget.set_message(detail)
                                                        if 'timer_start' in title:
                                                            try:
                                                                card._timer_start = float(title.get('timer_start') or 0)
                                                            except Exception:
                                                                card._timer_start = None
                                                            try:
                                                                if getattr(self, '_rh_running_cards', None) is not None:
                                                                    if hasattr(self._rh_running_cards, 'add'):
                                                                        self._rh_running_cards.add(card)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if getattr(self, '_rh_card_timer', None) is not None:
                                                                    if not self._rh_card_timer.isActive():
                                                                        self._rh_card_timer.start()
                                                            except Exception:
                                                                pass
                                                        if title.get('timer_stop'):
                                                            try:
                                                                start_ts = getattr(card, '_timer_start', None)
                                                                if start_ts:
                                                                    import time as _time
                                                                    elapsed = _time.time() - float(start_ts)
                                                                    lbl_timer = getattr(card, '_timer_label', None)
                                                                    if lbl_timer is not None:
                                                                        try:
                                                                            lbl_timer.setText(f"{elapsed:.2f}s")
                                                                        except Exception:
                                                                            pass
                                                            except Exception:
                                                                pass
                                                            try:
                                                                card._timer_start = None
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if getattr(self, '_rh_running_cards', None) is not None:
                                                                    if hasattr(self._rh_running_cards, 'discard'):
                                                                        self._rh_running_cards.discard(card)
                                                            except Exception:
                                                                pass
                                                            try:
                                                                if getattr(self, '_rh_card_timer', None) is not None:
                                                                    if not getattr(self, '_rh_running_cards', None):
                                                                        self._rh_card_timer.stop()
                                                            except Exception:
                                                                pass
                                                        if 'timer' in title:
                                                            lbl_timer = getattr(card, '_timer_label', None)
                                                            if lbl_timer is not None:
                                                                try:
                                                                    lbl_timer.setText(str(title.get('timer') or ''))
                                                                except Exception:
                                                                    pass
                                                        return
                                                except Exception:
                                                    pass
                                                if card is None:
                                                    _add_preview_card(path, title)
                                                    return

                                                # File notifications can precede the SUCCESS state event.
                                                # Keep every output hidden until download/post-processing succeeds.
                                                if hasattr(card, '_rh_results_ready'):
                                                    task_state = self._rh_status_entries.get(getattr(card, '_task_id', None))
                                                    if task_state in ('FAILED', 'CANCELED'):
                                                        return
                                                    if path and not card._rh_results_ready:
                                                        pending = card._rh_pending_outputs
                                                        if not any(item[0] == path for item in pending):
                                                            pending.append((path, title))
                                                        return
                                                    if not path and not card._rh_results_presented:
                                                        if title and not isinstance(title, dict):
                                                            card._rh_status_detail = str(title)
                                                            card._rh_progress_widget.set_message(title)
                                                        return
                                                    if path:
                                                        card._rh_results_presented = True
                                                        card._rh_progress_widget.hide()

                                                # maintain history of outputs per card
                                                try:
                                                    if getattr(card, '_outputs', None) is None:
                                                        card._outputs = []
                                                except Exception:
                                                    pass
                                                outputs = getattr(card, '_outputs', []) or []
                                                try:
                                                    idx = int(getattr(card, '_output_idx', -1) or -1)
                                                except Exception:
                                                    idx = -1

                                                if path:
                                                    existing_idx = -1
                                                    try:
                                                        for ii, ent in enumerate(outputs):
                                                            try:
                                                                if ent.get('path') == path:
                                                                    existing_idx = ii
                                                                    break
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        existing_idx = -1
                                                    if existing_idx < 0:
                                                        outputs.append({'path': path, 'title': title})
                                                        idx = len(outputs) - 1
                                                    else:
                                                        try:
                                                            if title:
                                                                outputs[existing_idx]['title'] = title
                                                        except Exception:
                                                            pass
                                                        if idx < 0:
                                                            idx = existing_idx

                                                if idx < 0 and outputs:
                                                    idx = len(outputs) - 1

                                                try:
                                                    card._outputs = outputs
                                                    card._output_idx = idx
                                                except Exception:
                                                    pass

                                                # ensure navigation controls reflect history length
                                                try:
                                                    _ensure_nav(card)
                                                except Exception:
                                                    pass
                                                try:
                                                    _refresh_nav(card)
                                                except Exception:
                                                    pass

                                                render_path = path
                                                render_title = title
                                                try:
                                                    if outputs and idx >= 0 and idx < len(outputs):
                                                        render_path = outputs[idx].get('path')
                                                        stored_title = outputs[idx].get('title')
                                                        render_title = stored_title if stored_title is not None else title
                                                except Exception:
                                                    pass

                                                _render_preview_content(card, render_path, render_title)
                                                try:
                                                    _refresh_nav(card)
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass
                                        # Compact action bar remains visible below the results.
                                        try:
                                            out_btn_widget = QtWidgets.QWidget()
                                            out_btn_widget.setObjectName('rhRunBar')
                                            out_btn_row = QtWidgets.QHBoxLayout(out_btn_widget)
                                            out_btn_row.setContentsMargins(0, 12, 0, 0)
                                            out_btn_row.setSpacing(10)
                                            run_count_label = QtWidgets.QLabel('批量次数')
                                            run_count_label.setObjectName('rhMuted')
                                            out_btn_row.addWidget(run_count_label)
                                            preview_run_btn = QtWidgets.QPushButton('运行应用')
                                            preview_run_btn.setObjectName('rhPrimaryButton')
                                            app_page._rh_run_button = preview_run_btn
                                            try:
                                                preview_run_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
                                                preview_run_btn.setFixedHeight(44)
                                                preview_run_btn.setMinimumWidth(120)
                                                preview_run_btn.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                                            except Exception:
                                                pass
                                            # add spinbox to choose run count
                                            try:
                                                run_count_spin = QtWidgets.QSpinBox()
                                                run_count_spin.setObjectName('rhRunCount')
                                                run_count_spin.setMinimum(1)
                                                run_count_spin.setMaximum(99)
                                                run_count_spin.setValue(1)
                                                run_count_spin.setFixedWidth(76)
                                                run_count_spin.setFixedHeight(36)
                                                run_count_spin.setSuffix(' 次')
                                                run_count_spin.setToolTip('按当前参数依次排队；前一任务开始运行后提交下一项')
                                                run_count_label.setBuddy(run_count_spin)
                                                app_page._rh_run_count = run_count_spin
                                                out_btn_row.addWidget(run_count_spin)
                                            except Exception:
                                                run_count_spin = None
                                            out_btn_row.addSpacing(6)
                                            out_btn_row.addWidget(preview_run_btn, 1)
                                            # per-app cancel button removed
                                            preview_layout.addWidget(out_btn_widget)

                                            def _on_preview_run():
                                                record_run_inputs(node_widgets)
                                                # Capture every input on the GUI thread before any
                                                # debounce callback or background task can change it.
                                                try:
                                                    import copy
                                                    captured_nodes = collect_node_values(node_list, node_widgets)
                                                    for index, captured in enumerate(captured_nodes):
                                                        node_list[index]['fieldValue'] = captured.get('fieldValue', '')
                                                    if captured_nodes:
                                                        _persist_and_write(captured_nodes[-1].get('fieldValue', ''), len(captured_nodes) - 1)
                                                    connection = self._rh_connection_snapshot()
                                                    host = connection['base_url']
                                                    api_key = connection['api_key']
                                                    if not api_key:
                                                        raise ValueError('请先配置当前 RunningHub 站点的 API Key')
                                                    run_snapshot = {
                                                        'webapp_id': str(wid),
                                                        'nodes': captured_nodes,
                                                        'parsed': copy.deepcopy(parsed),
                                                        'host': host if host.startswith('http') else f'https://{host}',
                                                        'api_key': api_key,
                                                        'api_keys': connection['api_keys'],
                                                        'output_dir': self.output_dir,
                                                        'input_dir': self.input_dir,
                                                        'retry_max': self.rh_retry_max,
                                                        'retry_delay': self.rh_retry_delay,
                                                        'retry_concurrency': self.rh_retry_concurrency,
                                                        'decode_settings': {
                                                            'enabled': bool(getattr(app_page, '_rh_local_decode_enabled', False)),
                                                            'mode': str(getattr(app_page, '_rh_local_decode_mode', 'grc')),
                                                            'grid_cols': int(getattr(app_page, '_rh_local_grid_cols', 32)),
                                                            'password': str(getattr(app_page, '_rh_local_password', '') or ''),
                                                            'delete_original': bool(getattr(app_page, '_rh_local_delete_original', True)),
                                                        },
                                                    }
                                                except Exception as exc:
                                                    self._show_toast(f'无法运行: {exc}', 4000)
                                                    return
                                                from aetherloom_core.rh_execution_ui import ensure_execution_service, app_snapshot
                                                try:
                                                    import uuid
                                                    service = ensure_execution_service(self)
                                                    count = int(run_count_spin.value()) if run_count_spin is not None else 1
                                                    count = max(1, count)
                                                    run_snapshot['origin'] = {
                                                        'kind': 'app_page',
                                                        'app_submission_group_id': uuid.uuid4().hex,
                                                        'app_submission_count': count,
                                                    }
                                                    snapshot = app_snapshot(self, run_snapshot)
                                                    app_page._rh_run_enabled = True
                                                    for index in range(count):
                                                        task_snapshot = copy.deepcopy(snapshot)
                                                        task_snapshot['origin']['app_submission_index'] = index
                                                        service.submit(task_snapshot)
                                                except Exception as exc:
                                                    self._show_toast(f'任务启动失败：{exc}', 4000)

                                            from aetherloom_core.rh_execution_ui import ensure_execution_service
                                            ensure_execution_service(self)
                                            self._rh_execution_bridge.bind(str(wid), app_page,
                                                                           _add_preview_card, _update_preview_card,
                                                                           _remove_result_card, preview_layout)

                                            try:
                                                    preview_run_btn.clicked.connect(_on_preview_run)
                                                    # per-app cancel button no longer present; nothing to connect
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass
                                        preview_frame.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
                                    except Exception:
                                        preview_frame = nodes_scroll

                                    try:
                                        splitter = QtWidgets.QSplitter(Qt.Horizontal)
                                        splitter.setChildrenCollapsible(False)
                                        splitter.setHandleWidth(10)
                                        splitter.addWidget(parameter_panel)
                                        splitter.addWidget(preview_frame)
                                        preview_frame.setMinimumWidth(300)
                                        app_page._rh_splitter = splitter
                                        splitter.setStretchFactor(0, 4)
                                        splitter.setStretchFactor(1, 5)
                                        # Restore the user's divider while keeping both panes usable.
                                        try:
                                            # attempt to restore previously saved sizes
                                            saved = None
                                            try:
                                                if isinstance(getattr(self, 'settings', None), dict):
                                                    saved = self.settings.get('splitter_sizes')
                                            except Exception:
                                                saved = None
                                            if isinstance(saved, (list, tuple)) and len(saved) >= 2:
                                                try:
                                                    splitter.setSizes([int(saved[0]), int(saved[1])])
                                                except Exception:
                                                    splitter.setSizes([420, 520])
                                            else:
                                                # set a sensible 1:2 pixel fallback
                                                splitter.setSizes([420, 520])
                                        except Exception:
                                            pass

                                        # when the user moves the splitter, persist sizes to settings.json
                                        try:
                                            def _save_splitter_sizes(pos=None, index=None):
                                                try:
                                                    if splitter.orientation() != Qt.Horizontal:
                                                        return
                                                    sizes = splitter.sizes()
                                                    if isinstance(getattr(self, 'settings', None), dict):
                                                        self.settings['splitter_sizes'] = [int(s) for s in sizes]
                                                    try:
                                                        self._save_settings()
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass
                                            splitter.splitterMoved.connect(_save_splitter_sizes)
                                        except Exception:
                                            pass

                                        app_layout.addWidget(splitter, 1)
                                    except Exception:
                                        # fallback to old behavior
                                        try:
                                            main_split = QtWidgets.QHBoxLayout()
                                            main_split.addWidget(parameter_panel)
                                            main_split.addWidget(preview_frame)
                                            app_layout.addLayout(main_split, 1)
                                        except Exception:
                                            app_layout.addWidget(nodes_scroll)

                                    # actions row
                                    actions = QtWidgets.QHBoxLayout()
                                    actions.addStretch(1)
                                    app_layout.addLayout(actions)

                                    # wire reset/update button
                                    try:
                                        def _do_update_action():
                                            try:
                                                result = {'done': False, 'success': False, 'connection': self._rh_connection_snapshot()}
                                                def _worker_update():
                                                    try:
                                                        try:
                                                            from api_calls import call_rh
                                                        except Exception:
                                                            call_rh = None
                                                        if call_rh is None:
                                                            raise RuntimeError('call_rh module not available')
                                                        host_now = result['connection']['base_url']
                                                        base_url = host_now if host_now.startswith('http') else f'https://{host_now}'
                                                        api_key = result['connection']['api_key']
                                                        b = call_rh.get_nodeinfo(wid, api_key, base_url=base_url, timeout=25)
                                                        nodes = None
                                                        try:
                                                            txt = b.decode('utf-8') if isinstance(b, (bytes, bytearray)) else str(b)
                                                            parsed = json.loads(txt)
                                                            nodes = parsed if isinstance(parsed, list) else parsed.get('data', {}).get('nodeInfoList', [])
                                                        except Exception:
                                                            nodes = None

                                                        success = isinstance(nodes, list) and len(nodes) > 0
                                                        if success:
                                                            try:
                                                                page_title = ''
                                                                description = ''
                                                                thumbnail_uri = ''
                                                                try:
                                                                    try:
                                                                        import get_apps as _get_apps
                                                                    except Exception:
                                                                        _get_apps = None
                                                                    if _get_apps is not None:
                                                                        detail = _get_apps.scrape_runninghub_detail(f"{base_url}/ai-detail/{wid}", session=None, timeout=10, api_base=base_url)
                                                                        # detail may contain 'name', 'description', 'covers'
                                                                        page_title = detail.get('name') or ''
                                                                        description = detail.get('description') or ''
                                                                        covers = detail.get('covers') or []
                                                                        if isinstance(covers, list) and covers:
                                                                            thumbnail_uri = covers[0].get('thumbnailUri') or covers[0].get('url') or ''
                                                                    else:
                                                                        # fallback to simple GET/title parse
                                                                        import requests as _requests
                                                                        resp = _requests.get(f"{base_url}/ai-detail/{wid}", timeout=10)
                                                                        if resp is not None and resp.status_code == 200:
                                                                            import re as _re
                                                                            m = _re.search(r"<title[^>]*>(.*?)</title>", resp.text, _re.I | _re.S)
                                                                            if m:
                                                                                page_title = m.group(1).strip()
                                                                except Exception:
                                                                    page_title = page_title or ''
                                                                    description = description or ''
                                                                    thumbnail_uri = thumbnail_uri or ''

                                                                data_to_save = {
                                                                    'webappId': wid,
                                                                    'url': f'{base_url}/webapp/{wid}',
                                                                    'base_url': base_url,
                                                                    'title': page_title or '',
                                                                    'description': description or '',
                                                                    'thumbnail_uri': thumbnail_uri or '',
                                                                    'nodeInfoList': nodes or []
                                                                }
                                                                try:
                                                                    os.makedirs(os.path.dirname(fpath), exist_ok=True)
                                                                except Exception:
                                                                    pass
                                                                tmp = fpath + '.tmp'
                                                                try:
                                                                    with open(tmp, 'wb') as f:
                                                                        f.write(json.dumps(data_to_save, ensure_ascii=False).encode('utf-8'))
                                                                    try:
                                                                        os.replace(tmp, fpath)
                                                                    except Exception:
                                                                        try:
                                                                            os.replace(tmp, fpath)
                                                                        except Exception:
                                                                            pass
                                                                except Exception:
                                                                    # fallback: direct write
                                                                    try:
                                                                        with open(fpath, 'wb') as f:
                                                                            f.write(json.dumps(data_to_save, ensure_ascii=False).encode('utf-8'))
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                success = False

                                                        result['done'] = True
                                                        result['success'] = success
                                                    except Exception as exc:
                                                        result['error'] = str(exc)
                                                        result['done'] = True
                                                        result['success'] = False

                                                import threading as _th
                                                _th.Thread(target=_worker_update, daemon=True).start()

                                                timer = QtCore.QTimer(self)
                                                def _check_update():
                                                    if not result.get('done'):
                                                        return
                                                    timer.stop()
                                                    if result.get('success'):
                                                        try:
                                                            # Refresh only the updated app's button and detail page
                                                            try:
                                                                # reload local json for title/thumbnail
                                                                parsed_local = {}
                                                                try:
                                                                    if os.path.exists(fpath):
                                                                        with open(fpath, 'rb') as _f:
                                                                            parsed_local = json.loads(_f.read().decode('utf-8') or '{}') or {}
                                                                except Exception:
                                                                    parsed_local = {}
                                                                title_now = parsed_local.get('title') if isinstance(parsed_local, dict) else ''
                                                                turl = parsed_local.get('thumbnail_uri') if isinstance(parsed_local, dict) else ''
                                                            except Exception:
                                                                title_now = ''
                                                                turl = ''
                                                            try:
                                                                btn = (getattr(self, '_rh_app_buttons', {}) or {}).get(wid)
                                                            except Exception:
                                                                btn = None
                                                            if btn is not None:
                                                                try:
                                                                    btn._full_title = title_now or wid
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    title_display = title_now or wid
                                                                    if not title_display:
                                                                        title_display = '待命名'
                                                                    elif len(title_display) > 20:
                                                                        title_display = title_display[:16] + '…'
                                                                    try:
                                                                        btn._full_title = title_now or wid
                                                                        if hasattr(self, '_reflow_rh_buttons'):
                                                                            try:
                                                                                self._reflow_rh_buttons()
                                                                            except Exception:
                                                                                pass
                                                                    except Exception:
                                                                        pass
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    # force refresh this app's thumbnail from network
                                                                    _set_button_thumbnail(btn, turl or '', wid, force=True)
                                                                except Exception:
                                                                    pass
                                                            try:
                                                                # evict cached page (if any) so we force a fresh recreation
                                                                if hasattr(self, '_rh_app_pages'):
                                                                    old = None if self._rh_task_lifecycle.has_active_app(wid) else self._rh_app_pages.pop(wid, None)
                                                                    if old is not None:
                                                                        try:
                                                                            # If the user is currently viewing this app page, do not
                                                                            # remove or delete it — keep it live so the UI remains
                                                                            # in-place and the user stays in the sub-interface.
                                                                            if getattr(self, 'pages', None) and self.pages.currentWidget() is old:
                                                                                # leave 'old' intact and allow in-place refresh to occur
                                                                                pass
                                                                            else:
                                                                                try:
                                                                                    self.pages.removeWidget(old)
                                                                                except Exception:
                                                                                    pass
                                                                                try:
                                                                                    old.deleteLater()
                                                                                except Exception:
                                                                                    pass
                                                                        except Exception:
                                                                            pass
                                                            except Exception:
                                                                pass
                                                            # do not automatically open the app detail page after update
                                                            try:
                                                                self._show_toast(f'{wid} 已成功更新', 2000)
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                    else:
                                                        try:
                                                            self._show_toast(result.get('error') or '应用未返回可用节点，请检查应用配置', 5000)
                                                        except Exception:
                                                            try:
                                                                QtWidgets.QMessageBox.warning(self, '失败', result.get('error') or '应用未返回可用节点，请检查应用配置')
                                                            except Exception:
                                                                pass
                                                timer.timeout.connect(_check_update)
                                                timer.start(300)
                                            except Exception:
                                                pass
                                        try:
                                            btn_reset.clicked.connect(_do_update_action)
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass

                                    try:
                                        _populate_app_nav(app_page, wid)
                                    except Exception:
                                        pass

                                    # add to pages and show
                                    try:
                                        make_responsive(app_page, splitters=((app_page._rh_splitter, 850),)
                                                        if hasattr(app_page, '_rh_splitter') else ())
                                        self.pages.addWidget(app_page)
                                        self.pages.setCurrentWidget(app_page)
                                    except Exception:
                                        pass

                                    # store in cache and enforce limit
                                    try:
                                        if wid:
                                            try:
                                                if not hasattr(self, '_rh_app_pages'):
                                                    self._rh_app_pages = {}
                                            except Exception:
                                                pass
                                            try:
                                                self._rh_app_pages[wid] = app_page
                                                try:
                                                    # mark as not enabled until the user actually clicks Run
                                                    if not hasattr(app_page, '_rh_run_enabled'):
                                                        app_page._rh_run_enabled = False
                                                except Exception:
                                                    pass
                                                # enforce cache size (evict oldest entries)
                                                try:
                                                    limit = int(getattr(self, 'app_cache_spin', None).value() if hasattr(self, 'app_cache_spin') else getattr(self, 'app_page_cache_limit', 20))
                                                except Exception:
                                                    limit = getattr(self, 'app_page_cache_limit', 20)
                                                try:
                                                    # compute how many entries to remove (do exact count)
                                                    # count only pages that have been 'enabled' by clicking Run
                                                    try:
                                                        cur = sum(1 for p in (self._rh_app_pages.values() if isinstance(getattr(self, '_rh_app_pages', None), dict) else []) if getattr(p, '_rh_run_enabled', False))
                                                    except Exception:
                                                        cur = 0
                                                    tgt = max(1, int(limit))
                                                    remove_count = max(0, cur - tgt)
                                                    try:
                                                        self.log(f'Cache size {cur}, limit {tgt}, will remove {remove_count}')
                                                    except Exception:
                                                        pass
                                                    if remove_count > 0:
                                                        try:
                                                            # pick keys in insertion order but only those that are enabled
                                                            keys = [k for k, v in (list(self._rh_app_pages.items()) if isinstance(getattr(self, '_rh_app_pages', None), dict) else []) if getattr(v, '_rh_run_enabled', False) and not self._rh_task_lifecycle.has_active_app(k)]
                                                        except Exception:
                                                            keys = []
                                                        # never evict the page we just added (wid)
                                                        try:
                                                            if wid in keys:
                                                                keys = [k for k in keys if k != wid]
                                                        except Exception:
                                                            pass
                                                        # cap to available keys
                                                        try:
                                                            keys_to_remove = keys[:max(0, min(remove_count, len(keys)))]
                                                        except Exception:
                                                            keys_to_remove = []
                                                        for k in keys_to_remove:
                                                            try:
                                                                old = self._rh_app_pages.pop(k, None)
                                                            except Exception:
                                                                old = None
                                                            if old is not None:
                                                                try:
                                                                    # remove from stacked pages and schedule deletion
                                                                    try:
                                                                        self.pages.removeWidget(old)
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        old.deleteLater()
                                                                    except Exception:
                                                                        pass
                                                                    try:
                                                                        self.log(f'Evicted cached app page: {k}')
                                                                    except Exception:
                                                                        pass
                                                                except Exception:
                                                                    pass
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass

                                    def _do_back():
                                        try:
                                            # navigate back to runninghub page
                                            self.pages.setCurrentWidget(self.runninghub_page)
                                            try:
                                                # if this page is cached, keep it in the stacked pages so it can be reused;
                                                # only remove if it is not present in cache (or cache does not contain this wid)
                                                if not (hasattr(self, '_rh_app_pages') and wid and wid in self._rh_app_pages and self._rh_app_pages.get(wid) is app_page):
                                                    try:
                                                        self.pages.removeWidget(app_page)
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass

                                    back_btn.clicked.connect(_do_back)
                                except Exception:
                                    pass
                                try:
                                    # allow Esc key to act as Back on the app detail page
                                    try:
                                        esc_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(Qt.Key_Escape), app_page)
                                        esc_shortcut.activated.connect(_do_back)
                                    except Exception:
                                        pass
                                except Exception:
                                    pass

                            btn.clicked.connect(lambda _=None, p=path, wid=webapp_id: _open_app_detail(p, wid))
                            # provide a right-click context menu as well
                            btn.setContextMenuPolicy(Qt.CustomContextMenu)
                            def _on_context(pos, wid=webapp_id, fpath=path, _btn=btn):
                                menu = QtWidgets.QMenu(_btn)
                                act_open = menu.addAction('打开应用网址')
                                act_open_local = menu.addAction('在本地文件夹中打开')
                                act_update = menu.addAction('更新应用')
                                act_rename = menu.addAction('重命名')
                                try:
                                    favs = getattr(self, 'rh_favorites', None)
                                    is_fav = bool(favs is not None and str(wid) in favs)
                                except Exception:
                                    is_fav = False
                                act_fav = menu.addAction('取消喜爱' if is_fav else '标记喜爱')
                                act_delete = menu.addAction('删除应用')

                                # Use the global cursor position to avoid mapping issues inside scroll/layout
                                act = menu.exec_(QtGui.QCursor.pos())
                                if act is None:
                                    return

                                try:
                                    host = self.rh_host_combo.currentText() or 'www.runninghub.cn'
                                    hostn = host if host.startswith('http') else f'https://{host}'
                                    url2 = f"{hostn.rstrip('/')}/ai-detail/{wid}"
                                except Exception:
                                    url2 = None

                                if act == act_open:
                                    try:
                                        if url2:
                                            import webbrowser as _wb
                                            _wb.open(url2)
                                    except Exception:
                                        pass
                                if act == act_open_local:
                                    try:
                                        # open containing folder for the json file (do not try to select)
                                        try:
                                            folder = os.path.dirname(fpath) if fpath else fpath
                                        except Exception:
                                            folder = fpath
                                        try:
                                            if sys.platform.startswith('win'):
                                                os.startfile(folder)
                                            elif sys.platform == 'darwin':
                                                subprocess.Popen(['open', folder])
                                            else:
                                                subprocess.Popen(['xdg-open', folder])
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass
                                elif act == act_fav:
                                    try:
                                        if not hasattr(self, 'rh_favorites'):
                                            self.rh_favorites = set()
                                        if str(wid) in self.rh_favorites:
                                            self.rh_favorites.discard(str(wid))
                                            is_fav_now = False
                                        else:
                                            self.rh_favorites.add(str(wid))
                                            is_fav_now = True
                                        try:
                                            if hasattr(_btn, '_rh_set_fav'):
                                                _btn._rh_set_fav(is_fav_now)
                                        except Exception:
                                            pass
                                        try:
                                            self._save_settings()
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass
                                elif act == act_rename:
                                    try:
                                        # read existing title from the local JSON (if present)
                                        try:
                                            cur_title = ''
                                            parsed_local = {}
                                            if os.path.exists(fpath):
                                                with open(fpath, 'rb') as _f:
                                                    try:
                                                        parsed_local = json.loads(_f.read().decode('utf-8') or '{}') or {}
                                                    except Exception:
                                                        parsed_local = {}
                                                cur_title = parsed_local.get('title') if isinstance(parsed_local, dict) else ''
                                        except Exception:
                                            cur_title = ''
                                            parsed_local = {}

                                        # ask user for new title
                                        try:
                                            new_title, ok = QtWidgets.QInputDialog.getText(self, '重命名', '输入新的应用标题：', QtWidgets.QLineEdit.Normal, cur_title or wid)
                                        except Exception:
                                            new_title, ok = ('', False)

                                        if not ok:
                                            pass
                                        else:
                                            new_title = (new_title or '').strip()
                                            if new_title:
                                                try:
                                                    # update in-memory dict then write atomically
                                                    if not isinstance(parsed_local, dict):
                                                        parsed_local = {}
                                                    parsed_local['title'] = new_title
                                                    tmp = fpath + '.tmp'
                                                    try:
                                                        with open(tmp, 'w', encoding='utf-8') as wf:
                                                            json.dump(parsed_local, wf, ensure_ascii=False, indent=2)
                                                        os.replace(tmp, fpath)
                                                    except Exception:
                                                        try:
                                                            with open(fpath, 'w', encoding='utf-8') as wf:
                                                                json.dump(parsed_local, wf, ensure_ascii=False, indent=2)
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                try:
                                                    _load_rh_apps()
                                                except Exception:
                                                    pass
                                                try:
                                                    self._show_toast('已重命名', 1500)
                                                except Exception:
                                                    try:
                                                        QtWidgets.QMessageBox.information(self, '成功', '已重命名')
                                                    except Exception:
                                                        pass
                                    except Exception:
                                        pass
                                elif act == act_delete:
                                    try:
                                        # remove the local JSON file if present
                                        try:
                                            if os.path.exists(fpath):
                                                try:
                                                    os.remove(fpath)
                                                except Exception:
                                                    try:
                                                        os.unlink(fpath)
                                                    except Exception:
                                                        pass
                                        except Exception:
                                            pass

                                        # also attempt to remove the containing app directory
                                        try:
                                            app_dir = os.path.dirname(fpath) if fpath else None
                                            outdir = os.path.join(current_dir, 'RH_apps')
                                            # ensure we only remove folders under RH_apps for safety
                                            try:
                                                if app_dir and os.path.isdir(app_dir):
                                                    abs_app = os.path.abspath(app_dir)
                                                    abs_out = os.path.abspath(outdir)
                                                    # commonpath check to avoid accidental deletions outside RH_apps
                                                    try:
                                                        if os.path.commonpath([abs_app, abs_out]) == abs_out:
                                                            try:
                                                                shutil.rmtree(abs_app)
                                                            except Exception:
                                                                # best-effort: remove files inside then rmdir
                                                                try:
                                                                    for root, dirs, files in os.walk(abs_app, topdown=False):
                                                                        for name in files:
                                                                            try:
                                                                                os.remove(os.path.join(root, name))
                                                                            except Exception:
                                                                                pass
                                                                        for name in dirs:
                                                                            try:
                                                                                os.rmdir(os.path.join(root, name))
                                                                            except Exception:
                                                                                pass
                                                                    try:
                                                                        os.rmdir(abs_app)
                                                                    except Exception:
                                                                        pass
                                                                except Exception:
                                                                    pass
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass

                                        try:
                                            _load_rh_apps()
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass
                                elif act == act_update:
                                    try:
                                        result = {'done': False, 'success': False, 'connection': self._rh_connection_snapshot()}
                                        def _worker_update():
                                            try:
                                                try:
                                                    from api_calls import call_rh
                                                except Exception:
                                                    call_rh = None
                                                if call_rh is None:
                                                    raise RuntimeError('call_rh module not available')
                                                host_now = result['connection']['base_url']
                                                base_url = host_now if host_now.startswith('http') else f'https://{host_now}'
                                                api_key = result['connection']['api_key']
                                                b = call_rh.get_nodeinfo(wid, api_key, base_url=base_url, timeout=25)
                                                nodes = None
                                                try:
                                                    txt = b.decode('utf-8') if isinstance(b, (bytes, bytearray)) else str(b)
                                                    parsed = json.loads(txt)
                                                    nodes = parsed if isinstance(parsed, list) else parsed.get('data', {}).get('nodeInfoList', [])
                                                except Exception:
                                                    nodes = None

                                                success = isinstance(nodes, list) and len(nodes) > 0
                                                if success:
                                                    try:
                                                        page_title = ''
                                                        description = ''
                                                        thumbnail_uri = ''
                                                        try:
                                                            try:
                                                                import get_apps as _get_apps
                                                            except Exception:
                                                                _get_apps = None
                                                            if _get_apps is not None and url2:
                                                                detail = _get_apps.scrape_runninghub_detail(url2, session=None, timeout=10, api_base=base_url)
                                                                page_title = detail.get('name') or ''
                                                                description = detail.get('description') or ''
                                                                covers = detail.get('covers') or []
                                                                if isinstance(covers, list) and covers:
                                                                    thumbnail_uri = covers[0].get('thumbnailUri') or covers[0].get('url') or ''
                                                            else:
                                                                import requests as _requests
                                                                resp = _requests.get(url2, timeout=10) if url2 else None
                                                                if resp is not None and resp.status_code == 200:
                                                                    import re as _re
                                                                    m = _re.search(r"<title[^>]*>(.*?)</title>", resp.text, _re.I | _re.S)
                                                                    if m:
                                                                        page_title = m.group(1).strip()
                                                        except Exception:
                                                            page_title = page_title or ''
                                                            description = description or ''
                                                            thumbnail_uri = thumbnail_uri or ''

                                                        data_to_save = {
                                                            'webappId': wid,
                                                            'url': f'{base_url}/webapp/{wid}',
                                                            'base_url': base_url,
                                                            'title': page_title or '',
                                                            'description': description or '',
                                                            'thumbnail_uri': thumbnail_uri or '',
                                                            'nodeInfoList': nodes or []
                                                        }
                                                        try:
                                                            os.makedirs(os.path.dirname(fpath), exist_ok=True)
                                                        except Exception:
                                                            pass
                                                        tmp = fpath + '.tmp'
                                                        try:
                                                            with open(tmp, 'wb') as f:
                                                                f.write(json.dumps(data_to_save, ensure_ascii=False).encode('utf-8'))
                                                            try:
                                                                os.replace(tmp, fpath)
                                                            except Exception:
                                                                try:
                                                                    os.replace(tmp, fpath)
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            try:
                                                                with open(fpath, 'wb') as f:
                                                                    f.write(json.dumps(data_to_save, ensure_ascii=False).encode('utf-8'))
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        success = False

                                                result['done'] = True
                                                result['success'] = success
                                            except Exception as exc:
                                                result['error'] = str(exc)
                                                result['done'] = True
                                                result['success'] = False

                                        import threading as _th
                                        _th.Thread(target=_worker_update, daemon=True).start()

                                        timer = QtCore.QTimer(self)
                                        def _check_update():
                                            if not result.get('done'):
                                                return
                                            timer.stop()
                                            if result.get('success'):
                                                try:
                                                    # Refresh only the updated app's button and detail page
                                                    try:
                                                        parsed_local = {}
                                                        try:
                                                            if os.path.exists(fpath):
                                                                with open(fpath, 'rb') as _f:
                                                                    parsed_local = json.loads(_f.read().decode('utf-8') or '{}') or {}
                                                        except Exception:
                                                            parsed_local = {}
                                                        title_now = parsed_local.get('title') if isinstance(parsed_local, dict) else ''
                                                        turl = parsed_local.get('thumbnail_uri') if isinstance(parsed_local, dict) else ''
                                                    except Exception:
                                                        title_now = ''
                                                        turl = ''
                                                    try:
                                                        btn = (getattr(self, '_rh_app_buttons', {}) or {}).get(wid)
                                                    except Exception:
                                                        btn = None
                                                    if btn is not None:
                                                        try:
                                                            btn._full_title = title_now or wid
                                                        except Exception:
                                                            pass
                                                        try:
                                                            title_display = title_now or wid
                                                            if not title_display:
                                                                title_display = '待命名'
                                                            elif len(title_display) > 20:
                                                                title_display = title_display[:16] + '…'
                                                            try:
                                                                btn._full_title = title_now or wid
                                                                if hasattr(self, '_reflow_rh_buttons'):
                                                                    try:
                                                                        self._reflow_rh_buttons()
                                                                    except Exception:
                                                                        pass
                                                            except Exception:
                                                                pass
                                                        except Exception:
                                                            pass
                                                        try:
                                                            _set_button_thumbnail(btn, turl or '', wid, force=True)
                                                        except Exception:
                                                            pass
                                                    try:
                                                        # evict cached page for this wid
                                                        if hasattr(self, '_rh_app_pages'):
                                                            old = None if self._rh_task_lifecycle.has_active_app(wid) else self._rh_app_pages.pop(wid, None)
                                                            if old is not None:
                                                                try:
                                                                    if getattr(self, 'pages', None) and self.pages.currentWidget() is old:
                                                                        try:
                                                                            self.pages.setCurrentWidget(self.runninghub_page)
                                                                        except Exception:
                                                                            pass
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    self.pages.removeWidget(old)
                                                                except Exception:
                                                                    pass
                                                                try:
                                                                    old.deleteLater()
                                                                except Exception:
                                                                    pass
                                                    except Exception:
                                                        pass
                                                    # do not automatically open the app detail page after update
                                                    try:
                                                        self._show_toast(f'{wid} 已成功更新', 2000)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass
                                            else:
                                                try:
                                                    try:
                                                        self._show_toast(result.get('error') or '应用未返回可用节点，请检查应用配置', 5000)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass
                                        timer.timeout.connect(_check_update)
                                        timer.start(300)
                                    except Exception:
                                        pass

                            btn.customContextMenuRequested.connect(_on_context)
                        except Exception:
                            pass
                        self.rh_workflow_buttons.append(btn)
                    _reflow_buttons()
                except Exception:
                    pass

            # populate any existing RH_apps on startup
            self._rh_reload_apps = _load_rh_apps
            from aetherloom_core.rh_app_install import install_apps
            self._rh_install_apps = lambda references, on_progress=None, on_finished=None: install_apps(
                self, references, on_progress, on_finished)
            try:
                _load_rh_apps()
            except Exception:
                pass

            def _show_add_dialog():
                try:
                    dlg = QtWidgets.QDialog(self)
                    # remove context-help (?) button from titlebar and keep close/title
                    try:
                        flags = dlg.windowFlags()
                        flags &= ~Qt.WindowContextHelpButtonHint
                        flags |= (Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
                        dlg.setWindowFlags(flags)
                    except Exception:
                        pass
                    dlg.setWindowTitle('添加应用')
                    # apply native dark/titlebar mode to dialog
                    try:
                        _set_native_titlebar_dark(dlg, getattr(self, '_theme_mode', 'dark') == 'dark')
                    except Exception:
                        pass
                    try:
                        dlg.setMinimumSize(900, 420)
                        dlg.resize(900, 420)
                    except Exception:
                        pass
                    v = QtWidgets.QVBoxLayout(dlg)
                    label = QtWidgets.QLabel('输入AI应用网址：')
                    v.addWidget(label)

                    mode_state = {'author': False}

                    edit = QtWidgets.QLineEdit(dlg)
                    edit.setPlaceholderText('https://www.runninghub.ai/ai-detail/1999435605639561217')
                    try:
                        edit.setMinimumWidth(820)
                        edit.setMinimumHeight(32)
                        edit.setFont(QtGui.QFont(edit.font().family(), 12))
                    except Exception:
                        pass
                    v.addWidget(edit)

                    author_wrap = QtWidgets.QWidget(dlg)
                    author_form = QtWidgets.QFormLayout(author_wrap)
                    author_form.setContentsMargins(0, 6, 0, 0)
                    author_form.setSpacing(8)
                    author_uid = QtWidgets.QLineEdit(author_wrap)
                    author_uid.setPlaceholderText('作者 UID，如 1911823721911500801')
                    try:
                        author_uid.setMinimumHeight(32)
                        author_uid.setFont(QtGui.QFont(author_uid.font().family(), 12))
                    except Exception:
                        pass
                    author_limit = QtWidgets.QSpinBox(author_wrap)
                    author_limit.setMinimum(1)
                    author_limit.setMaximum(200)
                    author_limit.setValue(15)
                    try:
                        author_limit.setFixedWidth(120)
                    except Exception:
                        pass
                    author_form.addRow('UID：', author_uid)
                    author_form.addRow('APP数量上限：', author_limit)
                    author_wrap.setVisible(False)
                    v.addWidget(author_wrap)

                    toggle_row = QtWidgets.QHBoxLayout()
                    toggle_row.addStretch(1)
                    toggle_btn = QtWidgets.QPushButton('按作者添加')
                    toggle_row.addWidget(toggle_btn)
                    v.addLayout(toggle_row)
                    btn_row = QtWidgets.QHBoxLayout()
                    btn_row.addStretch(1)
                    ok = QtWidgets.QPushButton('确认')
                    cancel = QtWidgets.QPushButton('取消')
                    btn_row.addWidget(ok)
                    btn_row.addWidget(cancel)
                    v.addLayout(btn_row)

                    def _accept():
                        try:
                            if mode_state.get('author'):
                                uid = (author_uid.text() or '').strip()
                                if not uid:
                                    QtWidgets.QMessageBox.warning(self, '错误', '请输入作者 UID')
                                    return
                                try:
                                    limit = int(author_limit.value() or 15)
                                except Exception:
                                    limit = 15
                                host = self.rh_host_combo.currentText() or 'www.runninghub.cn'
                                base_url = host if host.startswith('http') else f'https://{host}'
                                api_key = self.rh_apikey_input.text().strip()
                                if not api_key:
                                    QtWidgets.QMessageBox.warning(self, '缺少 apikey', '请在界面中输入 apikey 后重试')
                                    return

                                try:
                                    dlg.accept()
                                except Exception:
                                    pass
                                try:
                                    dlg.close()
                                except Exception:
                                    pass

                                batch_result = {'errors': [], 'warnings': []}
                                def _worker_wrap():
                                    added = 0
                                    try:
                                        import get_apps as _get_apps
                                        from api_calls import call_rh
                                        apps = _get_apps.get_runninghub_apps(user_id=uid, page=1, page_size=limit, n=limit, base_url=base_url)
                                    except Exception as exc:
                                        batch_result['errors'].append(f'获取应用列表失败: {exc}')
                                        return 0
                                    for app in (apps or []):
                                        wid = app.get('webappId') or app.get('id')
                                        if not wid:
                                            batch_result['errors'].append('应用缺少 webappId')
                                            continue
                                        try:
                                            raw = call_rh.get_nodeinfo(wid, api_key, base_url=base_url, timeout=25)
                                            nodes = json.loads(raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else str(raw))
                                            if not isinstance(nodes, list) or not nodes:
                                                raise ValueError('未返回可用节点')
                                        except Exception as exc:
                                            batch_result['errors'].append(f'应用 {wid}: {exc}')
                                            continue
                                        page_title = app.get('webappName') or ''
                                        description = app.get('description') or ''
                                        thumbnail_uri = ''
                                        try:
                                            detail = _get_apps.scrape_runninghub_detail(app.get('url') or f'{base_url}/webapp/{wid}', timeout=10, api_base=base_url)
                                            page_title = detail.get('name') or page_title
                                            description = detail.get('description') or description
                                            covers = detail.get('covers') or []
                                            if isinstance(covers, list) and covers:
                                                thumbnail_uri = covers[0].get('thumbnailUri') or covers[0].get('url') or ''
                                        except Exception as exc:
                                            batch_result['warnings'].append(f'应用 {wid} 详情: {exc}')
                                        try:
                                            dest_dir = os.path.join(current_dir, 'RH_apps', str(wid))
                                            os.makedirs(dest_dir, exist_ok=True)
                                            path = os.path.join(dest_dir, f'{wid}.json')
                                            data_to_save = {'webappId': wid, 'title': page_title, 'description': description,
                                                            'url': app.get('url') or f'{base_url}/webapp/{wid}', 'base_url': base_url,
                                                            'thumbnail_uri': thumbnail_uri, 'nodeInfoList': nodes}
                                            with open(path, 'wb') as file:
                                                file.write(json.dumps(data_to_save, ensure_ascii=False).encode('utf-8'))
                                            added += 1
                                        except Exception as exc:
                                            batch_result['errors'].append(f'保存应用 {wid} 失败: {exc}')
                                    return added

                                try:
                                    t = QtCore.QThread()
                                    class _Runner(QtCore.QObject):
                                        finished = QtCore.pyqtSignal(int)
                                        @QtCore.pyqtSlot()
                                        def run(self):
                                            try:
                                                count = _worker_wrap()
                                            except Exception as exc:
                                                batch_result['errors'].append(f'批量导入失败: {exc}')
                                                count = 0
                                            try:
                                                self.finished.emit(count)
                                            except Exception:
                                                pass

                                    runner = _Runner()
                                    runner.moveToThread(t)
                                    t.started.connect(runner.run)
                                    def _on_done(count):
                                        try:
                                            _load_rh_apps()
                                        except Exception:
                                            pass
                                        failures = batch_result['errors']
                                        warnings = batch_result['warnings']
                                        message = f'已添加/更新 {count} 个应用'
                                        if failures:
                                            message += f'，{len(failures)} 项失败：{failures[0]}'
                                        if warnings:
                                            message += f'；{len(warnings)} 项详情未获取'
                                            if not failures:
                                                message += f'：{warnings[0]}'
                                        try:
                                            self._show_toast(message, 5000 if failures or warnings else 3000)
                                        except Exception:
                                            pass
                                    runner.finished.connect(_on_done)
                                    runner.finished.connect(runner.deleteLater)
                                    runner.finished.connect(t.quit)
                                    t.finished.connect(t.deleteLater)
                                    if not hasattr(self, '_rh_worker_refs'):
                                        self._rh_worker_refs = []
                                    self._rh_worker_refs.append((t, runner))
                                    t.start()
                                except Exception:
                                    try:
                                        self._show_toast('按作者添加操作无法启动，请检查日志', 3000)
                                    except Exception:
                                        pass
                                return

                            url = edit.text().strip()
                            if not url:
                                dlg.accept()
                                return
                            from aetherloom_core.rh_app_install import application_reference
                            from aetherloom_core.rh_connections import ensure_connections
                            try:
                                reference = application_reference({'url': url})
                                if not ensure_connections(self).keys_for(reference['base_url']):
                                    raise ValueError('请先在连接设置中配置该应用站点的 API key')
                            except ValueError as exc:
                                QtWidgets.QMessageBox.warning(self, '无法添加应用', str(exc))
                                return
                            dlg.accept()

                            def _added(report):
                                if report['failed']:
                                    self._show_toast(report['failed'][0]['error'], 5000)
                                elif report['added']:
                                    self._show_toast('已添加应用', 2000)
                                else:
                                    self._show_toast('该应用已添加', 2000)

                            self._rh_install_apps([reference], on_finished=_added)
                        except Exception:
                            pass

                    def _apply_mode():
                        try:
                            if mode_state.get('author'):
                                label.setText('输入作者 UID 与数量上限：')
                                edit.setVisible(False)
                                author_wrap.setVisible(True)
                                toggle_btn.setText('按链接添加')
                            else:
                                label.setText('输入AI应用网址：')
                                edit.setVisible(True)
                                author_wrap.setVisible(False)
                                toggle_btn.setText('按作者添加')
                        except Exception:
                            pass

                    def _toggle_mode():
                        try:
                            mode_state['author'] = not mode_state.get('author')
                        except Exception:
                            mode_state['author'] = False
                        _apply_mode()

                    try:
                        toggle_btn.clicked.connect(_toggle_mode)
                    except Exception:
                        pass
                    _apply_mode()

                    ok.clicked.connect(_accept)
                    cancel.clicked.connect(lambda: dlg.reject())
                    # pressing Enter triggers Accept
                    edit.returnPressed.connect(_accept)
                    dlg.exec_()
                except Exception:
                    pass

            try:
                add_wf_btn.clicked.connect(_show_add_dialog)
            except Exception:
                pass

            # reflow when the container resizes so buttons stay square and responsive
            try:
                def _on_flow_resize(e):
                    try:
                        _reflow_buttons()
                    except Exception:
                        pass
                    return QtWidgets.QWidget.resizeEvent(rh_flow_widget, e)
                rh_flow_widget.resizeEvent = _on_flow_resize
                try:
                    # also ensure the scroll area's viewport resize triggers a reflow
                    orig_scroll_resize = getattr(rh_flow_scroll, 'resizeEvent', None)
                    def _on_scroll_resize(e):
                        try:
                            _reflow_buttons()
                        except Exception:
                            pass
                        if callable(orig_scroll_resize):
                            try:
                                return orig_scroll_resize(e)
                            except Exception:
                                pass
                    rh_flow_scroll.resizeEvent = _on_scroll_resize
                except Exception:
                    pass
                # initial layout
                QtCore.QTimer.singleShot(20, lambda: _reflow_buttons())
            except Exception:
                try:
                    _reflow_buttons()
                except Exception:
                    pass

            from aetherloom_core.rh_connections import install_legacy_controls
            connections = install_legacy_controls(self)
            connections.changed.connect(self._refresh_rh_task_credentials)
            connections.error.connect(lambda message: self._show_toast(message, 5000))
            self._refresh_rh_task_credentials()
        except Exception:
            # fallback: simple placeholder
            rh_label = QtWidgets.QLabel('Runninghub 应用 - 暂无内容')
            rh_label.setObjectName('runninghubPlaceholder')
            rh_label.setAlignment(Qt.AlignCenter)
            runninghub_layout.addWidget(rh_label)
        self.runninghub_page = runninghub_page
        make_responsive(runninghub_page, rows=tuple(
            (runninghub_layout.itemAt(i).layout(), 980)
            for i in range(runninghub_layout.count())
            if isinstance(runninghub_layout.itemAt(i).layout(), QtWidgets.QHBoxLayout)) + ((rh_hbox_l, 980),))
        self._rh_dashboard.apply_theme()
        self._rh_dashboard.setup_header(runninghub_layout)
        self._rh_dashboard.watch_grid(rh_flow_scroll, _reflow_buttons)
        self.pages.addWidget(runninghub_page)

        # --- Page: 设置 (settings) ---
        # Use a scroll area so the settings page can scroll when content overflows
        settings_scroll = QtWidgets.QScrollArea()
        settings_scroll.setObjectName('settings_page_root')
        settings_scroll.setWidgetResizable(True)
        settings_container = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings_container)
        try:
            settings_layout.setContentsMargins(32, 32, 32, 32)
            settings_layout.setSpacing(20)
        except Exception:
            pass
        # keep a reference to the scroll area so sidebar button can reliably switch to this page
        self.settings_page = settings_scroll
        try:
            settings_scroll.setWidget(settings_container)
        except Exception:
            # fallback: if setWidget fails, add container directly (rare)
            pass

        # hero heading with short description placed on accent frame
        hero_frame = QtWidgets.QFrame()
        hero_frame.setObjectName('settingsHeroFrame')
        hero_layout = QtWidgets.QVBoxLayout(hero_frame)
        hero_layout.setSpacing(4)
        hero_title = QtWidgets.QLabel('目录管理中心')
        hero_title.setObjectName('settingsHeroTitle')
        hero_layout.addWidget(hero_title)
        settings_layout.addWidget(hero_frame)

        cards_column = QtWidgets.QVBoxLayout()
        cards_column.setSpacing(16)
        settings_layout.addLayout(cards_column)

        def _build_folder_card(title, subtitle, line_edit, browse_btn, open_btn):
            card = QtWidgets.QFrame()
            card.setObjectName('settingsCard')
            card.setFrameShape(QtWidgets.QFrame.StyledPanel)
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setContentsMargins(16, 14, 16, 14)
            card_layout.setSpacing(6)

            title_label = QtWidgets.QLabel(title)
            title_label.setObjectName('settingsCardTitle')
            subtitle_label = QtWidgets.QLabel(subtitle)
            subtitle_label.setWordWrap(True)
            subtitle_label.setObjectName('settingsHint')

            field_row = QtWidgets.QHBoxLayout()
            field_row.setSpacing(10)
            try:
                line_edit.setClearButtonEnabled(True)
                line_edit.setMinimumWidth(420)
                line_edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                line_edit.setObjectName('settingsPathInput')
            except Exception:
                pass
            try:
                browse_btn.setMinimumWidth(124)
                open_btn.setMinimumWidth(124)
            except Exception:
                pass
            field_row.addWidget(line_edit, 1)
            field_row.addWidget(browse_btn)
            field_row.addWidget(open_btn)

            card_layout.addWidget(title_label)
            card_layout.addWidget(subtitle_label)
            card_layout.addLayout(field_row)
            cards_column.addWidget(card)

        # input/output/local rows rendered as compact cards
        self.input_label = QtWidgets.QLineEdit(self.input_dir)
        self.input_label.setPlaceholderText('例如: D:/ComfyUI/input')
        self.input_btn = QtWidgets.QPushButton('浏览输入目录')
        self.input_open_btn = QtWidgets.QPushButton('打开文件夹')
        _build_folder_card('输入目录', '拖入的文件默认保存到此目录', self.input_label, self.input_btn, self.input_open_btn)

        self.output_label = QtWidgets.QLineEdit(self.output_dir)
        self.output_label.setPlaceholderText('例如: D:/ComfyUI/output')
        self.output_btn = QtWidgets.QPushButton('浏览输出目录')
        self.output_open_btn = QtWidgets.QPushButton('打开文件夹')
        _build_folder_card('输出目录', '生成的文件会保存到此目录', self.output_label, self.output_btn, self.output_open_btn)

        self.local_decode_label = QtWidgets.QLineEdit(self.local_decode_dir)
        self.local_decode_label.setPlaceholderText('例如: D:/ComfyUI/local_decode')
        self.local_decode_btn = QtWidgets.QPushButton('浏览本地目录')
        self.local_decode_open_btn = QtWidgets.QPushButton('打开文件夹')
        _build_folder_card('本地解码目录', '待解码文件目录', self.local_decode_label, self.local_decode_btn, self.local_decode_open_btn)

        # thumbnail cache controls
        cache_card = QtWidgets.QFrame()
        cache_card.setObjectName('settingsCard')
        cache_card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        cache_layout = QtWidgets.QVBoxLayout(cache_card)
        cache_layout.setContentsMargins(16, 14, 16, 14)
        cache_layout.setSpacing(6)

        cache_title = QtWidgets.QLabel('本地文件缩略图缓存')
        cache_title.setObjectName('settingsCardTitle')
        cache_subtitle = QtWidgets.QLabel('设置缩略图缓存的上限，超出时会自动清理；如果加载的本地文件较多请设置一个较大的缓存上限以提升性能')
        cache_subtitle.setWordWrap(True)
        cache_subtitle.setObjectName('settingsHint')

        cache_row = QtWidgets.QHBoxLayout()
        cache_row.setSpacing(10)
        cache_row.addWidget(QtWidgets.QLabel('缓存上限 (MB)'))
        self.thumb_cache_spin = QtWidgets.QSpinBox()
        try:
            self.thumb_cache_spin.setRange(50, 5000)
            self.thumb_cache_spin.setSingleStep(50)
            self.thumb_cache_spin.setValue(int(getattr(self, 'thumb_cache_max_mb', 300) or 300))
        except Exception:
            try:
                self.thumb_cache_spin.setValue(300)
            except Exception:
                pass
        cache_row.addWidget(self.thumb_cache_spin)
        cache_row.addStretch(1)
        self.clear_cache_btn = QtWidgets.QPushButton('清理缓存')
        cache_row.addWidget(self.clear_cache_btn)

        cache_layout.addWidget(cache_title)
        cache_layout.addWidget(cache_subtitle)
        cache_layout.addLayout(cache_row)
        cards_column.addWidget(cache_card)

        # app page cache controls
        appcache_card = QtWidgets.QFrame()
        appcache_card.setObjectName('settingsCard')
        appcache_card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        appcache_layout = QtWidgets.QVBoxLayout(appcache_card)
        appcache_layout.setContentsMargins(16, 14, 16, 14)
        appcache_layout.setSpacing(6)

        appcache_title = QtWidgets.QLabel('RH应用界面缓存')
        appcache_title.setObjectName('settingsCardTitle')
        appcache_subtitle = QtWidgets.QLabel('控制保留展示的运行状态中应用数量上限，超过上限会按最旧顺序清理应用输出卡片缓存')
        appcache_subtitle.setWordWrap(True)
        appcache_subtitle.setObjectName('settingsHint')

        appcache_row = QtWidgets.QHBoxLayout()
        appcache_row.setSpacing(10)
        appcache_row.addWidget(QtWidgets.QLabel('保留上限 (个)'))
        self.app_cache_spin = QtWidgets.QSpinBox()
        try:
            self.app_cache_spin.setRange(1, 1000)
            self.app_cache_spin.setSingleStep(10)
            self.app_cache_spin.setValue(int(getattr(self, 'app_page_cache_limit', 20) or 20))
        except Exception:
            try:
                self.app_cache_spin.setValue(20)
            except Exception:
                pass
        appcache_row.addWidget(self.app_cache_spin)
        appcache_row.addStretch(1)

        appcache_layout.addWidget(appcache_title)
        appcache_layout.addWidget(appcache_subtitle)
        appcache_layout.addLayout(appcache_row)
        cards_column.addWidget(appcache_card)

        # RunningHub retry settings
        rh_card = QtWidgets.QFrame()
        rh_card.setObjectName('settingsCard')
        rh_card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        rh_layout = QtWidgets.QVBoxLayout(rh_card)
        rh_layout.setContentsMargins(16, 14, 16, 14)
        rh_layout.setSpacing(6)

        rh_title = QtWidgets.QLabel('RunningHub 重试设置')
        rh_title.setObjectName('settingsCardTitle')
        rh_sub = QtWidgets.QLabel('App 与画布共用等候队列。按发起顺序，让队首前 N 项提交或重试；任务开始运行、直接完成、失败或取消后立即补位。云端仍在排队的任务占用名额。')
        rh_sub.setWordWrap(True)
        rh_sub.setObjectName('settingsHint')

        rh_row = QtWidgets.QHBoxLayout()
        rh_row.setSpacing(10)
        # max retries
        rh_row.addWidget(QtWidgets.QLabel('重试上限 (次)'))
        self.rh_retry_max_spin = QtWidgets.QSpinBox()
        try:
            self.rh_retry_max_spin.setRange(1, 100000)
            self.rh_retry_max_spin.setSingleStep(1)
            self.rh_retry_max_spin.setValue(int(getattr(self, 'rh_retry_max', 100) or 100))
        except Exception:
            try:
                self.rh_retry_max_spin.setValue(100)
            except Exception:
                pass
        rh_row.addWidget(self.rh_retry_max_spin)

        # retry delay
        rh_row.addWidget(QtWidgets.QLabel('间隔 (秒)'))
        self.rh_retry_delay_spin = QtWidgets.QSpinBox()
        try:
            self.rh_retry_delay_spin.setRange(1, 3600)
            self.rh_retry_delay_spin.setSingleStep(1)
            self.rh_retry_delay_spin.setValue(int(getattr(self, 'rh_retry_delay', 5) or 5))
        except Exception:
            try:
                self.rh_retry_delay_spin.setValue(5)
            except Exception:
                pass
        rh_row.addWidget(self.rh_retry_delay_spin)

        rh_row.addStretch(1)

        rh_layout.addWidget(rh_title)
        rh_layout.addWidget(rh_sub)
        rh_layout.addLayout(rh_row)
        rh_head_row = QtWidgets.QHBoxLayout()
        rh_head_row.addWidget(QtWidgets.QLabel('队首重试名额 (N)'))
        self.rh_retry_head_count_spin = RhNumberSpinBox(integer=True)
        self.rh_retry_head_count_spin.configure({'min': 1, 'max': 16, 'step': 1})
        self.rh_retry_head_count_spin.setValue(self.rh_retry_head_count)
        self.rh_retry_head_count_spin.setToolTip('默认 1。增大后立即补位；调小不会中断已获得名额的任务。该设置不限制云端已运行任务数量。')
        rh_head_row.addWidget(self.rh_retry_head_count_spin)
        rh_head_row.addStretch(1)
        rh_layout.addLayout(rh_head_row)
        rh_session_hint = QtWidgets.QLabel('关闭客户端后，普通等候和生成任务不再恢复；仅保留已生成结果的下载重试。关闭客户端不会向云端发送取消指令，需要终止时请先取消任务。')
        rh_session_hint.setWordWrap(True)
        rh_session_hint.setObjectName('settingsHint')
        rh_layout.addWidget(rh_session_hint)
        cards_column.addWidget(rh_card)

        # expansion system prompt card (可自定义扩写 system prompt)
        expand_card = QtWidgets.QFrame()
        expand_card.setObjectName('settingsCard')
        expand_card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        expand_layout = QtWidgets.QVBoxLayout(expand_card)
        expand_layout.setContentsMargins(16, 14, 16, 14)
        expand_layout.setSpacing(6)

        expand_title = QtWidgets.QLabel('扩写系统提示词')
        expand_title.setObjectName('settingsCardTitle')
        expand_sub = QtWidgets.QLabel('用于“扩写”的系统提示词；留空则使用默认提示词')
        expand_sub.setWordWrap(True)
        expand_sub.setObjectName('settingsHint')

        expand_layout.addWidget(expand_title)
        expand_layout.addWidget(expand_sub)

        try:
            self.expand_system_prompt_edit = QtWidgets.QTextEdit()
            try:
                cur_prompt = (getattr(self, 'settings', {}) or {}).get('expand_system_prompt') or DEFAULT_EXPAND_SYSTEM_PROMPT
            except Exception:
                cur_prompt = DEFAULT_EXPAND_SYSTEM_PROMPT
            try:
                self.expand_system_prompt_edit.setPlainText(str(cur_prompt))
                self.expand_system_prompt_edit.setFixedHeight(300)
            except Exception:
                pass

            def _on_expand_prompt_changed():
                try:
                    v = self.expand_system_prompt_edit.toPlainText()
                    if isinstance(getattr(self, 'settings', None), dict):
                        self.settings['expand_system_prompt'] = v
                    try:
                        self._save_settings()
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                self.expand_system_prompt_edit.textChanged.connect(_on_expand_prompt_changed)
            except Exception:
                pass

            expand_layout.addWidget(self.expand_system_prompt_edit)
        except Exception:
            pass

        cards_column.addWidget(expand_card)

        # image-reverse prompt card (可自定义图像反推提示词)
        imgrev_card = QtWidgets.QFrame()
        imgrev_card.setObjectName('settingsCard')
        imgrev_card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        imgrev_layout = QtWidgets.QVBoxLayout(imgrev_card)
        imgrev_layout.setContentsMargins(16, 14, 16, 14)
        imgrev_layout.setSpacing(6)

        imgrev_title = QtWidgets.QLabel('图像反推提示词')
        imgrev_title.setObjectName('settingsCardTitle')
        imgrev_sub = QtWidgets.QLabel('用于“图像反推”的提示词；留空则使用默认提示词')
        imgrev_sub.setWordWrap(True)
        imgrev_sub.setObjectName('settingsHint')

        imgrev_layout.addWidget(imgrev_title)
        imgrev_layout.addWidget(imgrev_sub)

        try:
            self.image_reverse_prompt_edit = QtWidgets.QTextEdit()
            try:
                cur_ir_prompt = (getattr(self, 'settings', {}) or {}).get('image_reverse_prompt') or DEFAULT_IMAGE_REVERSE_PROMPT
            except Exception:
                cur_ir_prompt = DEFAULT_IMAGE_REVERSE_PROMPT
            try:
                self.image_reverse_prompt_edit.setPlainText(str(cur_ir_prompt))
                self.image_reverse_prompt_edit.setFixedHeight(300)
                # keep a runtime copy
                try:
                    self.image_reverse_prompt = str(cur_ir_prompt)
                except Exception:
                    pass
            except Exception:
                pass

            def _on_image_reverse_prompt_changed():
                try:
                    v = self.image_reverse_prompt_edit.toPlainText()
                    try:
                        self.image_reverse_prompt = v
                    except Exception:
                        pass
                    if isinstance(getattr(self, 'settings', None), dict):
                        self.settings['image_reverse_prompt'] = v
                    try:
                        self._save_settings()
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                self.image_reverse_prompt_edit.textChanged.connect(_on_image_reverse_prompt_changed)
            except Exception:
                pass

            imgrev_layout.addWidget(self.image_reverse_prompt_edit)
        except Exception:
            pass

        cards_column.addWidget(imgrev_card)

        # connect controls to save settings
        try:
            self.rh_retry_max_spin.valueChanged.connect(lambda v: (setattr(self, 'rh_retry_max', int(v)), self.settings.__setitem__('rh_retry_max', int(v)), self._save_settings()))
        except Exception:
            pass
        try:
            self.rh_retry_delay_spin.valueChanged.connect(lambda v: (setattr(self, 'rh_retry_delay', int(v)), self.settings.__setitem__('rh_retry_delay', int(v)), self._save_settings()))
        except Exception:
            pass
        def _on_retry_head_count_changed(value):
            from aetherloom_core.rh_submission_queue import get_submission_queue
            self.rh_retry_head_count = max(1, min(16, int(value)))
            self.settings['rh_retry_head_count'] = self.rh_retry_head_count
            get_submission_queue(self).set_admission_limit(self.rh_retry_head_count)
            self._save_settings()

        self.rh_retry_head_count_spin.valueChanged.connect(_on_retry_head_count_changed)
        try:
            def _on_app_cache_changed(v):
                try:
                    self.app_page_cache_limit = int(v)
                    if isinstance(getattr(self, 'settings', None), dict):
                        self.settings['app_page_cache_limit'] = self.app_page_cache_limit
                    try:
                        self._save_settings()
                    except Exception:
                        pass
                except Exception:
                    pass
            self.app_cache_spin.valueChanged.connect(_on_app_cache_changed)
        except Exception:
            pass

        def _cache_spin_changed(v):
            try:
                self.thumb_cache_max_mb = int(max(50, v))
            except Exception:
                self.thumb_cache_max_mb = 300
        try:
            self.thumb_cache_spin.valueChanged.connect(_cache_spin_changed)
        except Exception:
            pass

        def _clear_cache():
            cache_dir = getattr(self, '_thumb_cache_dir', None)
            removed = 0
            freed_bytes = 0
            try:
                cap = int(max(50, self.thumb_cache_spin.value()))
                self.thumb_cache_max_mb = cap
            except Exception:
                cap = getattr(self, 'thumb_cache_max_mb', 300)
            try:
                if cache_dir and os.path.exists(cache_dir):
                    for name in os.listdir(cache_dir):
                        path = os.path.join(cache_dir, name)
                        try:
                            st = os.stat(path)
                            freed_bytes += st.st_size
                        except Exception:
                            pass
                        try:
                            if os.path.isdir(path):
                                import shutil
                                shutil.rmtree(path, ignore_errors=True)
                            else:
                                os.remove(path)
                            removed += 1
                        except Exception:
                            try:
                                os.remove(path)
                                removed += 1
                            except Exception:
                                pass
                    try:
                        os.makedirs(cache_dir, exist_ok=True)
                    except Exception:
                        pass
                # clear in-memory caches so UI won't show stale icons
                try:
                    if hasattr(self, '_thumb_mem_cache'):
                        self._thumb_mem_cache.clear()
                except Exception:
                    pass
                try:
                    if hasattr(self, '_lowres_cache'):
                        self._lowres_cache.clear()
                except Exception:
                    pass
                remaining_mb = 0
                try:
                    res = self._prune_thumb_cache(max_size_mb=cap, max_files=4000, max_age_days=14, aggressive=True)
                    if isinstance(res, tuple) and len(res) == 2:
                        remaining_mb = (res[1] or 0) // (1024 * 1024)
                except Exception:
                    pass
                try:
                    freed_mb = freed_bytes // (1024 * 1024)
                    msg = f'已清空缩略图缓存，释放约 {freed_mb} MB，当前占用约 {remaining_mb} MB'
                    if hasattr(self, 'log'):
                        self.log(msg)
                    try:
                        QtWidgets.QMessageBox.information(self, '清理完成', msg)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass
        try:
            self.clear_cache_btn.clicked.connect(_clear_cache)
        except Exception:
            pass

        from aetherloom_core.ui.preferences import configure_settings
        configure_settings(self, settings_layout, cards_column, hero_frame)

        # Keep short sections aligned at the top of the scroll area.
        settings_layout.addStretch(1)

        # add the scroll area (which contains the settings container) to the pages
        try:
            self.pages.addWidget(self.settings_page)
        except Exception:
            # fallback: if something went wrong, add the raw container
            try:
                self.pages.addWidget(settings_container)
            except Exception:
                pass

        # store local_page reference for safe page switching
        self.local_page = local_page

        # connect sidebar buttons to pages and refresh views when clicked (use helper methods)
        def _set_decode_page():
            try:
                try:
                    self.home_btn.setChecked(False)
                except Exception:
                    pass
                self.decode_btn.setChecked(True)
                self.settings_btn.setChecked(False)
                self.api_btn.setChecked(False)
                try:
                    self.runninghub_btn.setChecked(False)
                except Exception:
                    pass
                self.local_btn.setChecked(False)
                # switch to the decode page (don't assume index 0)
                try:
                    idx = self.pages.indexOf(decode_page) if 'decode_page' in locals() else 0
                    self.pages.setCurrentIndex(idx)
                except Exception:
                    try:
                        self.pages.setCurrentIndex(0)
                    except Exception:
                        pass
                QtCore.QTimer.singleShot(50, lambda: self.load_folder(getattr(self, 'local_decode_dir', None)))
            except Exception:
                pass

        def _set_home_page():
            try:
                # set checked state for sidebar buttons
                try:
                    self.home_btn.setChecked(True)
                except Exception:
                    pass
                for b in (self.decode_btn, self.local_btn, self.api_btn, getattr(self, 'runninghub_btn', None), getattr(self, 'settings_btn', None)):
                    try:
                        if b is not None:
                            b.setChecked(False)
                    except Exception:
                        pass
                # show home page (should be index 0)
                idx = self.pages.indexOf(home_page) if hasattr(self, 'pages') and hasattr(self, 'home_btn') else 0
                self.pages.setCurrentIndex(idx)
            except Exception:
                pass

        def _set_settings_page():
            try:
                try:
                    self.home_btn.setChecked(False)
                except Exception:
                    pass
                self.settings_btn.setChecked(True)
                self.decode_btn.setChecked(False)
                self.api_btn.setChecked(False)
                try:
                    self.runninghub_btn.setChecked(False)
                except Exception:
                    pass
                self.local_btn.setChecked(False)
                idx = self.pages.indexOf(self.settings_page) if hasattr(self, 'pages') and hasattr(self, 'settings_page') else 1
                self.pages.setCurrentIndex(idx)
                QtCore.QTimer.singleShot(50, lambda: self._apply_settings(self._load_settings(), apply_window_geometry=False, apply_page_index=False))
            except Exception:
                pass

        def _set_local_page():
            try:
                try:
                    self.home_btn.setChecked(False)
                except Exception:
                    pass
                self.local_btn.setChecked(True)
                self.decode_btn.setChecked(False)
                self.api_btn.setChecked(False)
                try:
                    self.runninghub_btn.setChecked(False)
                except Exception:
                    pass
                self.settings_btn.setChecked(False)
                idx = self.pages.indexOf(self.local_page) if hasattr(self, 'pages') and hasattr(self, 'local_page') else 2
                self.pages.setCurrentIndex(idx)
                QtCore.QTimer.singleShot(50, lambda: self._refresh_local_list())
            except Exception:
                pass

        def _set_api_page():
            try:
                try:
                    self.home_btn.setChecked(False)
                except Exception:
                    pass
                self.api_btn.setChecked(True)
                self.decode_btn.setChecked(False)
                self.local_btn.setChecked(False)
                try:
                    self.runninghub_btn.setChecked(False)
                except Exception:
                    pass
                self.settings_btn.setChecked(False)
                idx = self.pages.indexOf(self.api_page) if hasattr(self, 'pages') and hasattr(self, 'api_page') else 3
                self.pages.setCurrentIndex(idx)
            except Exception:
                pass

        self.decode_btn.clicked.connect(_set_decode_page)
        try:
            self.home_btn.clicked.connect(_set_home_page)
        except Exception:
            pass
        self.settings_btn.clicked.connect(_set_settings_page)
        self.local_btn.clicked.connect(_set_local_page)
        self.api_btn.clicked.connect(_set_api_page)
        try:
            if getattr(self, 'sidebar_toggle_btn', None):
                self.sidebar_toggle_btn.clicked.connect(lambda: self._set_sidebar_collapsed(not getattr(self, '_sidebar_effective_collapsed', False)))
        except Exception:
            pass
        try:
            def _set_runninghub_page():
                try:
                    try:
                        self.home_btn.setChecked(False)
                    except Exception:
                        pass
                    self.runninghub_btn.setChecked(True)
                    self.decode_btn.setChecked(False)
                    self.api_btn.setChecked(False)
                    self.local_btn.setChecked(False)
                    self.settings_btn.setChecked(False)

                    # If currently on an app subpage, go back to main RH page.
                    try:
                        cur = self.pages.currentWidget() if hasattr(self, 'pages') else None
                    except Exception:
                        cur = None
                    try:
                        app_pages = list((getattr(self, '_rh_app_pages', {}) or {}).values())
                    except Exception:
                        app_pages = []
                    if cur is not None and cur in app_pages:
                        try:
                            idx = self.pages.indexOf(self.runninghub_page) if hasattr(self, 'pages') and hasattr(self, 'runninghub_page') else 4
                            self.pages.setCurrentIndex(idx)
                            return
                        except Exception:
                            pass

                    # Otherwise, prefer returning to the last opened app subpage.
                    try:
                        last_app = getattr(self, '_rh_last_app_page', None)
                        if last_app is not None and hasattr(self, 'pages') and self.pages.indexOf(last_app) >= 0:
                            self.pages.setCurrentWidget(last_app)
                            return
                    except Exception:
                        pass

                    idx = self.pages.indexOf(self.runninghub_page) if hasattr(self, 'pages') and hasattr(self, 'runninghub_page') else 4
                    self.pages.setCurrentIndex(idx)
                except Exception:
                    pass
            self.runninghub_btn.clicked.connect(_set_runninghub_page)
        except Exception:
            pass

        from aetherloom_core.rh_execution_ui import install_canvas_page
        install_canvas_page(self)
