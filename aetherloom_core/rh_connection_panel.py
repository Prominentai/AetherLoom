"""Shared connection editor and bounded, read-only account queries."""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import hashlib
import re
import threading
import time

from PyQt5 import QtCore, QtGui, QtWidgets, sip


def identity(host, key):
    return host, hashlib.sha256(key.encode('utf8')).hexdigest()


def account_text(data):
    def number(name):
        try:
            raw = str(data.get(name))
            if len(raw) > 64:
                return '—'
            value = Decimal(raw)
            return format(value, ',f') if value.is_finite() and -12 <= value.as_tuple().exponent <= 12 and value.adjusted() <= 30 else '—'
        except (InvalidOperation, ValueError):
            return '—'
    currency = str(data.get('currency') or '')
    currency = currency if re.fullmatch('[A-Z]{3}', currency) else ''
    kind = str(data.get('apiType') or '—')
    kind = kind if re.fullmatch(r'[A-Za-z0-9_-]{1,40}', kind) else '—'
    return (f"RH 币 {number('remainCoins')}  ·  钱包 {number('remainMoney')} {currency}\n"
            f"当前任务 {number('currentTaskCounts')}  ·  Key 类型 {kind}")


class AccountQueries(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    finished = QtCore.pyqtSignal(object, object)

    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings
        self.pending = set()
        self.results = OrderedDict()
        self.finished.connect(self._finished, QtCore.Qt.QueuedConnection)
        settings.changed.connect(self._prune)

    def _valid(self):
        return {identity(host, key) for host, keys in self.settings.site_keyrings().items() for key in keys}

    def _prune(self):
        valid = self._valid()
        for token in list(self.results):
            if token not in valid:
                self.results.pop(token, None)
        self.changed.emit()

    def request(self, host, key):
        token = identity(host, key)
        if token in self.pending:
            return ''
        if len(self.pending) >= 2:
            return '已有两个账户正在查询，请稍候再试。'
        self.pending.add(token)
        self.changed.emit()

        def work():
            from api_calls.call_rh import get_account_status
            try:
                data = get_account_status(key, base_url=host, timeout=15)
                result = (True, account_text(data), time.time())
            except Exception as exc:
                code = str(getattr(exc, 'code', ''))
                http = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = ('错误码 ' + code if re.fullmatch(r'\d{1,6}', code) else
                          'HTTP ' + str(http) if isinstance(http, int) else '网络或响应异常')
                result = (False, '查询失败：' + detail + '，可重新查询。', time.time())
            try:
                self.finished.emit(token, result)
            except RuntimeError:
                pass  # Owner may have closed while the bounded request finished.

        threading.Thread(target=work, name='rh-account-query', daemon=True).start()
        return ''

    @QtCore.pyqtSlot(object, object)
    def _finished(self, token, result):
        self.pending.discard(token)
        if token in self._valid():
            self.results[token] = result
            self.results.move_to_end(token)
            while len(self.results) > 128:
                self.results.popitem(last=False)
        self.changed.emit()


def button(text, action, parent=None):
    value = QtWidgets.QPushButton(text, parent)
    value.setAutoDefault(False)
    value.setCursor(QtCore.Qt.PointingHandCursor)
    value.clicked.connect(action)
    return value


class _StatusLabel(QtWidgets.QLabel):
    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)
        self.setVisible(bool(text))

    def clear(self):
        self.setText('')


