"""RunningHub app cards and a shared, virtualized task queue presentation."""
import math
import time
import weakref
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5 import sip
from aetherloom_core.rh_ui import palette, app_stylesheet
from aetherloom_core.rh_tasks import ACTIVE_STATUSES
from aetherloom_core.rh_progress import progress_text

LABELS = {'QUEUED': '云端排队', 'RUNNING': '运行中', 'DOWNLOADING': '下载中',
          'DOWNLOAD_FAILED': '等待下载重试', 'POLL_TIMEOUT': '等待状态重查',
          'CANCEL_FAILED': '取消未确认', 'WAITING_FOR_KEY': '等待密钥',
          'SUCCESS': '已完成', 'FAILED': '失败', 'CANCELED': '已取消',
          'REMOVED': '已移除', 'LOCAL_WAIT': '等待提交', 'SUBMITTING': '准备 / 提交中'}
ACTIVE_UI = ACTIVE_STATUSES | {'LOCAL_WAIT', 'SUBMITTING'}


def status_color(p, status):
    if status == 'SUCCESS':
        return p['success']
    if status in ('FAILED', 'CANCEL_FAILED', 'DOWNLOAD_FAILED'):
        return p['danger']
    if status in ('RUNNING', 'DOWNLOADING'):
        return p['accent']
    if status in ACTIVE_UI:
        return p['warning']
    return p['muted']


class AppCard(QtWidgets.QPushButton):
    def __init__(self, title, dashboard):
        super().__init__(title)
        self.dashboard = dashboard
        self._full_title = title
        self._favorite = False
        self._task_status = None
        self._task_count = 0
        self._progress = None
        self._cover = QtGui.QPixmap()
        self._cover_path = None
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumSize(160, 174)
        self.setMouseTracking(True)
        dashboard.cards.add(self)

    def _rh_set_fav(self, on):
        self._favorite = bool(on)
        self.update()

    def setIcon(self, icon):
        super().setIcon(icon)
        path = getattr(self, '_thumb_path', None)
        if path and path != self._cover_path:
            reader = QtGui.QImageReader(path)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(640, 420, QtCore.Qt.KeepAspectRatio))
            self._cover = QtGui.QPixmap.fromImage(reader.read())
            self._cover_path = path
        if self._cover.isNull():
            self._cover = icon.pixmap(400, 260)
        self.update()

    def set_task_state(self, status, count, progress=None):
        state = (status, count, progress)
        if state != (self._task_status, self._task_count, self._progress):
            self._task_status, self._task_count, self._progress = state
            self.setAccessibleDescription(LABELS.get(status, status or '准备就绪') + f' · {count} 项活动任务')
            self.update()

    def paintEvent(self, event):
        p = palette(getattr(self.dashboard.owner, '_theme_mode', 'dark'))
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2)
        accent = status_color(p, self._task_status)
        active = self._task_count > 0
        border = accent if active else p['accent'] if self.hasFocus() else p['muted'] if self.underMouse() else p['border']
        painter.setPen(QtGui.QPen(QtGui.QColor(border), 1.5))
        painter.setBrush(QtGui.QColor(p['hover'] if self.isDown() or self.underMouse() else p['surface']))
        painter.drawRoundedRect(rect, 12, 12)
        is_add = not getattr(self, '_wid', None)
        cover = rect.adjusted(8, 8, -8, -62)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(cover, 8, 8)
        painter.save()
        painter.setClipPath(clip)
        gradient = QtGui.QLinearGradient(cover.topLeft(), cover.bottomRight())
        gradient.setColorAt(0, QtGui.QColor(p['accent_soft']))
        gradient.setColorAt(1, QtGui.QColor(p['input']))
        painter.fillRect(cover, gradient)
        if not self._cover.isNull():
            # Crop the source rectangle, never allocate a scaled pixmap per frame.
            source = QtCore.QRectF(self._cover.rect())
            ratio = cover.width() / max(1, cover.height())
            if source.width() / source.height() > ratio:
                new_width = source.height() * ratio
                source.adjust((source.width() - new_width) / 2, 0, -(source.width() - new_width) / 2, 0)
            else:
                new_height = source.width() / ratio
                source.adjust(0, (source.height() - new_height) / 2, 0, -(source.height() - new_height) / 2)
            painter.drawPixmap(cover, self._cover, source)
        else:
            painter.setPen(QtGui.QColor(p['accent']))
            font = QtGui.QFont('Microsoft YaHei', 28)
            painter.setFont(font)
            painter.drawText(cover, QtCore.Qt.AlignCenter, '+' if is_add else 'A')
        painter.restore()
        font = QtGui.QFont('Microsoft YaHei')
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['text']))
        title = '添加应用' if is_add else str(self._full_title or self.text()).replace('\n', ' ')
        painter.drawText(QtCore.QRectF(14, self.height() - 54, self.width() - 28, 22),
                         QtCore.Qt.AlignVCenter, painter.fontMetrics().elidedText(title, QtCore.Qt.ElideRight, self.width() - 28))
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(accent if self._task_status else p['muted']))
        state = LABELS.get(self._task_status, self._task_status or '准备就绪')
        if active:
            state += ' · %d 项' % self._task_count
        if self._progress and not self._progress.get('stale') and self._progress.get('percent') is not None:
            state += ' · 节点 %.0f%%' % self._progress['percent']
        painter.drawText(QtCore.QRectF(14, self.height() - 29, self.width() - 28, 18),
                         QtCore.Qt.AlignVCenter, '导入工作流，开始创作' if is_add else
                         painter.fontMetrics().elidedText(state, QtCore.Qt.ElideRight, self.width() - 28))
        if self._favorite:
            painter.setPen(QtGui.QColor('#f079a1'))
            painter.drawText(QtCore.QRectF(14, 12, 24, 24), QtCore.Qt.AlignCenter, '♥')
        if active and self._task_status in ('RUNNING', 'DOWNLOADING'):
            phase = self.dashboard.phase
            alpha = int(100 + 100 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi)))
            color = QtGui.QColor(accent)
            color.setAlpha(alpha)
            painter.setPen(QtGui.QPen(color, 2.5))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(rect, 12, 12)
            ring = QtCore.QRectF(self.width() - 35, 16, 17, 17)
            painter.drawArc(ring, int(-phase * 360 * 16), 100 * 16)


