import os
import re
from PyQt5 import QtCore, QtGui, QtWidgets


def format_completion(word, following_text):
    """Escape literal tag parentheses and reuse an adjacent existing comma."""
    text = word.replace('_', ' ')
    # An odd backslash run already escapes the parenthesis. Preserve it rather
    # than turning an already escaped tag back into weighting syntax.
    text = re.sub(r'(\\*)([()])', lambda match: match[1] +
                  ('\\' if len(match[1]) % 2 == 0 else '') + match[2], text)
    return text if following_text.lstrip().startswith((',', '，')) else text + ', '


class AutocompletePopup(QtWidgets.QListWidget):
    """补全建议的弹出列表框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setObjectName("AutocompletePopup")
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerItem)
        # 增加最大高度以在一页内显示更多条目
        try:
            self.setMaximumHeight(600)
        except Exception:
            pass
        self.setStyleSheet('''
            QListWidget#AutocompletePopup {
                border: 1px solid #444;
                background-color: #2a2a2a;
                color: #ddd;
                border-radius: 4px;
            }
            QListWidget#AutocompletePopup::item {
                padding: 2px 8px;
                font-size: 11px;
            }
            QListWidget#AutocompletePopup::item:selected {
                background-color: #4a90e2;
                color: white;
            }
        ''')

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
