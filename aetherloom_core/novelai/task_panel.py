"""Bounded accepted-task cards with small, asynchronously loaded thumbnails."""
import copy
import heapq
import math
import os
import time
from collections import OrderedDict

from PyQt5 import QtCore, QtGui, QtWidgets
from .styles import workspace_palette as palette
from .jobs import Job


MAX_CARDS = 50
MAX_THUMBNAILS = 64
MAX_PREVIEWS = 8
THUMB_SIZE = 256
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_777_216
WAITING_STATES = frozenset(('queued', 'preparing', 'ready', 'retry_wait', 'running'))
STATE_LABELS = {'running': '正在生成', 'saving': '正在保存', 'save_failed': '等待重新保存',
                'succeeded': '已完成', 'canceling': '正在停止'}


def _text(value, limit=180):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


def _number(value, fallback=0):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    return fallback


def _stamp(value):
    try:
        return time.strftime('%H:%M', time.localtime(_number(value))) if _number(value) > 0 else '时间未知'
    except (ValueError, OverflowError, OSError):
        return '时间未知'


def _read_image(reader):
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > MAX_PIXELS:
        return QtGui.QImage()
    reader.setScaledSize(size.scaled(THUMB_SIZE, THUMB_SIZE, QtCore.Qt.KeepAspectRatio))
    image = reader.read()
    if not image.isNull() and (image.width() > THUMB_SIZE or image.height() > THUMB_SIZE):
        image = image.scaled(THUMB_SIZE, THUMB_SIZE, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
    return image


def _read_thumbnail(path):
    """Runs in a Job thread; QPixmap creation remains on the GUI thread."""
    try:
        if not 0 < os.stat(path).st_size <= MAX_IMAGE_BYTES:
            return QtGui.QImage()
        return _read_image(QtGui.QImageReader(path))
    except (OSError, ValueError, RuntimeError):
        return QtGui.QImage()


class _ElidedLabel(QtWidgets.QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    def set_full_text(self, text):
        if text != self._full_text:
            self._full_text = text
            self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self._full_text, QtCore.Qt.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class _TaskCard(QtWidgets.QFrame):
    clicked = QtCore.pyqtSignal(str)
    reuseClicked = QtCore.pyqtSignal(str, int)
    menuRequested = QtCore.pyqtSignal(str, object)

    def __init__(self, identity, parent=None):
        super().__init__(parent)
        self.identity = identity
        self._summary = None
        self._thumbnail_key = None
        self._source_pixmap = QtGui.QPixmap()
        self._placeholder = '暂无图片'
        self._compact = False
        self.setObjectName('naiTaskCard')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setProperty('naiTaskSelected', False)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFixedHeight(90)
        self.setMinimumWidth(0)
        box = self.box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight, self)
        box.setContentsMargins(10, 10, 10, 10)
        box.setSpacing(10)
        self.thumbnail = QtWidgets.QLabel('生成中')
        self.thumbnail.setObjectName('naiTaskThumb')
        self.thumbnail.setFixedSize(58, 58)
        self.thumbnail.setAlignment(QtCore.Qt.AlignCenter)
        self.thumbnail.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        box.addWidget(self.thumbnail)
        labels = self.labels = QtWidgets.QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(4)
        self.state_label = _ElidedLabel()
        self.state_label.setObjectName('naiTaskState')
        self.title_label = _ElidedLabel()
        self.title_label.setObjectName('naiTaskTitle')
        self.meta_label = _ElidedLabel()
        self.meta_label.setObjectName('naiTaskMeta')
        for label in (self.state_label, self.title_label, self.meta_label):
            labels.addWidget(label)
        box.addLayout(labels, 1)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda point: self.menuRequested.emit(self.identity, self.mapToGlobal(point)))

    def update_summary(self, summary):
        if summary == self._summary:
            return
        self._summary = summary
        state, title, meta, message, index = summary
        self.state_label.set_full_text((f'#{index} · ' if index else '') + STATE_LABELS.get(state, '已受理'))
        self.title_label.set_full_text(title)
        self.meta_label.set_full_text(meta)
        self.setToolTip(title + '\n' + STATE_LABELS.get(state, '已受理') + ' · ' + meta + ('\n' + message if message else '') + '\n单击预览 · 右键更多\n有结果后：Ctrl＋点击复用参数，Shift＋点击回填种子\nCtrl＋Shift＋点击复用参数与种子（保留输入素材）')
        self.setAccessibleName(title + '，' + STATE_LABELS.get(state, '已受理'))
        tone = 'success' if state == 'succeeded' else 'warning' if state in ('save_failed', 'canceling') else 'accent'
        if self.state_label.property('naiTaskTone') != tone:
            self.state_label.setProperty('naiTaskTone', tone)
            self.state_label.style().unpolish(self.state_label)
            self.state_label.style().polish(self.state_label)

    def set_selected(self, selected):
        if self.property('naiTaskSelected') != selected:
            self.setProperty('naiTaskSelected', selected)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def set_compact(self, compact):
        compact = bool(compact)
        if compact == self._compact:
            return
        self._compact = compact
        self.box.setDirection(QtWidgets.QBoxLayout.TopToBottom if compact else QtWidgets.QBoxLayout.LeftToRight)
        self.box.setContentsMargins(*( (5, 5, 5, 5) if compact else (10, 10, 10, 10) ))
        self.box.setSpacing(5 if compact else 10)
        self.title_label.setVisible(not compact)
        self.meta_label.setVisible(not compact)
        self.labels.setSpacing(0 if compact else 4)
        self.thumbnail.setSizePolicy(QtWidgets.QSizePolicy.Ignored if compact else QtWidgets.QSizePolicy.Fixed,
                                     QtWidgets.QSizePolicy.Fixed)
        self.thumbnail.setMinimumWidth(0 if compact else 58)
        self.thumbnail.setMaximumWidth(16777215 if compact else 58)
        self._resize_thumbnail()

    def _resize_thumbnail(self):
        side = max(44, self.width() - 12) if self._compact else 58
        self.thumbnail.setFixedHeight(side)
        target = side + 31 if self._compact else 90
        if self.height() != target:
            self.setFixedHeight(target)
        self._render_thumbnail()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'thumbnail'):
            self._resize_thumbnail()

    def set_thumbnail(self, pixmap=None, placeholder='暂无图片'):
        self._source_pixmap = QtGui.QPixmap(pixmap) if pixmap is not None else QtGui.QPixmap()
        self._placeholder = placeholder
        self._render_thumbnail()

    def _render_thumbnail(self):
        pixmap = self._source_pixmap
        if pixmap.isNull():
            key = ('empty', self._placeholder)
            if key != self._thumbnail_key:
                self._thumbnail_key = key
                self.thumbnail.clear()
                self.thumbnail.setText(self._placeholder)
            return
        ratio = self.devicePixelRatioF()
        side = max(1, self.width() - 12) if self._compact else 58
        key = (pixmap.cacheKey(), ratio, side)
        if key == self._thumbnail_key:
            return
        self._thumbnail_key = key
        scaled = pixmap.scaled(round(side * ratio), round(side * ratio), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self.thumbnail.setPixmap(scaled)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.setFocus(QtCore.Qt.MouseFocusReason)
            self._activate(event.modifiers())
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Space):
            self._activate(event.modifiers())
            event.accept()
            return
        super().keyPressEvent(event)


    def _activate(self, modifiers):
        modifiers &= QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier
        if modifiers:
            self.reuseClicked.emit(self.identity, int(modifiers))
        else:
            self.clicked.emit(self.identity)


