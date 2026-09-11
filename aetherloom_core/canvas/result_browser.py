"""Bounded result pages with a shared, lazy image/video/text preview."""
import os
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_parameters import RhEnumComboBox
from . import preview_data as data
from . import model


class MediaPreview(QtWidgets.QWidget):
    def __init__(self, cache, parent=None):
        super().__init__(parent)
        self.cache, self.value = cache, {}
        self.setMinimumHeight(185)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        cache.ready.connect(self.update)

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
        pixmap = self.cache.get(path, kind) if path and kind in ('image', 'video') else None
        if pixmap is not None and not pixmap.isNull():
            body = rect.adjusted(8, 8, -8, -8)
            size = pixmap.size().scaled(body.size().toSize(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRectF(0, 0, size.width(), size.height())
            target.moveCenter(body.center())
            # A checkerboard makes transparent paint layers distinguishable from white images.
            painter.save();painter.setClipRect(target)
            for y in range(int(target.top()), int(target.bottom()) + 1, 12):
                for x in range(int(target.left()), int(target.right()) + 1, 12):
                    painter.fillRect(x, y, 12, 12, QtGui.QColor('#d1d5db' if ((x-int(target.left()))//12+(y-int(target.top()))//12)%2 else '#f3f4f6'))
            painter.drawPixmap(target, pixmap, QtCore.QRectF(pixmap.rect()));painter.restore()
        else:
            text = ('文件已移动或删除' if path and not os.path.exists(path) else
                    '无法生成预览\n可点击“打开所选”查看原文件' if kind in ('image', 'video') and self.cache.failed(path, kind) else
                    '正在加载预览，无法预览时可打开原文件' if kind in ('image', 'video') else
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
        self.setMinimumWidth(0)
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0, 0, 0, 0);layout.setSpacing(9)
        # Visibility is initialized before layout insertion; keep this a child.
        self.source = QtWidgets.QComboBox(self)
        self.source.addItem('最近运行结果', 'results')
        if data.has_inputs(node):self.source.addItem('当前输入预览', 'input')
        self.source.setVisible(data.has_inputs(node))
        if not node.get('results') and data.has_inputs(node):self.source.setCurrentIndex(1)
        layout.addWidget(self.source)
        self.type_filter = RhEnumComboBox()
        self.type_filter.setObjectName('canvasResultType')
        self.type_filter.addItem('全部类型（分组展示）', '')
        self.type_filter.hide()
        layout.addWidget(self.type_filter)
        self.hint = QtWidgets.QLabel();self.hint.setWordWrap(True);self.hint.setObjectName('canvasMuted');layout.addWidget(self.hint)
        self.preview_title = QtWidgets.QLabel();self.preview_title.setWordWrap(True);layout.addWidget(self.preview_title)
        self.media = MediaPreview(cache)
        self.text = QtWidgets.QPlainTextEdit();self.text.setReadOnly(True);self.text.setMinimumHeight(185)
        self.text.setPlaceholderText('空文本')
        self.stack = QtWidgets.QStackedWidget();self.stack.addWidget(self.media);self.stack.addWidget(self.text);layout.addWidget(self.stack)
        self.detail = QtWidgets.QLabel();self.detail.setWordWrap(True);self.detail.setObjectName('canvasMuted');layout.addWidget(self.detail)
        self.listing = QtWidgets.QListWidget();self.listing.setMinimumHeight(110);self.listing.setMaximumHeight(235)
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
        total = data.count(context, mode)
        grouped = self.node['kind'] in ('app', 'list_select', 'merge_list', 'batch2list') and mode == 'results' and not data.has_batches(context)
        self.type_filter.setVisible(grouped and bool(total))
        self._indices = None
        if grouped:
            groups = {}
            for index, value in enumerate(self.results):groups.setdefault(data.kind_of(value), []).append(index)
            chosen = self.type_filter.currentData() or ''
            with QtCore.QSignalBlocker(self.type_filter):
                self.type_filter.clear();self.type_filter.addItem('全部类型（分组展示）', '')
                for kind, indices in groups.items():
                    self.type_filter.addItem(f'{data.TYPE_NAMES.get(kind, kind)} List · {len(indices)}', kind)
                self.type_filter.setCurrentIndex(max(0, self.type_filter.findData(chosen)))
            chosen = self.type_filter.currentData()
            self._indices = [index for kind, indices in groups.items() if not chosen or kind == chosen for index in indices]
            total = len(self._indices)
        current = self.listing.currentRow()
        selected = {item.data(QtCore.Qt.UserRole+1) for item in self.listing.selectedItems()} if not reset else set()
        self.page = 0 if reset else min(self.page, max(0, (total-1)//self.PAGE_SIZE))
        stale = self.node.get('stale') or self.node.get('_ui_stale')
        note = ('当前节点设置中的输入；文件夹在运行时展开。' if mode == 'input' else
                'Batch 与普通项按输出顺序预览；预览展开不改变 Batch 的分组，普通项仍逐项传递。' if data.has_batches(context) else
                '显示上次运行结果；节点已修改，重新运行后更新。' if stale else
                '正在执行新任务，当前仍展示上次可用结果。' if self.node.get('status') in ('RUNNING', 'PREPARING', 'SUBMITTING', 'DOWNLOADING', 'DECODING') else
                '各类型分别组成 List；类型内保持输出顺序，连接对应输出端口使用该列表。' if grouped else
                '图像 List：每张图像独立输出，下游逐项处理；需要共同编辑时请先转为 Batch。' if self.node['kind'] in ('image_model', 'edit_model') else
                '按输出顺序展示。选择一项预览，双击或打开所选查看原文件。')
        empty = ('尚未添加输入，请在节点设置中选择或拖入素材。' if mode == 'input' else
                 '任务尚未完成，可用结果返回后会自动显示。' if self.node.get('status') in ('PENDING', 'WAITING', 'RUNNING', 'PREPARING', 'SUBMITTING', 'DOWNLOADING', 'DECODING', 'QUEUED', 'LOCAL_WAIT') else
                 '暂无运行结果。连接并运行节点后，结果会显示在这里。')
        self.hint.setText(note if total else empty)
        with QtCore.QSignalBlocker(self.listing):
            self.listing.clear()
            for index in range(self.page*self.PAGE_SIZE, min(total, (self.page+1)*self.PAGE_SIZE)):
                original_index = self._indices[index] if self._indices is not None else index
                value = data.item_at(context, original_index, mode)
                kind = data.kind_of(value)
                label = value.get('_batch_label', str(index+1))
                item = QtWidgets.QListWidgetItem(f'{label} · {data.TYPE_NAMES.get(kind, "文件")}  {data.title_of(value)}')
                item.setData(QtCore.Qt.UserRole, value);item.setData(QtCore.Qt.UserRole+1, original_index)
                item.setToolTip(data.path_of(value) or data.text_of(value))
                self.listing.addItem(item)
            if total:self.listing.setCurrentRow(max(0, min(current if not reset else 0, self.listing.count()-1)))
            if selected:
                for row in range(self.listing.count()):
                    item = self.listing.item(row)
                    item.setSelected(item.data(QtCore.Qt.UserRole+1) in selected)
        self.previous.setEnabled(self.page > 0);self.next.setEnabled((self.page+1)*self.PAGE_SIZE < total)
        self.previous.setVisible(total > self.PAGE_SIZE);self.next.setVisible(total > self.PAGE_SIZE)
        self.counter.setText(f'共 {total} 项' + (f' · 第 {self.page+1}/{(total+self.PAGE_SIZE-1)//self.PAGE_SIZE} 页' if total > self.PAGE_SIZE else ''))
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
        if self.node['kind'] in ('app', 'list_select', 'merge_list', 'batch2list') and source == 'results':
            with QtCore.QSignalBlocker(self.type_filter):self.type_filter.setCurrentIndex(0)
            self.refresh()
            if self._indices is not None:
                try:index = self._indices.index(index)
                except ValueError:return
        self.page = max(0, index)//self.PAGE_SIZE;self.refresh()
        self.listing.setCurrentRow(index % self.PAGE_SIZE, QtCore.QItemSelectionModel.ClearAndSelect)

    def show_item(self, item, previous=None):
        self.stack.setVisible(item is not None);self.detail.setVisible(item is not None)
        if item is None:self.preview_title.clear();return
        value = item.data(QtCore.Qt.UserRole);kind = data.kind_of(value);path = data.path_of(value)
        self.preview_title.setText(data.title_of(value))
        self.preview_title.setToolTip(path or data.text_of(value))
        self.media.value = value
        self.stack.setCurrentWidget(self.text if kind in {'text'} | model.VALUE_TYPES else self.media)
        self.detail.setText(data.TYPE_NAMES.get(kind, '文件') + (' · 首帧预览' if kind == 'video' else '')
                            + (' · 文件已移动或删除' if path and not os.path.exists(path) else '')
                            + (' · 预览最多 4096 字符，完整内容请打开查看' if kind == 'text' and (path or len(data.text_of(value)) > 4096) else ''))
        self.refresh_text();self.media.update()

    def refresh_text(self):
        item = self.listing.currentItem()
        if item is None or self.stack.currentWidget() is not self.text:return
        value = item.data(QtCore.Qt.UserRole);path = data.path_of(value)
        content = data.text_of(value)
        if not any(key in value for key in ('text', 'value')) and path:
            content = self.cache.get(path, 'text')
            if content is None:
                content = ('文件已移动或删除' if not os.path.isfile(path) else
                           '无法读取文本，请打开原文件查看。' if self.cache.failed(path, 'text') else '正在读取文本…')
        content = str(content)[:4096]
        if self.text.toPlainText() != content:self.text.setPlainText(content)