class TaskModel(QtCore.QAbstractListModel):
    def __init__(self, parent):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == QtCore.Qt.UserRole:
            return row
        if role in (QtCore.Qt.DisplayRole, QtCore.Qt.ToolTipRole, QtCore.Qt.AccessibleTextRole):
            return '\n'.join((row['title'], LABELS.get(row['status'], row['status']), row['tid'],
                              progress_text(row.get('progress')), row['note']))

    def replace(self, rows):
        if rows == self.rows:
            return
        if [r['key'] for r in rows] == [r['key'] for r in self.rows]:
            previous, self.rows = self.rows, rows
            for i, row in enumerate(rows):
                if row != previous[i]:
                    self.dataChanged.emit(self.index(i), self.index(i))
        else:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()


class TaskFilter(QtCore.QSortFilterProxyModel):
    category = 'all'
    query = ''

    def filterAcceptsRow(self, source_row, parent):
        row = self.sourceModel().rows[source_row]
        active = row['status'] in ACTIVE_UI
        return ((self.category == 'all' or active == (self.category == 'active')) and
                self.query in (' '.join((row['title'], row['tid'], row['note']))).casefold())


class TaskDelegate(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QtCore.QSize(240, 86)

    def paint(self, painter, option, index):
        row = index.data(QtCore.Qt.UserRole)
        if row is None:
            return
        # The owner is shared by the embedded list and the detached dialog.
        p = palette(getattr(self.parent()._dashboard.owner, '_theme_mode', 'dark'))
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = option.rect.adjusted(2, 3, -2, -3)
        selected = option.state & QtWidgets.QStyle.State_Selected
        painter.setPen(QtGui.QColor(p['accent'] if selected else p['border']))
        painter.setBrush(QtGui.QColor(p['accent_soft'] if selected else p['surface']))
        painter.drawRoundedRect(rect, 9, 9)
        painter.fillRect(rect.left() + 1, rect.top() + 14, 3, rect.height() - 28,
                         QtGui.QColor(status_color(p, row['status'])))
        font = QtGui.QFont(option.font)
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['text']))
        width = max(10, rect.width() - 26)
        painter.drawText(rect.adjusted(13, 8, -13, -48), QtCore.Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(row['title'], QtCore.Qt.ElideRight, width))
        font.setBold(False)
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(status_color(p, row['status'])))
        painter.drawText(rect.adjusted(13, 32, -13, -26), QtCore.Qt.AlignVCenter,
                         LABELS.get(row['status'], row['status']))
        painter.setPen(QtGui.QColor(p['muted']))
        detail = progress_text(row.get('progress')) or row['note'] or ('任务 ' + row['tid'] if row['tid'] else '尚未分配云端任务 ID')
        painter.drawText(rect.adjusted(13, 55, -13, -6), QtCore.Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(detail, QtCore.Qt.ElideRight, width))
        painter.restore()


