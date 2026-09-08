"""Optional RunningHub WebSocket node progress, subordinate to HTTP task state.

Protocol: https://www.runninghub.cn/runninghub-api-doc-en/doc-8287471
The reported value/max describes the current node, not total task completion.
"""
import json
import math
import time
from PyQt5 import QtCore, QtGui, QtNetwork, QtWebSockets, QtWidgets, sip
from aetherloom_core.rh_ui import palette


def progress_text(progress):
    if not progress:
        return ''
    if progress.get('stale'):
        return '进度连接中断，等待重连'
    if progress.get('finished'):
        return '节点执行结束，等待结果确认'
    node = str(progress.get('node') or '')
    percent = progress.get('percent')
    if percent is not None:
        return f'节点 {node} · {percent:.0f}% ({progress["value"]:g}/{progress["maximum"]:g})'
    if node:
        return f'节点 {node} · 等待进度'
    completed = progress.get('completed', 0)
    return f'已完成 {completed} 个节点，等待后续进度' if completed else '已连接，等待节点进度'


def progress_percent(status, progress=None, previous=0):
    """Normalize shared progress for both output cards and painted canvas nodes."""
    if status in ('DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET', 'SUCCESS'):
        return 100
    if status in ('PENDING', 'WAITING', 'PREPARING', 'QUEUED', 'SUBMITTING', 'LOCAL_WAIT'):
        return 0
    if isinstance(progress, dict):
        value = 100 if progress.get('finished') else progress.get('percent')
    else:
        value = progress
    if value is None:
        value = 0 if status == 'RUNNING' else previous
    try:
        value = float(value)
        return max(0, min(100, int(round(value)))) if math.isfinite(value) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def draw_circular_progress(painter, rect, percent, status, colors, *, stroke=8, font=None):
    """Passive shared paint routine; no QWidget, timer or network dependency."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.setPen(QtGui.QPen(QtGui.QColor(colors['border']), stroke))
    painter.drawEllipse(rect)
    color = colors['danger'] if status in ('FAILED', 'DOWNLOAD_FAILED', 'CANCEL_FAILED') else colors['accent']
    if status in ('CANCELED', 'INTERRUPTED'):
        color = colors['muted']
    painter.setPen(QtGui.QPen(QtGui.QColor(color), stroke, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
    if percent:
        painter.drawArc(rect, 90 * 16, -round(360 * 16 * percent / 100))
    if font is None:
        font = QtGui.QFont(painter.font())
        font.setPointSize(24)
    painter.setFont(font)
    painter.setPen(QtGui.QColor(colors['text']))
    painter.drawText(rect, QtCore.Qt.AlignCenter, f'{percent}%')
    painter.restore()


class CircularProgress(QtWidgets.QWidget):
    """A passive view of shared task state; never starts polling or timers."""
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.percent = 0
        self.status = 'SUBMITTING'
        self.setMinimumHeight(208)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 162, 8, 10)
        self.label = QtWidgets.QLabel('准备 / 等待提交', self)
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setTextFormat(QtCore.Qt.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.setAccessibleName('任务节点进度')

    def set_state(self, status, progress=None):
        self.status = status
        progress = progress or {}
        self.percent = progress_percent(status, progress, self.percent)
        messages = {'SUBMITTING': '准备 / 等待提交', 'LOCAL_WAIT': '等待提交',
                    'QUEUED': '云端排队中', 'RUNNING': '运行中 · 等待节点进度',
                    'DOWNLOADING': '生成完成 · 下载 / 处理输出中',
                    'DOWNLOAD_FAILED': '生成完成 · 等待下载重试',
                    'POLL_TIMEOUT': '等待重新查询任务状态', 'WAITING_FOR_KEY': '等待密钥',
                    'CANCELING': '正在取消 · 自动重试并确认状态',
                    'INTERRUPTED': '会话已中断',
                    'CANCEL_FAILED': '取消未确认 · 可再次取消', 'CANCELED': '已取消',
                    'FAILED': '任务失败', 'SUCCESS': '下载完成 · 准备展示结果'}
        self.set_message(progress_text(progress) if status == 'RUNNING' and progress else messages.get(status, status))

    def set_message(self, text):
        self.label.setText(str(text))
        self.setAccessibleDescription(f'{self.percent}% · {text}')
        self.update()

    def paintEvent(self, event):
        colors = palette(getattr(self.owner, '_theme_mode', 'dark'))
        painter = QtGui.QPainter(self)
        diameter = getattr(self, '_diameter', 124)
        rect = QtCore.QRectF((self.width() - diameter) / 2, 16, diameter, diameter)
        font = QtGui.QFont(self.font())
        font.setPointSize(max(14, int(24 * diameter / 124)))
        font.setBold(True)
        draw_circular_progress(painter, rect, self.percent, self.status, colors, font=font)
        painter.end()

    def set_available_height(self, available):
        text_height = max(self.label.fontMetrics().height(), self.label.heightForWidth(max(1, self.width() - 16)))
        height = max(text_height + 98, min(248, available))
        diameter = max(64, min(124, self.width() - 24, height - text_height - 38))
        self._diameter = diameter
        margins = QtCore.QMargins(8, diameter + 28, 8, 10)
        if self.layout().contentsMargins() != margins:
            self.layout().setContentsMargins(margins)
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)
        self.update()


def update_card_progress(owner, card, status, progress=None):
    if card is None or sip.isdeleted(card) or card.layout() is None:
        return
    card._rh_display_status = status
    widget = getattr(card, '_rh_progress_widget', None)
    if getattr(card, '_rh_results_presented', False):
        if widget is not None:
            widget.hide()
        return
    if widget is None:
        widget = CircularProgress(owner, card)
        card.layout().insertWidget(1, widget)
        card._rh_progress_widget = widget
    widget.set_state(status, progress)
    title = getattr(card, '_title_label', None)
    if title is not None:
        title.setText({'SUBMITTING': '准备任务', 'LOCAL_WAIT': '等待提交', 'QUEUED': '云端排队',
                       'RUNNING': '任务运行中', 'DOWNLOADING': '下载 / 处理输出',
                       'DOWNLOAD_FAILED': '等待下载重试', 'FAILED': '任务失败', 'CANCELED': '已取消',
                       'SUCCESS': '下载完成', 'POLL_TIMEOUT': '等待状态重查',
                       'CANCELING': '正在取消', 'CANCEL_FAILED': '取消未确认', 'WAITING_FOR_KEY': '等待密钥',
                       'INTERRUPTED': '会话已中断',
                       'WAITING_FOR_SECRET': '等待解码密码',
                       'UNKNOWN': '提交结果未知', 'PAUSED': '已暂停'}.get(status, '等待结果'))
    for name in ('_img_label', '_nav_wrap'):
        child = getattr(card, name, None)
        if child is not None and not sip.isdeleted(child):
            child.hide()
    widget.show()


class NodeProgress:
    """Bounded state; cached nodes and node transitions never imply total percent."""
    def __init__(self, task_id):
        self.task_id = str(task_id)
        self.prompt_id = None
        self.node = None
        self.done = set()
        self.percent = self.value = self.maximum = None
        self.finished = False

    def receive(self, message):
        if not isinstance(message, str) or len(message) > 65536:
            return None
        try:
            event = json.loads(message)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict) or not isinstance(event.get('data'), dict):
            return None
        kind, data = event.get('type'), event['data']
        if kind not in {'progress', 'executing', 'execution_cached', 'execution_start', 'execution_success'}:
            return None
        explicit_id = data.get('taskId', data.get('task_id'))
        if explicit_id is not None and str(explicit_id) != self.task_id:
            return None
        # The native prompt ID need not equal RH taskId. Bind the first ID on
        # this task-specific socket and ignore events belonging to another run.
        prompt = data.get('prompt_id')
        if prompt is not None:
            if self.prompt_id is not None and str(prompt) != self.prompt_id:
                return None
            self.prompt_id = str(prompt)
        if self.finished:
            return None
        if kind == 'execution_start':
            self.done.clear()
            self.node = None
            self.percent = self.value = self.maximum = None
        elif kind == 'execution_cached':
            nodes = data.get('nodes')
            if not isinstance(nodes, list):
                return None
            self.done.update(str(node)[:100] for node in nodes[:10000] if isinstance(node, (str, int)))
            if len(self.done) > 10000:
                self.done = set(sorted(self.done)[:10000])
        elif kind == 'execution_success':
            self.finished = True
            self.percent = self.value = self.maximum = None
        elif kind == 'executing':
            node = data.get('node')
            if self.node and node != self.node and len(self.done) < 10000:
                self.done.add(self.node)
            self.node = str(node)[:100] if node is not None else None
            self.percent = self.value = self.maximum = None
        elif kind == 'progress':
            node = data.get('node', self.node)
            if node is None:
                return None
            try:
                value, maximum = float(data['value']), float(data['max'])
                if (isinstance(data['value'], bool) or isinstance(data['max'], bool) or
                        not math.isfinite(value) or not math.isfinite(maximum) or maximum <= 0 or value < 0):
                    return None
            except (KeyError, ValueError, TypeError, OverflowError):
                return None
            self.node = str(node)[:100]
            self.maximum = maximum
            self.value = min(value, maximum)
            self.percent = self.value / maximum * 100
        return dict(node=self.node, percent=self.percent, value=self.value, maximum=self.maximum,
                    completed=len(self.done), finished=self.finished, stale=False)


class ProgressMonitor(QtCore.QObject):
    def __init__(self, owner, socket_factory=None):
        super().__init__(owner)
        self.owner = owner
        self.socket_factory = socket_factory or (lambda: QtWebSockets.QWebSocket(parent=self))
        self.connections = {}
        self.pending = {}
        self.flush_timer = QtCore.QTimer(self)
        self.flush_timer.setSingleShot(True)
        self.flush_timer.setInterval(250)
        self.flush_timer.timeout.connect(self.flush)
        self.heartbeat = QtCore.QTimer(self)
        self.heartbeat.setInterval(15000)
        self.heartbeat.timeout.connect(self.check_connections)
        owner.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Close:
            self.close()
        return False

    def connect_task(self, task_id, url):
        if getattr(self.owner, '_closing', False) or task_id in self.connections:
            return
        # Source events contain signed URLs, so never log/store/re-render them.
        parsed = QtCore.QUrl(url)
        if parsed.scheme() != 'wss' or not parsed.host() or parsed.userInfo():
            return
        socket = self.socket_factory()
        state = dict(socket=socket, parser=NodeProgress(task_id), seen=time.monotonic(), connected=False)
        self.connections[task_id] = state
        if hasattr(socket, 'setMaxAllowedIncomingMessageSize'):
            socket.setMaxAllowedIncomingMessageSize(2 * 1024 * 1024)
        socket.connected.connect(lambda: self.connected(task_id, state))
        socket.textMessageReceived.connect(lambda text: self.message(task_id, state, text))
        socket.pong.connect(lambda *_: state.update(seen=time.monotonic()))
        socket.disconnected.connect(lambda: self.failed(task_id, state))
        socket.error.connect(lambda *_: self.failed(task_id, state))
        socket.open(QtNetwork.QNetworkRequest(parsed))
        QtCore.QTimer.singleShot(8000, lambda: self.failed(task_id, state)
                                 if self.connections.get(task_id) is state and not state['connected'] else None)
        if self.connections:
            self.heartbeat.start()

    def connected(self, task_id, state):
        if self.connections.get(task_id) is not state:
            return
        state.update(connected=True, seen=time.monotonic())
        lifecycle = self.owner._rh_task_lifecycle
        with lifecycle.lock:
            lifecycle._progress_connected.add(task_id)
        self.queue(task_id, dict(node=None, percent=None, completed=0, stale=False))

    def message(self, task_id, state, text):
        if self.connections.get(task_id) is not state:
            return
        state['seen'] = time.monotonic()
        progress = state['parser'].receive(text)
        if progress is not None:
            self.queue(task_id, progress)

    def queue(self, task_id, progress):
        self.pending[task_id] = progress
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def failed(self, task_id, state):
        if self.connections.get(task_id) is not state:
            return
        previous = self.pending.get(task_id) or self.owner._rh_progress_entries.get(task_id) or {}
        self.stop_task(task_id)
        if self.owner._rh_status_entries.get(task_id) == 'RUNNING':
            self.queue(task_id, dict(previous, stale=True))

    def stop_task(self, task_id):
        state = self.connections.pop(task_id, None)
        self.pending.pop(task_id, None)
        lifecycle = getattr(self.owner, '_rh_task_lifecycle', None)
        if lifecycle is not None:
            with lifecycle.lock:
                lifecycle._progress_connected.discard(task_id)
        if state is not None:
            state['socket'].abort()
            state['socket'].deleteLater()
        if not self.connections:
            self.heartbeat.stop()

    def sync_card(self, task_id, status):
        card = self.owner._rh_task_contexts.get(task_id, {}).get('card')
        if card is None or sip.isdeleted(card):
            return
        if status == 'SUCCESS':
            card._rh_results_ready = True
            present = getattr(card, '_rh_present_pending', None)
            if callable(present):
                present()
        elif status in ('FAILED', 'CANCELED'):
            card._rh_pending_outputs = []
        update_card_progress(self.owner, card, status, self.owner._rh_progress_entries.get(task_id))

    def check_connections(self):
        now = time.monotonic()
        for task_id, state in list(self.connections.items()):
            if now - state['seen'] > 45:
                self.failed(task_id, state)
            else:
                state['socket'].ping()

    def flush(self):
        pending, self.pending = self.pending, {}
        for task_id, progress in pending.items():
            if self.owner._rh_status_entries.get(task_id) != 'RUNNING' or getattr(self.owner, '_closing', False):
                continue
            self.owner._rh_progress_entries[task_id] = progress
            card = self.owner._rh_task_contexts.get(task_id, {}).get('card')
            update_card_progress(self.owner, card, 'RUNNING', progress)
        dashboard = getattr(self.owner, '_rh_dashboard', None)
        if dashboard is not None and pending:
            dashboard.refresh()

    def close(self):
        for task_id in list(self.connections):
            self.stop_task(task_id)
        self.pending.clear()
        self.flush_timer.stop()
        self.heartbeat.stop()
