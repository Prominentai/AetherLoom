"""Lightweight graphics items for the native canvas (no per-node media players)."""
import math
import os
from collections import OrderedDict

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_ui import palette
from aetherloom_core.media_limits import MAX_DECODE_PIXELS
from aetherloom_core.rh_progress import draw_circular_progress, progress_percent, current_node_percent, progress_text
from .model import input_ports
from . import model
from . import preview_data
from .appearance import tint, kind_color, draw_kind_icon, bypass_colors


KIND_NAMES = {'app': 'APP', 'image': '图像', 'video': '视频', 'audio': '音频',
              'text': '文本', 'select': '内容过滤', 'preview': '预览 / 保存'}
KIND_NAMES.update(llm_model='LLM · API / Agent', vision_model='VISION · API / Agent',
                  image_model='IMAGE · API / Agent', edit_model='EDIT · API / Agent')
KIND_NAMES.update(filename='读取文件名', rename='文件重命名')
KIND_NAMES['list2batch'] = 'LIST → BATCH'
KIND_NAMES['batch2list'] = 'BATCH → LIST'
KIND_NAMES.update(merge_batch='MERGE BATCH', merge_list='MERGE LIST')
KIND_NAMES['list_select'] = 'LIST SELECT'
KIND_NAMES.update(batch_select='BATCH SELECT', rebatch='REBATCH')
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
            if self.key[3]=='text':
                with open(self.key[0],'r',encoding='utf-8-sig') as stream:image=stream.read(4096)
                self.signals.complete.emit(self.key,image)
                return
            if self.key[3] == 'video':
                import subprocess
                import imageio_ffmpeg
                completed = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error',
                    '-threads', '1', '-i', os.path.abspath(self.key[0]), '-an', '-sn', '-dn', '-frames:v', '1',
                    '-vf', 'scale=512:320:force_original_aspect_ratio=decrease', '-filter_threads', '1',
                    '-threads', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if completed.returncode == 0 and len(completed.stdout) <= 4 * 1024 * 1024:
                    decoded = QtGui.QImage.fromData(completed.stdout)
                    if not decoded.isNull():image = decoded
                self.signals.complete.emit(self.key, image)
                return
            reader = QtGui.QImageReader(self.key[0])
            reader.setDecideFormatFromContent(True)
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
        self.entries[key] = decoded if isinstance(decoded,str) else QtGui.QPixmap.fromImage(decoded) if decoded is not None else None
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        self.ready.emit()

    def close(self):
        self.closed = True
        self.pool.clear()
        self.entries.clear()

    def get(self, path, kind='image'):
        if self.closed:
            return None
        try:
            stat = os.stat(path)
            key = (path, stat.st_size, stat.st_mtime_ns, kind)
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

    def failed(self, path, kind):
        try:
            stat = os.stat(path)
            key = (path, stat.st_size, stat.st_mtime_ns, kind)
            return key in self.entries and self.entries[key] is None
        except (OSError, TypeError):
            return True


class PortItem(QtWidgets.QGraphicsEllipseItem):
    def __init__(self, node, key, label, kind, output=False):
        super().__init__(-7, -7, 14, 14, node)
        self.node_item, self.key, self.output = node, key, output
        self.kind = kind
        self.content_kind = kind[:-6] if kind.endswith('_input') else 'any' if kind == 'batch' else kind
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
        color = QtGui.QColor(kind_color(self.content_kind,colors))
        self.setBrush(color if self.output or connected else QtGui.QColor(colors['surface']))
        self.setPen(QtGui.QPen(color, 2))
        if self.output:
            detail = ('每个 Batch 作为一组独立传递；多个 Batch 不会自动合并。' if self.kind == 'batch'
                      else '同类型结果组成 List，保持返回顺序；不会自动转为 Batch。')
            self.setToolTip(self.label + ' · 拖到输入端口或空白处添加下游节点\n' + detail)
        else:
            has_internal = self.node_item.node['kind'] in {'app'} | set(model.MODEL_KINDS) or (self.node_item.node['kind'] == 'rename' and self.key != 'value')
            if connected:
                detail = ('已连接：运行时覆盖内部值；' if has_internal else '已连接上游结果；') + '拖到其他端口可改接，拖到空白处可断开'
            else:
                detail = '未连接：使用节点内部值；也可拖动连接输出' if has_internal else '等待连接上游结果；此输入为必填'
            type_label = model.port_type_name(self.kind)
            self.setToolTip(self.label + ' · ' + type_label + '\n' + detail)