class TaskPanel(QtWidgets.QWidget):
    def __init__(self, dashboard, detached=False):
        super().__init__()
        self.dashboard = dashboard
        self.setObjectName('rhQueuePanel')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('任务队列')
        title.setObjectName('rhSectionTitle')
        header.addWidget(title, 1)
        if not detached:
            expand = QtWidgets.QPushButton('独立窗口')
            expand.clicked.connect(dashboard.open_queue)
            header.addWidget(expand)
        layout.addLayout(header)
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('rhMuted')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索应用或任务 ID')
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.category = QtWidgets.QComboBox()
        for title, value in (('全部任务', 'all'), ('进行中 / 等待中', 'active'), ('已结束', 'history')):
            self.category.addItem(title, value)
        layout.addWidget(self.category)
        self.proxy = TaskFilter(self)
        self.proxy.setSourceModel(dashboard.model)
        self.view = QtWidgets.QListView()
        self.view._dashboard = dashboard
        self.view.setModel(self.proxy)
        self.view.setItemDelegate(TaskDelegate(self.view))
        self.view.setUniformItemSizes(True)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.view.setMinimumHeight(180)
        self.view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.context_menu)
        self.view.doubleClicked.connect(self.open_app)
        layout.addWidget(self.view, 1)
        self.empty = QtWidgets.QLabel('暂无任务\n运行应用后可在这里查看状态')
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setObjectName('rhMuted')
        self.empty.setMinimumHeight(100)
        layout.addWidget(self.empty)
        self.search.textChanged.connect(self.filter_changed)
        self.category.currentIndexChanged.connect(self.filter_changed)
        dashboard.panels.add(self)
        self.refresh_summary()

    def filter_changed(self):
        self.proxy.category = self.category.currentData()
        self.proxy.query = self.search.text().strip().casefold()
        self.proxy.invalidateFilter()
        self.refresh_summary()

    def refresh_summary(self):
        rows = self.dashboard.model.rows
        active = sum(r['status'] in ACTIVE_UI for r in rows)
        self.summary.setText(f'{active} 项进行中 · {len(rows) - active} 项已结束')
        empty = self.proxy.rowCount() == 0
        self.view.setVisible(not empty)
        self.empty.setVisible(empty)
        self.empty.setText('没有匹配的任务' if rows else '暂无任务\n运行应用后可在这里查看状态')

    def open_app(self, index):
        row = index.data(QtCore.Qt.UserRole) or {}
        button = getattr(self.dashboard.owner, '_rh_app_buttons', {}).get(row.get('wid'))
        if button is not None:
            button.click()

    def context_menu(self, pos):
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        row = index.data(QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self)
        copy = menu.addAction('复制任务 ID')
        copy.setEnabled(bool(row['tid']))
        details = menu.addAction('复制任务信息')
        open_app = menu.addAction('打开应用')
        open_app.setEnabled(row['wid'] in getattr(self.dashboard.owner, '_rh_app_buttons', {}))
        action = menu.exec_(self.view.viewport().mapToGlobal(pos))
        if action == copy:
            QtWidgets.QApplication.clipboard().setText(row['tid'])
        elif action == details:
            QtWidgets.QApplication.clipboard().setText(index.data(QtCore.Qt.DisplayRole))
        elif action == open_app:
            self.open_app(index)


