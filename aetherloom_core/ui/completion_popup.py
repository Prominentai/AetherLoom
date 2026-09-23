"""Compact, non-activating local prompt tag suggestions."""
import re

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_ui import palette


_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def contains_event_receiver(widget, receiver):
    """Recognize both widget events and their native-window delivery stage.

    Windows first delivers pointer input to a QWidgetWindow, before forwarding
    it to the list viewport. Treating that QWindow as outside dismisses the
    popup before its candidate can receive the click or wheel event.
    """
    if receiver is widget or (isinstance(receiver, QtWidgets.QWidget)
                              and widget.isAncestorOf(receiver)):
        return True
    if isinstance(receiver, QtGui.QWindow):
        handle = widget.windowHandle()
        while receiver is not None:
            if receiver is handle:
                return True
            # Native child windows belong to us; transient owner windows do not.
            receiver = receiver.parent()
    return False


def _display_tag(text):
    return str(text or '').replace('_', ' ')


def _match_ranges(text, query):
    """Highlight matching word prefixes, independently of tag word order."""
    terms = tuple(dict.fromkeys(word.casefold() for word in _WORDS.findall(query)))
    ranges = []
    for match in _WORDS.finditer(text):
        folded = match.group().casefold()
        lengths = [len(term) for term in terms if folded.startswith(term)]
        if not lengths:
            continue
        # Casefold can expand a character (e.g. ß), so map its length back to
        # display characters instead of painting a differently sized substring.
        needed, consumed, end = max(lengths), 0, match.start()
        for char in match.group():
            consumed += len(char.casefold())
            end += 1
            if consumed >= needed:
                break
        ranges.append((match.start(), end))
    return ranges


