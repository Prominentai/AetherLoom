"""Optional RunningHub WebSocket node progress, subordinate to HTTP task state.

Protocol: https://www.runninghub.cn/runninghub-api-doc-en/doc-8287471
The reported value/max describes the current node, not total task completion.
"""
import json
import math
import time
from collections import OrderedDict
from PyQt5 import QtCore, QtGui, QtNetwork, QtWebSockets, QtWidgets, sip
from aetherloom_core.rh_ui import palette


def progress_text(progress):
    if not progress:
        return ''
    if not isinstance(progress, dict):
        progress = {'percent': progress}
    if progress.get('stale'):
        return '进度连接中断，等待重连'
    if progress.get('finished'):
        return '节点执行结束，等待结果确认'
    overall = progress.get('overall_percent')
    prefix = (f'总进度 {overall:.0f}% · ' if isinstance(overall, (int, float)) else '总进度未知 · ')
    node = str(progress.get('node_name') or progress.get('node') or '')
    percent = progress.get('percent')
    if percent is not None:
        return prefix + f'当前节点 {node}'
    if node:
        return prefix + f'节点 {node} · 等待进度'
    completed = progress.get('completed', 0)
    return prefix + (f'已完成 {completed} 个节点' if completed else '等待节点进度')


def progress_pair(status, progress=None):
    """Overall generation and current internal node; None means unavailable."""
    if status in {'SUCCESS', 'REUSED', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'WAITING_FOR_SECRET', 'DECODING'}:
        return 100, 100
    if status in {'PENDING', 'WAITING', 'PREPARING', 'QUEUED', 'SUBMITTING', 'LOCAL_WAIT'}:
        return 0, 0
    data = progress if isinstance(progress, dict) else {'percent': progress}
    if data.get('finished'):
        return 100, 100
    def percent(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
            return max(0, min(100, round(value))) if math.isfinite(value) else None
        except (ValueError, TypeError, OverflowError):
            return None
    return percent(data.get('overall_percent')), percent(data.get('percent'))


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


def draw_circular_progress(painter, rect, percent, status, colors, *, stroke=8, font=None, overall=None):
    """Passive shared paint routine; no QWidget, timer or network dependency."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setBrush(QtCore.Qt.NoBrush)
    color = colors['danger'] if status in ('FAILED', 'DOWNLOAD_FAILED', 'CANCEL_FAILED') else colors['accent']
    if status in ('CANCELED', 'INTERRUPTED'):
        color = colors['muted']
    inset = rect.width() * .16
    inner = rect.adjusted(inset, inset, -inset, -inset)
    for ring, value, tint in ((rect, overall, color), (inner, percent, colors['success'])):
        painter.setPen(QtGui.QPen(QtGui.QColor(colors['border']), stroke))
        painter.drawEllipse(ring)
        if value is None:
            painter.setPen(QtGui.QPen(QtGui.QColor(colors['muted']), max(1, stroke * .45), QtCore.Qt.DotLine))
            painter.drawEllipse(ring)
        elif value:
            painter.setPen(QtGui.QPen(QtGui.QColor(tint), stroke, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
            painter.drawArc(ring, 90 * 16, -round(360 * 16 * value / 100))
    font = QtGui.QFont(font or painter.font())
    font.setPixelSize(max(8, int(rect.width() * .22)))
    painter.setFont(font)
    painter.setPen(QtGui.QColor(colors['text']))
    painter.drawText(inner, QtCore.Qt.AlignCenter, '—' if overall is None else f'{overall}%')
    painter.restore()


class CircularProgress(QtWidgets.QWidget):
    """A passive view of shared task state; never starts polling or timers."""
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.percent = 0
        self.overall = 0
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
        self.setAccessibleName('外环总进度，内环当前节点进度')

    def set_state(self, status, progress=None):
        self.status = status
        progress = progress or {}
        self.overall, self.percent = progress_pair(status, progress)
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
        self.setAccessibleDescription(f'总进度 {self.overall if self.overall is not None else "未知"}；当前节点 {self.percent if self.percent is not None else "未知"} · {text}')
        self.setToolTip('外环：总进度（按内部节点完成数量）\n内环：当前节点进度\n' + str(text))
        self.update()

    def paintEvent(self, event):
        colors = palette(getattr(self.owner, '_theme_mode', 'dark'))
        painter = QtGui.QPainter(self)
        diameter = getattr(self, '_diameter', 124)
        rect = QtCore.QRectF((self.width() - diameter) / 2, 16, diameter, diameter)
        font = QtGui.QFont(self.font())
        font.setPointSize(max(14, int(24 * diameter / 124)))
        font.setBold(True)
        draw_circular_progress(painter, rect, self.percent, self.status, colors, font=font, overall=self.overall)
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
    """Count completed nodes only against a verified complete workflow map."""
    def __init__(self, task_id):
        self.task_id = str(task_id)
        self.prompt_id = None
        self.node = None
        self.done = set()
        self.percent = self.value = self.maximum = None
        self.finished = False
        self.node_names = {}
        self.mapping_mismatch = False

    def set_nodes(self, nodes):
        if isinstance(nodes, dict) and 0 < len(nodes) <= 10000:
            self.node_names = {str(key): str(value)[:120] for key, value in nodes.items()}
            self.mapping_mismatch = not self.done.issubset(self.node_names) or bool(self.node and self.node not in self.node_names)
        return self.snapshot()

    def snapshot(self):
        total = len(self.node_names) if self.node_names and not self.mapping_mismatch else None
        overall = 100 if self.finished else (min(100, len(self.done) / total * 100) if total else None)
        return dict(node=self.node, node_name=self.node_names.get(self.node, ''), percent=self.percent,
                    value=self.value, maximum=self.maximum, completed=len(self.done), total=total,
                    overall_percent=overall, finished=self.finished, stale=False)

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
            self.mapping_mismatch = False
        elif kind == 'execution_cached':
            nodes = data.get('nodes')
            if not isinstance(nodes, list):
                return None
            self.done.update(str(node)[:100] for node in nodes[:10000] if isinstance(node, (str, int)))
            if len(self.done) > 10000:
                self.done = set(sorted(self.done)[:10000])
        elif kind == 'execution_success':
            if self.node:
                self.done.add(self.node)
            self.finished = True
            self.percent = self.value = self.maximum = None
        elif kind == 'executing':
            node = data.get('node')
            node = str(node)[:100] if node is not None else None
            if node == self.node:
                return None
            if self.node and len(self.done) < 10000:
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
        if self.node_names and (not self.done.issubset(self.node_names) or self.node and self.node not in self.node_names):
            self.mapping_mismatch = True
        return self.snapshot()


class ProgressMonitor(QtCore.QObject):
    def __init__(self, owner, socket_factory=None):
        super().__init__(owner)
        self.owner = owner
        self.socket_factory = socket_factory or (lambda: QtWebSockets.QWebSocket(parent=self))
        self.connections = {}
        self._suspended = OrderedDict()
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
        state = dict(socket=socket, parser=self._suspended.pop(task_id, None) or NodeProgress(task_id),
                     seen=time.monotonic(), connected=False)
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
        self.queue(task_id, state['parser'].snapshot())

    def set_nodes(self, task_id, nodes):
        state = self.connections.get(task_id)
        if state is not None:
            self.queue(task_id, state['parser'].set_nodes(nodes))

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
        self.stop_task(task_id, preserve_parser=True)
        if self.owner._rh_status_entries.get(task_id) == 'RUNNING':
            self.queue(task_id, dict(previous, stale=True))

    def stop_task(self, task_id, preserve_parser=False):
        state = self.connections.pop(task_id, None)
        self._suspended.pop(task_id, None)
        if preserve_parser and state is not None:
            self._suspended[task_id] = state['parser']
            while len(self._suspended) > 64:
                self._suspended.popitem(last=False)
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
        self._suspended.clear()
        self.flush_timer.stop()
        self.heartbeat.stop()