class Dashboard(QtCore.QObject):
    thumbnail_ready = QtCore.pyqtSignal(object, str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.cards = weakref.WeakSet()
        self.panels = weakref.WeakSet()
        self.model = TaskModel(self)
        self.dialog = None
        self.phase = 0
        self.animation = QtCore.QTimer(self)
        self.animation.setInterval(50)
        self.animation.timeout.connect(self.animate)
        self.thumbnail_ready.connect(self.apply_thumbnail, QtCore.Qt.QueuedConnection)
        self.reflow_timer = QtCore.QTimer(self)
        self.reflow_timer.setSingleShot(True)
        owner.installEventFilter(self)

    def visible_active_cards(self):
        if getattr(self.owner, '_closing', False) or not self.owner.isVisible() or self.owner.isMinimized():
            return []
        return [card for card in self.cards if not sip.isdeleted(card) and card._task_count
                and card._task_status in ('RUNNING', 'DOWNLOADING')
                and card.isVisible() and not card.visibleRegion().isEmpty()]

    def animate(self):
        cards = self.visible_active_cards()
        if not cards:
            self.animation.stop()
            return
        self.phase = (time.monotonic() % 2.4) / 2.4
        for card in cards:
            card.update()

    def eventFilter(self, obj, event):
        if obj is getattr(self, '_grid_viewport', None) and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self.reflow_timer.start(60)
        if event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.Close):
            self.animation.stop()
            if event.type() == QtCore.QEvent.Close and self.dialog is not None:
                self.dialog.close()
        return False

    def watch_grid(self, scroll, reflow):
        self._grid_viewport = scroll.viewport()
        self._grid_viewport.installEventFilter(self)
        def fit():
            panel = self.owner._rh_task_panel
            vertical = panel.parentWidget().layout().direction() == QtWidgets.QBoxLayout.TopToBottom
            panel.setMaximumWidth(16777215 if vertical else 380)
            reflow()
        self.reflow_timer.timeout.connect(fit)
        scroll.verticalScrollBar().valueChanged.connect(lambda _: self.refresh())
        self.reflow_timer.start(0)

    def setup_header(self, layout):
        # Keep credential controls and their signals; make them available on demand.
        connection = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(connection)
        for _ in range(2):
            item = layout.takeAt(0)
            if item is not None and item.layout() is not None:
                body.addLayout(item.layout())
        old_heading = layout.takeAt(0)
        if old_heading is not None and old_heading.widget() is not None:
            old_heading.widget().hide()
        hero = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(hero)
        header.setContentsMargins(0, 0, 0, 8)
        titles = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel('RH 应用')
        title.setObjectName('rhPageTitle')
        self.subtitle = QtWidgets.QLabel('选择应用，开始创作')
        self.subtitle.setObjectName('rhMuted')
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        toggle = QtWidgets.QPushButton('连接设置')
        toggle.setCheckable(True)
        toggle.toggled.connect(connection.setVisible)
        header.addWidget(toggle)
        queue = QtWidgets.QPushButton('任务队列')
        queue.setObjectName('rhPrimaryButton')
        queue.clicked.connect(self.open_queue)
        header.addWidget(queue)
        layout.insertWidget(0, hero)
        layout.insertWidget(1, connection)
        connection.hide()

    def apply_thumbnail(self, reference, path):
        button = reference()
        if button is None or sip.isdeleted(button) or getattr(self.owner, '_closing', False):
            return
        button._has_thumbnail = True
        button._thumb_path = path
        button._cover_path = None
        button.setIcon(QtGui.QIcon(path))

    def refresh(self):
        if getattr(self.owner, '_closing', False):
            self.animation.stop()
            return
        buttons = getattr(self.owner, '_rh_app_buttons', {})
        def title(wid):
            return str(getattr(buttons.get(wid), '_full_title', '') or '应用 ' + wid)
        rows = []
        lock = getattr(self.owner, '_rh_retry_lock', None)
        if lock is not None:
            with lock:
                waiting = list(getattr(self.owner, '_rh_retry_queue', ()))
        else:
            waiting = list(getattr(self.owner, '_rh_retry_queue', ()))
        for i, entry in enumerate(waiting):
            wid = str(entry.get('webapp_id') or '')
            queue = getattr(self.owner, '_rh_submission_queue', None)
            waiting_start = (queue is not None and entry.get('_submission_order') is not None
                             and queue.waiting_for_start(entry['_submission_order']))
            status = 'SUBMITTING' if entry.get('_submitting') else 'LOCAL_WAIT'
            note = ('正在提交请求' if entry.get('_submitting') else
                    ('等待前序任务开始运行' if waiting_start else '队首 · 等待重试')
                    if i == 0 else f'等待第 {i + 1} 位 · 由队首依次提交')
            rows.append(dict(key='local:' + str(id(entry)), wid=wid, title=title(wid), status=status, tid='', note=note))
            card = entry.get('card')
            if card is not None and not sip.isdeleted(card) and not getattr(card, '_task_id', None):
                from aetherloom_core.rh_progress import update_card_progress
                update_card_progress(self.owner, card, status)
                widget = getattr(card, '_rh_progress_widget', None)
                if widget is not None:
                    widget.set_message(note)
        waiting_cards = {id(entry.get('card')) for entry in waiting}
        for card in list(getattr(self.owner, '_rh_local_cards', ())):
            if (sip.isdeleted(card) or id(card) in waiting_cards or getattr(card, '_task_id', None)
                    or not getattr(card, '_webapp_id', None)):
                continue
            wid = str(getattr(card, '_webapp_id', '') or '')
            state = getattr(card, '_rh_display_status', 'SUBMITTING')
            widget = getattr(card, '_rh_progress_widget', None)
            note = widget.label.text() if widget is not None else '准备素材或提交请求，等待云端任务 ID'
            rows.append(dict(key='preparing:' + str(id(card)), wid=wid, title=title(wid), status=state,
                             tid='', note=note))
        mapping = getattr(self.owner, '_rh_task_to_wid', {})
        notes = getattr(self.owner, '_rh_download_notes', {})
        for tid, status in list(getattr(self.owner, '_rh_status_entries', {}).items()):
            wid = str(mapping.get(tid) or '')
            progress = getattr(self.owner, '_rh_progress_entries', {}).get(tid) if status == 'RUNNING' else None
            rows.append(dict(key=str(tid), tid=str(tid), wid=wid, title=title(wid), status=status,
                             note=str(notes.get(tid) or ''), progress=progress))
        rows.sort(key=lambda row: row['status'] not in ACTIVE_UI)
        positions = [(panel, (panel.view.currentIndex().data(QtCore.Qt.UserRole) or {}).get('key'),
                      panel.view.verticalScrollBar().value()) for panel in self.panels]
        self.model.replace(rows)
        if hasattr(self, 'subtitle'):
            active_count = sum(row['status'] in ACTIVE_UI for row in rows)
            self.subtitle.setText(f'{len(buttons)} 个应用 · {active_count} 项任务进行中')
        for panel, key, scroll in positions:
            panel.refresh_summary()
            if key is not None:
                for i in range(panel.proxy.rowCount()):
                    if panel.proxy.index(i, 0).data(QtCore.Qt.UserRole)['key'] == key:
                        panel.view.setCurrentIndex(panel.proxy.index(i, 0))
                        break
            panel.view.verticalScrollBar().setValue(scroll)
        by_app = {}
        running_progress = {}
        for row in rows:
            by_app.setdefault(row['wid'], []).append(row['status'])
            if row['status'] == 'RUNNING':
                running_progress[row['wid']] = row.get('progress')
        for wid, button in list(buttons.items()):
            statuses = by_app.get(str(wid), [])
            active = [st for st in statuses if st in ACTIVE_UI]
            priority = ('RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'CANCEL_FAILED', 'POLL_TIMEOUT',
                        'WAITING_FOR_KEY', 'QUEUED', 'SUBMITTING', 'LOCAL_WAIT')
            state = next((st for st in priority if st in active),
                         getattr(self.owner, '_rh_app_last_result', {}).get(str(wid), statuses[-1] if statuses else None))
            # A single task's node percent must not masquerade as an app-wide
            # percentage when several tasks run concurrently.
            progress = running_progress.get(str(wid)) if len(active) == 1 else None
            button.set_task_state(state, len(active), progress)
        if self.visible_active_cards() and not self.animation.isActive():
            self.animation.start()
        elif not self.visible_active_cards():
            self.animation.stop()

    def apply_theme(self):
        p = palette(getattr(self.owner, '_theme_mode', 'dark'))
        css = app_stylesheet(getattr(self.owner, '_theme_mode', 'dark')).replace('#rhAppPage', '#rhDashboard')
        css += f'QWidget#rhQueuePanel {{ background: {p["input"]}; border: 1px solid {p["border"]}; border-radius: 12px; }}'
        self.owner.runninghub_page.setObjectName('rhDashboard')
        self.owner.runninghub_page.setStyleSheet(css)
        if self.dialog is not None:
            self.dialog.setStyleSheet(css.replace('#rhDashboard', '#rhQueueWindow'))
        for card in self.cards:
            if not sip.isdeleted(card):
                card.update()

    def open_queue(self):
        if self.dialog is None:
            self.dialog = QtWidgets.QDialog(self.owner)
            self.dialog.setObjectName('rhQueueWindow')
            self.dialog.setWindowTitle('任务队列 · AetherLoom')
            layout = QtWidgets.QVBoxLayout(self.dialog)
            layout.addWidget(TaskPanel(self, detached=True))
            screen = self.owner.screen().availableGeometry()
            self.dialog.resize(min(520, screen.width() - 40), min(700, screen.height() - 60))
            self.apply_theme()
        self.refresh()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
