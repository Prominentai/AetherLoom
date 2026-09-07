"""Presentation and lightweight state updates for the local media browser."""

import os
from collections import OrderedDict
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_ui import app_stylesheet, palette


def current_folder(window, kind):
    """Browsing location is independent of the input/output destination settings."""
    root = os.path.abspath(window.input_dir if kind == 'input' else window.output_dir)
    locations = getattr(window, '_local_browser_locations', None)
    if locations is None:
        locations = window._local_browser_locations = {}
    state = locations.get(kind)
    if state is None or os.path.normcase(state['root']) != os.path.normcase(root):
        state = locations[kind] = dict(root=root, path=root, views=OrderedDict())
    return state['path']


def navigate_folder(window, kind, path):
    if kind not in ('input', 'output') or getattr(window, '_closing', False):
        return False
    current = current_folder(window, kind)
    path = os.path.abspath(path)
    if os.path.normcase(path) == os.path.normcase(current):
        return False
    grid = window.local_list_in if kind == 'input' else window.local_list_out
    state = window._local_browser_locations[kind]
    displayed = grid.property('localBrowserFolder') or current
    item = grid.currentItem()
    selected_path = (item.data(QtCore.Qt.UserRole) or {}).get('path') if item is not None else None
    key = os.path.normcase(displayed)
    state['views'].pop(key, None)
    # Retain a single focus path, not thousands of selected items per directory.
    state['views'][key] = dict(scroll=grid.verticalScrollBar().value(), current=selected_path)
    while len(state['views']) > 32:
        state['views'].popitem(last=False)
    state['path'] = path
    window._local_active_list = grid
    window._refresh_local_list()
    return True


def prepare_location(window, kind, folder, grid, selected):
    current_folder(window, kind)
    changed = os.path.normcase(str(grid.property('localBrowserFolder') or '')) != os.path.normcase(folder)
    state = window._local_browser_locations[kind]
    current = grid.currentItem()
    view = state['views'].get(os.path.normcase(folder), {}) if changed else {
        'scroll': grid.verticalScrollBar().value(),
        'current': (current.data(QtCore.Qt.UserRole) or {}).get('path') if current is not None else None,
    }
    if not hasattr(window, '_local_restore_positions'):
        window._local_restore_positions = {}
    window._local_restore_positions[id(grid)] = dict(kind=kind, folder=folder, **view)
    grid.setProperty('localBrowserFolder', folder)
    return {view['current']} if changed and view.get('current') else set() if changed else selected


def restore_location(window, grid):
    target = getattr(window, '_local_restore_positions', {}).pop(id(grid), None)
    if target is None:
        return

    def apply():
        if getattr(window, '_closing', False) or current_folder(window, target['kind']) != target['folder']:
            return
        try:
            if grid.property('localBrowserFolder') != target['folder']:
                return
            item = getattr(window, '_local_item_lookup', {}).get(id(grid), {}).get(target.get('current'))
            if item is not None and not item.isHidden():
                grid.setCurrentItem(item, QtCore.QItemSelectionModel.NoUpdate)
            # QListView defers layout after insertion. Its scroll maximum can
            # still be zero until this pass, which would discard the saved offset.
            scroll = target.get('scroll', 0)
            if scroll > grid.verticalScrollBar().maximum():
                grid.doItemsLayout()
            grid.verticalScrollBar().setValue(scroll)
        except RuntimeError:
            pass
    QtCore.QTimer.singleShot(0, apply)


def stylesheet(mode):
    p = palette(mode)
    return app_stylesheet(mode).replace('#rhAppPage', '#localFilesPage') + f'''
        QWidget#localFilesPage QLabel#localTitle {{ font-size: 24px; font-weight: 700; }}
        QWidget#localFilesPage QFrame#localFolderPanel {{ background: {p['surface']};
            border: 1px solid {p['border']}; border-radius: 12px; }}
        QWidget#localFilesPage QRadioButton {{ border: 1px solid {p['border']};
            border-radius: 7px; background: {p['input']}; padding: 7px 12px;
            font-size: 12px; color: {p['muted']}; }}
        QWidget#localFilesPage QRadioButton::indicator {{ width: 0; height: 0; }}
        QWidget#localFilesPage QRadioButton:checked {{ background: {p['accent_soft']};
            border-color: {p['accent']}; color: {p['text']}; }}
        QWidget#localFilesPage QListWidget {{ background: transparent; border: none; padding: 0; }}
        QWidget#localFilesPage QWidget#localAdvanced {{ background: {p['surface']};
            border: 1px solid {p['border']}; border-radius: 10px; }}
        QWidget#localFilesPage QLabel#localSelection {{ color: {p['muted']}; font-size: 12px; }}
    '''


