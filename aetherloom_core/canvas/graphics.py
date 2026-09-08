"""Lightweight graphics items for the native canvas (no per-node media players)."""
import math
import os
from collections import OrderedDict

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_ui import palette
from aetherloom_core.media_limits import MAX_DECODE_PIXELS
from aetherloom_core.rh_progress import draw_circular_progress, progress_percent, current_node_percent, progress_text
from .model import input_ports, output_types
from . import model


KIND_NAMES = {'app': 'APP', 'image': '图像', 'video': '视频', 'audio': '音频',
              'text': '文本', 'select': '结果选择', 'preview': '预览 / 保存'}
STATUS_NAMES = {'IDLE': '就绪', 'READY': '就绪', 'WAITING': '等待上游',
                'SKIPPED': '已跳过', 'INTERRUPTED': '会话已中断',
                'PENDING': '等待上游',
                'PREPARING': '准备输入',
                'LOCAL_WAIT': '等待提交', 'SUBMITTING': '正在提交', 'QUEUED': '云端排队',
                'RUNNING': '运行中', 'DOWNLOADING': '正在下载', 'DOWNLOAD_FAILED': '等待下载重试',
                'SUCCESS': '已完成', 'FAILED': '失败', 'CANCELED': '已取消', 'BLOCKED': '已终止 · 上游不可用',
                'PAUSED': '已暂停', 'UNKNOWN': '结果未知', 'REUSED': '复用结果',
                'WAITING_FOR_KEY': '等待 API 配置', 'WAITING_FOR_SECRET': '等待解码密码',
                'POLL_TIMEOUT': '等待查询恢复', 'CANCELING': '取消中', 'CANCEL_FAILED': '取消待确认'}


RUNNING_STATES = frozenset({'RUNNING', 'DOWNLOADING', 'DECODING'})
WAITING_STATES = frozenset({'PENDING', 'WAITING', 'PREPARING', 'SUBMITTING', 'LOCAL_WAIT', 'QUEUED',
                            'DOWNLOAD_FAILED', 'WAITING_FOR_KEY', 'WAITING_FOR_SECRET',
                            'POLL_TIMEOUT', 'CANCELING', 'CANCEL_FAILED', 'RETRYING'})


def node_is_active(node):
    if 'activated' in node:
        return bool(node['activated'])
    # Old snapshots predate the explicit dependency activation flag. A pending
    # dependency never counts as active; submitted/running task states do.
    return node.get('status') in (RUNNING_STATES | WAITING_STATES) - {'PENDING', 'WAITING'}


def node_state_color(node, colors):
    status = str(node.get('status') or 'IDLE')
    if status == 'FAILED':
        return colors['danger']
    if node_is_active(node):
        if status in RUNNING_STATES:
            return colors['success']
        if status in WAITING_STATES:
            return colors['warning']
    return None


class _ThumbnailSignals(QtCore.QObject):
    complete = QtCore.pyqtSignal(object, object)


class _ThumbnailWorker(QtCore.QRunnable):
    def __init__(self, key, signals):
        super().__init__()
        self.key, self.signals = key, signals

    def run(self):
        image = None
        try:
            reader = QtGui.QImageReader(self.key[0])
            size = reader.size()
            if size.isValid() and size.width() * size.height() <= MAX_DECODE_PIXELS:
                reader.setAutoTransform(True)
                reader.setScaledSize(size.scaled(512, 320, QtCore.Qt.KeepAspectRatio))
                decoded = reader.read()
                if not decoded.isNull():
                    image = decoded
        except Exception:
            image = None
        try:
            self.signals.complete.emit(self.key, image)
        except RuntimeError:
            pass


class ThumbnailCache(QtCore.QObject):
    """Visible-only requests, two decoders, 64 small GUI-owned pixmaps."""
    ready = QtCore.pyqtSignal()

    def __init__(self, parent=None, limit=64):
        super().__init__(parent)
        self.limit, self.entries = limit, OrderedDict()
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.pool.setExpiryTimeout(1000)
        self.signals = _ThumbnailSignals(self)
        self.signals.complete.connect(self._complete)
        self.pending = set()
        self.closed = False

    @QtCore.pyqtSlot(object, object)
    def _complete(self, key, decoded):
        self.pending.discard(key)
        if self.closed:
            return
        self.entries[key] = QtGui.QPixmap.fromImage(decoded) if decoded is not None else None
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        self.ready.emit()

    def close(self):
        self.closed = True
        self.pool.clear()
        self.entries.clear()

    def get(self, path):
        if self.closed:
            return None
        try:
            stat = os.stat(path)
            key = (path, stat.st_size, stat.st_mtime_ns)
        except (OSError, TypeError):
            return None
        if key in self.entries:
            result = self.entries.pop(key)
            self.entries[key] = result
            return result
        if key not in self.pending and len(self.pending) < 2:
            self.pending.add(key)
            self.pool.start(_ThumbnailWorker(key, self.signals))
        return None