class KeyRow(QtWidgets.QFrame):
    def __init__(self, panel, key, index, count, draft=None):
        super().__init__()
        self.panel, self.key, self.host = panel, key, panel.settings.host
        self.setObjectName('rhKeyRow')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)
        header = QtWidgets.QHBoxLayout();header.setSpacing(6)
        order = QtWidgets.QLabel(f'{index + 1:02}  ' + ('首选密钥' if index == 0 else '备用密钥'))
        order.setObjectName('rhKeyOrder')
        order.setToolTip('首选密钥' if index == 0 else '重试顺序 ' + str(index + 1))
        header.addWidget(order);header.addStretch(1)
        self.up = button('↑', lambda: panel.move(self, -1))
        self.down = button('↓', lambda: panel.move(self, 1))
        self.up.setToolTip('提前使用此 Key'); self.down.setToolTip('延后使用此 Key')
        self.up.setAccessibleName('提前使用此密钥');self.down.setAccessibleName('延后使用此密钥')
        self.up.setEnabled(index > 0); self.down.setEnabled(index < count - 1)
        for control in (self.up, self.down):
            control.setObjectName('rhKeyMove');control.setFixedSize(28, 26);header.addWidget(control)
        self.remove_button = button('删除', lambda: panel.remove(self))
        self.remove_button.setObjectName('rhKeyDelete');header.addWidget(self.remove_button)
        layout.addLayout(header)
        editor = QtWidgets.QHBoxLayout();editor.setSpacing(6)
        self.edit = QtWidgets.QLineEdit(key if draft is None else draft)
        self.edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.edit.setMinimumWidth(60)
        self.edit.setAccessibleName(f'第 {index + 1} 个 API Key')
        editor.addWidget(self.edit, 1)
        self.reveal = button('显示', self._reveal)
        self.reveal.setAccessibleName('显示或隐藏此密钥')
        self.reveal.setCheckable(True)
        editor.addWidget(self.reveal)
        self.save = button('保存', self._save)
        self.save.setObjectName('rhConnectionPrimary')
        editor.addWidget(self.save)
        layout.addLayout(editor)
        actions = QtWidgets.QHBoxLayout();actions.setSpacing(10)
        self.query = button('查询账户', self._query)
        self.query.setObjectName('rhAccountQuery')
        self.account = QtWidgets.QLabel()
        self.account.setObjectName('rhAccountResult')
        self.account.setTextFormat(QtCore.Qt.PlainText)
        self.account.setWordWrap(True)
        self.account.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.account.setMinimumWidth(0)
        self.account.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        actions.addWidget(self.account, 1);actions.addWidget(self.query)
        layout.addLayout(actions)
        self.edit.textChanged.connect(self._edited)
        self.edit.returnPressed.connect(self._save)
        self._edited()
        self.refresh_account()

    def _reveal(self):
        show = self.reveal.isChecked()
        self.edit.setEchoMode(QtWidgets.QLineEdit.Normal if show else QtWidgets.QLineEdit.Password)
        self.reveal.setText('隐藏' if show else '显示')

    def _edited(self):
        dirty = self.edit.text().strip() != self.key
        self.save.setEnabled(dirty)
        self.query.setEnabled(not dirty and identity(self.host, self.key) not in self.panel.queries.pending)
        self.query.setToolTip('请先保存修改后的 Key' if dirty else '查询此 Key 所属站点的账户信息')

    def _save(self):
        self.panel.replace(self, self.edit.text().strip())

    def _query(self):
        self.panel.message.setText(self.panel.queries.request(self.host, self.key))

    def refresh_account(self):
        token = identity(self.host, self.key)
        pending = token in self.panel.queries.pending
        result = self.panel.queries.results.get(token)
        self.query.setText('查询中…' if pending else '刷新账户' if result else '查询账户')
        self._edited()
        suffix = '尾号 ' + self.key[-4:] if len(self.key) > 8 else '短密钥'
        text = result[1] if result else suffix + (' · 查询中…' if pending else ' · 未查询账户')
        self.account.setText(text)
        self.account.setToolTip(text + ('\n' + suffix + ' · 更新于 ' + time.strftime('%H:%M:%S', time.localtime(result[2])) if result else ''))
        self.account.setProperty('failed', bool(result and not result[0]))
        self.account.style().unpolish(self.account); self.account.style().polish(self.account)