def readable_size(size):
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


class ElidedLabel(QtWidgets.QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def setText(self, text):
        self.full_text = str(text)
        self.setToolTip(self.full_text)
        super().setText(self.fontMetrics().elidedText(self.full_text, QtCore.Qt.ElideMiddle, max(0, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.full_text)


class MediaDelegate(QtWidgets.QStyledItemDelegate):
    """A readable file tile with a thin selection outline and cached metadata."""
    def paint(self, painter, option, index):
        view = self.parent()
        p = palette(getattr(view.window(), '_theme_mode', 'dark'))
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        hover = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(QtGui.QColor(p['accent_soft'] if selected else p['hover'] if hover else p['input']))
        painter.setPen(QtGui.QPen(QtGui.QColor(p['accent'] if selected else p['border']), 2 if selected else 1))
        painter.drawRoundedRect(rect, 8, 8)
        size = view.iconSize()
        image_rect = QtCore.QRect(rect.x() + (rect.width() - size.width()) // 2,
                                 rect.y() + 5, size.width(), size.height())
        icon = index.data(QtCore.Qt.DecorationRole)
        meta = index.data(QtCore.Qt.UserRole) or {}
        if isinstance(icon, QtGui.QPixmap):
            icon = QtGui.QIcon(icon)
        if meta.get('is_dir'):
            folder = QtCore.QRectF(image_rect).adjusted(size.width() * 0.18, size.height() * 0.25,
                                                       -size.width() * 0.18, -size.height() * 0.23)
            shape = QtGui.QPainterPath()
            shape.moveTo(folder.left(), folder.top() + folder.height() * 0.16)
            shape.lineTo(folder.left(), folder.top())
            shape.lineTo(folder.left() + folder.width() * 0.42, folder.top())
            shape.lineTo(folder.left() + folder.width() * 0.52, folder.top() + folder.height() * 0.16)
            shape.lineTo(folder.right(), folder.top() + folder.height() * 0.16)
            shape.lineTo(folder.right(), folder.bottom())
            shape.lineTo(folder.left(), folder.bottom())
            shape.closeSubpath()
            painter.setPen(QtGui.QPen(QtGui.QColor('#b78f49'), 1.5))
            painter.setBrush(QtGui.QColor('#d6b477'))
            painter.drawPath(shape)
        elif isinstance(icon, QtGui.QIcon) and not icon.isNull():
            icon.paint(painter, image_rect, QtCore.Qt.AlignCenter, QtGui.QIcon.Normal)
        else:
            painter.setPen(QtGui.QColor(p['muted']))
            failed = (index.data(QtCore.Qt.UserRole) or {}).get('preview_error')
            painter.drawText(image_rect, QtCore.Qt.AlignCenter, '预览不可用' if failed else '载入预览…')
        font = QtGui.QFont(option.font)
        font.setPointSizeF(10)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['text']))
        name = str(index.data(QtCore.Qt.DisplayRole) or '')
        name_rect = QtCore.QRect(rect.x() + 7, image_rect.bottom() + 7, rect.width() - 14, 19)
        painter.drawText(name_rect, QtCore.Qt.AlignCenter,
                         QtGui.QFontMetrics(font).elidedText(name, QtCore.Qt.ElideMiddle, name_rect.width()))
        info = '文件夹 · 双击进入' if meta.get('is_dir') else os.path.splitext(meta.get('name', name))[1].lstrip('.').upper()
        if not meta.get('is_dir') and meta.get('size_bytes') is not None:
            info += ' · ' + readable_size(meta['size_bytes'])
        font.setPointSizeF(8.5)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['muted']))
        painter.drawText(QtCore.QRect(rect.x() + 7, name_rect.bottom() + 1, rect.width() - 14, 16),
                         QtCore.Qt.AlignCenter, info)
        painter.restore()


