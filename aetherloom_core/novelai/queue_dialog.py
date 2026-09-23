"""Session-only NovelAI queue inspector with virtual, lightweight task rows."""
import html
import math
import re
import time
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets
from .styles import workspace_palette as palette
from .references import guard_wheel


STATES = {
    'queued': '等待准备', 'preparing': '准备输入', 'ready': '等待提交', 'retry_wait': '等待轮试',
    'running': '正在生成', 'saving': '保存图片', 'save_failed': '等待重新保存',
    'succeeded': '已完成', 'failed': '失败', 'canceling': '正在停止', 'cancelled': '已取消',
}
FINISHED = frozenset(('succeeded', 'failed', 'cancelled'))
CANCELLABLE = frozenset(('queued', 'preparing', 'ready', 'retry_wait', 'running'))
TABS = ('重试中', '执行中', '已拒绝', '已完成')
PAGE_SIZE = 100


def _bucket(task):
    state = task.get('state')
    if state == 'succeeded':
        return 3
    if state in ('queued', 'preparing', 'ready', 'retry_wait') or (state == 'running' and not task.get('accepted')):
        return 0
    if state in ('running', 'saving', 'save_failed', 'canceling'):
        return 1
    return 2


ACTIONS = {'generate': '文生图', 'img2img': '图生图', 'infill': '局部重绘',
           'augment': 'Director Tools', 'upscale': '超分辨率'}
_SECRET = re.compile(r'''(?i)\b(?:api[_-]?key|access[_-]?token|authorization|token|input[_-]?base64|image[_-]?base64|mask[_-]?base64)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''')
_BEARER = re.compile(r'(?i)\bBearer\s+[^\s,;]+')
_ENCODED = re.compile(r'data:image/[^\s,;]+;base64,[A-Za-z0-9+/=]+|[A-Za-z0-9+/=_-]{180,}', re.I)


