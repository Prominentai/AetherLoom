"""Bounded result pages with a shared, lazy image/video/text preview."""
import os
from functools import lru_cache
from .input_requirements import display_issue
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_parameters import RhEnumComboBox
from . import preview_data as data
from . import model


@lru_cache(maxsize=4)
def _checker_tile(dark, light):
    tile = QtGui.QImage(24, 24, QtGui.QImage.Format_RGB32)
    tile.fill(QtGui.QColor(light))
    painter = QtGui.QPainter(tile)
    painter.fillRect(0, 0, 12, 12, QtGui.QColor(dark))
    painter.fillRect(12, 12, 12, 12, QtGui.QColor(dark));painter.end()
    return tile


def paint_checkerboard(painter, rect, colors=None):
    dark = QtGui.QColor((colors or {}).get('input', '#ffffff')).lightness() < 128
    tile = _checker_tile('#343e4c', '#26303d') if dark else _checker_tile('#d1d5db', '#f3f4f6')
    painter.fillRect(rect, QtGui.QBrush(tile))


class MediaPreview(QtWidgets.QWidget):
    def __init__(self, cache, parent=None):
        super().__init__(parent)
        self.cache, self._value = cache, {}
        from aetherloom_core.hover_preview import WidgetHover
        self.hover_preview = WidgetHover(self, self, lambda: data.path_of(self.value))
        self.setMinimumHeight(185)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        cache.ready.connect(self.update)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if data.path_of(value) != data.path_of(self._value):
            from aetherloom_core.hover_preview import hover_player
            hover_player().stop(self.hover_preview)
        self._value = value
        if self.underMouse() and self.isVisible():
            self.hover_preview.start()
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        parent = self.parentWidget()
        while parent is not None and parent.objectName() != 'aetherloomCanvasPage':parent = parent.parentWidget()
        colors = parent.scene.colors if parent is not None else {}
        border = QtGui.QColor(colors['border']) if colors else self.palette().color(QtGui.QPalette.Mid)
        background = QtGui.QColor(colors['input']) if colors else self.palette().color(QtGui.QPalette.Base)
        foreground = QtGui.QColor(colors['text']) if colors else self.palette().color(QtGui.QPalette.Text)
        painter.fillRect(self.rect(), QtGui.QColor(colors['surface']) if colors else self.palette().color(QtGui.QPalette.Window))
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(border)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 8, 8)
        path, kind = data.path_of(self.value), data.kind_of(self.value)
        pixmap = self.cache.get(path, kind) if path and kind in ('image', 'video', 'mask') else None
        if pixmap is not None and not pixmap.isNull():
            body = rect.adjusted(8, 8, -8, -8)
            size = pixmap.size().scaled(body.size().toSize(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRectF(0, 0, size.width(), size.height())
            target.moveCenter(body.center())
            # A checkerboard makes transparent paint layers distinguishable from white images.
            painter.save();painter.setClipRect(target)
            paint_checkerboard(painter, target, colors)
            painter.drawPixmap(target, pixmap, QtCore.QRectF(pixmap.rect()));painter.restore()
        else:
            text = (self.value.get('_preview_error') or '暂无本地文件，下载完成后可预览' if not path else
                    '文件已移动或删除' if not os.path.exists(path) else
                    '无法生成预览\n可点击“打开所选”查看原文件' if kind in ('image', 'video', 'mask') and self.cache.failed(path, kind) else
                    '正在加载预览，无法预览时可打开原文件' if kind in ('image', 'video', 'mask') else
                    '音频文件\n点击“打开所选”播放' if kind == 'audio' else
                    '运行时读取文件夹内符合格式的文件' if kind == 'folder' else '此文件可使用系统应用打开')
            painter.setPen(foreground)
            painter.drawText(rect.adjusted(18, 18, -18, -18), QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, text)


class ResultBrowser(QtWidgets.QWidget):
    PAGE_SIZE = 60
    open_requested = QtCore.pyqtSignal(object)

    def __init__(self, node, cache, parent=None):
        super().__init__(parent)
        self.node, self.cache, self.page = node, cache, 0
        self.results = node.get('results', [])
        self._index = data.ResultIndex(self.results)
        self._listing_signature = None
        self.setMinimumWidth(0)
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0, 0, 0, 0);layout.setSpacing(9)
        # Visibility is initialized before layout insertion; keep this a child.
        self.source = RhEnumComboBox(self)
        self.source.addItem('最近运行结果', 'results')
        if data.has_inputs(node):self.source.addItem('当前输入预览', 'input')
        self.source.setVisible(data.has_inputs(node))
        if not node.get('results') and data.has_inputs(node):self.source.setCurrentIndex(1)
        layout.addWidget(self.source)
        self.type_filter = RhEnumComboBox()
        self.type_filter.setObjectName('canvasResultType')
        self.type_filter.addItem('全部类型 · 按输出顺序', '')
        self.type_filter.hide()
        layout.addWidget(self.type_filter)
        self.hint = QtWidgets.QLabel();self.hint.setWordWrap(True);self.hint.setObjectName('canvasMuted');layout.addWidget(self.hint)
        self.preview_title = QtWidgets.QLabel();self.preview_title.setWordWrap(True);layout.addWidget(self.preview_title)
        self.media = MediaPreview(cache)
        self.text = QtWidgets.QPlainTextEdit();self.text.setReadOnly(True);self.text.setMinimumHeight(185)
        self.text.setPlaceholderText('空文本')
        self.stack = QtWidgets.QStackedWidget();self.stack.addWidget(self.media);self.stack.addWidget(self.text);layout.addWidget(self.stack, 1)
        self.detail = QtWidgets.QLabel();self.detail.setWordWrap(True);self.detail.setObjectName('canvasMuted');layout.addWidget(self.detail)
        self.listing = QtWidgets.QListWidget();self.listing.setMinimumHeight(64);self.listing.setMaximumHeight(128)
        self.listing.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.listing.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.listing.setSelectionMode(self.listing.ExtendedSelection);layout.addWidget(self.listing)
        row = QtWidgets.QHBoxLayout()
        self.previous = QtWidgets.QPushButton('上一页');self.next = QtWidgets.QPushButton('下一页');self.counter = QtWidgets.QLabel()
        row.addWidget(self.previous);row.addWidget(self.counter, 1, QtCore.Qt.AlignCenter);row.addWidget(self.next);layout.addLayout(row)
        self.previous.clicked.connect(lambda:self.turn(-1));self.next.clicked.connect(lambda:self.turn(1))
        self.source.currentIndexChanged.connect(lambda:self.refresh(reset=True))
        self.type_filter.currentIndexChanged.connect(lambda:self.refresh(reset=True))
        self.listing.currentItemChanged.connect(self.show_item)
        self.listing.itemDoubleClicked.connect(lambda item:self.open_requested.emit(item.data(QtCore.Qt.UserRole)))
        cache.ready.connect(self.refresh_text)
        self.refresh()

    def refresh(self, reset=False):
        mode = self.source.currentData()
        context = dict(self.node, results=self.results)
        if not self._index.matches(self.results):self._index = data.ResultIndex(self.results)
        total = data.count(context, mode) if mode == 'input' else self._index.total
        grouped = mode == 'results'
        self._indices = None
        if grouped:
            groups = self._index.type_indices()
            chosen = self.type_filter.currentData() or ''
            with QtCore.QSignalBlocker(self.type_filter):
                self.type_filter.clear();self.type_filter.addItem('全部类型 · 按输出顺序', '')
                for kind, indices in groups.items():
                    self.type_filter.addItem(f'{data.TYPE_NAMES.get(kind, kind)} · {len(indices)} 项', kind)
                self.type_filter.setCurrentIndex(max(0, self.type_filter.findData(chosen)))
            chosen = self.type_filter.currentData()
            if chosen:self._indices = groups[chosen];total = len(self._indices)
        self.type_filter.setVisible(grouped and len(self._index.type_indices()) > 1)
        current = self.listing.currentRow()
        selected = {item.data(QtCore.Qt.UserRole+1) for item in self.listing.selectedItems()} if not reset else set()
        self.page = 0 if reset else max(0, min(self.page, max(0, (total-1)//self.PAGE_SIZE)))
        stale = self.node.get('stale') or self.node.get('_ui_stale')
        note = ('当前节点设置中的输入；文件夹在运行时展开。' if mode == 'input' else
                '缺少输入：' + display_issue(self.node) + '。保留上次预览，本分支本次不执行。' if display_issue(self.node) else
                '本次已跳过：' + str(self.node.get('message') or '上游未提供输入') + '。当前展示上次可用结果。' if self.node.get('status') == 'SKIPPED' else
                '显示上次运行结果；节点或连线已修改，重新运行后更新。' if stale else
                '本次运行尚未完成，当前仍展示上次可用结果。' if self.node.get('status') in ('PENDING', 'WAITING', 'QUEUED', 'LOCAL_WAIT', 'RUNNING', 'PREPARING', 'SUBMITTING', 'DOWNLOADING', 'DECODING') else
                'Batch 已展开供预览，组号标在各项前；筛选仅影响展示，不改变下游输入。' if self._index.batch_count else
                '图像 List：每张图像独立输出，下游逐项处理；需要共同编辑时请先转为 Batch。' if self.node['kind'] in ('image_model', 'edit_model') else
                '按输出顺序展示。选择一项预览，双击或打开所选查看原文件。')
        empty = ('尚未添加输入，请在节点设置中选择或拖入素材。' if mode == 'input' else
                 '缺少输入：' + display_issue(self.node) + '。补齐后再运行此分支。' if display_issue(self.node) else
                 '本次已跳过：' + str(self.node.get('message') or '上游未提供输入') if self.node.get('status') == 'SKIPPED' else
                 '本次未生成可用结果：' + str(self.node.get('message') or '此分支未完成') if self.node.get('status') in ('FAILED', 'BLOCKED', 'CANCELED', 'UNKNOWN') else
                 '任务尚未完成，可用结果返回后会自动显示。' if self.node.get('status') in ('PENDING', 'WAITING', 'RUNNING', 'PREPARING', 'SUBMITTING', 'DOWNLOADING', 'DECODING', 'QUEUED', 'LOCAL_WAIT') else
                 '暂无运行结果。连接并运行节点后，结果会显示在这里。')
        self.hint.setText(note if total else empty)
        signature = (id(self.results), len(self.results), mode, self.type_filter.currentData(), self.page,
                     tuple(self.node.get('params', {}).get('files') or []) if mode == 'input' else None,
                     self.node.get('params', {}).get('text') if mode == 'input' else None)
        if not reset and signature == self._listing_signature:
            self.show_item(self.listing.currentItem())
            return
        self._listing_signature = signature
        with QtCore.QSignalBlocker(self.listing):
            self.listing.clear()
            for index in range(self.page*self.PAGE_SIZE, min(total, (self.page+1)*self.PAGE_SIZE)):
                original_index = self._indices[index] if self._indices is not None else index
                value = data.item_at(context, original_index, mode) if mode == 'input' else self._index.item(original_index)
                kind = data.kind_of(value)
                label = value.get('_batch_label', str(original_index+1))
                item = QtWidgets.QListWidgetItem(f'{label} · {data.TYPE_NAMES.get(kind, "文件")}  {data.title_of(value)}')
                item.setData(QtCore.Qt.UserRole, value);item.setData(QtCore.Qt.UserRole+1, original_index)
                item.setToolTip(data.path_of(value) or data.text_of(value)[:4096])
                self.listing.addItem(item)
            if total:self.listing.setCurrentRow(max(0, min(current if not reset else 0, self.listing.count()-1)))
            if selected:
                for row in range(self.listing.count()):
                    item = self.listing.item(row)
                    item.setSelected(item.data(QtCore.Qt.UserRole+1) in selected)
        self.previous.setEnabled(self.page > 0);self.next.setEnabled((self.page+1)*self.PAGE_SIZE < total)
        self.previous.setVisible(total > self.PAGE_SIZE);self.next.setVisible(total > self.PAGE_SIZE)
        self.counter.setText(f'共 {total} 项' + (f' · 第 {self.page+1}/{(total+self.PAGE_SIZE-1)//self.PAGE_SIZE} 页' if total > self.PAGE_SIZE else ''))
        # A single result is already shown above; its duplicate list row need not take space.
        self.listing.setVisible(total > 1)
        self.show_item(self.listing.currentItem())
        self.listing.itemSelectionChanged.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def turn(self, delta):
        self.page += delta;self.refresh()

    def focus_index(self, index, source='results'):
        choice = self.source.findData(source)
        if choice >= 0:
            with QtCore.QSignalBlocker(self.source):self.source.setCurrentIndex(choice)
        if source == 'results':
            with QtCore.QSignalBlocker(self.type_filter):self.type_filter.setCurrentIndex(0)
            self.refresh()
            if self._indices is not None:
                try:index = self._indices.index(index)
                except ValueError:return
        self.page = max(0, index)//self.PAGE_SIZE;self.refresh()
        self.listing.setCurrentRow(index % self.PAGE_SIZE, QtCore.QItemSelectionModel.ClearAndSelect)

    def show_item(self, item, previous=None):
        self.stack.setVisible(item is not None);self.detail.setVisible(item is not None)
        if item is None:
            self.preview_title.clear();self.detail.clear();self.text.clear();self.media.value={};self.media.update();return
        value = item.data(QtCore.Qt.UserRole);kind = data.kind_of(value);path = data.path_of(value)
        self.preview_title.setText(data.title_of(value))
        self.preview_title.setToolTip(path or data.text_of(value))
        self.media.value = value
        self.stack.setCurrentWidget(self.text if kind in {'text', 'bounding'} | model.VALUE_TYPES else self.media)
        self.detail.setText(data.TYPE_NAMES.get(kind, '文件') + (' · 首帧预览' if kind == 'video' else '')
                            + (' · 文件已移动或删除' if path and not os.path.exists(path) else '')
                            + (' · 预览最多 4096 字符，完整内容请打开查看' if kind == 'text' and (path or len(data.text_of(value)) > 4096) else ''))
        self.refresh_text();self.media.update()

    def refresh_text(self):
        item = self.listing.currentItem()
        if item is None or self.stack.currentWidget() is not self.text:return
        value = item.data(QtCore.Qt.UserRole);path = data.path_of(value)
        content = data.text_of(value)
        if path and (data.kind_of(value) == 'bounding' or not any(key in value for key in ('text', 'value'))):
            content = self.cache.get(path, 'text')
            if content is None:
                content = ('文件已移动或删除' if not os.path.isfile(path) else
                           '无法读取文本，请打开原文件查看。' if self.cache.failed(path, 'text') else '正在读取文本…')
        content = str(content)[:4096]
        if self.text.toPlainText() != content:self.text.setPlainText(content)