class NodeItem(QtWidgets.QGraphicsObject):
    WIDTH = 268

    def __init__(self, node, scene):
        super().__init__()
        self.node, self.canvas_scene = node, scene
        self.ports = {}
        self._start_positions = None
        self._resize_start = None
        self._resize_hover = False
        self._hovered = False
        self._result_offset = 0
        self.inline_proxy = None
        self.setFlags(self.ItemIsMovable | self.ItemIsSelectable | self.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setCacheMode(self.NoCache)
        self.setToolTip('单击选中；双击标题打开完整设置，双击结果查看预览。拖动右下角调整大小。')
        ports = input_ports(node)
        outputs = model.visible_output_ports(node, getattr(scene, '_document', {}).get('edges', []))
        # Reserve a result area below all input ports, including Apps with many
        # fields; result arrival then needs no geometry rebuild or moving ports.
        self.minimum_height = max(self.minimum_size(node)[1], 96 + 22 * len(outputs))
        self.width, self.height = self.normalized_size(node.get('size'))
        for index, port in enumerate(ports):
            item = PortItem(self, port['key'], port['label'], port['type'])
            item.setPos(0, 48 + index * 22)
            self.ports[port['key']] = item
        self.outputs = {}
        for index, port in enumerate(outputs):
            item = PortItem(self, port['key'], port['label'], port['type'], True)
            item.setPos(self.width, self.output_y(index))
            self.outputs[port['key']] = item
        for index, port in enumerate(self.outputs.values()):port.setPos(self.width, self.output_y(index))
        self.output = self.outputs.get('output', next(iter(self.outputs.values())))
        self.refresh_output_counts()
        self.setPos(float(node.get('x', 0)), float(node.get('y', 0)))
        self.refresh_bypass()

    def refresh_bypass(self):
        ignored = bool(self.node.get('bypass'))
        if self.inline_proxy is not None:
            self.inline_proxy.setOpacity(.6 if ignored else 1)
        hint = ('已忽略：执行时跳过此节点，按现有旁路规则传递输入。\n'
                '右键取消“忽略节点”可恢复；正在执行的快照不受修改影响。\n' if ignored else '')
        self.setToolTip(hint + str(self.node.get('error') or self.node.get('message') or '')
                        + '\n当前节点进度\n' + progress_text(self.node.get('node_progress'))
                        + '\n标题右侧 ▶ 运行此节点；双击标题、右键或 Alt+Enter 打开完整设置。'
                        + '\n双击结果查看预览，拖动右下角调整大小。')

    @classmethod
    def minimum_size(cls, node):
        ports = max(len(input_ports(node)), len(model.output_ports(node)))
        # Inputs and outputs occupy independent columns in the same slot area.
        height = max(128, 96 + 22 * ports)
        if node['kind'] == 'text':height = max(height, 220)
        if node['kind'] in ('image', 'video', 'audio'):
            height = max(height, 350)
        return 300 if node['kind'] != 'text' else cls.WIDTH, height

    def source_port(self, edge):
        return self.outputs.get(edge.get('output', 'output'), self.output)

    def refresh_output_counts(self):
        results = self.node.get('results') or []
        self.output_counts = {kind: len(values) for kind, values in model.result_groups(results).items()}
        self.output_counts['output'] = len(results)

    def normalized_size(self, size):
        defaults = (self.minimum_size(self.node)[0], max(self.minimum_height, getattr(self, 'form_minimum_height', 0)))
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
        for index, port in enumerate(self.outputs.values()):port.setPos(width, self.output_y(index))
        self.canvas_scene.update_edges(self.node['id'])
        self.layout_inline()
        self.update()

    def ensure_inline(self):
        if not hasattr(self.canvas_scene.parent(),'histories'):return
        if self.inline_proxy is None:
            from .inline_text import InlineText
            from .inline_controls import InlineControls
            self.inline_proxy=QtWidgets.QGraphicsProxyWidget(self)
            self.inline_proxy.setWidget(InlineText(self) if self.node['kind']=='text' else InlineControls(self))
            self.inline_proxy.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.refresh_bypass()
        self.layout_inline()

    def layout_inline(self):
        if self.inline_proxy is not None:
            self.inline_proxy.setGeometry(self.controls_rect().adjusted(1,1,-1,-1))
        self.align_input_ports()

    def inline_ports(self):
        if self.has_inline_form():
            widget = self.inline_proxy.widget()
            if hasattr(widget, 'port_positions'):return widget.port_positions()
        return {}

    def align_input_ports(self):
        positions = self.inline_ports()
        moved = False
        for index, (key, port) in enumerate(self.ports.items()):
            y = self.inline_proxy.pos().y() + positions[key] if key in positions else 48 + index * 22
            if abs(port.pos().y() - y) > .1:
                port.setPos(0, y);moved = True
        if moved:
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

    def hoverEnterEvent(self, event):
        self._hovered=True;self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered=False;self.update()
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
        return QtCore.QRectF(-9, -8, self.width + 112, self.height + 19)

    def shape(self):
        shape = QtGui.QPainterPath()
        shape.addRoundedRect(QtCore.QRectF(0, 0, self.width, self.height), 6, 6)
        if (model.supports_local_decode(self.node) and self.node.get('decode_settings', {}).get('enabled')):
            shape.addRect(self.decode_rect())
        return shape

    def decode_rect(self):
        return QtCore.QRectF(self.width + 10, max(94, 76 + 22 * (len(self.outputs) - 1)), 86, 27)

    def run_rect(self):
        return QtCore.QRectF(self.width - 32, 4, 26, 24)

    def progress_rect(self):
        return QtCore.QRectF(self.run_rect().center().x() - 12, 4, 24, 24)

    def shows_progress(self):
        return (self.node['kind'] in {'app'} | set(model.MODEL_KINDS) and node_is_active(self.node)
                and self.node.get('status') in RUNNING_STATES | WAITING_STATES)

    def itemChange(self, change, value):
        if change == self.ItemPositionHasChanged and hasattr(self, 'canvas_scene'):
            self.canvas_scene.update_edges(self.node['id'])
        return super().itemChange(change, value)

    def input_preview(self):
        return preview_data.has_inputs(self.node)

    def result_count(self):
        return preview_data.count(self.node, 'input' if self.input_preview() else 'results')

    def result_at(self, index):
        return preview_data.item_at(self.node, index, 'input' if self.input_preview() else 'results')

    def output_y(self, index):
        return 48 + index * 22

    def has_inline_form(self):
        return (self.inline_proxy is not None and self.inline_proxy.isVisible()
                and self.inline_proxy.widget() is not None)

    def output_column_width(self):
        # A narrow right column keeps sockets independent of widget rows.
        # Its width depends on labels, never on the number of output sockets.
        font = QtGui.QFont('Microsoft YaHei UI');font.setPixelSize(12)
        metrics = QtGui.QFontMetrics(font)
        return max(40, min(112, max((metrics.horizontalAdvance(p.label) for p in self.outputs.values()), default=0) + 22))

    def body_rect(self):
        rows = max(len(self.ports), len(self.outputs))
        top = 40 if self.has_inline_form() else 42 + 22 * rows
        return QtCore.QRectF(12, top, self.width - 24, self.height - top - 29)

    def controls_rect(self):
        rect = self.body_rect()
        rect.setRight(rect.right() - self.output_column_width())
        if self.node['kind'] != 'text' and self.result_count():
            rect.setHeight(max(80, rect.height() - max(100, min(220, rect.height() * .42)) - 8))
        return rect

    def content_rect(self):
        rect = self.body_rect()
        if self.inline_proxy is not None and self.inline_proxy.isVisible():
            rect.setTop(max(self.controls_rect().bottom() + 8, self.output_y(len(self.outputs) - 1) + 18))
        return rect

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
        count = max(1, self.result_count())
        columns = min(count, 3, max(1, int((content.width() + 6) // 146)))
        rows = min((count + columns - 1) // columns, 2, max(1, int((content.height() - 18) // 120)))
        return columns, rows

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
        running = node_is_active(self.node) and self.node.get('status') in RUNNING_STATES | WAITING_STATES
        grouped_heading = ('List / Batch' if 0 < self.output_counts.get('batch', 0) < self.output_counts['output'] else 'Batch 内容')
        heading = '输入预览' if self.input_preview() else '上次结果' if self.node.get('stale') or self.node.get('_ui_stale') or running else grouped_heading if preview_data.has_batches(self.node) else '运行结果'
        if self.output_counts.get('batch') and not self.input_preview():
            heading = (('上次结果 · ' if self.node.get('stale') or self.node.get('_ui_stale') or running else '')
                       + f"List {self.output_counts['output']} 项 · Batch {self.output_counts['batch']} 组 · 预览")
        heading_rect = QtCore.QRectF(content.x(), content.y(), max(0, content.width() - (54 if pages > 1 else 0)), 20)
        heading += f' · {self.result_count()} 项' + (f' · {page + 1}/{pages}' if pages > 1 else '')
        painter.drawText(heading_rect, QtCore.Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(heading, QtCore.Qt.ElideRight, int(heading_rect.width())))
        for rect, label, enabled in (zip(self.result_nav_rects(), ('‹', '›'), (page > 0, page + 1 < pages)) if pages > 1 else []):
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(p['accent_soft'] if enabled else p['surface']))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QtGui.QColor(p['accent'] if enabled else p['muted']))
            painter.drawText(rect, QtCore.Qt.AlignCenter, label)
        for index, rect in tiles:
            result = self.result_at(index)
            kind = preview_data.kind_of(result)
            path = result.get('path') or result.get('file_path') or ''
            painter.setPen(QtGui.QPen(QtGui.QColor(p['border']), 1))
            painter.setBrush(QtGui.QColor(p['input']))
            painter.drawRoundedRect(rect,8,8)
            painter.save()
            painter.setClipRect(rect.adjusted(4, 2, -4, -2), QtCore.Qt.IntersectClip)
            title = result.get('_batch_label', f'{index + 1}') + f" · {preview_data.TYPE_NAMES.get(kind, '文件')}"
            painter.fillRect(QtCore.QRectF(rect.x()+1,rect.y()+1,rect.width()-2,18),tint(kind_color(kind,p),18))
            painter.setPen(QtGui.QColor(kind_color(kind,p)))
            painter.drawText(rect.adjusted(6, 0, -6, 0), QtCore.Qt.AlignTop, title)
            body = rect.adjusted(6, 22, -6, -23 if path else -6)
            thumb = None
            if (kind in ('image', 'video') and path and index in self.canvas_scene.thumbnail_slots.get(self.node['id'], ())):
                thumb = self.canvas_scene.thumbnails.get(path, kind)
            if thumb is not None and not thumb.isNull():
                size = thumb.size().scaled(body.size().toSize(), QtCore.Qt.KeepAspectRatio)
                target = QtCore.QRectF(body.center().x() - size.width() / 2,
                                       body.center().y() - size.height() / 2, size.width(), size.height())
                painter.drawPixmap(target, thumb, QtCore.QRectF(thumb.rect()))
            else:
                preview=self.canvas_scene.thumbnails.get(path,'text') if kind=='text' and path and self.node['id'] in self.canvas_scene.thumbnail_nodes else None
                text = str(preview if preview is not None else result['text'] if 'text' in result else result['value'] if 'value' in result
                           else os.path.basename(path) or result.get('url') or '暂无本地预览')
                if path and kind in ('image', 'video', 'audio'):
                    text = ('文件已移动或删除' if not os.path.isfile(path) else
                            '双击查看音频' if kind == 'audio' else
                            '无法生成预览 · 双击打开' if self.canvas_scene.thumbnails.failed(path, kind) else '正在加载预览…')
                if not text and kind == 'text':text = '空文本'
                painter.setPen(QtGui.QColor(p['text']))
                painter.drawText(body, (QtCore.Qt.AlignTop if kind in {'text'} | model.VALUE_TYPES else QtCore.Qt.AlignVCenter) | QtCore.Qt.TextWordWrap, text[:240])
            if path:
                painter.setPen(QtGui.QColor(p['muted']))
                painter.drawText(QtCore.QRectF(rect.x()+6,rect.bottom()-21,rect.width()-12,18), QtCore.Qt.AlignVCenter,
                                 painter.fontMetrics().elidedText(preview_data.title_of(result), QtCore.Qt.ElideMiddle, max(1,int(rect.width()-12))))
            painter.restore()
        painter.restore()

    def paint(self, painter, option, widget=None):
        p = self.canvas_scene.colors
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        selected = self.isSelected()
        state_color = node_state_color(self.node, p)
        ignored = bool(self.node.get('bypass'))
        bypass_accent, bypass_body, bypass_text = bypass_colors(p)
        accent = bypass_accent if ignored else kind_color(self.node['kind'],p)
        border = state_color or (accent if ignored else p['accent'] if selected else p['muted'] if self._hovered else p['border'])
        width = (3.3 if selected else 2.3) if state_color else (2.3 if ignored else 2 if selected else 1)
        body=QtCore.QRectF(0,0,self.width,self.height)
        lod=QtWidgets.QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        if lod>=.42:
            painter.setPen(QtCore.Qt.NoPen);painter.setBrush(tint('#000000',22 if QtGui.QColor(p['canvas']).lightness()<128 else 10))
            painter.drawRoundedRect(body.translated(0,2),6,6)
            if selected or state_color:
                painter.setBrush(QtCore.Qt.NoBrush);painter.setPen(QtGui.QPen(tint(border,25),3))
                painter.drawRoundedRect(body.adjusted(-1,-1,1,1),7,7)
        if ignored and selected:
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor(p['accent']), 1.5))
            painter.drawRoundedRect(body.adjusted(-4,-4,4,4),9,9)
        outline = QtGui.QPen(QtGui.QColor(border), width,
                            QtCore.Qt.DashLine if ignored and not state_color else QtCore.Qt.SolidLine)
        outline.setCosmetic(ignored)
        painter.setPen(outline)
        painter.setBrush(tint(bypass_body, 220) if ignored else QtGui.QColor(p['surface']))
        painter.drawRoundedRect(body,6,6)
        painter.save()
        clip=QtGui.QPainterPath();clip.addRoundedRect(body.adjusted(1,1,-1,-1),5,5);painter.setClipPath(clip)
        painter.fillRect(QtCore.QRectF(0,0,self.width,32),tint(accent,24));painter.restore()
        draw_kind_icon(painter,QtCore.QRectF(10,8,16,16),self.node['kind'],accent)
        font = QtGui.QFont('Microsoft YaHei UI')
        font.setStyleStrategy(QtGui.QFont.PreferAntialias)
        font.setPixelSize(14);font.setBold(True);painter.setFont(font)
        painter.setPen(QtGui.QColor(p['text']))
        title = ('已忽略 · ' if ignored else '') + model.node_title(self.node)
        painter.drawText(QtCore.QRectF(34,3,self.width-72,26),QtCore.Qt.AlignVCenter,
                         QtGui.QFontMetrics(font).elidedText(title,QtCore.Qt.ElideRight,int(self.width-72)))
        if lod<.35:return
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
            triangle = QtGui.QPolygonF([QtCore.QPointF(self.width-22,10),
                                       QtCore.QPointF(self.width-22,22),QtCore.QPointF(self.width-13,16)])
            painter.setBrush(QtGui.QColor(p['accent']));painter.drawPolygon(triangle)
        font.setBold(False)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(p['border']))
        painter.drawLine(QtCore.QPointF(1, 32), QtCore.QPointF(self.width - 1, 32))
        painter.setPen(QtGui.QColor(p['muted']))
        aligned_ports = self.inline_ports()
        for index, port in enumerate(input_ports(self.node)):
            if port['key'] in aligned_ports:continue
            connected = self.ports[port['key']].connected
            painter.setPen(QtGui.QColor(p['text'] if connected else p['muted']))
            label = str(port['label'])
            label_width = int((self.width - 36) / 2)
            painter.drawText(QtCore.QRectF(14, 37 + index * 22, label_width, 22), QtCore.Qt.AlignVCenter,
                             QtGui.QFontMetrics(font).elidedText(label, QtCore.Qt.ElideRight, label_width - 5))
        for index, port in enumerate(self.outputs.values()):
            label = port.label
            painter.setPen(QtGui.QColor(p['muted']))
            label_width = self.output_column_width() - 18 if self.has_inline_form() else self.width / 2 - 18
            painter.drawText(QtCore.QRectF(self.width - 14 - label_width, port.pos().y() - 11, label_width, 22),
                             QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                             QtGui.QFontMetrics(font).elidedText(label, QtCore.Qt.ElideRight, int(label_width)))
        content = self.content_rect()
        painter.save()
        if ignored:
            painter.setOpacity(painter.opacity() * (.75 if selected else .5))
        if content.height() > 20:
            if self.result_count():
                self._paint_results(painter)
            else:
                painter.setPen(QtGui.QPen(tint(p['border'],170),1,QtCore.Qt.DashLine))
                painter.setBrush(tint(p['input'],130));painter.drawRoundedRect(content.adjusted(0,4,0,-2),8,8)
                painter.setPen(QtGui.QColor(p['muted']))
                params = self.node.get('params', {})
                summary = str(params.get('text') or '')
                if self.node['kind'] in model.MODEL_KINDS:
                    summary = (self.node.get('model_config', {}).get('model') or '选择模型连接') + '\n' + str(params.get('prompt') or '填写或连接提示词')
                if self.node['kind'] in ('image', 'video', 'audio'):
                    files = params.get('files', [])
                    summary = f'{len(files)} 个输入路径' + (' · ' + os.path.basename(files[0]) if files else ' · 选择或拖入素材')
                elif self.node['kind'] == 'app':
                    summary = '任务完成后在此展示结果' if node_is_active(self.node) else '运行后在此查看结果'
                elif not summary:
                    summary = '连接上游结果' if self.ports else KIND_NAMES.get(self.node['kind'], '')
                text_rect=content.adjusted(12,8,-12,-8)
                alignment=QtCore.Qt.AlignVCenter|QtCore.Qt.TextWordWrap
                if self.node['kind']!='text':
                    alignment|=QtCore.Qt.AlignHCenter
                    if content.height()>100:
                        center=content.center()
                        draw_kind_icon(painter,QtCore.QRectF(center.x()-12,center.y()-33,24,24),
                                       'preview' if self.node['kind']=='app' else self.node['kind'],p['muted'])
                        text_rect.setTop(center.y()+2)
                        alignment=QtCore.Qt.AlignHCenter|QtCore.Qt.AlignTop|QtCore.Qt.TextWordWrap
                painter.drawText(text_rect,alignment,summary[:150])
        painter.restore()
        status = str(self.node.get('status') or 'IDLE')
        color = state_color or (p['success'] if status in ('SUCCESS', 'REUSED') else p['muted'])
        painter.setPen(QtGui.QColor(color))
        label = '等待调度' if status == 'PENDING' and node_is_active(self.node) else STATUS_NAMES.get(status, status)
        if ignored and not state_color:
            color = accent
            label = '已忽略 · 旁路传递'
        progress = self.node.get('progress')
        if not show_progress and progress is not None and status in RUNNING_STATES:
            label += f'  {progress_percent(status, progress)}%'
        if (self.node.get('_ui_stale') or self.node.get('stale')) and (self.node.get('results') or status not in ('IDLE', 'READY')):
            label += ' · 参数已修改'
        painter.setPen(QtGui.QPen(tint(p['border'],150),1))
        painter.drawLine(QtCore.QPointF(14,self.height-26),QtCore.QPointF(self.width-14,self.height-26))
        painter.setPen(QtCore.Qt.NoPen);painter.setBrush(QtGui.QColor(color));painter.drawEllipse(QtCore.QPointF(18,self.height-13),3,3)
        painter.setPen(QtGui.QColor(color))
        painter.drawText(QtCore.QRectF(29,self.height-25,self.width-59,24),QtCore.Qt.AlignVCenter,
                         QtGui.QFontMetrics(font).elidedText(label,QtCore.Qt.ElideRight,int(self.width-59)))
        painter.setPen(QtGui.QPen(QtGui.QColor(p['accent'] if self._resize_hover or self._resize_start else p['muted']),
                                 1.5, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        for offset in (7, 12):
            painter.drawLine(QtCore.QPointF(self.width - 6 - offset, self.height - 6),
                             QtCore.QPointF(self.width - 6, self.height - 6 - offset))
        if (model.supports_local_decode(self.node) and self.node.get('decode_settings', {}).get('enabled')):
            painter.setPen(QtGui.QPen(QtGui.QColor(p['border']), 1))
            center_y = self.decode_rect().center().y()
            painter.drawLine(QtCore.QPointF(self.width, center_y), QtCore.QPointF(self.width + 10, center_y))
            painter.setBrush(QtGui.QColor(p['accent_soft']))
            painter.drawRoundedRect(self.decode_rect(), 4, 4)
            painter.setPen(QtGui.QColor(p['accent']))
            painter.drawText(self.decode_rect(), QtCore.Qt.AlignCenter, '本地解码')

    def mousePressEvent(self, event):
        self._selection_click=False
        if event.button()==QtCore.Qt.LeftButton and event.modifiers() & (QtCore.Qt.ControlModifier|QtCore.Qt.ShiftModifier):
            self._selection_click=True
            self._start_positions=None
            self.setSelected(not self.isSelected() if event.modifiers() & QtCore.Qt.ControlModifier else True)
            event.accept();return
        if event.button() == QtCore.Qt.LeftButton and self.result_count():
            tiles, page, pages = self.result_layout()
            for step, rect in (zip((-1, 1), self.result_nav_rects()) if pages > 1 else []):
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
        if event.button() == QtCore.Qt.LeftButton and (model.supports_local_decode(self.node) and self.node.get('decode_settings', {}).get('enabled')) and self.decode_rect().contains(event.pos()):
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
        if getattr(self,'_selection_click',False):
            event.accept();return
        if self._resize_start is not None:
            origin, size = self._resize_start
            delta = event.scenePos() - origin
            self.set_size((size[0] + delta.x(), size[1] + delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.result_count():
            for index, rect in self.result_layout()[0]:
                if rect.contains(event.pos()):
                    self.canvas_scene.result_requested.emit(self.node['id'], index)
                    event.accept()
                    return
        if event.button() == QtCore.Qt.LeftButton:
            self.canvas_scene.settings_requested.emit(self.node['id'])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self,'_selection_click',False):
            self._selection_click=False
            event.accept();return
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
        self._direction = None
        self.setZValue(-1)
        self.update_path()

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.canvas_scene.settings_requested.emit(self.edge['id'])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def update_path(self):
        scene, edge = self.canvas_scene, self.edge
        if edge['source'] not in scene.nodes or edge['target'] not in scene.nodes:
            return
        target = scene.nodes[edge['target']].ports.get(edge['input'])
        if target is None:
            return
        start, end = scene.nodes[edge['source']].source_port(edge).scenePos(), target.scenePos()
        path = connection_path(start, end)
        self.setPath(path)
        fraction = min(.2, 20 / max(1, path.length()))
        self._endpoints = path.pointAtPercent(fraction), path.pointAtPercent(1 - fraction)
        self._direction = None
        if path.length()>90:
            mid=path.pointAtPercent(.55);previous=path.pointAtPercent(.53)
            angle=math.atan2(mid.y()-previous.y(),mid.x()-previous.x())
            self._direction=QtGui.QPolygonF([mid+QtCore.QPointF(-6*math.cos(angle)+3*math.sin(angle),-6*math.sin(angle)-3*math.cos(angle)),
                                            mid,mid+QtCore.QPointF(-6*math.cos(angle)-3*math.sin(angle),-6*math.sin(angle)+3*math.cos(angle))])
        self.setToolTip((model.ITEM_OUTPUT_TYPES.get(edge.get('output'), '') + ' List\n' if edge.get('output') in model.ITEM_OUTPUT_TYPES else '')
                        + {'first': '首个匹配结果', 'index': '指定结果', 'all': '全部匹配结果逐项运行'}.get(edge.get('mode'), '首个匹配结果')
                        + '\n双击打开连线设置；拖动端点可改接，释放到空白处断开')

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
        p=self.canvas_scene.colors
        target=self.canvas_scene.nodes.get(self.edge['target'])
        port=target.ports.get(self.edge['input']) if target else None
        color=p['accent'] if highlighted else kind_color(port.content_kind if port else 'any',p)
        if highlighted:
            painter.setPen(QtGui.QPen(tint(color,30),8,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap));painter.drawPath(self.path())
        painter.setPen(QtGui.QPen(tint(color,255 if highlighted else 150),
                                 2.7 if highlighted else 1.8,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap))
        painter.drawPath(self.path())
        if self._direction is not None and abs(painter.transform().m11())>.5:
            painter.setPen(QtGui.QPen(tint(color,230),1.8,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap,QtCore.Qt.RoundJoin))
            painter.drawPolyline(self._direction)
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
    connect_requested = QtCore.pyqtSignal(str, str, str, str)
    run_requested = QtCore.pyqtSignal(str, bool)
    decode_requested = QtCore.pyqtSignal(str)
    mask_requested = QtCore.pyqtSignal(str)
    options_requested = QtCore.pyqtSignal(str)
    settings_requested = QtCore.pyqtSignal(str)
    option_changed = QtCore.pyqtSignal(str,str,object)
    batch_option_changed = QtCore.pyqtSignal(object,str,object)
    run_selection_requested = QtCore.pyqtSignal(object,bool)
    action_requested = QtCore.pyqtSignal(str)
    add_requested = QtCore.pyqtSignal(object, object)
    reconnect_requested = QtCore.pyqtSignal(str, str, str, str, str)
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
        model.sync_dynamic_inputs(doc)
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
        input_colors, output_colors = model.port_colors(self._document)
        connected = {(item.edge['target'], item.edge['input']) for item in self.edges.values()}
        for node_id, node in self.nodes.items():
            input_labels = {port['key']: port['label'] for port in model.input_ports(node.node)}
            for port in node.outputs.values():
                port.content_kind = output_colors.get((node_id,port.key),'any')
                port.label = model.port_type_name(port.content_kind)
                port.refresh_connection()
            for key, port in node.ports.items():
                port.label = input_labels.get(key, port.label)
                port.content_kind = input_colors.get((node_id,key),'any')
                port.refresh_connection((node_id, key) in connected)

    def sync_connections(self, doc):
        """Reconcile sockets and edges without destroying existing node widgets."""
        self.cancel_link()
        self._document = doc
        desired_edges = {edge['id']: edge for edge in doc.get('edges', [])}
        outgoing = {}
        for edge in desired_edges.values():outgoing.setdefault(edge['source'], []).append(edge)
        for key in set(self.edges) - desired_edges.keys():
            self.removeItem(self.edges.pop(key))
        for node in doc.get('nodes', []):
            item = self.nodes[node['id']]
            item.node = node
            desired = {port['key']: port for port in model.input_ports(node)}
            for key in set(item.ports) - desired.keys():
                port = item.ports.pop(key)
                self.removeItem(port);port.setParentItem(None)
            for key, spec in desired.items():
                if key not in item.ports:
                    item.ports[key] = PortItem(item, key, spec['label'], spec['type'])
                item.ports[key].kind = spec['type']
            item.ports = {key: item.ports[key] for key in desired}
            desired_outputs = {port['key']: port for port in model.visible_output_ports(node, outgoing.get(node['id'], []))}
            for key in set(item.outputs) - desired_outputs.keys():
                port = item.outputs.pop(key)
                self.removeItem(port);port.setParentItem(None)
            for key, spec in desired_outputs.items():
                if key not in item.outputs:
                    item.outputs[key] = PortItem(item, key, spec['label'], spec['type'], True)
                item.outputs[key].kind = spec['type']
            item.outputs = {key: item.outputs[key] for key in desired_outputs}
            item.output = item.outputs.get('output', next(iter(item.outputs.values())))
            item.minimum_height = max(item.minimum_size(node)[1], 96 + 22 * len(item.outputs))
        for key, edge in desired_edges.items():
            if key not in self.edges:
                item = EdgeItem(edge, self);self.edges[key] = item;self.addItem(item)
            else:
                self.edges[key].edge = edge
        self.refresh_nodes(doc)
        for item in self.nodes.values():item.layout_inline()
        for edge in self.edges.values():edge.update_path()

    def refresh_nodes(self, doc):
        self._document = doc
        for node in doc.get('nodes', []):
            item = self.nodes.get(node['id'])
            if item:
                item.node = node
                if item.inline_proxy is not None:item.inline_proxy.widget().refresh()
                if item._resize_start is None:
                    item.set_size(node.get('size'))
                for port in item.ports.values():port.refresh_connection(port.connected)
                for port in model.output_ports(node, legacy=True):
                    if port['key'] in item.outputs:
                        item.outputs[port['key']].kind = port['type']
                        item.outputs[port['key']].refresh_connection()
                item.refresh_output_counts()
                item.refresh_bypass()
                item.update()
        self.refresh_ports()

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
        step = 24 * (2 ** max(0,math.ceil(math.log2(18 / (24 * scale)))))
        left, top = math.floor(rect.left() / step) * step, math.floor(rect.top() / step) * step
        painter.setPen(QtGui.QPen(tint(self.colors['border'],150),0))
        points = [QtCore.QPointF(x, y) for x in range(int(left), int(rect.right()) + step, step)
                  for y in range(int(top), int(rect.bottom()) + step, step)]
        if points:
            painter.drawPoints(*points)
        major=step*4
        left,top=math.floor(rect.left()/major)*major,math.floor(rect.top()/major)*major
        painter.setPen(QtGui.QPen(tint(self.colors['muted'],90),0))
        points=[QtCore.QPointF(x,y) for x in range(int(left),int(rect.right())+major,major)
                for y in range(int(top),int(rect.bottom())+major,major)]
        if points:painter.drawPoints(*points)

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
                return port.node_item.node['id'], edge['target'], edge['input'], port.key
            if self._rewire_end == 'target' and not port.output:
                return edge['source'], port.node_item.node['id'], port.key, edge.get('output', 'output')
            return None
        if first.output == port.output:
            return None
        source, target = (first, port) if first.output else (port, first)
        return source.node_item.node['id'], target.node_item.node['id'], target.key, source.key

    def _valid_connection(self, port):
        connection = self._connection_to(port)
        if connection is None:
            return False
        source, target, key, output = connection
        old_id = self._rewire_edge.edge['id'] if self._rewire_edge is not None else None
        graph = dict(self._document, edges=[edge for edge in self._document['edges']
                     if edge['id'] != old_id and (edge['target'], edge['input']) != (target, key)])
        try:
            model.connect(graph, source, target, key, output=output)
            return True
        except ValueError as error:
            if port is not None:port.setToolTip(str(error))
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
        if not any(isinstance(i,QtWidgets.QGraphicsProxyWidget) for i in self.items(event.scenePos())):
            self.clearFocus()
        if event.button()==QtCore.Qt.RightButton and any(isinstance(i,NodeItem) for i in self.items(event.scenePos())):
            event.accept();return
        edge, end = self._edge_endpoint_at(event.scenePos()) if event.button() == QtCore.Qt.LeftButton else (None, None)
        if edge is not None:
            port = (self.nodes[edge.edge['source']].source_port(edge.edge) if end == 'source'
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
                          else self.nodes[edge['source']].source_port(edge))
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
        if (len([i for i in self.selectedItems() if isinstance(i,NodeItem)])<2
                and any(isinstance(i,QtWidgets.QGraphicsProxyWidget) for i in self.items(event.scenePos()))):
            super().contextMenuEvent(event);return
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
        selected=sorted([i.node for i in self.selectedItems() if isinstance(i,NodeItem)],key=lambda n:n['id'])
        if selected:
            from .selection import options
            menu.addAction('打开批量设置' if len(selected)>1 else '打开节点设置',
                           lambda:self.settings_requested.emit(selected[0]['id']))
            menu.addSeparator()
            if len(selected)>1:menu.addSection(f'已选中 {len(selected)} 个节点')
            for option in options(selected):
                label=option['label']+('（部分开启）' if option['mixed'] else '')
                if len(option['ids'])!=len(selected):label+=f" · {len(option['ids'])} 个适用"
                action=menu.addAction(label);action.setCheckable(True);action.setChecked(option['checked'])
                action.toggled.connect(lambda value,o=option:self.batch_option_changed.emit(o['ids'],o['path'],value))
            menu.addAction('打开批量设置' if len(selected)>1 else '打开其他设置',lambda:self.options_requested.emit(selected[0]['id']))
            if len(selected)==1 and selected[0]['kind']=='image':
                menu.addAction('遮罩 / 绘画…', lambda:self.mask_requested.emit(selected[0]['id']))
            ids=[node['id'] for node in selected]
            menu.addAction('运行选中节点及所需上游',lambda:self.run_selection_requested.emit(ids,False))
            menu.addAction('强制重跑选中节点及上游',lambda:self.run_selection_requested.emit(ids,True))
            menu.addSeparator()
        menu.addAction('全选节点',lambda:self.action_requested.emit('select_all'))
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
    node_dropped = QtCore.pyqtSignal(dict, object)
    view_changed = QtCore.pyqtSignal()

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(self.BoundingRectViewportUpdate)
        self.setDragMode(self.RubberBandDrag)
        self.setRubberBandSelectionMode(QtCore.Qt.IntersectsItemShape)
        self._rubber_selecting=False
        self._selection_style = _CanvasSelectionStyle(self)
        self.viewport().setStyle(self._selection_style)
        self._selection_rect = QtCore.QRect()
        self.rubberBandChanged.connect(self._refresh_selection_rect)
        self.setTransformationAnchor(self.AnchorUnderMouse)
        self.setResizeAnchor(self.NoAnchor)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._pan, self._space = None, False
        self._last_scene_pos = None
        self.overlay_exclusion = 0
        self.bottom_exclusion = 0
        self._view_reference = None
        self._screen_window = None
        self._observed_screen = None
        self._adapting = False
        self._adapt_timer = QtCore.QTimer(self)
        self._adapt_timer.setSingleShot(True)
        self._adapt_timer.setInterval(80)
        self._adapt_timer.timeout.connect(self._adapt_view)
        self._inline_timer = QtCore.QTimer(self)
        self._inline_timer.setSingleShot(True)
        self._inline_timer.timeout.connect(self._sync_inline)
        self.view_changed.connect(self.remember_view)
        self.centerOn(0, 0)

    def available_rect(self):
        rect = QtCore.QRectF(self.viewport().rect()).adjusted(20, 20, -20, -20)
        rect.setWidth(max(80, rect.width() - self.overlay_exclusion))
        rect.setHeight(max(80, rect.height() - self.bottom_exclusion))
        return rect

    def remember_view(self):
        if self._adapting:
            return
        rect = self.available_rect()
        self._view_reference = (self.screen_size(), self.transform().m11(),
                                self.mapToScene(rect.center().toPoint()))

    def screen_size(self):
        # QScreen reports logical pixels: do not multiply by devicePixelRatio.
        handle = self.window().windowHandle()
        screen = handle.screen() if handle else QtWidgets.QApplication.primaryScreen()
        return QtCore.QSizeF(screen.availableGeometry().size()) if screen else QtCore.QSizeF(1920, 1080)

    def showEvent(self, event):
        super().showEvent(event)
        handle = self.window().windowHandle()
        if handle is not self._screen_window:
            if self._screen_window is not None:
                try:
                    self._screen_window.screenChanged.disconnect(self._screen_changed)
                except (TypeError, RuntimeError):
                    pass
            self._screen_window = handle
            if handle is not None:
                handle.screenChanged.connect(self._screen_changed)
        self._screen_changed(handle.screen() if handle else QtWidgets.QApplication.primaryScreen())

    def _screen_changed(self, screen):
        if screen is not self._observed_screen:
            for name in ('availableGeometryChanged', 'geometryChanged', 'logicalDotsPerInchChanged'):
                if self._observed_screen is not None:
                    try:
                        getattr(self._observed_screen, name).disconnect(self.schedule_adapt)
                    except (TypeError, RuntimeError):
                        pass
                if screen is not None:
                    getattr(screen, name).connect(self.schedule_adapt)
            self._observed_screen = screen
        self.schedule_adapt()

    def schedule_adapt(self, *args):
        if self._view_reference is None:
            self.remember_view()
        self._adapt_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_adapt_timer'):
            self.schedule_adapt()

    def _center_available(self, point):
        rect = self.available_rect()
        offset = QtCore.QRectF(self.viewport().rect()).center() - rect.center()
        self.centerOn(point + offset / self.transform().m11())

    def _adapt_view(self):
        if not self.isVisible():
            return
        if (self._pan is not None or self.scene().mouseGrabberItem() is not None or
                self.scene()._link_port is not None or QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton):
            self._adapt_timer.start()
            return
        if self._view_reference is None or not self.scene().nodes:
            self.remember_view()
            return
        size, zoom, center = self._view_reference
        screen = self.screen_size()
        if size.width() <= 0 or size.height() <= 0:
            self.remember_view()
            return
        # Panel/window layout changes preserve the viewport origin. Recentering
        # on selection changes moves the scene beneath a pending node drag.
        target = max(.12, min(3.5, zoom * self._screen_scale(screen) / self._screen_scale(size)))
        if abs(self.transform().m11() - target) < .0001:
            # Keep the original zoom basis across automatic transitions, even
            # at the zoom limits. Round trips must not compound aspect ratios.
            self._view_reference = (size, zoom, self.mapToScene(self.available_rect().center().toPoint()))
            return
        self._adapting = True
        try:
            self.resetTransform();self.scale(target, target)
            self._center_available(center)
            self.view_changed.emit()
        finally:
            self._adapting = False

    def view_state(self):
        center = self.mapToScene(self.viewport().rect().center())
        size = self.screen_size()
        return {'zoom': self.transform().m11(), 'x': center.x(), 'y': center.y(),
                'screen': [size.width(), size.height()]}

    def restore_view(self, state):
        self._adapt_timer.stop()
        zoom = min(3.5, max(.12, float(state.get('zoom', 1))))
        # Legacy viewport measurements must not change the restored zoom.
        previous = state.get('screen')
        if (isinstance(previous, (list, tuple)) and len(previous) == 2 and
                all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in previous)):
            screen = self.screen_size()
            zoom = max(.12, min(3.5, zoom * self._screen_scale(screen) /
                                self._screen_scale(QtCore.QSizeF(*previous))))
        self.resetTransform();self.scale(zoom, zoom)
        self.centerOn(float(state.get('x', 0)), float(state.get('y', 0)))
        self.remember_view()

    def initial_node_size(self, node):
        screen = self.screen_size()
        factor = self._screen_scale(screen)
        width, height = NodeItem.minimum_size(node)
        return [round(max(width, min(480, 350 * factor))),
                round(max(height, min(height * 1.2, height * factor)))]

    @staticmethod
    def _screen_scale(size):
        return max(.01, min(size.width() / 1920, size.height() / 1080))

    def reveal_nodes(self, identities):
        self._adapt_timer.stop()
        rect = QtCore.QRectF()
        for identity in identities:
            item = self.scene().nodes.get(identity)
            if item is not None:
                rect = rect.united(item.mapRectToScene(item.shape().boundingRect()))
        if rect.isEmpty():
            return
        rect = rect.adjusted(-16, -16, 16, 16)
        available = self.available_rect()
        shown = self.mapToScene(available.toRect()).boundingRect()
        dx = min(0, rect.left() - shown.left()) + max(0, rect.right() - shown.right())
        dy = min(0, rect.top() - shown.top()) + max(0, rect.bottom() - shown.bottom())
        if dx or dy:
            self._center_available(shown.center() + QtCore.QPointF(dx, dy))
        self.view_changed.emit()

    def _refresh_selection_rect(self, rect, _start, _end):
        dirty = self._selection_rect.united(rect).adjusted(-2, -2, 2, 2)
        self._selection_rect = QtCore.QRect(rect)
        self.viewport().update(dirty.intersected(self.viewport().rect()))

    def wheelEvent(self, event):
        if not event.modifiers() & QtCore.Qt.ControlModifier and any(
                isinstance(item, QtWidgets.QGraphicsProxyWidget) for item in self.items(event.pos())):
            # Scroll long forms/text inside the node. Ctrl+wheel still zooms the canvas.
            super().wheelEvent(event)
            return
        factor = 1.15 ** (event.angleDelta().y() / 120)
        zoom = self.transform().m11() * factor
        if .12 <= zoom <= 3.5:
            self.scale(factor, factor)
            self.view_changed.emit()
        event.accept()

    def _sync_inline(self):
        """Build at most two forms per event turn and keep a bounded working set."""
        if not hasattr(self.scene().parent(), 'histories'):return
        center=self.mapToScene(self.viewport().rect().center())
        candidates=[item for item in self.items(self.viewport().rect()) if isinstance(item,NodeItem)] if self.transform().m11()>=.42 else []
        ordered=sorted(candidates,key=lambda item:(item.scenePos()-center).manhattanLength())[:32]
        visible=set(ordered)
        pending=[item for item in ordered if item.inline_proxy is None]
        for item in pending[:2]:item.ensure_inline()
        retained=[item for item in self.scene().nodes.values() if item.inline_proxy is not None]
        for item in retained:
            proxy=item.inline_proxy
            focused=self.scene().focusItem() is proxy
            proxy.setVisible(item in visible or focused)
            item.layout_inline()
        # Preserve focused editors, open model pickers and text-tool jobs.
        excess=max(0,len(retained)-48)
        for item in retained:
            if not excess:break
            proxy=item.inline_proxy;widget=proxy.widget()
            if item in visible or self.scene().focusItem() is proxy or getattr(widget,'_job',None):continue
            if any(getattr(w,'dialog',None) is not None for w in widget.findChildren(QtWidgets.QWidget)):continue
            if hasattr(widget,'validate') and not widget.validate():continue
            if hasattr(widget,'inspector'):widget.inspector.changed.disconnect()
            proxy.hide();proxy.deleteLater();item.inline_proxy=None;excess-=1
        if pending[2:]:self._inline_timer.start(0)

    def paintEvent(self, event):
        if not self._inline_timer.isActive():self._inline_timer.start(0)
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
                                  if model.result_type(item.result_at(index)) in ('image', 'video')}
                for item in visible}
            for node_id, indices in self.scene().thumbnail_slots.items():
                self.scene().thumbnail_slots[node_id] = set(sorted(indices)[:quota])
        super().paintEvent(event)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter,rect)
        if self.scene().nodes:return
        painter.save();painter.resetTransform()
        area=self.available_rect();center=area.center();p=self.scene().colors
        icon=QtCore.QRectF(center.x()-24,center.y()-80,48,48)
        painter.setPen(QtCore.Qt.NoPen);painter.setBrush(QtGui.QColor(p['accent_soft']));painter.drawRoundedRect(icon,14,14)
        draw_kind_icon(painter,icon.adjusted(10,10,-10,-10),'app',p['accent'])
        font=painter.font();font.setPixelSize(18);font.setBold(True);painter.setFont(font);painter.setPen(QtGui.QColor(p['text']))
        painter.drawText(QtCore.QRectF(area.left(),center.y()-12,area.width(),32),QtCore.Qt.AlignCenter,'开始搭建你的工作流')
        font.setPixelSize(12);font.setBold(False);painter.setFont(font);painter.setPen(QtGui.QColor(p['muted']))
        painter.drawText(QtCore.QRectF(area.left()+8,center.y()+30,area.width()-16,52),QtCore.Qt.AlignHCenter|QtCore.Qt.TextWordWrap,
                         '双击空白处或按 Tab 添加节点\n拖入素材 · 连接 App · 运行画布')
        painter.restore()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MiddleButton or (self._space and event.button() == QtCore.Qt.LeftButton):
            self._pan = event.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            event.accept()
            return
        self._rubber_selecting=(event.button()==QtCore.Qt.LeftButton and not any(isinstance(i,(NodeItem,PortItem,EdgeItem,QtWidgets.QGraphicsProxyWidget)) for i in self.items(event.pos())))
        if self._rubber_selecting and event.modifiers() & QtCore.Qt.ShiftModifier:
            event=QtGui.QMouseEvent(event.type(),event.localPos(),event.windowPos(),event.screenPos(),event.button(),event.buttons(),event.modifiers()|QtCore.Qt.ControlModifier)
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
        if self._rubber_selecting:
            self._rubber_selecting=False;self.scene().selectionChanged.emit()

    def keyPressEvent(self, event):
        focus=self.scene().focusItem()
        if isinstance(focus,QtWidgets.QGraphicsProxyWidget):
            super().keyPressEvent(event)
            event.accept()
            return
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
                                 (QtGui.QKeySequence.Undo, 'undo'), (QtGui.QKeySequence.Redo, 'redo'),(QtGui.QKeySequence.SelectAll,'select_all')):
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
        if self._rubber_selecting:self._rubber_selecting=False;self.scene().selectionChanged.emit()
        self.scene().cancel_resize()
        self.scene().cancel_link()
        self.unsetCursor()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self.scene().cancel_resize()
        self.scene().cancel_link()
        super().hideEvent(event)

    def dragEnterEvent(self, event):
        from .controls import node_choice
        from aetherloom_core.image_import import accepts_mime
        if node_choice(event.mimeData()) or accepts_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        from .controls import node_choice
        from aetherloom_core.image_import import accepts_mime
        if node_choice(event.mimeData()) or accepts_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        from .controls import node_choice
        choice=node_choice(event.mimeData())
        if choice:
            self.node_dropped.emit(choice,self.mapToScene(event.pos()));event.acceptProposedAction();return
        from aetherloom_core.image_import import import_mime
        position=self.mapToScene(event.pos())
        identity=getattr(self.scene(),'_document',{}).get('id')
        def imported(paths):
            if getattr(self.scene(),'_document',{}).get('id')==identity:
                self.files_dropped.emit(paths,position)
        if import_mime(event.mimeData(),self,imported,'any'):
            event.acceptProposedAction()

    def fit_nodes(self):
        rect = QtCore.QRectF()
        for node in self.scene().nodes.values():
            rect = rect.united(node.sceneBoundingRect())
        if not rect.isEmpty():
            rect = rect.adjusted(-50, -50, 50, 50)
            available = self.available_rect()
            zoom = min(1.15, available.width() / rect.width(), available.height() / rect.height())
            self.resetTransform()
            self.scale(max(.12, zoom), max(.12, zoom))
            self._center_available(rect.center())
        self.view_changed.emit()
