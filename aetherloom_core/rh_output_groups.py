"""Two bounded, independently paged result sections for an RH App."""
from PyQt5 import QtCore, QtWidgets, sip


class _OutputGroup(QtWidgets.QFrame):
    toggled = QtCore.pyqtSignal(str, bool)
    page_requested = QtCore.pyqtSignal(str, int)

    def __init__(self, key, title, hint, parent=None):
        super().__init__(parent)
        self.key, self.title = key, title
        self._cards = []
        self._columns = 0
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
        self.grid.setSpacing(12)
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
        self.content.installEventFilter(self)
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
        columns = max(1, self.content.width() // 292)
        if columns == self._columns:
            return
        self._columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self._cards):
            if not sip.isdeleted(card):
                self.grid.addWidget(card, index // columns, index % columns)
                card.show()

    def eventFilter(self, watched, event):
        if watched is self.content and event.type() == QtCore.QEvent.Resize:
            self._reflow()
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
            ('active', '运行与结果', '已获得提交或重试资格的任务，以及已结束的任务。'),
            ('waiting', '等候任务', '尚未获得提交资格的任务；获得资格后移至上方。'),
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
