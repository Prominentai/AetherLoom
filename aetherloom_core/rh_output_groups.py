"""Two bounded, independently paged result sections for an RH App."""
from PyQt5 import QtCore, QtGui, QtWidgets, sip


class ResultTitle(QtWidgets.QLabel):
    """Keep the full filename accessible without making narrow cards taller."""

    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)

    def sizeHint(self):
        return QtCore.QSize(0, self.fontMetrics().height() + 4)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(QtGui.QPalette.WindowText))
        text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.ElideMiddle, self.contentsRect().width())
        painter.drawText(self.contentsRect(), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)


class OutputCard(QtWidgets.QFrame):
    """Resize existing preview content without reopening files or media players."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._preview_size_key = None
        self._available_height = 0
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._fit_preview)

    def preview_width(self):
        margins = self.layout().contentsMargins() if self.layout() else QtCore.QMargins()
        return max(1, self.contentsRect().width() - margins.left() - margins.right())

    def refresh_preview(self):
        if not self._preview_timer.isActive():
            self._preview_timer.start(0)

    def set_available_size(self, width, height):
        self.setFixedWidth(width)
        if height != self._available_height:
            self._available_height = height
            self.refresh_preview()

    def _height_limit(self, label, width):
        layout = self.layout()
        margins = layout.contentsMargins()
        overhead = margins.top() + margins.bottom() + 2 * self.frameWidth()
        visible = 0
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.isEmpty():
                continue
            visible += 1
            if item.widget() is label:
                continue
            height = item.heightForWidth(width) if item.hasHeightForWidth() else item.sizeHint().height()
            overhead += max(0, height)
        overhead += max(0, visible - 1) * layout.spacing()
        return max(96, min(420, self._available_height - overhead)) if self._available_height else 420

    def event(self, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show,
                            QtCore.QEvent.LayoutRequest, QtCore.QEvent.FontChange):
            if hasattr(self, '_preview_timer'):
                self.refresh_preview()
        return super().event(event)

    def _fit_preview(self):
        label = getattr(self, '_img_label', None)
        if label is None or sip.isdeleted(label) or not self.isVisible():
            return
        width = self.preview_width()
        progress = getattr(self, '_rh_progress_widget', None)
        if progress is not None and not sip.isdeleted(progress) and not progress.isHidden():
            progress.set_available_height(self._height_limit(progress, width))
            return
        height_limit = self._height_limit(label, width)
        label.setMinimumWidth(0)
        label.setMaximumWidth(QtWidgets.QWIDGETSIZE_MAX)
        label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        if isinstance(label, QtWidgets.QTextEdit):
            # Wrapping follows the actual viewport; long text remains scrollable.
            height = int(label.document().documentLayout().documentSize().height()) + 12
            height = max(96, min(height, height_limit, max(160, min(360, width))))
        elif isinstance(label, QtWidgets.QLabel):
            pixmap = getattr(label, '_orig_pixmap', None)
            if isinstance(pixmap, QtGui.QPixmap) and not pixmap.isNull():
                limit = QtCore.QSize(max(1, width - 8), max(1, min(height_limit - 12, int(width * 1.35))))
                key = (id(label), pixmap.cacheKey(), limit.width(), limit.height())
                if key != self._preview_size_key:
                    scaled = pixmap.scaled(limit, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                    label.setPixmap(scaled)
                    self._preview_size_key = key
                height = label.pixmap().height() + 12
            else:
                label.setWordWrap(True)
                height = max(48, min(160, label.heightForWidth(width)))
        else:
            return
        if label.minimumHeight() != height or label.maximumHeight() != height:
            label.setFixedHeight(height)


class _OutputGroup(QtWidgets.QFrame):
    toggled = QtCore.pyqtSignal(str, bool)
    page_requested = QtCore.pyqtSignal(str, int)

    def __init__(self, key, title, hint, parent=None):
        super().__init__(parent)
        self.key, self.title = key, title
        self._cards = []
        self._columns = 0
        self._card_width = 0
        self._available_height = 0
        self._reflow_timer = QtCore.QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)
        self.setMinimumWidth(0)
        self.setObjectName('rhOutputGroup')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.toggle = QtWidgets.QToolButton(self)
        self.toggle.setObjectName('rhSecondaryButton')
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(QtCore.Qt.DownArrow)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setMinimumHeight(32)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.toggle.setToolTip(hint)
        layout.addWidget(self.toggle)
        self.body = QtWidgets.QWidget(self)
        body_layout = QtWidgets.QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(6)
        self.pager = QtWidgets.QWidget(self.body)
        pager_layout = QtWidgets.QHBoxLayout(self.pager)
        pager_layout.setContentsMargins(0, 0, 0, 0)
        pager_layout.setSpacing(4)
        self.previous = QtWidgets.QPushButton('上一页', self.pager)
        self.previous.setMinimumWidth(0)
        self.label = QtWidgets.QLabel(self.pager)
        self.label.setObjectName('rhMuted')
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setMinimumWidth(0)
        self.following = QtWidgets.QPushButton('下一页', self.pager)
        self.following.setMinimumWidth(0)
        pager_layout.addWidget(self.previous)
        pager_layout.addWidget(self.label, 1)
        pager_layout.addWidget(self.following)
        body_layout.addWidget(self.pager)
        self.scroll = QtWidgets.QScrollArea(self.body)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.content = QtWidgets.QWidget()
        self.content.setMinimumWidth(0)
        self.grid = QtWidgets.QGridLayout(self.content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.scroll.setWidget(self.content)
        self.empty = QtWidgets.QLabel('暂无任务', self.body)
        self.empty.setObjectName('rhMuted')
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setMinimumHeight(32)
        body_layout.addWidget(self.empty)
        body_layout.addWidget(self.scroll, 1)
        layout.addWidget(self.body, 1)
        self.scroll.viewport().installEventFilter(self)
        self.previous.clicked.connect(lambda: self.page_requested.emit(key, -1))
        self.following.clicked.connect(lambda: self.page_requested.emit(key, 1))
        self.toggle.toggled.connect(self._toggle)
        self.set_page(0, 0, 60)

    def _toggle(self, expanded):
        self.toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.body.setVisible(expanded)
        self.toggled.emit(self.key, expanded)

    def set_page(self, offset, total, size):
        self.total = total
        self.toggle.setText(f'{self.title} · {total}')
        self.pager.setVisible(total > size)
        self.previous.setEnabled(offset > 0)
        self.following.setEnabled(offset + size < total)
        self.label.setText(f'{offset // size + 1} / {max(1, (total + size - 1) // size)} 页')
        self.label.setToolTip(f'{offset + 1 if total else 0}–{min(total, offset + size)} / {total} 项')
        self.empty.setVisible(not total)
        self.scroll.setVisible(bool(total))

    def set_cards(self, cards):
        cards = [card for card in cards if not sip.isdeleted(card)]
        if cards == self._cards:
            return
        self._cards = cards
        self._columns = 0
        self._reflow()

    def _reflow(self):
        # The viewport is authoritative. Content width can still reflect the
        # previous grid's minimum size when the window is being narrowed.
        available = max(1, self.scroll.viewport().width())
        height = self.scroll.viewport().height()
        spacing = self.grid.horizontalSpacing()
        columns = max(1, (available + spacing) // (196 + spacing))
        width = max(1, (available - spacing * (columns - 1)) // columns)
        if columns == self._columns and width == self._card_width and height == self._available_height:
            return
        rebuild = columns != self._columns
        self._columns, self._card_width = columns, width
        self._available_height = height
        if rebuild:
            while self.grid.count():
                self.grid.takeAt(0)
        for index, card in enumerate(self._cards):
            if not sip.isdeleted(card):
                if isinstance(card, OutputCard):
                    card.set_available_size(width, height)
                else:
                    card.setFixedWidth(width)
                if rebuild:
                    self.grid.addWidget(card, index // columns, index % columns, QtCore.Qt.AlignTop)
                card.show()

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self._reflow_timer.start(16)
        return False


class RhOutputGroups(QtWidgets.QWidget):
    """Card creation/state belongs to the bridge; this widget only places its pages."""
    page_requested = QtCore.pyqtSignal(str, int)
    group_toggled = QtCore.pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.groups = {}
        for key, title, hint in (
            ('active', '运行与结果', '已提交成功的任务，以及已结束的任务。'),
            ('waiting', '等候与重试', '尚未提交成功的等候、提交及重试任务。'),
        ):
            group = _OutputGroup(key, title, hint, self)
            self.groups[key] = group
            self._layout.addWidget(group, 1)
            group.page_requested.connect(self.page_requested)
            group.toggled.connect(self._toggle)

    def _toggle(self, key, expanded):
        self._layout.setStretch(list(self.groups).index(key), 1 if expanded and self.groups[key].total else 0)
        self.group_toggled.emit(key, expanded)

    def is_expanded(self, key):
        return self.groups[key].toggle.isChecked()

    def set_page(self, key, offset, total, size):
        self.groups[key].set_page(offset, total, size)
        self._layout.setStretch(list(self.groups).index(key), 1 if total and self.is_expanded(key) else 0)

    def set_cards(self, key, cards):
        self.groups[key].set_cards(cards)