class _TagDelegate(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        popup = self.parent()
        text = _display_tag(index.data())
        return QtCore.QSize(min(popup._chip_width, option.fontMetrics.horizontalAdvance(text) + 22),
                            max(28, option.fontMetrics.height() + 10))

    def paint(self, painter, option, index):
        popup = self.parent()
        colors = popup.colors
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        rect = option.rect.adjusted(0, 0, -1, -1)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(QtGui.QColor(colors['accent_soft'] if selected else
                                     colors['hover'] if hovered else colors['input']))
        painter.setPen(QtGui.QColor(colors['accent'] if selected else colors['border']))
        painter.drawRoundedRect(QtCore.QRectF(rect), 6, 6)
        text_rect = rect.adjusted(9, 0, -9, 0)
        painter.setFont(option.font)
        text = option.fontMetrics.elidedText(_display_tag(index.data()), QtCore.Qt.ElideRight,
                                            max(0, text_rect.width()))
        ranges = _match_ranges(text, popup.prefix)
        baseline = text_rect.center().y() + (option.fontMetrics.ascent() - option.fontMetrics.descent()) / 2
        painter.setClipRect(text_rect)
        start, x = 0, text_rect.left()
        for left, right in ranges + [(len(text), len(text))]:
            for segment, color in ((text[start:left], colors['text']),
                                   (text[left:right], colors['accent'])):
                painter.setPen(QtGui.QColor(color))
                painter.drawText(QtCore.QPointF(x, baseline), segment)
                x += option.fontMetrics.horizontalAdvance(segment)
            start = right
        painter.restore()


class AutocompletePopup(QtWidgets.QListWidget):
    """QListWidget-compatible tag chips; only explicit dismissal emits a signal.

    Owners can call ``hide()`` while updating a query without suppressing later
    results. ``dismiss()`` (also used for outside interaction and Escape) emits
    ``dismissed`` so an asynchronous owner can reject late results.
    """
    dismissed = QtCore.pyqtSignal()
    MAX_HEIGHT = 330

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint |
                            QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setObjectName('AutocompletePopup')
        self.setAccessibleName('提示词标签候选')
        self.setViewMode(QtWidgets.QListView.IconMode)
        self.setFlow(QtWidgets.QListView.LeftToRight)
        self.setWrapping(True)
        self.setMovement(QtWidgets.QListView.Static)
        self.setResizeMode(QtWidgets.QListView.Adjust)
        self.setLayoutMode(QtWidgets.QListView.SinglePass)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setFocusPolicy(QtCore.Qt.NoFocus)
        self.setSpacing(4)
        self.setMouseTracking(True)
        self.prefix = ''
        self._theme = None
        self._chip_width = 420
        self.header_height, self.footer_height = 34, 26
        self.setViewportMargins(5, self.header_height, 5, self.footer_height)
        self.header = QtWidgets.QWidget(self)
        self.header.setObjectName('completionHeader')
        head = QtWidgets.QHBoxLayout(self.header)
        head.setContentsMargins(10, 0, 7, 0)
        head.setSpacing(6)
        self.title_label = QtWidgets.QLabel('标签候选')
        self.count_label = QtWidgets.QLabel()
        self.count_label.setObjectName('completionMuted')
        head.addWidget(self.title_label)
        head.addStretch(1)
        head.addWidget(self.count_label)
        self.close_button = QtWidgets.QToolButton(self.header)
        self.close_button.setText('×')
        self.close_button.setToolTip('关闭建议 (Esc)')
        self.close_button.setAccessibleName('关闭提示词建议')
        self.close_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self.close_button.setFixedSize(22, 22)
        self.close_button.clicked.connect(self.dismiss)
        head.addWidget(self.close_button)
        self.footer = QtWidgets.QLabel(self)
        self.footer.setObjectName('completionFooter')
        self.footer.setContentsMargins(10, 0, 10, 0)
        self.footer.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self._hint = '↑↓ 选择 · Tab / Enter 填入 · Esc 关闭'
        self.footer.setToolTip(self._hint)
        self.setItemDelegate(_TagDelegate(self))
        self.apply_theme('dark')

    @property
    def extra_height(self):
        return self.header_height + self.footer_height

    def apply_theme(self, mode):
        mode = 'light' if mode == 'light' else 'dark'
        if mode == self._theme:
            return
        self._theme = mode
        self.colors = p = palette(mode)
        self.setStyleSheet(f'''
            QListWidget#AutocompletePopup {{background:{p['surface']};color:{p['text']};
                border:1px solid {p['border']};border-radius:8px;outline:none;padding:0;font-size:12px;}}
            #AutocompletePopup QWidget#completionHeader {{background:transparent;border:none;}}
            #AutocompletePopup QLabel {{background:transparent;border:none;color:{p['text']};font-size:12px;}}
            #AutocompletePopup QLabel#completionMuted,
            #AutocompletePopup QLabel#completionFooter {{color:{p['muted']};font-size:11px;}}
            #AutocompletePopup QToolButton {{background:transparent;color:{p['muted']};
                border:none;border-radius:4px;padding:0;font-size:17px;}}
            #AutocompletePopup QToolButton:hover {{background:{p['hover']};color:{p['text']};}}
            #AutocompletePopup QScrollBar:vertical {{width:7px;background:transparent;
                margin:{self.header_height + 3}px 0 {self.footer_height + 3}px 0;}}
            #AutocompletePopup QScrollBar::handle:vertical {{background:{p['border']};border-radius:3px;min-height:20px;}}
            #AutocompletePopup QScrollBar::add-line:vertical,
            #AutocompletePopup QScrollBar::sub-line:vertical {{height:0;}}
            #AutocompletePopup QScrollBar::add-page:vertical,
            #AutocompletePopup QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        self.viewport().update()

    def set_candidates(self, matches, prefix, mode):
        self.apply_theme(mode)
        same_query = self.prefix == prefix
        selected = self.currentItem().text() if same_query and self.currentItem() else None
        scroll = self.verticalScrollBar().value() if same_query else 0
        self.prefix = str(prefix or '')
        values = [tag for tag in matches if isinstance(tag, str) and tag.strip()]
        with QtCore.QSignalBlocker(self):
            self.clear()
            self.addItems(values)
            for row, text in enumerate(values):
                self.item(row).setToolTip(_display_tag(text))
            self.setCurrentRow(values.index(selected) if selected in values else 0 if values else -1)
        self.count_label.setText(f'{len(values)} 个')
        self.verticalScrollBar().setValue(scroll)

    def layout_height(self, width, visible_tags=15):
        """Fit the first N chips, keeping remaining candidates scrollable."""
        width = max(1, int(width))
        try:
            count = max(1, min(50, int(visible_tags)))
        except (ValueError, TypeError, OverflowError):
            count = 15
        # Reserve a scrollbar even if it is not currently needed, preventing a
        # height calculation from reflowing when it later becomes visible.
        extent = self.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent)
        inner = max(1, width - self.frameWidth() * 2 - 10 - extent)
        self._chip_width = max(1, inner - self.spacing() * 2)
        metrics = self.fontMetrics()
        height = max(28, metrics.height() + 10)
        x, rows = self.spacing(), 1
        for row in range(self.count()):
            item = self.item(row)
            item_width = min(self._chip_width, metrics.horizontalAdvance(_display_tag(item.text())) + 22)
            item.setSizeHint(QtCore.QSize(item_width, height))
            if row < count:
                if x > self.spacing() and x + item_width + self.spacing() > inner:
                    rows += 1
                    x = self.spacing()
                x += item_width + self.spacing() * 2
        return min(self.MAX_HEIGHT, self.extra_height + self.frameWidth() * 2 +
                   rows * (height + self.spacing() * 2) + self.spacing())

    def setCurrentRow(self, row, *args):
        super().setCurrentRow(row, *args)
        if self.currentItem() is not None:
            self.scrollToItem(self.currentItem(), QtWidgets.QAbstractItemView.EnsureVisible)

    def dismiss(self):
        if self.isVisible():
            self.hide()
            self.dismissed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, 'header'):
            return
        frame = self.frameWidth()
        width = max(0, self.width() - frame * 2)
        self.header.setGeometry(frame, frame, width, self.header_height)
        self.footer.setGeometry(frame, self.height() - frame - self.footer_height, width, self.footer_height)
        self.footer.setText(self.footer.fontMetrics().elidedText(self._hint, QtCore.Qt.ElideRight,
                                                               max(0, width - 20)))

    def showEvent(self, event):
        super().showEvent(event)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def hideEvent(self, event):
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, receiver, event):
        if self.isVisible():
            kind = event.type()
            inside = contains_event_receiver(self, receiver)
            if kind == QtCore.QEvent.ApplicationDeactivate:
                self.dismiss()
            elif kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick,
                          QtCore.QEvent.NonClientAreaMouseButtonPress,
                          QtCore.QEvent.NonClientAreaMouseButtonDblClick,
                          QtCore.QEvent.TouchBegin, QtCore.QEvent.Wheel) and not inside:
                self.dismiss()
            elif kind in (QtCore.QEvent.Move, QtCore.QEvent.Resize):
                editor = self.parentWidget()
                if editor is not None and receiver is editor.window():
                    self.dismiss()
            elif kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape:
                self.dismiss()
                return True
        return super().eventFilter(receiver, event)