class RhConnectionPanel(QtWidgets.QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from .rh_connections import SITES, _HostCombo
        self.settings = settings
        if not hasattr(settings, 'account_queries'):
            settings.account_queries = AccountQueries(settings)
        self.queries = settings.account_queries
        self.rows = []
        self._drafts = {}
        self._host = settings.host
        self._new_drafts = {}
        self.setObjectName('rhConnectionPanel')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        layout.setContentsMargins(22, 20, 22, 18);layout.setSpacing(12)
        title = QtWidgets.QLabel('连接设置')
        title.setObjectName('rhConnectionTitle'); layout.addWidget(title)
        self.hint = QtWidgets.QLabel('RunningHub · 应用主页与画布共用连接')
        self.hint.setObjectName('rhConnectionMuted'); self.hint.setWordWrap(True); layout.addWidget(self.hint)
        self.host_combo = _HostCombo(self)
        self.site_buttons = []
        self.site_group = QtWidgets.QButtonGroup(self);self.site_group.setExclusive(True)
        self.site_frame = QtWidgets.QFrame();self.site_frame.setObjectName('rhSiteSelector')
        site_row = QtWidgets.QHBoxLayout(self.site_frame);site_row.setContentsMargins(4, 4, 4, 4);site_row.setSpacing(4)
        for host, unused, label in SITES:
            self.host_combo.addItem(label, host)
            index = len(self.site_buttons)
            site_button = button('中文站 · .cn' if index == 0 else '国际站 · .ai',
                                 lambda checked=False, i=index:self.host_combo.setCurrentIndex(i))
            site_button.setObjectName('rhSiteButton');site_button.setCheckable(True)
            site_button.setToolTip(host + '\n此站点的密钥独立保存')
            self.site_group.addButton(site_button,index);self.site_buttons.append(site_button);site_row.addWidget(site_button,1)
        self.host_combo.hide()
        layout.addWidget(self.site_frame)
        self.site_note = QtWidgets.QLabel('.cn 与 .ai 的 API Key 不通用，请在对应站点添加。')
        self.site_note.setObjectName('rhConnectionMuted');self.site_note.setWordWrap(True);layout.addWidget(self.site_note)
        key_heading = QtWidgets.QHBoxLayout()
        key_title = QtWidgets.QLabel('API 密钥');key_title.setObjectName('rhConnectionSection');key_heading.addWidget(key_title)
        key_heading.addStretch(1)
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('rhConnectionMuted');key_heading.addWidget(self.summary)
        layout.addLayout(key_heading)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.container = QtWidgets.QWidget()
        self.items = QtWidgets.QVBoxLayout(self.container)
        self.items.setContentsMargins(0, 0, 5, 0); self.items.setSpacing(10)
        self.scroll.setWidget(self.container)
        self.key_stack = QtWidgets.QStackedWidget();self.key_stack.setMinimumHeight(90)
        self.key_stack.addWidget(self.scroll)
        self.empty = QtWidgets.QLabel('尚未添加密钥\n\n粘贴本站 API Key，建立第一个连接')
        self.empty.setAlignment(QtCore.Qt.AlignCenter); self.empty.setObjectName('rhConnectionMuted')
        self.empty.setWordWrap(True);self.key_stack.addWidget(self.empty);layout.addWidget(self.key_stack,1)
        self.add_frame = QtWidgets.QFrame();self.add_frame.setObjectName('rhConnectionAdd')
        add_layout = QtWidgets.QVBoxLayout(self.add_frame);add_layout.setContentsMargins(12,12,12,12);add_layout.setSpacing(8)
        add_header = QtWidgets.QHBoxLayout()
        add_title = QtWidgets.QLabel('添加密钥');add_title.setObjectName('rhConnectionSection');add_header.addWidget(add_title)
        add_header.addStretch(1)
        self.get_key_button = button('获取 API Key ↗', lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(self.settings.host + '/enterprise-api/sharedApi')))
        self.get_key_button.setObjectName('rhConnectionLink');add_header.addWidget(self.get_key_button);add_layout.addLayout(add_header)
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText('粘贴本站 API Key')
        self.key_edit.setAccessibleName('待添加的 API Key')
        self.key_edit.setClearButtonEnabled(True)
        self.key_edit.setMinimumWidth(60)
        entry = QtWidgets.QHBoxLayout(); entry.addWidget(self.key_edit, 1)
        self.reveal_button = button('显示', self._reveal_new)
        self.reveal_button.setCheckable(True)
        self.reveal_button.setAccessibleName('显示或隐藏待添加的密钥')
        entry.addWidget(self.reveal_button)
        self.add_button = button('添加', self._add);self.add_button.setObjectName('rhConnectionPrimary')
        entry.addWidget(self.add_button);add_layout.addLayout(entry)
        self.add_hint = QtWidgets.QLabel('可批量粘贴，用空格或逗号分隔；按列表顺序尝试。')
        self.add_hint.setWordWrap(True);self.add_hint.setObjectName('rhConnectionMuted');add_layout.addWidget(self.add_hint)
        layout.addWidget(self.add_frame)
        self.message = _StatusLabel()
        self.message.setTextFormat(QtCore.Qt.PlainText); self.message.setWordWrap(True)
        self.message.setObjectName('rhConnectionMessage')
        self.message.setMaximumHeight(42)
        self.message.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self.message.setVisible(False)
        layout.addWidget(self.message)
        self.key_edit.returnPressed.connect(self._add)
        self.host_combo.currentIndexChanged.connect(self._host_changed)
        settings.changed.connect(self._refresh)
        settings.error.connect(self.message.setText)
        self.queries.changed.connect(self._accounts_changed)
        self._refresh(); self.apply_theme()

    def _host_changed(self, index):
        self.settings._guard(lambda: self.settings.set_host(self.host_combo.itemData(index)))
        self._refresh()

    def _reveal_new(self):
        visible = self.reveal_button.isChecked()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password)
        self.reveal_button.setText('隐藏' if visible else '显示')

    def _refresh(self):
        if self._host != self.settings.host:
            self._new_drafts[self._host] = self.key_edit.text()
            self._host = self.settings.host
            self.key_edit.setText(self._new_drafts.get(self._host, ''))
            self.reveal_button.setChecked(False); self._reveal_new()
            self.message.clear()
        for row in self.rows:
            self._drafts[identity(row.host, row.key)] = row.edit.text()
        valid = {identity(host, key) for host, keys in self.settings.site_keyrings().items() for key in keys}
        self._drafts = {token: value for token, value in self._drafts.items() if token in valid}
        self.rows = []
        while self.items.count():
            item = self.items.takeAt(0)
            if item.widget():
                item.widget().hide(); item.widget().deleteLater()
        blocker = QtCore.QSignalBlocker(self.host_combo)
        self.host_combo.setCurrentIndex(self.host_combo.findData(self.settings.host))
        del blocker
        for index, site_button in enumerate(self.site_buttons):site_button.setChecked(index == self.host_combo.currentIndex())
        keys = self.settings.keys_for()
        self.summary.setText(f'{len(keys)} 个 · 从上到下依次尝试')
        for index, key in enumerate(keys):
            row = KeyRow(self, key, index, len(keys), self._drafts.get(identity(self.settings.host, key)))
            self.items.addWidget(row); self.rows.append(row)
        self.items.addStretch(1)
        self.key_stack.setCurrentIndex(0 if keys else 1)

    def _accounts_changed(self):
        for row in self.rows:
            row.refresh_account()

    def _add(self):
        values = list(dict.fromkeys(re.split(r'[\s,;，；]+', self.key_edit.text().strip())))
        values = [value for value in values if value]
        if not values:
            self.message.setText('请先粘贴 API Key。'); return
        keys = self.settings.keys_for()
        added = [value for value in values if value not in keys]
        if not added:
            self.message.setText('这些 Key 已在本站列表中。'); return
        if self.settings._guard(lambda: self.settings.set_keys(keys + added)):
            self.key_edit.clear(); self.message.setText(f'已添加 {len(added)} 个 Key。')
            QtCore.QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def replace(self, row, value):
        keys = self.settings.keys_for(row.host)
        if not value or re.search(r'\s', value):
            self.message.setText('请输入一个完整的 API Key，不要包含空格。'); return
        if value != row.key and value in keys:
            self.message.setText('这个 Key 已在本站列表中。'); return
        if row.key in keys:
            keys[keys.index(row.key)] = value
            if self.settings._guard(lambda: self.settings.set_keys(keys, row.host)):
                self.message.setText('修改已保存。')

    def remove(self, row):
        keys = self.settings.keys_for(row.host)
        if row.key in keys:
            keys.remove(row.key)
            if self.settings._guard(lambda: self.settings.set_keys(keys, row.host)):
                self.message.setText('Key 已删除。')

    def move(self, row, offset):
        keys = self.settings.keys_for(row.host)
        if row.key not in keys: return
        index = keys.index(row.key); target = index + offset
        if 0 <= target < len(keys):
            keys[index], keys[target] = keys[target], keys[index]
            if self.settings._guard(lambda: self.settings.set_keys(keys, row.host)):
                self.message.setText('使用顺序已保存。')

    def showEvent(self, event):
        self.apply_theme(); super().showEvent(event)

    def minimumSizeHint(self):
        # Allow the first resize to reach compact mode instead of forcing the
        # expanded layout's minimum height back onto the native dialog.
        return QtCore.QSize(280, 320)

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, width):
        # The key list scrolls inside the available height; wrapped row labels
        # must not make QDialog resize itself to the list's preferred height.
        return -1

    def resizeEvent(self, event):
        compact = self.height() < 520
        self.hint.setVisible(not compact)
        self.add_hint.setVisible(not compact)
        self.layout().setContentsMargins(14 if compact else 22, 12 if compact else 20, 14 if compact else 22, 12 if compact else 18)
        self.layout().setSpacing(6 if compact else 12)
        self.site_note.setText('.cn / .ai 密钥独立，互不通用。' if compact else '.cn 与 .ai 的 API Key 不通用，请在对应站点添加。')
        self.key_stack.setMinimumHeight(64 if compact else 90)
        self.add_frame.layout().setContentsMargins(10 if compact else 12, 8 if compact else 12, 10 if compact else 12, 8 if compact else 12)
        self.add_frame.layout().setSpacing(6 if compact else 8)
        super().resizeEvent(event)

    def hideEvent(self, event):
        self.reveal_button.setChecked(False); self._reveal_new()
        for row in self.rows:
            row.reveal.setChecked(False); row._reveal()
        super().hideEvent(event)

    def apply_theme(self):
        from .rh_ui import palette
        p = palette(getattr(self.settings.owner, '_theme_mode', 'dark'))
        self.setStyleSheet(f'''
            QWidget#rhConnectionPanel {{ background: {p['canvas']}; color: {p['text']}; }}
            QWidget#rhConnectionPanel QWidget {{ color: {p['text']}; font-size: 12px; }}
            QWidget#rhConnectionPanel QScrollArea, QWidget#rhConnectionPanel QScrollArea QWidget {{ background: transparent; }}
            QWidget#rhConnectionPanel QFrame#rhKeyRow {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; }}
            QWidget#rhConnectionPanel QFrame#rhConnectionAdd {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 12px; }}
            QWidget#rhConnectionPanel QFrame#rhSiteSelector {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 11px; }}
            QWidget#rhConnectionPanel QLabel {{ background: transparent; border: none; padding: 0; }}
            QWidget#rhConnectionPanel QLabel#rhConnectionTitle {{ font-size: 23px; font-weight: 700; }}
            QWidget#rhConnectionPanel QLabel#rhConnectionSection {{ font-size: 13px; font-weight: 600; }}
            QWidget#rhConnectionPanel QLabel#rhKeyOrder {{ color: {p['accent']}; font-weight: 600; }}
            QWidget#rhConnectionPanel QLabel#rhConnectionMessage {{ color: {p['accent']}; padding: 2px 0; }}
            QWidget#rhConnectionPanel QLabel#rhConnectionMuted, QWidget#rhConnectionPanel QLabel#rhAccountResult {{ color: {p['muted']}; }}
            QWidget#rhConnectionPanel QLabel[failed="true"] {{ color: {p['danger']}; }}
            QWidget#rhConnectionPanel QLineEdit, QWidget#rhConnectionPanel QComboBox {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 8px 6px; selection-background-color: {p['accent']}; }}
            QWidget#rhConnectionPanel QLineEdit:focus {{ border-color: {p['accent']}; }}
            QWidget#rhConnectionPanel QPushButton {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 6px 9px; }}
            QWidget#rhConnectionPanel QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
            QWidget#rhConnectionPanel QPushButton:disabled {{ color: {p['muted']}; background: {p['input']}; }}
            QWidget#rhConnectionPanel QPushButton#rhConnectionPrimary:enabled {{ background: {p['accent']}; color: white; border: 1px solid {p['accent']}; }}
            QWidget#rhConnectionPanel QPushButton#rhSiteButton {{ background: transparent; border: none; padding: 9px 6px; border-radius: 8px; color: {p['muted']}; }}
            QWidget#rhConnectionPanel QPushButton#rhSiteButton:checked {{ background: {p['accent_soft']}; color: {p['accent']}; font-weight: 600; }}
            QWidget#rhConnectionPanel QPushButton#rhSiteButton:hover {{ color: {p['text']}; background: {p['hover']}; }}
            QWidget#rhConnectionPanel QPushButton#rhConnectionLink, QWidget#rhConnectionPanel QPushButton#rhAccountQuery {{ color: {p['accent']}; background: transparent; border-color: transparent; padding: 5px 4px; }}
            QWidget#rhConnectionPanel QPushButton#rhConnectionLink:hover, QWidget#rhConnectionPanel QPushButton#rhAccountQuery:hover {{ background: {p['accent_soft']}; }}
            QWidget#rhConnectionPanel QPushButton#rhKeyMove {{ padding: 0; background: transparent; border-color: transparent; }}
            QWidget#rhConnectionPanel QPushButton#rhKeyMove:hover {{ background: {p['hover']}; }}
            QWidget#rhConnectionPanel QPushButton#rhKeyDelete {{ padding: 3px 7px; background: transparent; border-color: transparent; color: {p['muted']}; }}
            QWidget#rhConnectionPanel QPushButton#rhKeyDelete:hover {{ color: {p['danger']}; background: {p['hover']}; }}
            QWidget#rhConnectionPanel QScrollBar:vertical {{ background: transparent; width: 7px; margin: 0; }}
            QWidget#rhConnectionPanel QScrollBar::handle:vertical {{ background: {p['border']}; min-height: 24px; border-radius: 3px; }}
            QWidget#rhConnectionPanel QScrollBar::add-line:vertical, QWidget#rhConnectionPanel QScrollBar::sub-line:vertical {{ height: 0; }}
            QWidget#rhConnectionPanel QScrollBar::add-page:vertical, QWidget#rhConnectionPanel QScrollBar::sub-page:vertical {{ background: transparent; }}
            QWidget#rhConnectionPanel QComboBox QAbstractItemView {{ background: {p['surface']}; color: {p['text']}; selection-background-color: {p['accent_soft']}; }}
        ''')