class TaskPanel(QtWidgets.QFrame):
    taskSelected = QtCore.pyqtSignal(str)
    queueRequested = QtCore.pyqtSignal()
    copyRequested = QtCore.pyqtSignal(str)
    resultSelected = QtCore.pyqtSignal(dict)
    useImageRequested = QtCore.pyqtSignal(dict)
    reuseSettingsRequested = QtCore.pyqtSignal(dict)
    reuseSeedRequested = QtCore.pyqtSignal(dict)
    reuseAllRequested = QtCore.pyqtSignal(dict)
    MAX_CARDS = MAX_CARDS
    MAX_THUMBNAILS = MAX_THUMBNAILS
    MAX_PREVIEWS = MAX_PREVIEWS

    def __init__(self, owner=None, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.selected_task_id = None
        self._compact = False
        self.waiting_count = 0
        self.accepted_count = 0
        self._closed = False
        self._cards = {}
        self._card_order = []
        self._states = {}
        self._records = {}
        self._thumbnail_keys = {}
        self._thumbnail_cache = OrderedDict()
        self._path_cache = OrderedDict()
        self._preview_cache = OrderedDict()
        self._preview_times = {}
        self._pending_thumbnails = OrderedDict()
        self._thumbnail_job = None
        self._active_thumbnail_keys = set()
        self._thumbnail_timer = QtCore.QTimer(self)
        self._thumbnail_timer.setSingleShot(True)
        self._thumbnail_timer.timeout.connect(self._pump_thumbnails)
        self.setObjectName('novelaiTaskPanel')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setMinimumWidth(210)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        box = self.box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(12, 14, 12, 12)
        box.setSpacing(10)
        top = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel('任务预览')
        self.title.setObjectName('naiTaskPanelTitle')
        top.addWidget(self.title, 1)
        self.count = QtWidgets.QLabel('0')
        self.count.setObjectName('naiTaskCount')
        self.count.setAlignment(QtCore.Qt.AlignCenter)
        top.addWidget(self.count)
        self.queue_button = QtWidgets.QToolButton(text='队列')
        self.queue_button.setToolTip('打开完整任务队列')
        self.queue_button.clicked.connect(self.queueRequested)
        top.addWidget(self.queue_button)
        box.addLayout(top)
        self.stack = QtWidgets.QStackedWidget()
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setObjectName('naiTaskScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setObjectName('naiTaskViewport')
        self.cards_widget = QtWidgets.QWidget()
        self.cards_widget.setObjectName('naiTaskList')
        self.cards_layout = QtWidgets.QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(7)
        self.cards_layout.setAlignment(QtCore.Qt.AlignTop)
        self.scroll.setWidget(self.cards_widget)
        self.stack.addWidget(self.scroll)
        self.empty = QtWidgets.QLabel('尚无已受理任务\n\n任务受理后会显示在这里\n等待中的任务见下方')
        self.empty.setObjectName('naiTaskEmpty')
        self.empty.setWordWrap(True)
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.stack.addWidget(self.empty)
        self.stack.setCurrentWidget(self.empty)
        box.addWidget(self.stack, 1)
        # This footer is outside the scrolling card list and never moves with it.
        self.waiting_card = QtWidgets.QPushButton('0 个任务等待中')
        self.waiting_card.setObjectName('naiTaskWaiting')
        self.waiting_card.setMinimumHeight(46)
        self.waiting_card.setAutoDefault(False)
        self.waiting_card.setCursor(QtCore.Qt.PointingHandCursor)
        self.waiting_card.setToolTip('查看尚未受理的等待、准备、提交及轮试任务')
        self.waiting_card.clicked.connect(self.queueRequested)
        box.addWidget(self.waiting_card)
        self.apply_theme(getattr(owner, '_theme_mode', 'dark'))

    def set_compact(self, compact=True):
        """Use the narrow image history rail without changing queue behavior."""
        self._compact = bool(compact)
        self.setProperty('naiCompactRail', self._compact)
        self.setMinimumWidth(110 if self._compact else 210)
        self.setMaximumWidth(190 if self._compact else 16777215)
        self.box.setContentsMargins(*( (7, 9, 7, 8) if self._compact else (12, 14, 12, 12) ))
        self.box.setSpacing(7 if self._compact else 10)
        self.title.setText('结果' if self._compact else '任务预览')
        self.count.setVisible(not self._compact)
        self.empty.setText('生成的图像\n会显示在这里' if self._compact else '尚无已受理任务\n\n任务受理后会显示在这里\n等待中的任务见下方')
        self.waiting_card.setText(self._waiting_label())
        self.cards_layout.setContentsMargins(0, 0, 0 if self._compact else 4, 0)
        self.cards_layout.setSpacing(5 if self._compact else 7)
        for card in self._cards.values():
            card.set_compact(self._compact)
        self.apply_theme(self._mode)
        self.updateGeometry()

    def sizeHint(self):
        return QtCore.QSize(140, 420) if self._compact else super().sizeHint()

    def _waiting_label(self):
        return (f'{self.waiting_count} 个任务\n等待中' if self._compact
                else f'{self.waiting_count} 个任务等待中')

    def set_tasks(self, tasks):
        if self._closed:
            return
        latest = []
        waiting = accepted = 0
        for position, task in enumerate(tasks if isinstance(tasks, (list, tuple)) else ()):
            if not isinstance(task, dict):
                continue
            state = _text(task.get('state'), 32)
            if task.get('accepted') is not True:
                waiting += state in WAITING_STATES
                continue
            if state in ('failed', 'cancelled') or not isinstance(task.get('id'), str) or not task['id']:
                continue
            accepted += 1
            rank = (_number(task.get('created')), _number(task.get('index')), position)
            entry = (rank, task)
            if len(latest) < MAX_CARDS:
                heapq.heappush(latest, entry)
            elif rank > latest[0][0]:
                heapq.heapreplace(latest, entry)
        visible = [entry[1] for entry in sorted(latest, key=lambda entry: entry[0], reverse=True)]
        identities = [task['id'] for task in visible]
        identity_set = set(identities)
        for identity in set(self._cards) - identity_set:
            card = self._cards.pop(identity)
            self.cards_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._states, self._records, self._thumbnail_keys = {}, {}, {}
        for position, task in enumerate(visible):
            identity = task['id']
            card = self._cards.get(identity)
            if card is None:
                card = self._cards[identity] = _TaskCard(identity, self.cards_widget)
                card.set_compact(self._compact)
                card.clicked.connect(self._card_selected)
                card.reuseClicked.connect(self._reuse_click)
                card.menuRequested.connect(self._show_menu)
            if self.cards_layout.itemAt(position) is None or self.cards_layout.itemAt(position).widget() is not card:
                self.cards_layout.removeWidget(card)
                self.cards_layout.insertWidget(position, card)
            state = self._states[identity] = _text(task.get('state'), 32)
            results = task.get('results') if isinstance(task.get('results'), list) else []
            record = self._result_record(results)
            if record is not None:
                # Do not retain image bytes, task options, or entire result lists.
                self._records[identity] = {key: record[key] for key in ('id', 'path', 'created', 'seed', 'width', 'height', 'mime', 'settings') if key in record}
            title = _text(task.get('title')) or '图像任务'
            index = task.get('index') if isinstance(task.get('index'), int) and not isinstance(task.get('index'), bool) else ''
            summary = (state, title, f'{_stamp(task.get("created"))} · {len(results)} 张图片', _text(task.get('message'), 300), index)
            card.update_summary(summary)
            card.set_selected(identity == self.selected_task_id)
            path = record.get('path') if record else None
            if isinstance(path, str) and path:
                key = self._path_key(path)
                self._thumbnail_keys[identity] = key
                if key not in self._thumbnail_cache and key not in self._active_thumbnail_keys:
                    self._pending_thumbnails[key] = path
            if state != 'running':
                self._preview_cache.pop(identity, None)
                self._preview_times.pop(identity, None)
            self._apply_thumbnail(identity)
        self._card_order = identities
        self.accepted_count, self.waiting_count = accepted, waiting
        self.count.setText(str(len(identities)))
        self.count.setToolTip(f'共 {accepted} 个已受理任务；仅显示最近 {MAX_CARDS} 项，失败和取消任务隐藏。')
        self.waiting_card.setText(self._waiting_label())
        self.waiting_card.setAccessibleName(f'{waiting} 个任务等待中，点击打开完整任务队列')
        self.stack.setCurrentWidget(self.scroll if identities else self.empty)
        wanted = set(self._thumbnail_keys.values())
        self._pending_thumbnails = OrderedDict((key, path) for key, path in self._pending_thumbnails.items() if key in wanted)
        for identity in set(self._preview_cache) - identity_set:
            self._preview_cache.pop(identity, None)
        # Invalid preview bytes still get throttled, but their timestamps must
        # leave with the card just like successfully decoded preview entries.
        for identity in set(self._preview_times) - identity_set:
            self._preview_times.pop(identity, None)
        if self._pending_thumbnails and self._thumbnail_job is None:
            self._thumbnail_timer.start(0)

    def _result_record(self, results):
        # Prefer the first result still available on disk, matching the main
        # preview's missing-file handling. Keep metadata usable after deletion.
        fallback = None
        for record in results:
            if not isinstance(record, dict):
                continue
            if fallback is None:
                fallback = record
            path = record.get('path')
            if isinstance(path, str) and path:
                key = self._path_key(path)
                if key[1] is not None and key[2] > 0:
                    return record
        return fallback

    def _path_key(self, path):
        now = time.monotonic()
        cached = self._path_cache.get(path)
        if cached and now - cached[0] < 2:
            self._path_cache.move_to_end(path)
            return cached[1]
        try:
            info = os.stat(path)
            key = (path, info.st_mtime_ns, info.st_size)
        except (OSError, ValueError):
            key = (path, None, None)
            self._thumbnail_cache[key] = QtGui.QPixmap()
            self._trim_cache()
        self._path_cache[path] = (now, key)
        self._path_cache.move_to_end(path)
        while len(self._path_cache) > MAX_THUMBNAILS:
            self._path_cache.popitem(last=False)
        return key

    def _trim_cache(self):
        while len(self._thumbnail_cache) > MAX_THUMBNAILS:
            self._thumbnail_cache.popitem(last=False)

    def _apply_thumbnail(self, identity):
        card = self._cards.get(identity)
        if card is None:
            return
        key = self._thumbnail_keys.get(identity)
        if key in self._thumbnail_cache:
            pixmap = self._thumbnail_cache[key]
            self._thumbnail_cache.move_to_end(key)
            card.set_thumbnail(pixmap, '图片不可用')
            return
        if identity in self._preview_cache:
            card.set_thumbnail(self._preview_cache[identity])
            return
        label = '读取图片…' if key is not None else {'running': '生成中', 'saving': '保存中', 'save_failed': '待保存', 'canceling': '停止中'}.get(self._states.get(identity), '暂无图片')
        card.set_thumbnail(placeholder=label)

    def _pump_thumbnails(self):
        job = self._thumbnail_job
        if job is not None:
            if not getattr(job, '_thumbnails_finished', False):
                return
            if job.thread is not None and job.thread.is_alive():
                self._thumbnail_timer.start(10)
                return
            self._thumbnail_job = None
            self._active_thumbnail_keys.clear()
            job.deleteLater()
        if self._closed or not self._pending_thumbnails:
            return
        batch = []
        while self._pending_thumbnails and len(batch) < 8:
            key, path = self._pending_thumbnails.popitem(last=False)
            if key not in self._thumbnail_cache:
                batch.append((key, path))
        if not batch:
            return
        self._active_thumbnail_keys = {key for key, path in batch}
        def operation(current):
            result = []
            for key, path in batch:
                if current.stop.is_set():
                    break
                image = _read_thumbnail(path)
                if current.stop.is_set():
                    break
                result.append((key, image))
            return result
        job = self._thumbnail_job = Job(operation, self)
        job.succeeded.connect(self._thumbnails_loaded)
        job.failed.connect(self._thumbnails_failed)
        job.finished.connect(self._thumbnails_finished)
        job.start()

    def _thumbnails_loaded(self, results):
        if self._closed:
            return
        for key, image in results:
            self._thumbnail_cache[key] = QtGui.QPixmap.fromImage(image) if not image.isNull() else QtGui.QPixmap()
            self._thumbnail_cache.move_to_end(key)
        self._trim_cache()
        completed = {key for key, image in results}
        for identity, key in self._thumbnail_keys.items():
            if key in completed:
                self._apply_thumbnail(identity)

    def _thumbnails_failed(self, unused):
        self._thumbnails_loaded([(key, QtGui.QImage()) for key in self._active_thumbnail_keys])

    def _thumbnails_finished(self):
        job = self.sender()
        if job is self._thumbnail_job:
            job._thumbnails_finished = True
            self._thumbnail_timer.start(0)

    def set_preview(self, task_id, value):
        if self._closed or task_id not in self._cards or self._states.get(task_id) != 'running' or task_id in self._records:
            return
        now = time.monotonic()
        if now - self._preview_times.get(task_id, -1) < .25:
            return
        raw = value.get('bytes') if isinstance(value, dict) else value
        if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= MAX_PREVIEW_BYTES:
            return
        self._preview_times[task_id] = now
        buffer = QtCore.QBuffer()
        buffer.setData(bytes(raw))
        buffer.open(QtCore.QIODevice.ReadOnly)
        try:
            image = _read_image(QtGui.QImageReader(buffer))
        finally:
            buffer.close()
        if image.isNull():
            return
        self._preview_cache[task_id] = QtGui.QPixmap.fromImage(image)
        self._preview_cache.move_to_end(task_id)
        while len(self._preview_cache) > MAX_PREVIEWS:
            discarded, _ = self._preview_cache.popitem(last=False)
            self._preview_times.pop(discarded, None)
        self._apply_thumbnail(task_id)

    def select_task(self, identity):
        """Keep the page's chosen identity without emitting or forcing scroll."""
        self.selected_task_id = identity if isinstance(identity, str) and identity else None
        for task_id, card in self._cards.items():
            card.set_selected(task_id == self.selected_task_id)

    def _card_selected(self, identity):
        if identity in self._cards:
            self.select_task(identity)
            self.taskSelected.emit(identity)

    def _reuse_click(self, identity, modifiers):
        record = self._records.get(identity)
        if not isinstance(record, dict):
            return  # An accepted/streaming task has no reusable final result yet.
        ctrl = bool(modifiers & QtCore.Qt.ControlModifier)
        shift = bool(modifiers & QtCore.Qt.ShiftModifier)
        if ctrl and shift and (record.get('settings') or record.get('seed') is not None):
            self.reuseAllRequested.emit(copy.deepcopy(record))
        elif ctrl and isinstance(record.get('settings'), dict) and record['settings']:
            self.reuseSettingsRequested.emit(copy.deepcopy(record))
        elif shift and not ctrl and record.get('seed') is not None:
            self.reuseSeedRequested.emit(copy.deepcopy(record))

    def _create_menu(self, identity):
        if identity not in self._cards:
            return None
        menu = QtWidgets.QMenu(self)
        complete = menu.addAction('复制完整任务到主页', lambda: self.copyRequested.emit(identity))
        complete.setToolTip('包括任务参数、种子和输入素材；仅回填，不自动提交。')
        record = self._records.get(identity)
        if record is not None:
            menu.addSeparator()
            settings = record.get('settings') if isinstance(record.get('settings'), dict) else {}
            params = menu.addAction('复用参数（保留种子和素材）', lambda: self._reuse_click(identity, QtCore.Qt.ControlModifier))
            params.setEnabled(bool(settings))
            seed = menu.addAction('回填种子', lambda: self._reuse_click(identity, QtCore.Qt.ShiftModifier))
            seed.setEnabled(record.get('seed') is not None)
            both = menu.addAction('复用参数与种子（保留素材）', lambda: self._reuse_click(identity, QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier))
            both.setEnabled(bool(settings) or record.get('seed') is not None)
            menu.addSeparator()
            menu.addAction('查看结果', lambda: self.resultSelected.emit(copy.deepcopy(record)))
            menu.addAction('作为底图', lambda: self.useImageRequested.emit(copy.deepcopy(record)))
        menu.addSeparator()
        menu.addAction('打开完整任务队列', self.queueRequested)
        return menu

    def _show_menu(self, identity, position):
        menu = self._create_menu(identity)
        if menu is not None:
            menu.exec_(position)
            menu.deleteLater()

    def apply_theme(self, mode):
        self._mode = mode
        p = palette(mode)
        self.setStyleSheet(f"""
            QFrame#novelaiTaskPanel {{background:{p['surface']};border:1px solid {p['border']};border-radius:11px;}}
            QFrame#novelaiTaskPanel QLabel {{background:transparent;border:none;color:{p['text']};font-size:12px;}}
            QFrame#novelaiTaskPanel QLabel#naiTaskPanelTitle {{font-size:14px;font-weight:600;}}
            QFrame#novelaiTaskPanel QLabel#naiTaskCount {{background:{p['accent_soft']};color:{p['accent']};border-radius:5px;padding:3px 7px;font-size:11px;}}
            QFrame#novelaiTaskPanel QScrollArea#naiTaskScroll,
            QFrame#novelaiTaskPanel QWidget#naiTaskViewport,
            QFrame#novelaiTaskPanel QWidget#naiTaskList {{background:transparent;border:none;}}
            QFrame#novelaiTaskPanel QFrame#naiTaskCard {{background:{p['input']};border:1px solid {p['border']};border-radius:8px;}}
            QFrame#novelaiTaskPanel QFrame#naiTaskCard:hover {{border-color:{p['muted']};}}
            QFrame#novelaiTaskPanel QFrame#naiTaskCard[naiTaskSelected="true"] {{background:{p['accent_soft']};border-color:{p['accent']};}}
            QFrame#novelaiTaskPanel QLabel#naiTaskThumb {{background:{p['surface']};border-radius:6px;color:{p['muted']};font-size:11px;}}
            QFrame#novelaiTaskPanel QLabel#naiTaskMeta,
            QFrame#novelaiTaskPanel QLabel#naiTaskEmpty {{color:{p['muted']};font-size:11px;}}
            QFrame#novelaiTaskPanel QLabel#naiTaskState {{font-size:11px;font-weight:600;}}
            QFrame#novelaiTaskPanel QLabel[naiTaskTone="success"] {{color:{p['success']};}}
            QFrame#novelaiTaskPanel QLabel[naiTaskTone="warning"] {{color:{p['warning']};}}
            QFrame#novelaiTaskPanel QLabel[naiTaskTone="accent"] {{color:{p['accent']};}}
            QFrame#novelaiTaskPanel QToolButton {{background:transparent;color:{p['muted']};border:1px solid transparent;border-radius:6px;padding:5px 7px;font-size:12px;}}
            QFrame#novelaiTaskPanel QToolButton:hover {{background:{p['hover']};color:{p['accent']};}}
            QFrame#novelaiTaskPanel QPushButton#naiTaskWaiting {{background:{p['input']};color:{p['muted']};border:1px solid {p['border']};border-radius:8px;padding:9px 8px;font-size:12px;text-align:center;}}
            QFrame#novelaiTaskPanel QPushButton#naiTaskWaiting:hover {{background:{p['hover']};color:{p['accent']};border-color:{p['accent']};}}
            QFrame#novelaiTaskPanel QMenu {{background:{p['surface']};color:{p['text']};border:1px solid {p['border']};padding:5px;}}
            QFrame#novelaiTaskPanel QMenu::item {{padding:7px 14px;}}
            QFrame#novelaiTaskPanel QMenu::item:selected {{background:{p['accent_soft']};}}
            QFrame#novelaiTaskPanel QScrollBar:vertical {{background:transparent;width:8px;margin:0;}}
            QFrame#novelaiTaskPanel QScrollBar::handle:vertical {{background:{p['border']};border-radius:4px;min-height:28px;}}
            QFrame#novelaiTaskPanel QScrollBar::add-line:vertical,
            QFrame#novelaiTaskPanel QScrollBar::sub-line:vertical {{height:0;}}
            QFrame#novelaiTaskPanel QScrollBar::add-page:vertical,
            QFrame#novelaiTaskPanel QScrollBar::sub-page:vertical {{background:transparent;}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] {{border:none;border-radius:0;background:{p['canvas']};}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QLabel#naiTaskPanelTitle {{font-size:11px;color:{p['muted']};}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QFrame#naiTaskCard {{border:1px solid transparent;border-radius:5px;background:transparent;}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QFrame#naiTaskCard:hover {{background:{p['hover']};border-color:{p['border']};}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QFrame#naiTaskCard[naiTaskSelected="true"] {{background:{p['accent_soft']};border-color:{p['accent']};}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QLabel#naiTaskThumb {{border-radius:3px;background:{p['input']};}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QLabel#naiTaskState {{font-size:10px;font-weight:400;}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QToolButton {{padding:3px 5px;font-size:11px;}}
            QFrame#novelaiTaskPanel[naiCompactRail="true"] QPushButton#naiTaskWaiting {{padding:6px 3px;font-size:11px;border-radius:5px;}}
        """)
        for card in self._cards.values():
            for label in (card.state_label, card.title_label, card.meta_label):
                label._elide()

    def shutdown(self):
        self._closed = True
        self._pending_thumbnails.clear()
        self._thumbnail_timer.stop()
        if self._thumbnail_job is not None:
            self._thumbnail_job.cancel()
        self._thumbnail_cache.clear()
        self._preview_cache.clear()
        self._preview_times.clear()
        self._path_cache.clear()