class PortItem(QtWidgets.QGraphicsEllipseItem):
    def __init__(self, node, key, label, kind, output=False):
        super().__init__(-7, -7, 14, 14, node)
        self.node_item, self.key, self.output = node, key, output
        self.kind = kind
        self.label = label
        self.connected = False
        self.setToolTip(('输出' if output else label) + ' · ' + str(kind))
        self.setBrush(QtGui.QColor('#739bff' if output else '#7bcab4'))
        self.setPen(QtGui.QPen(QtGui.QColor('#162032'), 2))
        self.setZValue(5)
        self.setAcceptedMouseButtons(QtCore.Qt.NoButton)

    def refresh_connection(self, connected=False):
        self.connected = bool(connected)
        colors = self.node_item.canvas_scene.colors
        type_colors = {'image': '#6fc6a6', 'video': '#dd84bd', 'audio': '#e6b169', 'text': '#76a6ef',
                       'number': '#aa8ce5', 'scalar': '#cbbb73', 'file': '#73bdc4', 'any': '#a2afbf'}
        color = QtGui.QColor(type_colors.get(self.kind, colors['accent']))
        self.setBrush(color if self.output or connected else QtGui.QColor(colors['surface']))
        self.setPen(QtGui.QPen(color, 2))
        if self.output:
            self.setToolTip('结果输出 · 拖到输入端口或空白处添加下游节点')
        else:
            has_internal = self.node_item.node['kind'] == 'app'
            if connected:
                detail = ('已连接：运行时覆盖内部值；' if has_internal else '已连接上游结果；') + '拖到其他端口可改接，拖到空白处可断开'
            else:
                detail = '未连接：使用节点内部值；也可拖动连接输出' if has_internal else '等待连接上游结果；此输入为必填'
            self.setToolTip(self.label + ' · ' + str(self.kind) + '\n' + detail)


