"""A passive, owner-scoped view of the shared workflow FIFO."""
from PyQt5 import QtCore, QtGui, QtWidgets, sip

from aetherloom_core.rh_ui import palette
from aetherloom_core.rh_progress import progress_percent
from .graphics import STATUS_NAMES


FINAL_STATES = frozenset({'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN', 'BLOCKED', 'SKIPPED', 'REUSED', 'INTERRUPTED'})
CANCEL_STATES = frozenset({'CANCELING'})
QUEUE_NAMES = dict(STATUS_NAMES, PENDING='等待运行', WAITING='等待运行', QUEUED='等待运行',
                   STARTING='准备运行', CANCELING='取消中', CANCEL_FAILED='取消待确认', REUSED='复用结果')


class WorkflowQueuePanel(QtWidgets.QDialog):
    """Keep rows stable across progress updates and materialize Apps on expand."""
    visibility_changed = QtCore.pyqtSignal(bool)
    GROUP_PAGE_SIZE = 100
    NODE_PAGE_SIZE = 250
    MAX_ROWS = 2500

    def __init__(self, owner, queue):
        super().__init__(owner)
        self.owner, self.queue = owner, queue
        self._items, self._groups_by_id, self._jobs = {}, {}, {}
        self._node_steps = {}
        self._expanded_keys, self._node_offsets, self._page_selections = set(), {}, {}
        self._offset, self._total = 0, 0
        self._updating = False
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
        self.setWindowTitle('画布工作流队列')
        self.setObjectName('workflowQueuePanel')
        self.setModal(False)
        self.setMinimumSize(390, 350)
        self.resize(700, 570)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(17, 16, 17, 15)
        layout.setSpacing(11)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('工作流运行队列')
        title.setObjectName('queueTitle')
        heading.addWidget(title, 1)
        self.collapse_button = QtWidgets.QPushButton('全部折叠')
        self.collapse_button.clicked.connect(self.tree_collapse_all)
        heading.addWidget(self.collapse_button)
        layout.addLayout(heading)
        hint = QtWidgets.QLabel('本次会话按加入顺序运行。展开任务查看 App；多批任务按批次分组。')
        hint.setWordWrap(True)
        hint.setObjectName('queueMuted')
        layout.addWidget(hint)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(['工作流 / App', '状态', '进度'])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setAnimated(False)
        self.tree.setIndentation(20)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemCollapsed.connect(self._collapsed)
        self.tree.itemClicked.connect(self._item_clicked)
        self.tree.itemSelectionChanged.connect(self._sync_selection)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setResizeContentsPrecision(80)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.setTextElideMode(QtCore.Qt.ElideRight)
        layout.addWidget(self.tree, 1)
        self.empty_label = QtWidgets.QLabel('还没有工作流任务。在画布中点击“运行画布”加入队列。')
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName('queueMuted')
        layout.addWidget(self.empty_label)
        navigation = QtWidgets.QHBoxLayout()
        self.previous_button = QtWidgets.QPushButton('上一页')
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        navigation.addWidget(self.previous_button)
        self.page_label = QtWidgets.QLabel()
        self.page_label.setAlignment(QtCore.Qt.AlignCenter)
        self.page_label.setObjectName('queueMuted')
        navigation.addWidget(self.page_label, 1)
        self.next_button = QtWidgets.QPushButton('下一页')
        self.next_button.clicked.connect(lambda: self._change_page(1))
        navigation.addWidget(self.next_button)
        self.navigation = QtWidgets.QWidget()
        self.navigation.setLayout(navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.navigation)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(QtCore.Qt.PlainText)
        self.message.setObjectName('queueWarning')
        self.message.hide()
        layout.addWidget(self.message)
        footer = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName('queueMuted')
        footer.addWidget(self.summary, 1)
        self.cancel_button = QtWidgets.QPushButton('取消所选')
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.cancel_button.setEnabled(False)
        footer.addWidget(self.cancel_button)
        self.hide_button = QtWidgets.QPushButton('隐藏队列')
        self.hide_button.clicked.connect(self.hide)
        footer.addWidget(self.hide_button)
        layout.addLayout(footer)
        self.queue.changed.connect(self._queue_changed)
        if hasattr(self.queue, 'error'):
            self.queue.error.connect(self._queue_error)
        self.refresh_theme()

    def _queue_changed(self, *unused):
        if self.isVisible() and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _queue_error(self, message):
        self.message.setText(str(message))
        self.message.show()

    def _change_page(self, direction):
        selected = self.tree.currentItem()
        if selected is not None:
            self._page_selections[self._offset] = selected.data(0, QtCore.Qt.UserRole)
        last = max(0, (self._total - 1) // self.GROUP_PAGE_SIZE * self.GROUP_PAGE_SIZE)
        offset = max(0, min(last, self._offset + direction * self.GROUP_PAGE_SIZE))
        if offset != self._offset:
            self._offset = offset
            self.refresh()
            item = self._items.get(self._page_selections.get(offset))
            if item is not None:
                self.tree.setCurrentItem(item)

    def tree_collapse_all(self):
        visible_groups = set(self._groups_by_id)
        self._expanded_keys = {key for key in self._expanded_keys if key[1] not in visible_groups}
        blocker = QtCore.QSignalBlocker(self.tree)
        try:
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                item.setExpanded(False)
                self._clear_children(item)
        finally:
            del blocker
        self._sync_selection()

    def _row(self, key, parent, position):
        item = self._items.get(key)
        if item is None:
            if parent is not None and len(self._items) >= self.MAX_ROWS - self.GROUP_PAGE_SIZE:
                self.message.setText('已展开较多任务，请先折叠部分内容再继续展开。')
                self.message.show()
                return None
            item = QtWidgets.QTreeWidgetItem()
            item.setData(0, QtCore.Qt.UserRole, key)
            item.setTextAlignment(2, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self._items[key] = item
            if parent is None:
                self.tree.insertTopLevelItem(position, item)
            else:
                parent.insertChild(position, item)
        return item

    def _set_row(self, item, name, status, progress='', tooltip='', cached=False, cancel_pending=False, dormant=False):
        label = '复用结果' if cached or status == 'REUSED' else str(dormant) if dormant else QUEUE_NAMES.get(status, status)
        for column, value in enumerate((str(name), label, str(progress))):
            if item.text(column) != value:
                item.setText(column, value)
        if item.data(0, QtCore.Qt.UserRole + 1) != status:
            item.setData(0, QtCore.Qt.UserRole + 1, status)
        if item.data(0, QtCore.Qt.UserRole + 2) != cancel_pending:
            item.setData(0, QtCore.Qt.UserRole + 2, cancel_pending)
        if item.toolTip(0) != tooltip:
            for column in range(3):
                item.setToolTip(column, tooltip)
        colors = palette(getattr(self.owner, '_theme_mode', 'dark'))
        color = (colors['muted'] if dormant else colors['success'] if status in ('RUNNING', 'DOWNLOADING', 'SUCCESS', 'REUSED') or cached
                 else colors['danger'] if status == 'FAILED'
                 else colors['warning'] if status not in FINAL_STATES else colors['muted'])
        brush = QtGui.QBrush(QtGui.QColor(color))
        if item.foreground(1) != brush:
            item.setForeground(1, brush)

    def _remove(self, key):
        item = self._items.get(key)
        if item is None:
            return
        self._forget_item(item)
        parent = item.parent()
        if parent is None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        else:
            parent.takeChild(parent.indexOfChild(item))

    def _forget_item(self, item):
        for index in range(item.childCount()):
            self._forget_item(item.child(index))
        key = item.data(0, QtCore.Qt.UserRole)
        self._items.pop(key, None)
        self._jobs.pop(key, None)

    def _clear_children(self, item):
        for child in item.takeChildren():
            self._forget_item(child)

    def refresh(self):
        if not self.isVisible():
            return
        self._refresh_timer.stop()
        page = self.queue.view_groups(offset=self._offset, limit=self.GROUP_PAGE_SIZE)
        self._total = page['total']
        if self._offset and self._offset >= self._total:
            self._offset = max(0, (self._total - 1) // self.GROUP_PAGE_SIZE * self.GROUP_PAGE_SIZE)
            page = self.queue.view_groups(offset=self._offset, limit=self.GROUP_PAGE_SIZE)
        groups = page.get('groups') or []
        self._updating = True
        self.tree.setUpdatesEnabled(False)
        blocker = QtCore.QSignalBlocker(self.tree)
        try:
            present = {str(group['id']) for group in groups}
            for key in list(self._items):
                if key[0] in ('group', 'single') and key[1] not in present:
                    self._remove(key)
            self._groups_by_id = {str(group['id']): group for group in groups}
            for position, group in enumerate(groups):
                group_id = str(group['id'])
                count = group.get('batch_count') or group.get('job_count') or 1
                single_id = str(group.get('single_job_id') or '')
                group_key = ('single', group_id, single_id) if count == 1 else ('group', group_id)
                group_item = self._row(group_key, None, position)
                status = str(group.get('status') or 'PENDING')
                target_title = str(group.get('target_title') or '')
                scope = (target_title + '及其上游' if target_title else '单节点及上游') if group.get('target') else '整张画布'
                title = str(group.get('name') or group.get('canvas_name') or '未命名画布')
                if group.get('target'):
                    title += ' · ' + (target_title or '单节点')
                if count > 1:
                    title += f' · {count} 批'
                self._set_row(group_item, title, status, '', f'{title}\n{scope}\n' + str(group.get('message') or ''),
                              cancel_pending=bool(group.get('cancel_requested') or group.get('single_cancel_requested')) and status in ('WAITING_FOR_KEY', 'CANCELING'))
                if count == 1:
                    self._jobs[group_key] = {'id': single_id, 'status': status}
                    children = group.get('single_node_count', group.get('node_count', 0))
                else:
                    children = group.get('job_count', count)
                group_item.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ShowIndicator if children else QtWidgets.QTreeWidgetItem.DontShowIndicatorWhenChildless)
                group_item.setExpanded(group_key in self._expanded_keys)
                if group_item.isExpanded():
                    self._sync_nodes(group_key) if count == 1 else self._sync_jobs(group_key)
        finally:
            del blocker
            self.tree.setUpdatesEnabled(True)
            self._updating = False
        self.empty_label.setVisible(not self._total)
        self.summary.setText(f'共 {self._total} 个工作流任务')
        pages = max(1, (self._total + self.GROUP_PAGE_SIZE - 1) // self.GROUP_PAGE_SIZE)
        self.page_label.setText(f'第 {self._offset // self.GROUP_PAGE_SIZE + 1} / {pages} 页')
        self.navigation.setVisible(pages > 1)
        self.previous_button.setEnabled(self._offset > 0)
        self.next_button.setEnabled(self._offset + self.GROUP_PAGE_SIZE < self._total)
        self._sync_selection()

    def _sync_jobs(self, group_key):
        page = self.queue.view_jobs(group_key[1], offset=0, limit=100)
        parent = self._items[group_key]
        keep = set()
        for index, job in enumerate(page.get('jobs') or []):
            key = ('job', group_key[1], str(job['id']))
            keep.add(key)
            item = self._row(key, parent, index)
            if item is None:
                break
            self._jobs[key] = job
            status = str(job.get('status') or 'PENDING')
            title = f'第 {int(job.get("index", index)) + 1} 批'
            self._set_row(item, title, status, '', str(job.get('message') or title),
                          cancel_pending=bool(job.get('cancel_requested')) and status in ('WAITING_FOR_KEY', 'CANCELING'))
            item.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ShowIndicator if job.get('node_count') else QtWidgets.QTreeWidgetItem.DontShowIndicatorWhenChildless)
            item.setExpanded(key in self._expanded_keys)
            if item.isExpanded():
                self._sync_nodes(key)
        for index in reversed(range(parent.childCount())):
            key = parent.child(index).data(0, QtCore.Qt.UserRole)
            if key not in keep:
                self._remove(key)

    def _sync_nodes(self, job_key):
        job = self._jobs.get(job_key)
        if job is None:
            return
        parent = self._items[job_key]
        offset = self._node_offsets.get(job_key, 0)
        page = self.queue.view_nodes(job_key[1], job_key[2], offset=offset, limit=self.NODE_PAGE_SIZE)
        total, nodes = page['total'], page.get('nodes') or []
        if offset and offset >= total:
            self._node_offsets[job_key] = 0
            page = self.queue.view_nodes(job_key[1], job_key[2], offset=0, limit=self.NODE_PAGE_SIZE)
            offset, nodes = 0, page.get('nodes') or []
        desired = {('node', job_key[1], job_key[2], str(node.get('id') or node.get('node_id') or index)) for index, node in enumerate(nodes)}
        for index in reversed(range(parent.childCount())):
            key = parent.child(index).data(0, QtCore.Qt.UserRole)
            if key[0] == 'node' and key not in desired:
                self._remove(key)
        keep = set()
        rendered = 0
        for index, node in enumerate(nodes):
            key = ('node', job_key[1], job_key[2], str(node.get('id') or node.get('node_id') or index))
            if key not in self._items and len(self._items) >= self.MAX_ROWS - self.GROUP_PAGE_SIZE - 2:
                self.message.setText('已展开较多任务，请折叠部分内容以查看更多 App。')
                self.message.show()
                break
            item = self._row(key, parent, index)
            if item is None:
                break
            keep.add(key)
            rendered += 1
            status = str(node.get('status') or 'PENDING')
            cached = bool(node.get('cached'))
            dormant = ('未执行' if job.get('status') in FINAL_STATES else '待执行') if (
                not cached and status in ('PENDING', 'WAITING') and not node.get('activated')) else ''
            percent = '' if dormant else '100%' if cached else f'{progress_percent(status, node.get("progress"))}%' if node.get('progress') is not None else ''
            title = str(node.get('title') or node.get('name') or node.get('webapp_id') or 'App')
            task_ids = node.get('task_ids') or ([node['task_id']] if node.get('task_id') else [])
            tooltip = title + '\n' + str(node.get('message') or '')
            if task_ids:
                tooltip += '\nTask ID：' + ', '.join(map(str, task_ids[:10]))
            elif cached:
                tooltip += '\n使用已有结果，本批未提交云端任务。'
            else:
                tooltip += '\n尚未返回 Task ID。'
            self._set_row(item, title, status, percent, tooltip, cached, dormant=dormant)
        self._node_steps[job_key] = rendered
        for direction, visible, label in (
                ('previous', offset > 0, '‹ 上一页 App'),
                ('next', bool(rendered and offset + rendered < total), f'下一页 App ›  ({offset + rendered} / {total})')):
            if visible:
                key = ('nodes_' + direction, job_key[1], job_key[2])
                item = self._row(key, parent, parent.childCount())
                if item is not None:
                    keep.add(key)
                    self._set_row(item, label, 'SUCCESS', '', '点击翻页，只加载当前页的 App 状态。')
                    item.setText(1, '')
                    item.setForeground(0, QtGui.QBrush(QtGui.QColor(palette(getattr(self.owner, '_theme_mode', 'dark'))['accent'])))
        for index in reversed(range(parent.childCount())):
            key = parent.child(index).data(0, QtCore.Qt.UserRole)
            if key not in keep:
                self._remove(key)

    def _expanded(self, item):
        key = item.data(0, QtCore.Qt.UserRole)
        if not self._updating and key and key[0] in ('group', 'single', 'job'):
            self._expanded_keys.add(key)
            updating = self.tree.updatesEnabled()
            self.tree.setUpdatesEnabled(False)
            blocker = QtCore.QSignalBlocker(self.tree)
            try:
                self._sync_jobs(key) if key[0] == 'group' else self._sync_nodes(key)
            finally:
                del blocker
                self.tree.setUpdatesEnabled(updating)

    def _collapsed(self, item):
        if self._updating:
            return
        key = item.data(0, QtCore.Qt.UserRole)
        if key:
            self._expanded_keys.discard(key)
        current = self.tree.currentItem()
        ancestor = current.parent() if current else None
        while ancestor is not None:
            if ancestor is item:
                self.tree.setCurrentItem(item)
                break
            ancestor = ancestor.parent()
        self._clear_children(item)
        self._queue_changed()

    def _item_clicked(self, item, column):
        key = item.data(0, QtCore.Qt.UserRole)
        if not key or key[0] not in ('nodes_previous', 'nodes_next'):
            return
        parent = item.parent()
        parent_key = parent.data(0, QtCore.Qt.UserRole)
        offset = self._node_offsets.get(parent_key, 0)
        self._node_offsets[parent_key] = (max(0, offset - self.NODE_PAGE_SIZE) if key[0] == 'nodes_previous'
                                          else offset + self._node_steps.get(parent_key, self.NODE_PAGE_SIZE))
        self.tree.setCurrentItem(parent)
        self._expanded(parent)

    def _selected(self):
        selected = self.tree.selectedItems()
        item = selected[0] if selected else None
        key = item.data(0, QtCore.Qt.UserRole) if item else None
        status = item.data(0, QtCore.Qt.UserRole + 1) if item else None
        return item, key, status

    def _sync_selection(self):
        item, key, status = self._selected()
        self.cancel_button.setEnabled(bool(key and key[0] in ('group', 'single', 'job') and status not in FINAL_STATES | CANCEL_STATES
                                           and not item.data(0, QtCore.Qt.UserRole + 2)))
        self.cancel_button.setText('取消所选组' if key and key[0] == 'group' else '取消所选任务' if key and key[0] == 'single'
                                   else '取消所选批次' if key and key[0] == 'job' else '取消所选')

    def _cancel_selected(self):
        item, key, status = self._selected()
        if (not key or key[0] not in ('group', 'single', 'job') or status in FINAL_STATES | CANCEL_STATES
                or item.data(0, QtCore.Qt.UserRole + 2)):
            return
        try:
            errors = self.queue.cancel_group(key[1]) if key[0] == 'group' else self.queue.cancel_job(key[1], key[2])
            self.message.setText('部分取消请求暂未确认，将继续重试。' if errors else '已请求取消；提交过的任务将等待服务端确认。')
            self.message.show()
        except (OSError, ValueError, RuntimeError) as error:
            self.message.setText('取消请求未完成：' + str(error))
            self.message.show()
        self.refresh()

    def _context_menu(self, point):
        item = self.tree.itemAt(point)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        unused, key, status = self._selected()
        if not key or key[0] not in ('group', 'single', 'job'):
            return
        menu = QtWidgets.QMenu(self)
        action = menu.addAction('取消整个工作流组' if key[0] == 'group' else '取消此工作流' if key[0] == 'single' else '取消此批次', self._cancel_selected)
        action.setEnabled(status not in FINAL_STATES | CANCEL_STATES and not item.data(0, QtCore.Qt.UserRole + 2))
        menu.exec_(self.tree.viewport().mapToGlobal(point))

    def showEvent(self, event):
        self.refresh_theme()
        self.refresh()
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        self._refresh_timer.stop()
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def refresh_theme(self):
        p = palette(getattr(self.owner, '_theme_mode', 'dark'))
        self.setStyleSheet(f'''
            QDialog#workflowQueuePanel {{ background: {p['surface']}; color: {p['text']}; }}
            QDialog#workflowQueuePanel QLabel {{ color: {p['text']}; background: transparent; }}
            QLabel#queueTitle {{ font-size: 19px; font-weight: 700; }}
            QLabel#queueMuted {{ color: {p['muted']}; }}
            QLabel#queueWarning {{ color: {p['warning']}; }}
            QTreeWidget {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 7px; outline: none; }}
            QTreeWidget::item {{ height: 31px; padding: 3px; }}
            QTreeWidget::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
            QHeaderView::section {{ background: {p['surface']}; color: {p['muted']}; border: none; border-bottom: 1px solid {p['border']}; padding: 8px 5px; }}
            QPushButton {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 8px 12px; }}
            QPushButton:hover {{ background: {p['hover']}; }}
            QPushButton:disabled {{ color: {p['muted']}; }}
        ''')
        colors = self.tree.palette()
        for role in (QtGui.QPalette.Text, QtGui.QPalette.WindowText, QtGui.QPalette.ButtonText):
            colors.setColor(role, QtGui.QColor(p['text']))
        self.tree.setPalette(colors)


def show_workflow_queue(owner, queue):
    dialog = getattr(owner, '_canvas_workflow_queue_panel', None)
    if dialog is None or sip.isdeleted(dialog):
        dialog = WorkflowQueuePanel(owner, queue)
        owner._canvas_workflow_queue_panel = dialog
    screen = QtWidgets.QApplication.screenAt(owner.mapToGlobal(owner.rect().center())) or QtWidgets.QApplication.primaryScreen()
    if screen:
        area = screen.availableGeometry()
        dialog.resize(min(dialog.width(), max(390, min(owner.width() - 30, area.width() - 30))),
                      min(dialog.height(), max(350, min(owner.height() - 30, area.height() - 40))))
    dialog.refresh_theme()
    dialog.refresh()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