def configure(window, page):
    """Rearrange existing widgets, preserving their connections and settings."""
    page.setObjectName('localFilesPage')
    page.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    window.local_page = page
    page.setStyleSheet(stylesheet(getattr(window, '_theme_mode', 'dark')))
    retired = QtWidgets.QWidget(page)
    retired.hide()
    window._local_retired_controls = retired

    def retire_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(retired)
            elif item.layout() is not None:
                retire_layout(item.layout())

    # Keep the dynamic filter rows; their signals and saved conditions remain live.
    advanced_layout = window.local_controls_wrapper.layout()
    for _ in range(2):
        item = advanced_layout.takeAt(0)
        if item is not None and item.layout() is not None:
            retire_layout(item.layout())
    layout = page.layout()
    retire_layout(layout)
    layout.setContentsMargins(14, 16, 16, 12)
    layout.setSpacing(12)

    header = QtWidgets.QHBoxLayout()
    titles = QtWidgets.QVBoxLayout()
    titles.setSpacing(3)
    title = QtWidgets.QLabel('本地文件')
    title.setObjectName('localTitle')
    hint = QtWidgets.QLabel('浏览输入素材与生成结果')
    hint.setObjectName('rhSubtitle')
    titles.addWidget(title)
    titles.addWidget(hint)
    header.addLayout(titles, 1)
    window.local_preview_btn = QtWidgets.QPushButton('快速预览 · Space')
    window.local_preview_btn.setObjectName('rhSecondaryButton')
    window.local_preview_btn.setEnabled(False)
    window.local_preview_btn.clicked.connect(lambda: window._preview_local_selection())
    window.local_refresh_btn = QtWidgets.QPushButton('刷新 · F5')
    window.local_refresh_btn.clicked.connect(lambda: window._refresh_local_list())
    header.addWidget(window.local_preview_btn)
    header.addWidget(window.local_refresh_btn)
    layout.addLayout(header)

    browse_row = QtWidgets.QHBoxLayout()
    browse_row.setSpacing(8)
    modes_row = QtWidgets.QHBoxLayout()
    for number, name in enumerate(('输入文件', '输出文件', '并排对照')):
        button = window.local_mode_group.button(number)
        button.setText(name)
        button.setCursor(QtCore.Qt.PointingHandCursor)
        modes_row.addWidget(button)
    browse_row.addLayout(modes_row)
    search_row = QtWidgets.QHBoxLayout()
    window.local_type_combo = QtWidgets.QComboBox()
    for label, value in (('全部媒体', 'all'), ('仅图片', 'image'), ('仅视频', 'video')):
        window.local_type_combo.addItem(label, value)
    window.local_type_combo.setMinimumWidth(110)
    window.local_type_combo.currentIndexChanged.connect(window._apply_local_search)
    search_row.addWidget(window.local_type_combo)
    window.local_search_edit.setPlaceholderText('搜索文件名 · Ctrl+F')
    window.local_search_edit.setMinimumWidth(120)
    window.local_search_edit.setMaximumWidth(380)
    search_row.addWidget(window.local_search_edit, 1)
    window.local_controls_toggle_btn.setText('排序与筛选')
    window.local_controls_toggle_btn.setCheckable(True)
    window.local_controls_toggle_btn.setChecked(False)
    search_row.addWidget(window.local_controls_toggle_btn)
    browse_row.addLayout(search_row, 1)
    layout.addLayout(browse_row)

    size_row = QtWidgets.QHBoxLayout()
    size_row.addWidget(QtWidgets.QLabel('缩略图大小'))
    window.thumb_size_spin.setFixedWidth(88)
    window.thumb_size_spin.setStyleSheet('')
    window.thumb_size_slider.setMinimumWidth(80)
    window.thumb_size_slider.setMaximumWidth(320)
    window.thumb_size_slider.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    size_row.addWidget(window.thumb_size_spin)
    size_row.addWidget(window.thumb_size_unit)
    size_row.addWidget(window.thumb_size_slider)
    size_row.addStretch(1)
    gesture = QtWidgets.QLabel('Ctrl + 滚轮也可调整')
    gesture.setObjectName('rhMuted')
    size_row.addWidget(gesture)
    advanced_layout.insertLayout(0, size_row)
    advanced_layout.setContentsMargins(12, 10, 12, 10)
    window.local_controls_wrapper.setObjectName('localAdvanced')
    window.local_filter_add_btn.setStyleSheet('')
    window.local_filter_clear_btn.setText('清空筛选')
    window.local_filter_clear_btn.setStyleSheet('')
    layout.addWidget(window.local_controls_wrapper)
    window.local_controls_wrapper.hide()
    window._local_controls_visible = False

    split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
    split.setChildrenCollapsible(False)
    split.setHandleWidth(10)
    window._local_panels = {}
    window._local_empty_states = {}
    window._local_empty_titles = {}
    window._local_empty_hints = {}
    window._local_empty_clear = {}
    window._local_stacks = {}
    window._local_path_labels = {}
    window._local_up_buttons = {}
    window._local_root_buttons = {}
    for kind, name, grid, sort, count in (
            ('input', '输入素材', window.local_list_in, window.local_sort_in_combo, window.local_count_label_in),
            ('output', '生成结果', window.local_list_out, window.local_sort_out_combo, window.local_count_label_out)):
        panel = QtWidgets.QFrame()
        panel.setObjectName('localFolderPanel')
        panel.setMinimumWidth(280)
        pane = QtWidgets.QVBoxLayout(panel)
        pane.setContentsMargins(12, 12, 12, 8)
        pane.setSpacing(8)
        heading = QtWidgets.QHBoxLayout()
        folder_title = QtWidgets.QLabel(name)
        folder_title.setObjectName('rhSectionTitle')
        count.setObjectName('rhMuted')
        count.setStyleSheet('')
        count.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        heading.addWidget(folder_title)
        heading.addWidget(count, 1)
        sort.setMinimumWidth(112)
        heading.addWidget(sort)
        pane.addLayout(heading)
        folder_row = QtWidgets.QHBoxLayout()
        up = QtWidgets.QToolButton()
        up.setIcon(window.style().standardIcon(QtWidgets.QStyle.SP_ArrowUp))
        up.setToolTip('上一级 · Alt+↑')
        up.setAccessibleName(name + '上一级')
        up.clicked.connect(lambda _checked=False, key=kind:
                           window._navigate_local_folder(key, os.path.dirname(current_folder(window, key))))
        home = QtWidgets.QToolButton()
        home.setIcon(window.style().standardIcon(QtWidgets.QStyle.SP_DirHomeIcon))
        home.setToolTip('回到配置的' + ('输入目录' if kind == 'input' else '输出目录'))
        home.setAccessibleName(name + '根目录')
        home.clicked.connect(lambda _checked=False, key=kind:
                             window._navigate_local_folder(key, window.input_dir if key == 'input' else window.output_dir))
        folder_row.addWidget(up)
        folder_row.addWidget(home)
        path_label = ElidedLabel()
        path_label.setObjectName('rhMuted')
        folder_row.addWidget(path_label, 1)
        folder_open = QtWidgets.QPushButton('打开目录')
        folder_open.setObjectName('rhToolButton')
        folder_open.clicked.connect(lambda _checked=False, key=kind:
                                   window._open_folder_path(current_folder(window, key)))
        folder_row.addWidget(folder_open)
        pane.addLayout(folder_row)
        stack = QtWidgets.QStackedWidget()
        grid.setStyleSheet('')
        grid.setSpacing(10)
        grid.setMouseTracking(True)
        grid.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        grid.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        grid.setItemDelegate(MediaDelegate(grid))
        grid.itemClicked.connect(lambda _item, widget=grid: window._update_local_selection_summary(widget))
        stack.addWidget(grid)
        empty = QtWidgets.QWidget()
        empty_layout = QtWidgets.QVBoxLayout(empty)
        empty_layout.setContentsMargins(18, 18, 18, 18)
        empty_layout.addStretch()
        empty_title = QtWidgets.QLabel('暂无媒体文件', alignment=QtCore.Qt.AlignCenter)
        empty_title.setObjectName('rhEmptyTitle')
        empty_hint = QtWidgets.QLabel('此目录中的图片和视频会显示在这里。', alignment=QtCore.Qt.AlignCenter)
        empty_hint.setWordWrap(True)
        empty_hint.setObjectName('rhEmptyHint')
        clear = QtWidgets.QPushButton('清除筛选')
        clear.clicked.connect(window._reset_local_browser_filters)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_hint)
        empty_layout.addSpacing(8)
        empty_layout.addWidget(clear, 0, QtCore.Qt.AlignHCenter)
        empty_layout.addStretch()
        stack.addWidget(empty)
        pane.addWidget(stack, 1)
        split.addWidget(panel)
        window._local_panels[kind] = panel
        window._local_empty_states[kind] = empty
        window._local_empty_titles[kind] = empty_title
        window._local_empty_hints[kind] = empty_hint
        window._local_empty_clear[kind] = clear
        window._local_stacks[kind] = stack
        window._local_path_labels[kind] = path_label
        window._local_up_buttons[kind] = up
        window._local_root_buttons[kind] = home
        up_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence('Alt+Up'), grid)
        up_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        up_shortcut.activated.connect(lambda key=kind:
                                     window._navigate_local_folder(key, os.path.dirname(current_folder(window, key))))
        shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), grid)
        shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda widget=grid: (setattr(window, '_local_active_list', widget),
                                                        window._preview_local_selection()))
    split.setSizes([500, 500])
    window._local_splitter = split
    layout.addWidget(split, 1)
    from aetherloom_core.ui.responsive import make_responsive
    make_responsive(page, rows=((header, 520), (browse_row, 900), (size_row, 650)),
                    splitters=((split, 750),))
    window.local_selection_label = ElidedLabel()
    window.local_selection_label.setObjectName('localSelection')
    window.local_selection_label.setText('双击文件夹进入 · 空格预览文件 · 双击文件使用系统应用打开')
    window.local_selection_label.setMinimumHeight(24)
    layout.addWidget(window.local_selection_label)
    refresh = QtWidgets.QShortcut(QtGui.QKeySequence.Refresh, page)
    refresh.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
    refresh.activated.connect(window._refresh_local_list)
    window._update_local_browser_state()


