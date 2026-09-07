import os
import re
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_ui import palette


def completion_options(settings=None):
    """Normalize persisted options, preserving behavior for older settings files."""
    raw = settings.get('autocomplete', {}) if isinstance(settings, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    rows = raw.get('visible_tags', 15)
    try:
        rows = 15 if isinstance(rows, bool) else max(1, min(50, int(rows)))
    except (TypeError, ValueError, OverflowError):
        rows = 15
    return {'escape_parentheses': raw.get('escape_parentheses', True) if isinstance(raw.get('escape_parentheses', True), bool) else True,
            'replace_spaces': raw.get('replace_spaces', False) if isinstance(raw.get('replace_spaces', False), bool) else False,
            'visible_tags': rows}


def format_completion(word, following_text, *, escape_parentheses=True, replace_spaces=False):
    """Escape literal tag parentheses and reuse an adjacent existing comma."""
    text = word.replace(' ', '_') if replace_spaces else word.replace('_', ' ')
    # An odd backslash run already escapes the parenthesis. Preserve it rather
    # than turning an already escaped tag back into weighting syntax.
    if escape_parentheses:
        text = re.sub(r'(\\*)([()])', lambda match: match[1] +
                      ('\\' if len(match[1]) % 2 == 0 else '') + match[2], text)
    return text if following_text.lstrip().startswith((',', '，')) else text + ', '


class CompletionDelegate(QtWidgets.QStyledItemDelegate):
    """Paint only visible candidates, including the matching prefix."""
    def sizeHint(self, option, index):
        metrics = QtGui.QFontMetrics(option.font)
        return QtCore.QSize(metrics.horizontalAdvance(str(index.data() or '')) + 76,
                            max(32, metrics.height() + 14))

    def paint(self, painter, option, index):
        popup = self.parent()
        colors = popup.colors
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        hover = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        rect = option.rect.adjusted(6, 1, -6, -1)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        if selected or hover:
            painter.setBrush(QtGui.QColor(colors['accent_soft'] if selected else colors['hover']))
            painter.drawRoundedRect(QtCore.QRectF(rect), 6, 6)
        painter.setFont(option.font)
        painter.setPen(QtGui.QColor(colors['accent'] if selected else colors['muted']))
        painter.drawText(rect.adjusted(10, 0, 0, 0), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, '#')
        text_rect = rect.adjusted(32, 0, -28, 0)
        text = str(index.data() or '')
        metrics = QtGui.QFontMetrics(option.font)
        text = metrics.elidedText(text, QtCore.Qt.ElideRight, max(0, text_rect.width()))
        prefix_length = len(popup.prefix) if text.casefold().startswith(popup.prefix.casefold()) else 0
        prefix, suffix = text[:prefix_length], text[prefix_length:]
        baseline = text_rect.center().y() + (metrics.ascent() - metrics.descent()) / 2
        painter.setClipRect(text_rect)
        painter.setPen(QtGui.QColor(colors['accent']))
        painter.drawText(QtCore.QPointF(text_rect.left(), baseline), prefix)
        painter.setPen(QtGui.QColor(colors['text']))
        painter.drawText(QtCore.QPointF(text_rect.left() + metrics.horizontalAdvance(prefix), baseline), suffix)
        painter.setClipping(False)
        if selected:
            painter.setPen(QtGui.QPen(QtGui.QColor(colors['accent']), 1.5,
                                     QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            x, y = rect.right() - 12, rect.center().y()
            arrow = QtGui.QPainterPath(QtCore.QPointF(x, y - 4))
            arrow.lineTo(x, y + 2)
            arrow.lineTo(x - 9, y + 2)
            arrow.moveTo(x - 6, y - 1)
            arrow.lineTo(x - 9, y + 2)
            arrow.lineTo(x - 6, y + 5)
            painter.drawPath(arrow)
        painter.restore()


class AutocompletePopup(QtWidgets.QListWidget):
    """补全建议的弹出列表框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setObjectName("AutocompletePopup")
        self.setAccessibleName('提示词补全候选')
        self.setMouseTracking(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerItem)
        self.prefix = ''
        self._theme = None
        self.header_height, self.footer_height = 34, 30
        self.setViewportMargins(0, self.header_height, 0, self.footer_height)
        self.header = QtWidgets.QWidget(self)
        self.header.setObjectName('completionHeader')
        header_layout = QtWidgets.QHBoxLayout(self.header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        self.title_label = QtWidgets.QLabel('提示词补全')
        self.count_label = QtWidgets.QLabel()
        self.count_label.setObjectName('completionMuted')
        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.count_label)
        self.footer = QtWidgets.QLabel(self)
        self.footer.setObjectName('completionFooter')
        self.footer.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self.footer.setContentsMargins(10, 0, 10, 0)
        self._hint = '↑↓ 选择    Tab / Enter 填入    Esc 关闭'
        self.footer.setToolTip(self._hint)
        self.setItemDelegate(CompletionDelegate(self))
        self.apply_theme('dark')

    @property
    def extra_height(self):
        return self.header_height + self.footer_height

    def apply_theme(self, mode):
        if mode == self._theme:
            return
        self._theme = mode
        self.colors = p = palette(mode)
        self.setStyleSheet(f'''
            QListWidget#AutocompletePopup {{ border: 1px solid {p['border']};
                border-radius: 10px; background: {p['surface']}; color: {p['text']}; outline: none; }}
            #AutocompletePopup QWidget#completionHeader {{ background: transparent; border: none;
                border-bottom: 1px solid {p['border']}; }}
            #AutocompletePopup QLabel {{ background: transparent; border: none; color: {p['text']}; font-size: 12px; }}
            #AutocompletePopup QLabel#completionMuted {{ color: {p['muted']}; }}
            #AutocompletePopup QLabel#completionFooter {{ color: {p['muted']}; font-size: 11px;
                border-top: 1px solid {p['border']}; }}
            #AutocompletePopup QScrollBar:vertical {{ width: 7px; background: transparent;
                margin: {self.header_height + 3}px 0 {self.footer_height + 3}px 0; }}
            #AutocompletePopup QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 3px; min-height: 24px; }}
            #AutocompletePopup QScrollBar::add-line:vertical, #AutocompletePopup QScrollBar::sub-line:vertical {{ height: 0; }}
            #AutocompletePopup QScrollBar::add-page:vertical, #AutocompletePopup QScrollBar::sub-page:vertical {{ background: transparent; }}
        ''')

    def set_candidates(self, matches, prefix, mode):
        self.apply_theme(mode)
        self.prefix = prefix
        self.clear()
        self.addItems(matches)
        for row, match in enumerate(matches):
            self.item(row).setToolTip(match)
        self.count_label.setText(f'{len(matches)} 个候选')
        self.setCurrentRow(0)
        self.scrollToTop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        frame = self.frameWidth()
        width = max(0, self.width() - frame * 2)
        self.header.setGeometry(frame, frame, width, self.header_height)
        self.footer.setGeometry(frame, self.height() - frame - self.footer_height, width, self.footer_height)
        self.footer.setText(self.footer.fontMetrics().elidedText(self._hint, QtCore.Qt.ElideRight, max(0, width - 20)))

    def showEvent(self, event):
        super().showEvent(event)
        QtWidgets.QApplication.instance().installEventFilter(self)

    def hideEvent(self, event):
        QtWidgets.QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, receiver, event):
        if self.isVisible():
            kind = event.type()
            if kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick,
                        QtCore.QEvent.NonClientAreaMouseButtonPress, QtCore.QEvent.NonClientAreaMouseButtonDblClick,
                        QtCore.QEvent.TouchBegin):
                inside = receiver is self or (isinstance(receiver, QtWidgets.QWidget)
                                               and self.isAncestorOf(receiver))
                if not inside:
                    self.hide()
            elif kind == QtCore.QEvent.ApplicationDeactivate:
                self.hide()
        # Do not swallow the outside click or a click on a candidate/scrollbar.
        return super().eventFilter(receiver, event)


class AutocompleteManager:
    """管理词库加载和搜索"""
    _INDEX_PREFIX_LENGTH = 3

    def __init__(self, file_path='autocomplete.txt'):
        self.suggestions = []  # 存储 (word, count) 元组
        self.load_words(file_path)

    def load_words(self, file_path):
        self.suggestions = []
        self._prefix_index = {}
        if not file_path:
            return
        if not os.path.exists(file_path):
            return
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.rsplit(',', 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        word, count = parts[0], int(parts[1])
                    else:
                        word, count = line, 0
                    self.suggestions.append((word, count))
            # 按使用次数(count)降序排列，次数相同按字母升序
            self.suggestions.sort(key=lambda x: (-x[1], x[0].lower()))
        except Exception as e:
            print(f"Error loading {file_path}: {e}")

        # 各前缀桶仅保存原词的引用，保留词频顺序、重复词和同分时的稳定顺序。
        # 输入时只扫描对应桶，避免每个按键都遍历整个词库。
        for word, _ in self.suggestions:
            normalized = word.lower()
            for length in range(1, min(len(normalized), self._INDEX_PREFIX_LENGTH) + 1):
                self._prefix_index.setdefault(normalized[:length], []).append(word)

    def get_matches(self, prefix, limit=50):
        if not prefix:
            return []
        prefix = prefix.lower()
        matches = []
        entries = self._prefix_index.get(prefix[:self._INDEX_PREFIX_LENGTH], ())
        for word in entries:
            if word.lower().startswith(prefix):
                matches.append(word)
                if len(matches) >= limit:
                    break
        return matches


# 全局单例管理器（基于当前目录的 autocomplete.txt）
_manager = None
_manager_path = None


def get_manager(current_dir):
    global _manager, _manager_path
    path = os.path.normcase(os.path.abspath(os.path.join(current_dir or os.getcwd(), 'autocomplete.txt')))
    if _manager is None or _manager_path != path:
        _manager = AutocompleteManager(path)
        _manager_path = path
    return _manager