def _text(value, limit=700):
    """Never stringify arbitrary task payloads or display credentials/base64."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    value = str(value)[:max(limit * 4, 4096)]
    value = _SECRET.sub('[已隐藏敏感内容]', value)
    value = _BEARER.sub('Bearer [已隐藏]', value)
    value = _ENCODED.sub('[已隐藏图像或编码数据]', value)
    value = ''.join(c for c in value if c in '\n\t' or ord(c) >= 32)
    return value[:limit] + ('…' if len(value) > limit else '')


def _stamp(value, full=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return '—'
    try:
        return time.strftime('%Y/%m/%d %H:%M:%S' if full else '%H:%M:%S', time.localtime(value))
    except (ValueError, OverflowError, OSError):
        return '—'


def _progress(value):
    if not isinstance(value, dict):
        return _text(value, 180)
    message = _text(value.get('message'), 180)
    fraction = value.get('progress')
    if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and math.isfinite(fraction) and 0 <= fraction <= 1:
        label = f'{round(fraction * 100)}%'
        # This is the service's reported progress, not an estimate from sampling steps.
        return label + (' · ' + message if message else '')
    return message


def _state_label(task):
    if task.get('result_unknown'):
        return ('已停止接收' if task['state'] == 'cancelled' else STATES.get(task['state'], '未知状态')) + ' · 结果未知'
    if task['state'] == 'cancelled' and task.get('submitted'):
        return '已停止接收'
    if task['state'] == 'running' and not task.get('accepted'):
        return '正在提交'
    return STATES.get(task['state'], '未知状态')


def _summary(task, position):
    results = task.get('results')
    def integer(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    attempts = integer(task.get('attempts', 0))
    key_index = integer(task.get('key_index', 0))
    key_count = integer(task.get('key_count', 0))
    return {
        'id': _text(task.get('id'), 160), 'index': _text(task.get('index', position + 1), 24),
        'submitted': task.get('submitted') is True, 'accepted': task.get('accepted') is True,
        'result_unknown': task.get('result_unknown') is True,
        'record_path': _text(task.get('record_path'), 2000), 'record_error': _text(task.get('record_error')),
        'state': _text(task.get('state'), 40), 'title': _text(task.get('title'), 180) or '图像任务',
        'model': _text(task.get('model'), 180), 'action': _text(task.get('action'), 40),
        'attempts': attempts, 'key_index': key_index, 'key_count': key_count,
        'connection': f'密钥 {key_index}' if key_index else '待分配',
        'next_retry': _stamp(task.get('next_retry')) if task.get('next_retry') else '',
        'created': _stamp(task.get('created')), 'created_full': _stamp(task.get('created'), True),
        'message': _text(task.get('message')), 'progress': _progress(task.get('progress')),
        'result_count': len(results) if isinstance(results, list) else 0,
    }


class QueueTableModel(QtCore.QAbstractTableModel):
    HEADERS = ('序号', '任务', '状态', '连接', '服务进度', '已保存', '发起时间')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.colors = palette()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orientation == QtCore.Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        task = self.rows[index.row()]
        if role == QtCore.Qt.UserRole:
            return task['id']
        if role == QtCore.Qt.DisplayRole:
            return (task['index'], task['title'], _state_label(task), task['connection'],
                    ('记录保存失败 · ' if task['record_error'] else '') + (task['progress'] or task['message'] or '—'), str(task['result_count']), task['created'])[index.column()]
        if role == QtCore.Qt.ToolTipRole:
            lines = [task['title'], _state_label(task), task['progress'] or task['message'],
                     f"{task['connection']} · 已尝试 {task['attempts']} 次", task['record_error']]
            return '<br>'.join(html.escape(v) for v in lines if v)
        if role == QtCore.Qt.SizeHintRole:
            return QtCore.QSize(0, 40)
        if role == QtCore.Qt.TextAlignmentRole and index.column() in (0, 3, 5, 6):
            return int(QtCore.Qt.AlignCenter)
        if role == QtCore.Qt.ForegroundRole and index.column() == 2:
            tone = ('danger' if task['state'] == 'failed' else 'warning' if task['state'] in ('save_failed', 'retry_wait') else
                    'success' if task['state'] == 'succeeded' else 'accent' if task['state'] in ('running', 'saving') else 'muted')
            return QtGui.QColor(self.colors[tone])
        return None

    def refresh(self, tasks):
        rows = [_summary(task, i) for i, task in enumerate(tasks) if isinstance(task, dict)]
        ids, previous = [r['id'] for r in rows], [r['id'] for r in self.rows]
        if ids == previous:
            changed = [i for i, row in enumerate(rows) if row != self.rows[i]]
            self.rows = rows
            if changed:
                self.dataChanged.emit(self.index(changed[0], 0), self.index(changed[-1], len(self.HEADERS) - 1))
            return False
        if len(ids) > len(previous) and ids[:len(previous)] == previous:
            count = len(previous)
            self.beginInsertRows(QtCore.QModelIndex(), count, len(rows) - 1)
            self.rows = rows
            self.endInsertRows()
            if count:
                self.dataChanged.emit(self.index(0, 0), self.index(count - 1, len(self.HEADERS) - 1))
            return False
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()
        return True

    def apply_theme(self, mode):
        self.colors = palette(mode)
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.HEADERS) - 1), [QtCore.Qt.ForegroundRole])


class QueueDialog(QtWidgets.QDialog):
    selected = QtCore.pyqtSignal(dict)
    copyRequested = QtCore.pyqtSignal(str)
    configurationChanged = QtCore.pyqtSignal(int)

    def __init__(self, service, parent=None, mode='dark'):
        super().__init__(parent)
        self.service = service
        self._mode = mode
        self._compact_layout = None
        self._refreshing = False
        self._view_tab = 0
        self._pages = [0] * len(TABS)
        self._selected_identity_cache = set()
        self._current_by_tab = [None] * len(TABS)
        self._theme_timer = QtCore.QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.timeout.connect(lambda: self.apply_theme(self._mode))
        self.setObjectName('novelaiQueueDialog')
        self.setWindowTitle('NovelAI 任务队列')
        self.setMinimumSize(660, 440)
        self.resize(930, 650)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)
        box = QtWidgets.QVBoxLayout(self)
        # The expanded layout hint must not prevent the first resize into compact mode.
        box.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        box.setContentsMargins(20, 18, 20, 16)
        box.setSpacing(12)
        title = QtWidgets.QLabel('任务队列')
        title.setObjectName('queueTitle')
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(title, 1)
        concurrency_label = QtWidgets.QLabel('并行任务')
        heading.addWidget(concurrency_label)
        self.concurrency = guard_wheel(QtWidgets.QComboBox())
        self.concurrency.setAccessibleName('并行任务上限')
        self.concurrency.setFixedWidth(100)
        for value in (1, 2, 3):
            self.concurrency.addItem('3 个（默认）' if value == 3 else f'{value} 个', value)
        self.concurrency.setToolTip('尚未结束的任务上限，默认 3。提交始终按顺序：队首请求被接受后，下一项才提交；已接受的任务可并行接收结果。')
        self.concurrency.currentIndexChanged.connect(self._concurrency_changed)
        concurrency_label.setBuddy(self.concurrency)
        heading.addWidget(self.concurrency)
        retry_label = QtWidgets.QLabel('轮试间隔')
        heading.addWidget(retry_label)
        self.retry_interval = guard_wheel(QtWidgets.QSpinBox())
        self.retry_interval.setAccessibleName('并发受限任务轮试间隔')
        self.retry_interval.setRange(1, 300)
        self.retry_interval.setSuffix(' 秒')
        self.retry_interval.setFixedWidth(82)
        self.retry_interval.setToolTip('所有密钥均明确并发受限后，队首等待此间隔再轮试。默认 5 秒。')
        retry_label.setBuddy(self.retry_interval)
        self.retry_interval.valueChanged.connect(self._retry_interval_changed)
        heading.addWidget(self.retry_interval)
        box.addLayout(heading)
        hint = self.hint = QtWidgets.QLabel('队首受理后才提交下一项；已受理任务可并行接收结果。关闭窗口不会停止队列。')
        hint.setObjectName('queueMuted')
        hint.setWordWrap(True)
        box.addWidget(hint)
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('queueSummary')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.PlainText)
        box.addWidget(self.summary)
        self.pause_panel = QtWidgets.QFrame()
        self.pause_panel.setObjectName('queuePausePanel')
        pause_row = QtWidgets.QHBoxLayout(self.pause_panel)
        pause_row.setContentsMargins(10, 8, 10, 8)
        pause_row.setSpacing(10)
        self.pause_hint = QtWidgets.QLabel()
        self.pause_hint.setObjectName('queuePauseHint')
        self.pause_hint.setWordWrap(True)
        self.pause_hint.setTextFormat(QtCore.Qt.PlainText)
        pause_row.addWidget(self.pause_hint, 1)
        self.resume_btn = QtWidgets.QPushButton('继续等待任务')
        self.resume_btn.setAutoDefault(False)
        self.resume_btn.setToolTip('恢复尚未提交的等待任务，不会重试已经失败的请求。')
        self.resume_btn.clicked.connect(lambda: self._call(self.service.resume_dispatch))
        pause_row.addWidget(self.resume_btn)
        box.addWidget(self.pause_panel)
        self.tabs = QtWidgets.QTabBar()
        self.tabs.setObjectName('queueTabs')
        self.tabs.setExpanding(True)
        self.tabs.setDrawBase(False)
        tips = ('本地等待、准备、轮试及尚未受理的提交。',
                '已受理的生成、本地保存、保存失败待恢复及正在停止。',
                '失败或已取消的任务；不代表所有请求都被服务端拒绝，具体以任务状态和说明为准。',
                '已完成并保存结果的任务；双击使用本地工具打开图片。')
        for label, tip in zip(TABS, tips):
            index = self.tabs.addTab(label + ' (0)')
            self.tabs.setTabToolTip(index, tip)
        self.tabs.currentChanged.connect(self._tab_changed)
        box.addWidget(self.tabs)
        pagination = QtWidgets.QHBoxLayout()
        pagination.setSpacing(8)
        self.page_info = QtWidgets.QLabel()
        self.page_info.setObjectName('queueMuted')
        pagination.addWidget(self.page_info, 1)
        self.prev_btn = QtWidgets.QPushButton('上一页')
        self.prev_btn.setAutoDefault(False)
        self.prev_btn.clicked.connect(lambda: self.page_number.setValue(self.page_number.value() - 1))
        pagination.addWidget(self.prev_btn)
        self.page_number = guard_wheel(QtWidgets.QSpinBox())
        self.page_number.setAccessibleName('当前任务页码')
        self.page_number.setRange(1, 1)
        self.page_number.setPrefix('第 ')
        self.page_number.setSuffix(' 页')
        self.page_number.setFixedWidth(110)
        self.page_number.valueChanged.connect(self._page_changed)
        pagination.addWidget(self.page_number)
        self.next_btn = QtWidgets.QPushButton('下一页')
        self.next_btn.setAutoDefault(False)
        self.next_btn.clicked.connect(lambda: self.page_number.setValue(self.page_number.value() + 1))
        pagination.addWidget(self.next_btn)
        box.addLayout(pagination)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.setMinimumHeight(64)
        self.table = QtWidgets.QTreeView()
        self.table.setAccessibleName('NovelAI 当前会话任务')
        self.table.setRootIsDecorated(False)
        self.table.setItemsExpandable(False)
        self.table.setUniformRowHeights(True)
        self.table.setAllColumnsShowFocus(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setTextElideMode(QtCore.Qt.ElideRight)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.model = QueueTableModel(self)
        self.table.setModel(self.model)
        header = self.table.header()
        header.setStretchLastSection(False)
        for col, width in enumerate((46, 166, 96, 88, 175, 58, 94)):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.Interactive)
            header.resizeSection(col, width)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.doubleClicked.connect(self.open_result)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.stack.addWidget(self.table)
        empty = self.empty = QtWidgets.QLabel()
        empty.setObjectName('queueEmpty')
        empty.setAlignment(QtCore.Qt.AlignCenter)
        empty.setWordWrap(True)
        self.stack.addWidget(empty)
        self.splitter.addWidget(self.stack)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName('所选任务详情')
        self.details.setPlaceholderText('选择任务查看详情；双击已完成任务，用本地工具打开图片。')
        self.details.setMinimumHeight(36)
        self.splitter.addWidget(self.details)
        self.splitter.setSizes([330, 150])
        box.addWidget(self.splitter, 1)
        actions = QtWidgets.QHBoxLayout()
        self.cancel_btn = QtWidgets.QPushButton('取消选中')
        self.cancel_btn.setToolTip('可用 Ctrl 或 Shift 多选。等待任务将取消，已提交请求只停止本地等待。')
        self.cancel_btn.clicked.connect(self.cancel_selected)
        actions.addWidget(self.cancel_btn)
        self.resave_btn = QtWidgets.QPushButton('重新保存')
        self.resave_btn.setToolTip('选择新目录保存已经生成的图片；不会再次请求生成。')
        self.resave_btn.clicked.connect(self.resave_selected)
        actions.addWidget(self.resave_btn)
        self.clear_btn = QtWidgets.QPushButton('清理已结束')
        self.clear_btn.setToolTip('清理已结束任务及其任务记录 JSON，保留生成图片；待重新保存的任务会保留。')
        self.clear_btn.clicked.connect(lambda: self._call(self.service.clear_finished))
        actions.addWidget(self.clear_btn)
        actions.addStretch(1)
        self.stop_btn = QtWidgets.QPushButton('全部停止')
        self.stop_btn.setObjectName('queueStop')
        self.stop_btn.setToolTip('取消等待任务并停止当前本地等待；待重新保存的图片会保留。')
        self.stop_btn.clicked.connect(lambda: self._call(self.service.cancel_all))
        actions.addWidget(self.stop_btn)
        close = QtWidgets.QPushButton('关闭')
        close.clicked.connect(self.close)
        actions.addWidget(close)
        for button in (self.cancel_btn, self.resave_btn, self.clear_btn, self.stop_btn, close):
            button.setAutoDefault(False)
        box.addLayout(actions)
        self.service.changed.connect(self._schedule_refresh)
        self.apply_theme(mode)
        self.refresh()

    def minimumSizeHint(self):
        # Windows can deliver a delayed native size correction from the expanded
        # layout. Compact layout owns the content minimum at this window size.
        return QtCore.QSize(660, 440)

    def _schedule_refresh(self):
        if self.isVisible() and not self._refresh_timer.isActive():
            self._refresh_timer.start(150)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        # The parent page may polish newly shown child views after construction.
        # Reapply once after that pass so native item-view palettes match the theme.
        self._theme_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, 'details'):
            return
        self.table.setColumnHidden(6, self.width() < 800)
        compact = self.height() < 530
        if compact == self._compact_layout:
            return
        self._compact_layout = compact
        self.hint.setVisible(not compact)
        self.summary.setVisible(not compact)
        self.layout().setContentsMargins(*(14, 10, 14, 10) if compact else (20, 18, 20, 16))
        self.layout().setSpacing(6 if compact else 12)
        self.stack.setMinimumHeight(64)
        self.details.setMinimumHeight(36)
        # Preserve task visibility when a pause banner appears in a short window.
        # Only reset on crossing the layout threshold, not every user resize.
        self.splitter.setSizes([260, 80] if compact else [330, 150])

    def _selected_ids(self):
        return [index.data(QtCore.Qt.UserRole) for index in self.table.selectionModel().selectedRows()]

    def _selected_rows(self):
        return [self.model.rows[index.row()] for index in self.table.selectionModel().selectedRows()
                if 0 <= index.row() < len(self.model.rows)]

    def _task(self, identity):
        if hasattr(self.service, 'get_task'):
            return self.service.get_task(identity)
        return next((task for task in self.service.tasks if task.get('id') == identity), None)

    def _remember_selection(self):
        if self._refreshing:
            return
        visible = {row['id'] for row in self.model.rows}
        self._selected_identity_cache.difference_update(visible)
        self._selected_identity_cache.update(self._selected_ids())
        current = self.table.currentIndex().data(QtCore.Qt.UserRole)
        if current:
            self._current_by_tab[self._view_tab] = current

    def _tab_changed(self, index):
        if self._refreshing or not 0 <= index < len(TABS):
            return
        self._remember_selection()
        self._view_tab = index
        self.refresh(remember=False)

    def _page_changed(self, page):
        if self._refreshing:
            return
        self._remember_selection()
        self._pages[self._view_tab] = page - 1
        self.refresh(remember=False)

    def refresh(self, *, remember=True):
        self._refresh_timer.stop()
        if remember:
            self._remember_selection()
        self._refreshing = True
        try:
            concurrency = getattr(self.service, 'concurrency', 3)
            with QtCore.QSignalBlocker(self.concurrency):
                self.concurrency.setCurrentIndex(self.concurrency.findData(concurrency))
            with QtCore.QSignalBlocker(self.retry_interval):
                self.retry_interval.setValue(int(getattr(self.service, 'retry_interval', 5)))
            # Inspect cheap state fields for all tasks, but summarize at most one
            # visible page. Progress signals must not serialize the whole queue.
            tasks = [task for task in self.service.tasks if isinstance(task, dict)]
            groups = [[] for _ in TABS]
            locations = {}
            for task in tasks:
                group = _bucket(task)
                groups[group].append(task)
                if isinstance(task.get('id'), str):
                    locations[task['id']] = group
            self._selected_identity_cache.intersection_update(locations)
            for identity in tuple(self._current_by_tab):
                if identity in locations:
                    self._current_by_tab[locations[identity]] = identity
            for index, group in enumerate(groups):
                self.tabs.setTabText(index, f'{TABS[index]} ({len(group)})')
            group = groups[self._view_tab]
            total_pages = max(1, math.ceil(len(group) / PAGE_SIZE))
            page = self._pages[self._view_tab] = min(self._pages[self._view_tab], total_pages - 1)
            with QtCore.QSignalBlocker(self.page_number):
                self.page_number.setRange(1, total_pages)
                self.page_number.setValue(page + 1)
            self.prev_btn.setEnabled(page > 0)
            self.next_btn.setEnabled(page < total_pages - 1)
            self.page_info.setText(f'共 {len(group)} 项 · {total_pages} 页 · 每页 {PAGE_SIZE} 项')
            scroll = self.table.verticalScrollBar().value()
            self.model.refresh(group[page * PAGE_SIZE:(page + 1) * PAGE_SIZE])
            selection = self.table.selectionModel()
            with QtCore.QSignalBlocker(selection):
                selection.clearSelection()
                selection.setCurrentIndex(QtCore.QModelIndex(), QtCore.QItemSelectionModel.NoUpdate)
                for row, task in enumerate(self.model.rows):
                    index = self.model.index(row, 0)
                    if task['id'] in self._selected_identity_cache:
                        selection.select(index, QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
                    if task['id'] == self._current_by_tab[self._view_tab]:
                        selection.setCurrentIndex(index, QtCore.QItemSelectionModel.NoUpdate)
                if self.model.rows and not self.table.currentIndex().isValid():
                    selected = selection.selectedRows()
                    index = selected[0] if selected else self.model.index(0, 0)
                    selection.setCurrentIndex(index, QtCore.QItemSelectionModel.NoUpdate)
                    if not selected:
                        selection.select(index, QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
            self.table.verticalScrollBar().setValue(scroll)
            self.stack.setCurrentIndex(0 if self.model.rows else 1)
            self.empty.setText('此分类暂无任务\n\n' + ('失败、已取消及结果未知的任务会显示在这里。' if self._view_tab == 2 else '任务状态变化后会自动归入对应分类。'))
            ended = sum(task.get('state') in FINISHED for task in tasks)
            active = getattr(self.service, 'active_count', len(groups[1]))
            self.summary.setText(f'共 {len(tasks)} 项 · 活跃 {active} · 已结束 {ended}')
            save_failed = any(task.get('state') == 'save_failed' for task in tasks)
            unsaved = save_failed or bool(getattr(self.service, 'has_unsaved', False))
            rate_limited = bool(getattr(self.service, 'rate_limited', False))
            paused = bool(getattr(self.service, 'paused', unsaved))
            messages = []
            if save_failed:
                messages.append('保存失败，队列已暂停。请到“执行中”选择待保存任务，重新保存；不会再次生成。')
            elif unsaved:
                messages.append('正在保存生成的图片；保存完成后才能继续等待任务。')
            if rate_limited:
                messages.append('已触发限流，等待任务暂停。稍后可继续等待任务；失败请求不会自动重试。')
            if paused and not messages:
                messages.append('队列已暂停，正在处理待保存图片。')
            self.pause_hint.setText('\n'.join(messages))
            self.pause_panel.setVisible(bool(messages))
            self.resume_btn.setVisible(rate_limited)
            self.resume_btn.setEnabled(rate_limited and not unsaved)
            self.clear_btn.setEnabled(bool(ended))
            self.stop_btn.setEnabled(any(task.get('state') in CANCELLABLE for task in tasks))
        finally:
            self._refreshing = False
        self._selection_changed()

    def _selection_changed(self, *_):
        if self._refreshing:
            return
        self._remember_selection()
        rows = self._selected_rows()
        cancellable = sum(row['state'] in CANCELLABLE for row in rows)
        self.cancel_btn.setEnabled(bool(cancellable))
        self.cancel_btn.setText(f'取消选中 ({cancellable})' if cancellable else '取消选中')
        can_resave = len(rows) == 1 and rows[0]['state'] == 'save_failed'
        self.resave_btn.setVisible(can_resave)
        active = getattr(self.service, 'active_count', 0)
        self.resave_btn.setEnabled(can_resave and not active)
        self.resave_btn.setToolTip('请等待当前活跃任务结束，再重新保存。' if active else
                                  '选择新目录保存已经生成的图片；不会再次请求生成。')
        if len(rows) == 1:
            row = rows[0]
            lines = [f"任务 #{row['index']} · {row['title']}", f"状态：{_state_label(row)}",
                     f"模型：{row['model'] or '—'}", f"模式：{ACTIONS.get(row['action'], row['action']) or '—'}",
                     f"连接：{row['connection']} · 共 {row['key_count']} 个密钥（按发起时的顺序）",
                     f"已尝试：{row['attempts']} 次",
                     f"发起：{row['created_full']}", f"已保存：{row['result_count']} 张"]
            if row['record_path']:
                lines.append('任务记录：' + row['record_path'])
            if row['record_error']:
                lines.append('任务记录保存失败：' + row['record_error'])
            if row['result_unknown']:
                lines.append('最终结果未确认；此状态不等同于服务端明确拒绝受理。')
            if row['state'] == 'retry_wait':
                lines.append('轮试：等待队首按密钥顺序重新尝试；任务已被服务端明确拒绝受理。')
                if row['next_retry']:
                    lines.append('下次轮试：' + row['next_retry'])
            if row['progress']:
                lines.append('服务进度：' + row['progress'])
            if row['message'] and row['message'] != row['progress']:
                lines.append('说明：' + row['message'])
            if row['state'] == 'cancelled' and row['submitted']:
                lines.append('已停止本地接收；已经提交的请求可能继续处理。')
            if row['state'] == 'save_failed':
                lines.append('生成已完成，图片等待本地保存；队列已暂停。点击“重新保存”选择目录，不会重复生成。')
            if row['result_count']:
                lines.append('右键可选择已保存图片，用本地工具打开；已完成任务可双击打开第一张现存图片。')
            lines.append('右键“复制任务到主页”仅回填参数，不会自动提交。')
            text = '\n'.join(lines)
        elif rows:
            text = f'已选择 {len(rows)} 个任务，其中 {cancellable} 个可以取消。\n\n' + '\n'.join(
                f"#{r['index']}  {_state_label(r)}  {r['title']}" for r in rows[:30])
            if len(rows) > 30:
                text += f'\n…另有 {len(rows) - 30} 项'
        else:
            text = ''
        if self.details.toPlainText() != text:
            self.details.setPlainText(text)

    def _call(self, operation):
        try:
            operation()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, '队列操作未完成', _text(str(error)) or '请检查任务状态后重试。')
        self.refresh()

    def _concurrency_changed(self, _index):
        value = self.concurrency.currentData()
        if value not in (1, 2, 3) or value == getattr(self.service, 'concurrency', 3):
            return
        def update():
            self.service.set_concurrency(value)
            self.configurationChanged.emit(self.service.concurrency)
        self._call(update)

    def _retry_interval_changed(self, value):
        if value == getattr(self.service, 'retry_interval', 5):
            return
        def update():
            self.service.set_retry_interval(value)
            self.configurationChanged.emit(self.service.concurrency)
        self._call(update)

    def cancel_selected(self):
        identities = [row['id'] for row in self._selected_rows() if row['state'] in CANCELLABLE]
        def cancel():
            for identity in identities:
                self.service.cancel(identity)
        self._call(cancel)

    def resave_selected(self):
        rows = self._selected_rows()
        if len(rows) != 1 or rows[0]['state'] != 'save_failed':
            return
        identity = rows[0]['id']
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, '选择生成图片的保存目录')
        if directory:
            self._call(lambda: self.service.resave(identity, directory))

    @staticmethod
    def _image_path(record):
        if not isinstance(record, dict) or not isinstance(record.get('path'), str) or not record['path']:
            return None
        try:
            path = Path(record['path']).expanduser()
            if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.avif'):
                return None
            return path.resolve() if path.is_file() else None
        except (OSError, ValueError, RuntimeError):
            return None

    def _open_record(self, record):
        path = self._image_path(record)
        if path is None:
            QtWidgets.QMessageBox.information(self, '图片无法打开', '图片文件已移动、删除或无法读取，请检查保存目录。')
            return False
        try:
            opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        except Exception:
            opened = False
        if not opened:
            QtWidgets.QMessageBox.warning(self, '图片无法打开', '未能启动本地图片工具，请检查文件关联或从保存目录打开。')
        return bool(opened)

    def open_result(self, index):
        task = self._task(index.data(QtCore.Qt.UserRole))
        if not isinstance(task, dict) or task.get('state') != 'succeeded':
            return
        for record in task.get('results') or []:
            if self._image_path(record) is not None:
                self._open_record(record)
                return
        QtWidgets.QMessageBox.information(self, '没有可打开的图片', '此任务的图片文件已移动、删除或未找到，请检查保存目录。')

    def _copy_task(self, identity):
        if self._task(identity) is None:
            QtWidgets.QMessageBox.information(self, '任务已移除', '任务记录已被清理，无法复制到主页。')
            return
        self.copyRequested.emit(identity)

    def _create_context_menu(self, identity):
        task = self._task(identity)
        if not isinstance(task, dict):
            return None
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction('复制任务到主页')
        copy_action.setToolTip('只回填参数与可用输入，不会自动提交生成。')
        copy_action.triggered.connect(lambda unused=False, value=identity: self._copy_task(value))
        results = task.get('results')
        if isinstance(results, list) and results:
            images = menu.addMenu('用本地工具打开图片')
            for number, record in enumerate(results, 1):
                if not isinstance(record, dict) or not isinstance(record.get('path'), str):
                    continue
                try:
                    name = Path(record['path']).name
                except (ValueError, OSError):
                    name = ''
                action = images.addAction(f'{number}. ' + (_text(name, 100) or '图片'))
                action.triggered.connect(lambda unused=False, value=record: self._open_record(value))
        return menu

    def _context_menu(self, position):
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        if not self.table.selectionModel().isSelected(index):
            self.table.setCurrentIndex(index)
        menu = self._create_context_menu(index.data(QtCore.Qt.UserRole))
        if menu is not None:
            menu.exec_(self.table.viewport().mapToGlobal(position))
            menu.deleteLater()

    def apply_theme(self, mode):
        self._mode = mode
        p = palette(mode)
        icon_theme = 'light' if mode == 'light' else 'dark'
        down_arrow = (Path(__file__).resolve().parents[2] / 'icons' / f'ui-chevron-down-{icon_theme}.svg').as_posix()
        up_arrow = down_arrow.replace('ui-chevron-down-', 'ui-chevron-up-')
        self.model.apply_theme(mode)
        self.setStyleSheet(f'''
            QDialog#novelaiQueueDialog {{background:{p['canvas']};color:{p['text']};}}
            QDialog#novelaiQueueDialog QLabel {{color:{p['text']};background:transparent;font-size:12px;}}
            QDialog#novelaiQueueDialog QTabBar::tab {{background:{p['surface']};color:{p['muted']};
                border:1px solid {p['border']};padding:7px 10px;margin-right:3px;border-radius:6px;font-size:12px;}}
            QDialog#novelaiQueueDialog QTabBar::tab:selected {{background:{p['accent_soft']};color:{p['accent']};border-color:{p['accent']};}}
            QDialog#novelaiQueueDialog QTabBar::tab:hover {{color:{p['text']};}}
            QDialog#novelaiQueueDialog QMenu {{background:{p['surface']};color:{p['text']};border:1px solid {p['border']};padding:5px;}}
            QDialog#novelaiQueueDialog QMenu::item {{padding:7px 18px;}}
            QDialog#novelaiQueueDialog QMenu::item:selected {{background:{p['accent_soft']};}}
            QDialog#novelaiQueueDialog QLabel#queueTitle {{font-size:20px;font-weight:600;}}
            QDialog#novelaiQueueDialog QLabel#queueMuted,
            QDialog#novelaiQueueDialog QLabel#queueEmpty {{color:{p['muted']};}}
            QDialog#novelaiQueueDialog QLabel#queueSummary {{color:{p['accent']};padding:7px 0;}}
            QDialog#novelaiQueueDialog QFrame#queuePausePanel {{background:{p['input']};
                border:1px solid {p['border']};border-radius:8px;}}
            QDialog#novelaiQueueDialog QLabel#queuePauseHint {{color:{p['warning']};}}
            QDialog#novelaiQueueDialog QComboBox, QDialog#novelaiQueueDialog QSpinBox {{background:{p['surface']};color:{p['text']};
                border:1px solid {p['border']};border-radius:7px;padding:5px 8px;font-size:12px;}}
            QDialog#novelaiQueueDialog QSpinBox {{padding-right:24px;}}
            QDialog#novelaiQueueDialog QSpinBox::up-button {{subcontrol-origin:border;subcontrol-position:top right;
                width:21px;background:{p['input']};border:none;border-left:1px solid {p['border']};border-top-right-radius:6px;}}
            QDialog#novelaiQueueDialog QSpinBox::down-button {{subcontrol-origin:border;subcontrol-position:bottom right;
                width:21px;background:{p['input']};border:none;border-left:1px solid {p['border']};border-bottom-right-radius:6px;}}
            QDialog#novelaiQueueDialog QSpinBox::up-arrow {{image:url("{up_arrow}");width:10px;height:10px;}}
            QDialog#novelaiQueueDialog QSpinBox::down-arrow {{image:url("{down_arrow}");width:10px;height:10px;}}
            QDialog#novelaiQueueDialog QComboBox:focus, QDialog#novelaiQueueDialog QSpinBox:focus {{border-color:{p['accent']};}}
            QDialog#novelaiQueueDialog QComboBox::drop-down {{width:20px;border:none;}}
            QDialog#novelaiQueueDialog QComboBox::down-arrow {{image:url("{down_arrow}");width:12px;height:12px;}}
            QDialog#novelaiQueueDialog QComboBox QAbstractItemView {{background:{p['surface']};color:{p['text']};
                selection-background-color:{p['accent_soft']};selection-color:{p['text']};}}
            QDialog#novelaiQueueDialog QTreeView,
            QDialog#novelaiQueueDialog QPlainTextEdit {{background:{p['surface']};color:{p['text']};
                alternate-background-color:{p['input']};border:1px solid {p['border']};border-radius:8px;
                selection-background-color:{p['accent_soft']};selection-color:{p['text']};font-size:12px;}}
            QDialog#novelaiQueueDialog QPlainTextEdit {{padding:10px;}}
            QDialog#novelaiQueueDialog QTreeView::item {{padding:6px 5px;border:none;}}
            QDialog#novelaiQueueDialog QTreeView::item:hover {{background:{p['hover']};}}
            QDialog#novelaiQueueDialog QTreeView::item:selected {{background:{p['accent_soft']};}}
            QDialog#novelaiQueueDialog QHeaderView::section {{background:{p['input']};color:{p['muted']};
                border:none;border-bottom:1px solid {p['border']};padding:9px 5px;font-size:11px;}}
            QDialog#novelaiQueueDialog QPushButton {{background:{p['surface']};color:{p['text']};
                border:1px solid {p['border']};border-radius:7px;padding:8px 12px;font-size:12px;}}
            QDialog#novelaiQueueDialog QPushButton:hover {{background:{p['hover']};border-color:{p['muted']};}}
            QDialog#novelaiQueueDialog QPushButton:focus {{border-color:{p['accent']};}}
            QDialog#novelaiQueueDialog QPushButton#queueStop:enabled {{color:{p['danger']};}}
            QDialog#novelaiQueueDialog QPushButton:disabled {{color:{p['muted']};background:{p['input']};}}
            QDialog#novelaiQueueDialog QSplitter::handle {{background:transparent;height:7px;}}
            QDialog#novelaiQueueDialog QScrollBar:vertical {{background:transparent;width:9px;margin:0;}}
            QDialog#novelaiQueueDialog QScrollBar::handle:vertical {{background:{p['border']};min-height:28px;border-radius:4px;}}
            QDialog#novelaiQueueDialog QScrollBar::add-line:vertical,
            QDialog#novelaiQueueDialog QScrollBar::sub-line:vertical {{height:0;}}
            QDialog#novelaiQueueDialog QScrollBar::add-page:vertical,
            QDialog#novelaiQueueDialog QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
