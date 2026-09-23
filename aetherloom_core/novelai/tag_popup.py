"""Two independent, non-focus-stealing NovelAI tag suggestion columns."""
from PyQt5 import QtCore, QtGui, QtWidgets

from .styles import workspace_palette


class _TagDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter, option, index):
        popup = self.parent()
        colors = popup.colors
        active = popup._active_tags()
        selected = (bool(option.state & QtWidgets.QStyle.State_Selected)
                    and active is not None and index.model() == active.model())
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        rect = option.rect.adjusted(0, 0, -1, -1)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(QtGui.QColor(colors['accent_soft'] if selected else
                                     colors['hover'] if hovered else colors['input']))
        painter.setPen(QtGui.QColor(colors['accent'] if selected else colors['border']))
        painter.drawRoundedRect(QtCore.QRectF(rect), 5, 5)
        painter.setFont(option.font)
        painter.setPen(QtGui.QColor(colors['accent'] if selected else colors['text']))
        text_rect = rect.adjusted(8, 0, -8, 0)
        text = option.fontMetrics.elidedText(str(index.data() or ''), QtCore.Qt.ElideRight,
                                            max(0, text_rect.width()))
        painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, text)
        painter.restore()


class _TagList(QtWidgets.QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setViewMode(QtWidgets.QListView.IconMode)
        self.setFlow(QtWidgets.QListView.LeftToRight)
        self.setWrapping(True)
        self.setMovement(QtWidgets.QListView.Static)
        self.setResizeMode(QtWidgets.QListView.Adjust)
        self.setLayoutMode(QtWidgets.QListView.SinglePass)
        self.setUniformItemSizes(False)
        self.setSpacing(4)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setFocusPolicy(QtCore.Qt.NoFocus)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setMouseTracking(True)
        self.setMinimumSize(0, 28)
        self.empty = QtWidgets.QLabel(self.viewport())
        self.empty.setObjectName('naiSuggestionEmpty')
        self.empty.setWordWrap(True)
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty.setGeometry(self.viewport().rect().adjusted(7, 5, -7, -5))


class _SuggestionColumn(QtWidgets.QWidget):
    def __init__(self, source, popup):
        super().__init__(popup)
        self.source = source
        self.state = 'ready' if source == 'local' else 'idle'
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = QtWidgets.QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(4)
        self.title = QtWidgets.QLabel('本地候选' if source == 'local' else '在线候选')
        self.title.setObjectName('naiSuggestionSource')
        heading.addWidget(self.title)
        heading.addStretch(1)
        self.status = QtWidgets.QLabel()
        self.status.setObjectName('naiSuggestionStatus')
        heading.addWidget(self.status)
        layout.addLayout(heading)
        self.tags = _TagList(self)
        self.tags.setAccessibleName(self.title.text())
        self.tags.setItemDelegate(_TagDelegate(popup))
        self.tags.itemPressed.connect(lambda _: popup._activate(source))
        self.tags.itemClicked.connect(lambda item: popup._clicked(source, item))
        layout.addWidget(self.tags, 1)


class TagSuggestionsPopup(QtWidgets.QFrame):
    dismissed = QtCore.pyqtSignal()
    itemClicked = QtCore.pyqtSignal(object)
    # Kept as an optional compatibility hook; sources are always shown together.
    sourceChanged = QtCore.pyqtSignal(str)
    MAX_HEIGHT = 340
    MAX_ITEMS = 10

    def __init__(self, parent):
        super().__init__(parent, QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint |
                         QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setObjectName('novelaiTagSuggestions')
        self.setAccessibleName('NovelAI 提示词标签建议')
        self._theme = None
        self.colors = workspace_palette('dark')
        self.prefix = ''
        self._sources = []
        self.active_source = 'local'
        self._data = {'local': [], 'online': []}
        self._states = {'local': 'ready', 'online': 'idle'}
        self._pending_scroll = {}
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(8, 6, 8, 6)
        self._layout.setSpacing(5)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        self.title_label = QtWidgets.QLabel('你想输入？')
        self.title_label.setObjectName('naiSuggestionTitle')
        head.addWidget(self.title_label, 1)
        self.close_button = QtWidgets.QToolButton()
        self.close_button.setText('×')
        self.close_button.setToolTip('关闭建议 (Esc)')
        self.close_button.setAccessibleName('关闭提示词建议')
        self.close_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self.close_button.setFixedSize(22, 22)
        self.close_button.clicked.connect(self._dismiss)
        head.addWidget(self.close_button)
        self._layout.addLayout(head)
        self._columns_layout = QtWidgets.QHBoxLayout()
        self._columns_layout.setContentsMargins(0, 0, 0, 0)
        self._columns_layout.setSpacing(8)
        self.columns = {}
        for source in ('local', 'online'):
            column = self.columns[source] = _SuggestionColumn(source, self)
            self._columns_layout.addWidget(column, 1)
        self._layout.addLayout(self._columns_layout, 1)
        self.footer = QtWidgets.QLabel()
        self.footer.setObjectName('naiSuggestionFooter')
        self._layout.addWidget(self.footer)
        self.set_sources(True, True)
        self.apply_theme('dark')
        self._refresh_footer()

    @property
    def count_label(self):
        return self.columns[self.active_source or 'local'].status

    def set_sources(self, online, local):
        sources = [s for s, enabled in (('local', local), ('online', online)) if enabled]
        changed = sources != self._sources
        self._sources = sources
        if self.active_source not in sources:
            self.active_source = sources[0] if sources else ''
        for source, column in self.columns.items():
            column.setVisible(source in sources)
        if not sources:
            self.hide()
        if changed:
            self._refresh_footer()
            self._refresh_active_heading()

    @staticmethod
    def _clean_tags(values):
        return [tag for tag in values if isinstance(tag, str) and tag.strip()][:10]

    def set_suggestions(self, local, online, prefix, mode, online_state='idle', local_state='ready'):
        self.apply_theme(mode)
        same_prefix = self.prefix == prefix
        self.prefix = prefix
        for source, values, state in (('local', local, local_state), ('online', online, online_state)):
            tags = self.columns[source].tags
            values = self._clean_tags(values)
            previous = tags.currentItem().text() if same_prefix and tags.currentItem() else None
            scroll = self._pending_scroll.get(source, tags.verticalScrollBar().value()) if same_prefix else 0
            unchanged = same_prefix and self._data[source] == values
            self._data[source] = values
            self._states[source] = state
            if not unchanged:
                self._pending_scroll[source] = scroll
                with QtCore.QSignalBlocker(tags):
                    tags.clear()
                    tags.addItems(values)
                    for row, value in enumerate(values):
                        tags.item(row).setToolTip(value)
                    tags.setCurrentRow(values.index(previous) if previous in values else 0 if values else -1)
                    tags.verticalScrollBar().setValue(scroll)
            self._set_column_state(source, state)
        self._refresh_footer()
        self._refresh_active_heading()

    def set_candidates(self, matches, prefix, mode):
        """Compatibility for callers migrating from the single-list widget."""
        values = dict(self._data)
        values[self.active_source or 'local'] = matches
        self.set_suggestions(values['local'], values['online'], prefix, mode,
                             self._states['online'], self._states['local'])

    def set_state(self, state):
        source = self.active_source or 'local'
        self._states[source] = state
        self._set_column_state(source, state)

    def _set_column_state(self, source, state):
        column = self.columns[source]
        column.state = state
        labels = {'loading': '查询中…', 'building': '加载中…', 'unavailable': '暂不可用',
                  'missing_key': '未连接', 'empty': '无匹配'}
        column.status.setText(labels.get(state, ''))
        messages = {
            'loading': '正在查询本地词库…' if source == 'local' else '正在获取在线标签…',
            'building': '正在加载本地词库…',
            'missing_key': '请在 NovelAI 连接设置中添加 API Key。',
            'unavailable': '暂时无法查询本地词库。' if source == 'local' else '在线建议暂不可用，可继续输入。',
            'idle': '输入至少两个字符以查询在线候选。',
            'empty': '暂无匹配标签，可继续输入。',
            'ready': '暂无本地匹配标签。', 'local': '暂无本地匹配标签。',
            'online': '暂无在线匹配标签。',
        }
        message = messages.get(state, '暂无匹配标签。')
        column.status.setToolTip(message)
        column.tags.empty.setText(message)
        column.tags.empty.setVisible(column.tags.count() == 0)

    def _refresh_active_heading(self):
        for source, column in self.columns.items():
            column.title.setProperty('activeSource', source == self.active_source)
            column.title.style().unpolish(column.title)
            column.title.style().polish(column.title)
            column.tags.viewport().update()

    def _refresh_footer(self):
        hint = ('↑↓ 选择 · Alt＋←→ 切换列 · Tab / Enter 填入'
                if len(self._sources) == 2 else '↑↓ 选择 · Tab / Enter 填入')
        self.footer.setToolTip(hint + ' · Esc 关闭')
        self.footer.setText(self.footer.fontMetrics().elidedText(
            hint, QtCore.Qt.ElideRight, max(0, self.width() - 16)))

    def _activate(self, source):
        if source in self._sources and source != self.active_source:
            self.active_source = source
            self._refresh_active_heading()
            self.sourceChanged.emit(source)

    def _clicked(self, source, item):
        self._activate(source)
        self.itemClicked.emit(item)

    def switch_source(self, direction):
        if not self._sources:
            return
        current = self._sources.index(self.active_source) if self.active_source in self._sources else 0
        self._activate(self._sources[(current + (1 if direction > 0 else -1)) % len(self._sources)])

    def _active_tags(self):
        column = self.columns.get(self.active_source)
        return column.tags if column is not None and self.active_source in self._sources else None

    def currentItem(self):
        tags = self._active_tags()
        return tags.currentItem() if tags is not None else None

    def currentRow(self):
        tags = self._active_tags()
        return tags.currentRow() if tags is not None else -1

    def count(self):
        tags = self._active_tags()
        return tags.count() if tags is not None else 0

    def item(self, index):
        tags = self._active_tags()
        return tags.item(index) if tags is not None else None

    def setCurrentRow(self, row):
        tags = self._active_tags()
        if tags is not None:
            tags.setCurrentRow(row)
            if tags.currentItem() is not None:
                tags.scrollToItem(tags.currentItem(), QtWidgets.QAbstractItemView.EnsureVisible)

    def layout_height(self, width):
        width = max(1, int(width))
        self.setFixedWidth(width)
        count = max(1, len(self._sources))
        column_width = max(1, (width - 16 - 8 * (count - 1)) // count)
        desired_body = 32
        saved_scroll = {}
        for source in self._sources:
            tags = self.columns[source].tags
            saved_scroll[source] = self._pending_scroll.pop(source, tags.verticalScrollBar().value())
            tags.setFixedWidth(column_width)
            metrics = tags.fontMetrics()
            for row in range(tags.count()):
                item = tags.item(row)
                item.setSizeHint(QtCore.QSize(min(max(1, column_width - 20),
                    metrics.horizontalAdvance(item.text()) + 20), max(28, metrics.height() + 10)))
            tags.doItemsLayout()
            bottom = max((tags.visualItemRect(tags.item(row)).bottom() + 1
                          for row in range(tags.count())), default=28)
            desired_body = max(desired_body, bottom + tags.verticalScrollBar().value() + tags.spacing() + 2)
        # All lists share the height but keep independent scrollbars; a screen
        # clamp by the caller can reduce it further without hiding any controls.
        needed = min(self.MAX_HEIGHT, desired_body + 91)
        self.setFixedHeight(needed)
        self._layout.activate()
        for source, value in saved_scroll.items():
            self.columns[source].tags.verticalScrollBar().setValue(value)
        return needed

    def apply_theme(self, mode):
        if mode == self._theme:
            return
        self._theme = mode
        self.colors = p = workspace_palette(mode)
        self.setStyleSheet(f'''
            QFrame#novelaiTagSuggestions {{background:{p['surface']};border:1px solid {p['border']};border-radius:6px;}}
            #novelaiTagSuggestions QLabel {{background:transparent;border:none;color:{p['text']};font-size:12px;}}
            #novelaiTagSuggestions QLabel#naiSuggestionSource {{font-size:11px;color:{p['muted']};}}
            #novelaiTagSuggestions QLabel#naiSuggestionSource[activeSource="true"] {{color:{p['accent']};font-weight:600;}}
            #novelaiTagSuggestions QLabel#naiSuggestionStatus,
            #novelaiTagSuggestions QLabel#naiSuggestionFooter,
            #novelaiTagSuggestions QLabel#naiSuggestionEmpty {{color:{p['muted']};font-size:11px;}}
            #novelaiTagSuggestions QListWidget {{background:transparent;border:none;outline:none;padding:0;}}
            #novelaiTagSuggestions QToolButton {{background:transparent;color:{p['muted']};border:none;padding:0;font-size:16px;}}
            #novelaiTagSuggestions QToolButton:hover {{background:{p['hover']};color:{p['text']};}}
            #novelaiTagSuggestions QScrollBar:vertical {{width:6px;background:transparent;margin:0;}}
            #novelaiTagSuggestions QScrollBar::handle:vertical {{background:{p['border']};border-radius:3px;min-height:16px;}}
            #novelaiTagSuggestions QScrollBar::add-line:vertical,
            #novelaiTagSuggestions QScrollBar::sub-line:vertical {{height:0;}}
            #novelaiTagSuggestions QScrollBar::add-page:vertical,
            #novelaiTagSuggestions QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        for column in getattr(self, 'columns', {}).values():
            column.tags.viewport().update()

    def _dismiss(self):
        self.hide()
        self.dismissed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        QtWidgets.QApplication.instance().installEventFilter(self)

    def hideEvent(self, event):
        QtWidgets.QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'footer'):
            self._refresh_footer()

    def eventFilter(self, receiver, event):
        if self.isVisible():
            kind = event.type()
            inside = receiver is self or isinstance(receiver, QtWidgets.QWidget) and self.isAncestorOf(receiver)
            if kind == QtCore.QEvent.ApplicationDeactivate:
                self._dismiss()
            elif kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick,
                          QtCore.QEvent.NonClientAreaMouseButtonPress,
                          QtCore.QEvent.NonClientAreaMouseButtonDblClick, QtCore.QEvent.TouchBegin,
                          QtCore.QEvent.Wheel) and not inside:
                self._dismiss()
            elif kind in (QtCore.QEvent.Move, QtCore.QEvent.Resize):
                editor = self.parentWidget()
                if editor is not None and receiver is editor.window():
                    self._dismiss()
            elif kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape:
                self._dismiss()
                return True
        return super().eventFilter(receiver, event)