def update_state(window):
    if not getattr(window, '_local_panels', None):
        return
    mode_id = window.local_mode_group.checkedId()
    mode = 'input' if mode_id == 0 else 'output' if mode_id == 1 else 'both'
    loading = bool(getattr(window, '_local_scan_loading', False))
    for kind, grid, count in (
            ('input', window.local_list_in, window.local_count_label_in),
            ('output', window.local_list_out, window.local_count_label_out)):
        folder = current_folder(window, kind)
        window._local_panels[kind].setVisible(mode in (kind, 'both'))
        window._local_path_labels[kind].setText(str(folder))
        window._local_up_buttons[kind].setEnabled(os.path.dirname(folder) != folder)
        root = os.path.abspath(window.input_dir if kind == 'input' else window.output_dir)
        window._local_root_buttons[kind].setEnabled(os.path.normcase(root) != os.path.normcase(folder))
        total = grid.count()
        visible = grid.property('filteredVisibleCount')
        visible = total if visible is None else int(visible)
        pending = getattr(window, '_local_pending_add', {}).get(str(id(grid)))
        busy = loading or bool(pending)
        error = getattr(window, '_local_scan_errors', {}).get(kind) if not busy else None
        count.setText((f'{visible}/{total} 项' if visible != total else f'{total} 项') + (' · 加载中' if busy else ''))
        stack = window._local_stacks[kind]
        # Keep the focused grid visible between clear() and the first async
        # batch. Hiding it here loses keyboard focus after entering a folder.
        stack.setCurrentIndex(0 if visible or (busy and stack.currentIndex() == 0) else 1)
        window._local_empty_hints[kind].setToolTip(str(error or ''))
        if error:
            count.setText('无法读取')
            window._local_stacks[kind].setCurrentIndex(1)
            window._local_empty_titles[kind].setText('目录暂时无法访问')
            window._local_empty_hints[kind].setText('请检查目录路径或访问权限，然后刷新。')
            window._local_empty_clear[kind].hide()
            continue
        window._local_empty_titles[kind].setText('正在读取文件' if busy else '没有匹配的文件' if total else '此文件夹为空')
        window._local_empty_hints[kind].setText('正在整理目录，完成后会自动显示。' if busy else
            '试试其他关键词，或清除筛选条件。' if total else '此目录中的文件夹、图片和视频会显示在这里。')
        window._local_empty_clear[kind].setVisible(not busy and total > 0)
    window.local_refresh_btn.setText('正在刷新…' if loading else '刷新 · F5')
    selection_summary(window, getattr(window, '_local_active_list', None))


def selection_summary(window, grid):
    if not hasattr(window, 'local_selection_label'):
        return
    if grid not in (window.local_list_in, window.local_list_out) or not grid.isVisible():
        grid = window.local_list_out if window.local_mode_group.checkedId() == 1 else window.local_list_in
    window._local_active_list = grid
    items = [item for item in grid.selectedItems() if not item.isHidden()]
    window.local_preview_btn.setEnabled(any(not (item.data(QtCore.Qt.UserRole) or {}).get('is_dir') for item in items))
    if not items:
        window.local_selection_label.setText('双击文件夹进入 · 空格预览文件 · 双击文件使用系统应用打开')
        return
    source = '输入' if grid is window.local_list_in else '输出'
    if len(items) == 1:
        meta = items[0].data(QtCore.Qt.UserRole) or {}
        detail = '文件夹 · 双击进入' if meta.get('is_dir') else readable_size(meta.get('size_bytes'))
        text = f"{source} · {meta.get('name', items[0].text())} · {detail}"
    else:
        total = sum((item.data(QtCore.Qt.UserRole) or {}).get('size_bytes', 0) for item in items)
        text = f'{source} · 已选 {len(items)} 项 · {readable_size(total)}'
    window.local_selection_label.setText(text)