class NodeItem(QtWidgets.QGraphicsObject):
    WIDTH = 268

    def __init__(self, node, scene):
        super().__init__()
        self.node, self.canvas_scene = node, scene
        self.ports = {}
        self._start_positions = None
        self._resize_start = None
        self._resize_hover = False
        self._result_offset = 0
        self.setFlags(self.ItemIsMovable | self.ItemIsSelectable | self.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setCacheMode(self.NoCache)
        self.setToolTip('拖动右下角调整大小；双击结果可在设置面板中查看。')
        ports = input_ports(node)
        # Reserve a result area below all input ports, including Apps with many
        # fields; result arrival then needs no geometry rebuild or moving ports.
        self.minimum_height = max(224 if node['kind'] in ('image', 'video') else 192,
                                  198 + 25 * max(0, len(ports) - 1))
        self.width, self.height = self.normalized_size(node.get('size'))
        for index, port in enumerate(ports):
            item = PortItem(self, port['key'], port['label'], port['type'])
            item.setPos(0, 66 + index * 25)
            self.ports[port['key']] = item
        types = output_types(node)
        self.output = PortItem(self, 'output', '结果', next(iter(types)) if len(types) == 1 else 'any', True)
        self.output.setPos(self.width, 66)
        self.setPos(float(node.get('x', 0)), float(node.get('y', 0)))

    def normalized_size(self, size):
        defaults = (self.WIDTH, self.minimum_height)
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            return defaults
        result = []
        for value, minimum in zip(size, defaults):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                value = minimum
            result.append(max(minimum, min(max(4096, minimum), float(value))))
        return tuple(result)

    def set_size(self, size):
        width, height = self.normalized_size(size)
        if (width, height) == (self.width, self.height):
            return
        self.prepareGeometryChange()
        self.width, self.height = width, height
        self.output.setPos(width, 66)
        self.canvas_scene.update_edges(self.node['id'])
        self.update()

    def resize_rect(self):
        return QtCore.QRectF(self.width - 22, self.height - 22, 22, 22)

    def _set_resize_hover(self, hovered):
        if hovered != self._resize_hover:
            self._resize_hover = hovered
            self.update(self.resize_rect())
        if hovered:
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        else:
            self.unsetCursor()

    def hoverMoveEvent(self, event):
        self._set_resize_hover(self.resize_rect().contains(event.pos()))
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        if self._resize_start is None:
            self._set_resize_hover(False)
        super().hoverLeaveEvent(event)

    def finish_resize(self, cancel=False):
        start = self._resize_start
        if start is None:
            return
        self._resize_start = None
        self.canvas_scene._resizing_node = None
        self._set_resize_hover(False)
        if cancel:
            self.set_size(start[1])
            if self.scene().mouseGrabberItem() is self:
                self.ungrabMouse()
        elif (self.width, self.height) != start[1]:
            # Commit once on release. Live resizing never edits execution state.
            self.canvas_scene.nodes_resized.emit({self.node['id']: [self.width, self.height]})

    def boundingRect(self):
        return QtCore.QRectF(-9, -3, self.width + 112, self.height + 8)

    def shape(self):
        shape = QtGui.QPainterPath()
        shape.addRoundedRect(QtCore.QRectF(0, 0, self.width, self.height), 12, 12)
        if self.node.get('decode_settings', {}).get('enabled'):
            shape.addRect(self.decode_rect())
        return shape

    def decode_rect(self):
        return QtCore.QRectF(self.width + 10, 94, 86, 27)

    def run_rect(self):
        return QtCore.QRectF(self.width - 66, 13, 52, 25)

    def progress_rect(self):
        return QtCore.QRectF(self.run_rect().center().x() - 22, 2, 44, 44)

    def shows_progress(self):
        return (self.node['kind'] == 'app' and node_is_active(self.node)
                and self.node.get('status') in RUNNING_STATES | WAITING_STATES)

    def itemChange(self, change, value):
        if change == self.ItemPositionHasChanged and hasattr(self, 'canvas_scene'):
            self.canvas_scene.update_edges(self.node['id'])
        return super().itemChange(change, value)

    def result_count(self):
        results = self.node.get('results') or []
        if results:
            return len(results)
        if self.node['kind'] in model.MEDIA:
            return len(self.node.get('params', {}).get('files') or [])
        return 0

    def result_at(self, index):
        results = self.node.get('results') or []
        value = results[index] if results else {'path': self.node['params']['files'][index], 'type': self.node['kind']}
        return value if isinstance(value, dict) else {'path': str(value)}

    def content_rect(self):
        top = 83 + 25 * max(0, len(self.ports) - 1)
        return QtCore.QRectF(14, top, self.width - 28, self.height - top - 43)

    def result_layout(self):
        """Constant-size visible slice even when a node has thousands of results."""
        count = self.result_count()
        if not count:
            return [], 0, 0
        content = self.content_rect()
        columns, rows = self.result_grid()
        capacity = columns * rows
        page = min(self._result_offset, count - 1) // capacity
        start = page * capacity
        width = (content.width() - (columns - 1) * 6) / columns
        height = (content.height() - 24 - (rows - 1) * 6) / rows
        tiles = [(index, QtCore.QRectF(content.x() + (slot % columns) * (width + 6),
                                       content.y() + 24 + (slot // columns) * (height + 6), width, height))
                 for slot, index in enumerate(range(start, min(count, start + capacity)))]
        return tiles, page, (count + capacity - 1) // capacity

    def result_grid(self):
        content = self.content_rect()
        return (min(3, max(1, int((content.width() + 6) // 146))),
                min(2, max(1, int((content.height() - 18) // 96))))

    def result_nav_rects(self):
        content = self.content_rect()
        return (QtCore.QRectF(content.right() - 50, content.top(), 22, 20),
                QtCore.QRectF(content.right() - 22, content.top(), 22, 20))

    def _paint_results(self, painter):
        p = self.canvas_scene.colors
        tiles, page, pages = self.result_layout()
        content = self.content_rect()
        painter.save()
        painter.setClipRect(content)
        painter.setPen(QtGui.QColor(p['muted']))
        painter.drawText(QtCore.QRectF(content.x(), content.y(), content.width() - 54, 20),
                         QtCore.Qt.AlignVCenter, f'结果 {self.result_count()} · {page + 1}/{pages}')
        for rect, label, enabled in zip(self.result_nav_rects(), ('‹', '›'), (page > 0, page + 1 < pages)):
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(p['accent_soft'] if enabled else p['surface']))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QtGui.QColor(p['accent'] if enabled else p['muted']))
            painter.drawText(rect, QtCore.Qt.AlignCenter, label)
        for index, rect in tiles:
            result = self.result_at(index)
            kind = model.result_type(result)
            path = result.get('path') or result.get('file_path') or ''
            painter.setPen(QtGui.QPen(QtGui.QColor(p['border']), 1))
            painter.setBrush(QtGui.QColor(p['input']))
            painter.drawRoundedRect(rect, 5, 5)
            painter.save()
            painter.setClipRect(rect.adjusted(4, 2, -4, -2), QtCore.Qt.IntersectClip)
            title = f"{index + 1} · {KIND_NAMES.get(kind, '数值' if kind in ('scalar', 'number') else '文件')}"
            painter.setPen(QtGui.QColor(p['accent']))
            painter.drawText(rect.adjusted(6, 0, -6, 0), QtCore.Qt.AlignTop, title)
            body = rect.adjusted(6, 19, -6, -5)
            thumb = None
            if (kind == 'image' and path and index in self.canvas_scene.thumbnail_slots.get(self.node['id'], ())):
                thumb = self.canvas_scene.thumbnails.get(path)
            if thumb is not None and not thumb.isNull():
                size = thumb.size().scaled(body.size().toSize(), QtCore.Qt.KeepAspectRatio)
                target = QtCore.QRectF(body.center().x() - size.width() / 2,
                                       body.center().y() - size.height() / 2, size.width(), size.height())
                painter.drawPixmap(target, thumb, QtCore.QRectF(thumb.rect()))
            else:
                text = str(result['text'] if 'text' in result else result['value'] if 'value' in result
                           else os.path.basename(path) or result.get('url') or '暂无本地预览')
                painter.setPen(QtGui.QColor(p['text']))
                painter.drawText(body, QtCore.Qt.AlignVCenter | QtCore.Qt.TextWordWrap, text[:240])
            painter.restore()
        painter.restore()

    def paint(self, painter, option, widget=None):
        p = self.canvas_scene.colors
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        selected = self.isSelected()
        state_color = node_state_color(self.node, p)
        border = state_color or (p['accent'] if selected else p['border'])
        width = (3.3 if selected else 2.3) if state_color else (2 if selected else 1)
        painter.setPen(QtGui.QPen(QtGui.QColor(border), width))
        painter.setBrush(QtGui.QColor(p['surface']))
        painter.drawRoundedRect(QtCore.QRectF(0, 0, self.width, self.height), 12, 12)
        font = painter.font()
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['text']))
        title = str(self.node.get('title') or KIND_NAMES.get(self.node['kind'], '节点'))
        painter.drawText(QtCore.QRectF(15, 12, self.width - 90, 28), QtCore.Qt.AlignVCenter,
                         QtGui.QFontMetrics(font).elidedText(title, QtCore.Qt.ElideRight, int(self.width - 91)))
        show_progress = self.shows_progress()
        if show_progress:
            current = current_node_percent(self.node.get('status'), self.node.get('node_progress'))
            draw_circular_progress(painter, self.progress_rect(), current,
                                   self.node.get('status'), p, stroke=2)
        else:
            painter.setBrush(QtGui.QColor(p['accent_soft']))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(self.run_rect(), 5, 5)
            font.setBold(False)
            font.setPixelSize(11)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(p['accent']))
            painter.drawText(self.run_rect(), QtCore.Qt.AlignCenter, '运行')
        font.setBold(False)
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['border']))
        painter.drawLine(QtCore.QPointF(14, 48), QtCore.QPointF(self.width - 14, 48))
        painter.setPen(QtGui.QColor(p['muted']))
        for index, port in enumerate(input_ports(self.node)):
            connected = self.ports[port['key']].connected
            label = str(port['label']) + (' · 连线' if connected else ' · 内部值' if self.node['kind'] == 'app' else ' · 等待连接')
            label_width = int(self.width - 78)
            painter.drawText(QtCore.QRectF(14, 54 + index * 25, label_width, 24), QtCore.Qt.AlignVCenter,
                             QtGui.QFontMetrics(font).elidedText(label, QtCore.Qt.ElideRight, label_width - 5))
        painter.drawText(QtCore.QRectF(self.width - 60, 54, 44, 24), QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, '结果')
        content = self.content_rect()
        if content.height() > 20:
            if self.result_count():
                self._paint_results(painter)
            else:
                params = self.node.get('params', {})
                summary = str(params.get('text') or '')
                if self.node['kind'] in ('image', 'video', 'audio'):
                    files = params.get('files', [])
                    summary = f'{len(files)} 个文件' + (' · ' + os.path.basename(files[0]) if files else ' · 选择或拖入素材')
                elif self.node['kind'] == 'app':
                    summary = f'{len(self.ports)} 个输入'
                elif not summary:
                    summary = '连接上游结果' if self.ports else KIND_NAMES.get(self.node['kind'], '')
                painter.drawText(content, QtCore.Qt.AlignVCenter | QtCore.Qt.TextWordWrap, summary[:150])
        status = str(self.node.get('status') or 'IDLE')
        color = state_color or (p['success'] if status in ('SUCCESS', 'REUSED') else p['muted'])
        painter.setPen(QtGui.QColor(color))
        label = '等待调度' if status == 'PENDING' and node_is_active(self.node) else STATUS_NAMES.get(status, status)
        progress = self.node.get('progress')
        if not show_progress and progress is not None and status in RUNNING_STATES:
            label += f'  {progress_percent(status, progress)}%'
        if (self.node.get('_ui_stale') or self.node.get('stale')) and (self.node.get('results') or status not in ('IDLE', 'READY')):
            label += ' · 参数已修改'
        painter.drawText(QtCore.QRectF(14, self.height - 34, self.width - 44, 24), QtCore.Qt.AlignVCenter,
                         QtGui.QFontMetrics(font).elidedText(label, QtCore.Qt.ElideRight, int(self.width - 44)))
        painter.setPen(QtGui.QPen(QtGui.QColor(p['accent'] if self._resize_hover or self._resize_start else p['muted']),
                                 1.5, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        for offset in (7, 12):
            painter.drawLine(QtCore.QPointF(self.width - 6 - offset, self.height - 6),
                             QtCore.QPointF(self.width - 6, self.height - 6 - offset))
        if self.node.get('decode_settings', {}).get('enabled'):
            painter.setPen(QtGui.QPen(QtGui.QColor(p['border']), 1))
            painter.drawLine(QtCore.QPointF(self.width, 107), QtCore.QPointF(self.width + 10, 107))
            painter.setBrush(QtGui.QColor(p['accent_soft']))
            painter.drawRoundedRect(self.decode_rect(), 4, 4)
            painter.setPen(QtGui.QColor(p['accent']))
            painter.drawText(self.decode_rect(), QtCore.Qt.AlignCenter, '本地解码')

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.result_count():
            tiles, page, pages = self.result_layout()
            for step, rect in zip((-1, 1), self.result_nav_rects()):
                if rect.contains(event.pos()):
                    if 0 <= page + step < pages:
                        # Capacity can change while resizing; keep a result anchor.
                        columns, rows = self.result_grid()
                        capacity = columns * rows
                        self._result_offset = (page + step) * capacity
                        self.update()
                    event.accept()
                    return
        if event.button() == QtCore.Qt.LeftButton and self.resize_rect().contains(event.pos()):
            if not self.isSelected():
                if not event.modifiers() & QtCore.Qt.ControlModifier:
                    self.canvas_scene.clearSelection()
                self.setSelected(True)
            self._start_positions = None
            self._resize_start = (event.scenePos(), (self.width, self.height))
            self.canvas_scene._resizing_node = self
            self._set_resize_hover(True)
            event.accept()
            return
        if (event.button() == QtCore.Qt.LeftButton and not self.shows_progress()
                and self.run_rect().contains(event.pos())):
            self.canvas_scene.run_requested.emit(self.node['id'], False)
            event.accept()
            return
        if event.button() == QtCore.Qt.LeftButton and self.node.get('decode_settings', {}).get('enabled') and self.decode_rect().contains(event.pos()):
            self.canvas_scene.clearSelection()
            self.setSelected(True)
            self.canvas_scene.decode_requested.emit(self.node['id'])
            event.accept()
            return
        self._start_positions = {item.node['id']: (item.pos().x(), item.pos().y())
                                 for item in self.scene().selectedItems() if isinstance(item, NodeItem)}
        self._start_positions[self.node['id']] = (self.pos().x(), self.pos().y())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_start is not None:
            origin, size = self._resize_start
            delta = event.scenePos() - origin
            self.set_size((size[0] + delta.x(), size[1] + delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.node.get('results'):
            for index, rect in self.result_layout()[0]:
                if rect.contains(event.pos()):
                    self.canvas_scene.result_requested.emit(self.node['id'], index)
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_start is not None and event.button() == QtCore.Qt.LeftButton:
            self.finish_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        positions = {key: (self.canvas_scene.nodes[key].x(), self.canvas_scene.nodes[key].y())
                     for key, before in (self._start_positions or {}).items()
                     if key in self.canvas_scene.nodes and before != (self.canvas_scene.nodes[key].x(), self.canvas_scene.nodes[key].y())}
        if positions:
            self.canvas_scene.nodes_moved.emit(positions)
        self._start_positions = None


def connection_path(start, end, direction=1):
    distance = max(60, min(180, abs(end.x() - start.x()) * .5))
    path = QtGui.QPainterPath(start)
    offset = QtCore.QPointF(direction * distance, 0)
    path.cubicTo(start + offset, end - offset, end)
    return path


class EdgeItem(QtWidgets.QGraphicsPathItem):
    def __init__(self, edge, scene):
        super().__init__()
        self.edge, self.canvas_scene = edge, scene
        self.setFlag(self.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self._hovered = False
        self.setPen(QtGui.QPen(QtGui.QColor(scene.colors['muted']), 3))
        self.setZValue(-1)
        self.update_path()

    def update_path(self):
        scene, edge = self.canvas_scene, self.edge
        if edge['source'] not in scene.nodes or edge['target'] not in scene.nodes:
            return
        target = scene.nodes[edge['target']].ports.get(edge['input'])
        if target is None:
            return
        start, end = scene.nodes[edge['source']].output.scenePos(), target.scenePos()
        path = connection_path(start, end)
        self.setPath(path)
        fraction = min(.2, 20 / max(1, path.length()))
        self._endpoints = path.pointAtPercent(fraction), path.pointAtPercent(1 - fraction)
        self.setToolTip({'first': '首个匹配结果', 'index': '指定结果', 'all': '全部匹配结果逐项运行'}.get(edge.get('mode'), '首个匹配结果')
                        + '\n拖动端点可改接，释放到空白处断开')

    def shape(self):
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(15)
        return stroker.createStroke(self.path())

    def boundingRect(self):
        return super().boundingRect().adjusted(-6, -6, 6, 6)

    def paint(self, painter, option, widget=None):
        highlighted = self.isSelected() or self._hovered
        painter.save()
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(self.canvas_scene.colors['accent'] if highlighted else self.canvas_scene.colors['muted']),
                                 3 if highlighted else 2, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        painter.drawPath(self.path())
        if highlighted:
            painter.setBrush(QtGui.QColor(self.canvas_scene.colors['surface']))
            painter.setPen(QtGui.QPen(QtGui.QColor(self.canvas_scene.colors['accent']), 2))
            for point in self.endpoints():
                painter.drawEllipse(point, 5, 5)
        painter.restore()

    def endpoints(self):
        # Keep the handles off shared output sockets so starting a new branch
        # never unexpectedly disconnects an existing branch.
        return getattr(self, '_endpoints', ())

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)


class CanvasScene(QtWidgets.QGraphicsScene):
    nodes_moved = QtCore.pyqtSignal(dict)
    nodes_resized = QtCore.pyqtSignal(dict)
    result_requested = QtCore.pyqtSignal(str, int)
    connect_requested = QtCore.pyqtSignal(str, str, str)
    run_requested = QtCore.pyqtSignal(str, bool)
    decode_requested = QtCore.pyqtSignal(str)
    action_requested = QtCore.pyqtSignal(str)
    add_requested = QtCore.pyqtSignal(object, object)
    reconnect_requested = QtCore.pyqtSignal(str, str, str, str)
    disconnect_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes, self.edges = {}, {}
        self._resizing_node = None
        self.colors = palette('dark')
        self.thumbnails = ThumbnailCache(self)
        self.thumbnails.ready.connect(self.update)
        self.thumbnail_nodes = set()
        self.thumbnail_slots = {}
        self._link_port, self._draft = None, None
        self._rewire_edge = None
        self._rewire_end = 'target'
        self._hover_port = None
        self._hover_valid = False
        self._link_clock = QtCore.QElapsedTimer()
        self._link_animation = QtCore.QTimer(self)
        self._link_animation.setInterval(16)
        self._link_animation.timeout.connect(self._animate_link)
        self.setSceneRect(-20000, -20000, 40000, 40000)

    def set_document(self, doc):
        self.cancel_resize()
        self.cancel_link()
        self.clear()
        self._document = doc
        self.nodes, self.edges = {}, {}
        self._link_port, self._draft = None, None
        self._rewire_edge = None
        for node in doc.get('nodes', []):
            item = NodeItem(node, self)
            self.nodes[node['id']] = item
            self.addItem(item)
        for edge in doc.get('edges', []):
            item = EdgeItem(edge, self)
            self.edges[edge['id']] = item
            self.addItem(item)
        self.refresh_ports()

    def refresh_ports(self):
        connected = {(item.edge['target'], item.edge['input']) for item in self.edges.values()}
        for node_id, node in self.nodes.items():
            node.output.refresh_connection()
            for key, port in node.ports.items():
                port.refresh_connection((node_id, key) in connected)

    def refresh_nodes(self, doc):
        self._document = doc
        for node in doc.get('nodes', []):
            item = self.nodes.get(node['id'])
            if item:
                item.node = node
                if item._resize_start is None:
                    item.set_size(node.get('size'))
                types = output_types(node)
                item.output.kind = next(iter(types)) if len(types) == 1 else 'any'
                item.output.refresh_connection()
                item.setToolTip(str(node.get('error') or node.get('message') or '')
                                + '\n当前节点进度\n' + progress_text(node.get('node_progress'))
                                + '\n拖动右下角调整大小；双击结果可在设置面板中查看。')
                item.update()

    def cancel_resize(self):
        if self._resizing_node is not None:
            self._resizing_node.finish_resize(cancel=True)

    def update_edges(self, node_id):
        for item in self.edges.values():
            if item.edge['source'] == node_id or item.edge['target'] == node_id:
                item.update_path()

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QtGui.QColor(self.colors['canvas']))
        scale = abs(painter.transform().m11()) or 1
        step = 24 if scale > .45 else 96
        left, top = math.floor(rect.left() / step) * step, math.floor(rect.top() / step) * step
        painter.setPen(QtGui.QPen(QtGui.QColor(self.colors['border']), 0))
        points = [QtCore.QPointF(x, y) for x in range(int(left), int(rect.right()) + step, step)
                  for y in range(int(top), int(rect.bottom()) + step, step)]
        if points:
            painter.drawPoints(*points)

    def _port_at(self, pos):
        radius = 12 / max(.12, abs(self.views()[0].transform().m11())) if self.views() else 12
        ports = [item for item in self.items(QtCore.QRectF(pos.x() - radius, pos.y() - radius, radius * 2, radius * 2))
                 if isinstance(item, PortItem) and QtCore.QLineF(pos, item.scenePos()).length() <= radius]
        return min(ports, key=lambda item: QtCore.QLineF(pos, item.scenePos()).length()) if ports else None

    def _edge_endpoint_at(self, pos):
        for item in self.items(QtCore.QRectF(pos.x() - 10, pos.y() - 10, 20, 20)):
            if isinstance(item, EdgeItem) and (item.isSelected() or item._hovered):
                for end, point in zip(('source', 'target'), item.endpoints()):
                    if QtCore.QLineF(pos, point).length() <= 9:
                        return item, end
        return None, None

    def _connection_to(self, port):
        first = self._link_port
        if port is None or port is first:
            return None
        if self._rewire_edge is not None:
            edge = self._rewire_edge.edge
            if self._rewire_end == 'source' and port.output:
                return port.node_item.node['id'], edge['target'], edge['input']
            if self._rewire_end == 'target' and not port.output:
                return edge['source'], port.node_item.node['id'], port.key
            return None
        if first.output == port.output:
            return None
        source, target = (first, port) if first.output else (port, first)
        return source.node_item.node['id'], target.node_item.node['id'], target.key

    def _valid_connection(self, port):
        connection = self._connection_to(port)
        if connection is None:
            return False
        source, target, key = connection
        old_id = self._rewire_edge.edge['id'] if self._rewire_edge is not None else None
        graph = dict(self._document, edges=[edge for edge in self._document['edges']
                     if edge['id'] != old_id and (edge['target'], edge['input']) != (target, key)])
        try:
            model.connect(graph, source, target, key)
            return True
        except ValueError:
            return False

    def _animate_link(self):
        if self._draft is None:
            self._link_animation.stop()
            return
        pen = self._draft.pen()
        pen.setDashOffset(-self._link_clock.elapsed() / 65)
        self._draft.setPen(pen)

    def mousePressEvent(self, event):
        port = self._port_at(event.scenePos())
        edge, end = self._edge_endpoint_at(event.scenePos()) if event.button() == QtCore.Qt.LeftButton else (None, None)
        if edge is not None:
            port = (self.nodes[edge.edge['source']].output if end == 'source'
                    else self.nodes[edge.edge['target']].ports[edge.edge['input']])
        if event.button() == QtCore.Qt.LeftButton and port is not None:
            self._link_port = port
            self._rewire_end = end or 'target'
            self._rewire_edge = edge or next((item for item in self.edges.values() if not port.output
                                     and item.edge['target'] == port.node_item.node['id']
                                     and item.edge['input'] == port.key), None)
            if self._rewire_edge is not None:
                self._rewire_edge.setOpacity(.25)
            self._draft = self.addPath(QtGui.QPainterPath(), QtGui.QPen(QtGui.QColor(self.colors['accent']), 3, QtCore.Qt.DashLine, QtCore.Qt.RoundCap))
            self._draft.setZValue(10)
            self._link_clock.start()
            self._link_animation.start()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._link_port is not None:
            anchor = self._link_port
            if self._rewire_edge is not None:
                edge = self._rewire_edge.edge
                anchor = (self.nodes[edge['target']].ports[edge['input']] if self._rewire_end == 'source'
                          else self.nodes[edge['source']].output)
            candidate = self._port_at(event.scenePos())
            if candidate is not self._hover_port:
                if self._hover_port is not None:
                    self._hover_port.refresh_connection(self._hover_port.connected)
                self._hover_port = candidate
                self._hover_valid = self._valid_connection(candidate)
                if candidate is not None:
                    color = self.colors['success'] if self._hover_valid else self.colors['danger']
                    candidate.setPen(QtGui.QPen(QtGui.QColor(color), 4))
            start = anchor.scenePos()
            end = candidate.scenePos() if self._hover_valid else event.scenePos()
            direction = 1 if anchor.output else -1
            self._draft.setPath(connection_path(start, end, direction))
            pen = self._draft.pen()
            pen.setColor(QtGui.QColor(self.colors['danger'] if candidate is not None and not self._hover_valid else self.colors['accent']))
            self._draft.setPen(pen)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._link_port is not None:
            first, second = self._link_port, self._port_at(event.scenePos())
            old_edge = dict(self._rewire_edge.edge) if self._rewire_edge is not None else None
            connection = self._connection_to(second)
            self.cancel_link()
            if second is first:
                event.accept()
                return
            if connection is not None:
                if old_edge is not None:
                    self.reconnect_requested.emit(old_edge['id'], *connection)
                else:
                    self.connect_requested.emit(*connection)
            elif second is None and not any(isinstance(item, NodeItem) for item in self.items(event.scenePos())):
                if old_edge:
                    self.disconnect_requested.emit(old_edge['id'])
                else:
                    context = {'node_id': first.node_item.node['id'], 'input': first.key, 'output': first.output}
                    self.add_requested.emit(event.scenePos(), context)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def cancel_link(self):
        self._link_animation.stop()
        if self._hover_port is not None:
            self._hover_port.refresh_connection(self._hover_port.connected)
        self._hover_port, self._hover_valid = None, False
        if self._rewire_edge is not None:
            self._rewire_edge.setOpacity(1)
        if self._draft is not None:
            self.removeItem(self._draft)
        self._link_port, self._draft, self._rewire_edge = None, None, None

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and not any(isinstance(item, (NodeItem, EdgeItem, PortItem)) for item in self.items(event.scenePos())):
            self.add_requested.emit(event.scenePos(), None)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        widget = event.widget()
        if widget is None and self.views():
            widget = self.views()[0]
        # GraphicsScene is not a QWidget: parent menus to its actual window so
        # they share the application font and live light/dark menu theme.
        parent = widget.window() if widget is not None else None
        port = self._port_at(event.scenePos())
        if port is not None and not port.output:
            edge = next((item.edge for item in self.edges.values() if item.edge['target'] == port.node_item.node['id'] and item.edge['input'] == port.key), None)
            if edge:
                menu = QtWidgets.QMenu(parent)
                text = '断开连线，恢复内部值' if port.node_item.node['kind'] == 'app' else '断开连线'
                menu.addAction(text, lambda: self.disconnect_requested.emit(edge['id']))
                menu.exec_(event.screenPos())
                menu.deleteLater()
                return
        item = next((i for i in self.items(event.scenePos()) if isinstance(i, NodeItem)), None)
        if item is not None and not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
        menu = QtWidgets.QMenu(parent)
        if item is None:
            position = QtCore.QPointF(event.scenePos())
            menu.addAction('添加节点…', lambda: self.add_requested.emit(position, None))
            menu.addSeparator()
        if item is not None:
            menu.addAction('运行节点及所需上游', lambda: self.run_requested.emit(item.node['id'], False))
            menu.addAction('强制重跑节点及上游', lambda: self.run_requested.emit(item.node['id'], True))
            menu.addSeparator()
        menu.addAction('复制选中节点', lambda: self.action_requested.emit('copy'))
        menu.addAction('粘贴节点', lambda: self.action_requested.emit('paste'))
        menu.addAction('删除选中项', lambda: self.action_requested.emit('delete'))
        menu.exec_(event.screenPos())
        menu.deleteLater()


class _CanvasSelectionStyle(QtWidgets.QProxyStyle):
    """Keep native selection semantics without the platform's opaque band."""
    def __init__(self, view):
        # Own a separate base style; never reparent the application's style.
        super().__init__(QtWidgets.QStyleFactory.create('Fusion'))
        self.setParent(view)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QtWidgets.QStyle.SH_RubberBand_Mask:
            # We paint a translucent interior, so invalidate the entire old
            # rectangle, not a platform-dependent border-only mask.
            return 0
        return super().styleHint(hint, option, widget, returnData)

    def drawControl(self, element, option, painter, widget=None):
        if element != QtWidgets.QStyle.CE_RubberBand:
            return super().drawControl(element, option, painter, widget)
        view = widget.parentWidget() if widget is not None else None
        scene = view.scene() if isinstance(view, QtWidgets.QGraphicsView) else None
        color = QtGui.QColor(getattr(scene, 'colors', {}).get('accent', '#739bff'))
        fill = QtGui.QColor(color)
        fill.setAlpha(22)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        painter.setPen(QtGui.QPen(color, 1))
        painter.setBrush(fill)
        painter.drawRect(QtCore.QRectF(option.rect).adjusted(.5, .5, -.5, -.5))
        painter.restore()


class CanvasView(QtWidgets.QGraphicsView):
    files_dropped = QtCore.pyqtSignal(list, object)
    view_changed = QtCore.pyqtSignal()

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(self.BoundingRectViewportUpdate)
        self.setDragMode(self.RubberBandDrag)
        self._selection_style = _CanvasSelectionStyle(self)
        self.viewport().setStyle(self._selection_style)
        self._selection_rect = QtCore.QRect()
        self.rubberBandChanged.connect(self._refresh_selection_rect)
        self.setTransformationAnchor(self.AnchorUnderMouse)
        self.setResizeAnchor(self.AnchorViewCenter)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._pan, self._space = None, False
        self._last_scene_pos = None
        self.overlay_exclusion = 0
        self.centerOn(0, 0)

    def _refresh_selection_rect(self, rect, _start, _end):
        dirty = self._selection_rect.united(rect).adjusted(-2, -2, 2, 2)
        self._selection_rect = QtCore.QRect(rect)
        self.viewport().update(dirty.intersected(self.viewport().rect()))

    def wheelEvent(self, event):
        factor = 1.15 ** (event.angleDelta().y() / 120)
        zoom = self.transform().m11() * factor
        if .12 <= zoom <= 3.5:
            self.scale(factor, factor)
            self.view_changed.emit()
        event.accept()

    def paintEvent(self, event):
        # Large zoomed-out canvases use simple node bodies. In a large viewport
        # only the nearest 64 visible media nodes decode, preventing LRU churn.
        if self.transform().m11() < .42:
            self.scene().thumbnail_nodes = set()
            self.scene().thumbnail_slots = {}
        else:
            center = self.mapToScene(self.viewport().rect().center())
            visible = [item for item in self.items(self.viewport().rect())
                       if isinstance(item, NodeItem) and item.result_count()]
            visible.sort(key=lambda item: (item.scenePos() - center).manhattanLength())
            visible = visible[:64]
            quota = max(1, self.scene().thumbnails.limit // max(1, len(visible)))
            self.scene().thumbnail_nodes = {item.node['id'] for item in visible}
            self.scene().thumbnail_slots = {
                item.node['id']: {index for index, _ in item.result_layout()[0]
                                  if model.result_type(item.result_at(index)) == 'image'}
                for item in visible}
            for node_id, indices in self.scene().thumbnail_slots.items():
                self.scene().thumbnail_slots[node_id] = set(sorted(indices)[:quota])
        super().paintEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MiddleButton or (self._space and event.button() == QtCore.Qt.LeftButton):
            self._pan = event.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self._last_scene_pos = self.mapToScene(event.pos())
        if self._pan is not None:
            delta = event.pos() - self._pan
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan = event.pos()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan is not None:
            self._pan = None
            self.unsetCursor()
            self.view_changed.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if self.scene()._resizing_node is not None:
            if event.key() == QtCore.Qt.Key_Escape:
                self.scene().cancel_resize()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_Tab:
            position = self._last_scene_pos or self.mapToScene(self.viewport().rect().center())
            self.scene().add_requested.emit(position, None)
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_Escape and self.scene()._link_port is not None:
            self.scene().cancel_link()
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_Space:
            self._space = True
            self.setCursor(QtCore.Qt.OpenHandCursor)
            event.accept()
            return
        if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
            self.scene().action_requested.emit('delete')
            event.accept()
            return
        for sequence, action in ((QtGui.QKeySequence.Copy, 'copy'), (QtGui.QKeySequence.Paste, 'paste'),
                                 (QtGui.QKeySequence.Undo, 'undo'), (QtGui.QKeySequence.Redo, 'redo')):
            if event.matches(sequence):
                self.scene().action_requested.emit(action)
                event.accept()
                return
        super().keyPressEvent(event)

    def focusNextPrevChild(self, forward):
        # Tab belongs to node search while the canvas itself has keyboard focus.
        return False

    def keyReleaseEvent(self, event):
        if event.key() == QtCore.Qt.Key_Space:
            self._space = False
            if self._pan is None:
                self.unsetCursor()
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._space, self._pan = False, None
        self.scene().cancel_resize()
        self.scene().cancel_link()
        self.unsetCursor()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self.scene().cancel_resize()
        self.scene().cancel_link()
        super().hideEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths, self.mapToScene(event.pos()))
            event.acceptProposedAction()

    def fit_nodes(self):
        rect = QtCore.QRectF()
        for node in self.scene().nodes.values():
            rect = rect.united(node.sceneBoundingRect())
        if not rect.isEmpty():
            rect = rect.adjusted(-50, -50, 50, 50)
            available = max(150, self.viewport().width() - self.overlay_exclusion)
            zoom = min(1.15, available / rect.width(), max(100, self.viewport().height() - 10) / rect.height())
            self.resetTransform()
            self.scale(max(.12, zoom), max(.12, zoom))
            self.centerOn(rect.center() + QtCore.QPointF(self.overlay_exclusion / (2 * max(.12, zoom)), 0))
        self.view_changed.emit()
