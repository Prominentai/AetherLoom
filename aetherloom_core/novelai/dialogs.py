"""Account, online tag suggestions and local reusable prompt chunks."""
import copy
import json
import os
from pathlib import Path
from PyQt5 import QtCore, QtGui, QtWidgets
from .jobs import Job
from . import storage
from .account_status import AccountStrip, subscription_state
from .quota_model import parse_account
from .credentials import token_for, tokens_for, tokens_from_record


class _AccountTokenRow(QtWidgets.QFrame):
    removeRequested = QtCore.pyqtSignal(object)
    moveRequested = QtCore.pyqtSignal(object, int)
    queryRequested = QtCore.pyqtSignal(object)
    tokenChanged = QtCore.pyqtSignal(object)

    def __init__(self, value='', mode='dark', parent=None):
        super().__init__(parent)
        self.job = None
        self._closed = False
        self._queued = False
        self._revision = 0
        self._account_data = None
        self._account_updated_at = None
        self.setObjectName('naiTokenCard')
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)
        header = QtWidgets.QHBoxLayout()
        self.label = QtWidgets.QLabel()
        self.label.setObjectName('naiTokenLabel')
        header.addWidget(self.label, 1)
        self.up = self._button('↑', '上移此连接')
        self.down = self._button('↓', '下移此连接')
        self.remove = self._button('移除', '移除此连接；保存后生效')
        self.up.clicked.connect(lambda: self.moveRequested.emit(self, -1))
        self.down.clicked.connect(lambda: self.moveRequested.emit(self, 1))
        self.remove.clicked.connect(lambda: self.removeRequested.emit(self))
        for button in (self.up, self.down, self.remove):
            header.addWidget(button)
        box.addLayout(header)
        entry = QtWidgets.QHBoxLayout()
        entry.setSpacing(7)
        self.token = QtWidgets.QLineEdit(value)
        self.token.setObjectName('naiConnectionToken')
        self.token.setMinimumWidth(0)
        self.token.setMinimumHeight(36)
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.token.setPlaceholderText('粘贴 Persistent API Token')
        self.label.setBuddy(self.token)
        entry.addWidget(self.token, 1)
        self.visible = self._button('显示', '显示或隐藏此 Token')
        self.visible.setCheckable(True)
        self.visible.toggled.connect(self._toggle_visible)
        entry.addWidget(self.visible)
        self.check = self._button('查询账户', '只查询此 Token 的订阅和余额，不会生成图像')
        self.check.setObjectName('naiQueryAccount')
        self.check.clicked.connect(self.probe)
        entry.addWidget(self.check)
        box.addLayout(entry)
        self.status = QtWidgets.QLabel('尚未查询账户')
        self.status.setObjectName('naiConnectionStatus')
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.PlainText)
        box.addWidget(self.status)
        self.account_strip = AccountStrip(self, compact=True)
        self.account_strip.apply_theme(mode)
        self.account_strip.render(status='unconfigured')
        box.addWidget(self.account_strip)
        self.token.textChanged.connect(self._token_changed)
        self.token.returnPressed.connect(self.probe)

    @staticmethod
    def _button(text, tooltip):
        button = QtWidgets.QToolButton(text=text)
        button.setProperty('naiConnectionAction', True)
        button.setMinimumHeight(30)
        button.setToolTip(tooltip)
        button.setCursor(QtCore.Qt.PointingHandCursor)
        return button

    def set_position(self, index, count):
        self.label.setText(f'连接 {index + 1}' + (' · 默认' if index == 0 else ''))
        self.token.setAccessibleName(f'连接 {index + 1} 的 API Token')
        self.up.setEnabled(index > 0)
        self.down.setEnabled(index < count - 1)

    def _toggle_visible(self, visible):
        self.token.setEchoMode(QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password)
        self.visible.setText('隐藏' if visible else '显示')

    def _token_changed(self, unused):
        self._revision += 1
        self._account_data = None
        self._account_updated_at = None
        self.account_strip.render(status='unconfigured')
        self.status.setText('Token 已更改，请查询此账户。')
        self.tokenChanged.emit(self)

    def probe(self):
        if not self._closed:
            self.queryRequested.emit(self)

    def query_token(self):
        from .client import _token
        token = self.token.text().strip()
        if not token:
            return ''
        if any(c.isspace() for c in token):
            raise ValueError('Token 内容不应包含空格或换行。')
        return _token(token)

    def set_waiting(self):
        self._queued = True
        self.check.setEnabled(False)
        self.status.setText('等待查询账户…')
        self.account_strip.render(self._account_data, status='loading', busy=True,
                                  updated_at=self._account_updated_at)

    def prepare_probe(self, token, parent):
        from .client import account
        self._queued = False
        self.check.setEnabled(False)
        self.status.setText('正在查询订阅与可用额度…')
        self.account_strip.render(self._account_data, status='loading', busy=True,
                                  updated_at=self._account_updated_at)
        # The dialog owns the Job: removing this row must not free an occupied
        # request slot while its blocking HTTP request is still running.
        job = self.job = Job(lambda unused: account(token, timeout=15), parent)
        job._probe_revision = self._revision
        job._probe_token = token
        job.succeeded.connect(self._account_loaded)
        job.failed.connect(self._account_failed)
        job.finished.connect(self._probe_finished)
        return job

    def _valid_reply(self):
        job = self.sender()
        return (not self._closed and job is self.job and job is not None
                and job._probe_revision == self._revision
                and job._probe_token == self.token.text().strip())

    def _account_loaded(self, data):
        if not self._valid_reply():
            return
        import time
        parsed = self._account_data = parse_account(data)
        self._account_updated_at = time.time()
        self.account_strip.render(parsed, status='ready', updated_at=self._account_updated_at)
        self.account_strip.show()
        self.status.setText(f'订阅：{parsed["tier_name"]} · {subscription_state(parsed)}')

    def _account_failed(self, error):
        if not self._valid_reply():
            return
        message = str(error).replace(self.job._probe_token, '[Token 已隐藏]')
        self.status.setText(message)
        self.account_strip.render(self._account_data, status='error', updated_at=self._account_updated_at, error=message)
        self.account_strip.show()

    def _probe_finished(self):
        job = self.sender()
        if job is self.job:
            self.job = None
            if not self._closed:
                self.check.setEnabled(not self._queued)

    def close_queries(self):
        self._closed = True
        self._revision += 1
        self.visible.setChecked(False)
        if self.job is not None:
            self.job.cancel()


class AccountDialog(QtWidgets.QDialog):
    MAX_KEYS = 64
    MAX_ACCOUNT_QUERIES = 2

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self._closed = False
        self._initial_query_started = False
        self._pending_queries = []
        self._query_jobs = set()
        self._query_timer = QtCore.QTimer(self)
        self._query_timer.setSingleShot(True)
        self._query_timer.timeout.connect(self._drain_queries)
        self._initial_query_timer = QtCore.QTimer(self)
        self._initial_query_timer.setSingleShot(True)
        self._initial_query_timer.timeout.connect(self.refresh_all)
        self._rows = []
        self._scroll_target = None
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._show_target_row)
        self._mode = getattr(owner, '_theme_mode', 'dark')
        self.setWindowTitle('NovelAI 连接设置')
        self.setObjectName('novelaiAccountDialog')
        self.setMinimumSize(470, 390)
        self.resize(660, 590)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(20, 18, 20, 16)
        box.setSpacing(12)
        title = QtWidgets.QLabel('连接 NovelAI')
        title.setObjectName('naiConnectionTitle')
        box.addWidget(title)
        hint = QtWidgets.QLabel('添加账户的 Persistent API Token。第一项为默认连接，可用箭头调整顺序。')
        hint.setObjectName('naiConnectionHint')
        hint.setWordWrap(True)
        box.addWidget(hint)
        toolbar = QtWidgets.QHBoxLayout()
        self.count = QtWidgets.QLabel()
        self.count.setObjectName('naiConnectionHint')
        self.count.setWordWrap(True)
        self.count.setToolTip('空行忽略，重复 Token 在保存时合并。')
        toolbar.addWidget(self.count, 1)
        self.refresh_all_button = QtWidgets.QPushButton('刷新全部')
        self.refresh_all_button.setObjectName('naiRefreshAccounts')
        self.refresh_all_button.setProperty('naiConnectionAction', True)
        self.refresh_all_button.setAutoDefault(False)
        self.refresh_all_button.setToolTip('逐个读取所有已填写账户的订阅和额度，最多同时查询 2 个。')
        self.refresh_all_button.clicked.connect(self.refresh_all)
        toolbar.addWidget(self.refresh_all_button)
        self.add_button = QtWidgets.QPushButton('＋ 添加连接')
        self.add_button.setProperty('naiConnectionAction', True)
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(lambda: self.add_row())
        toolbar.addWidget(self.add_button)
        box.addLayout(toolbar)
        self.query_status = QtWidgets.QLabel('打开后自动查询已填写的账户 · 最多同时查询 2 个')
        self.query_status.setObjectName('naiConnectionHint')
        self.query_status.setWordWrap(True)
        box.addWidget(self.query_status)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setObjectName('naiConnectionsScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.rows_widget = QtWidgets.QWidget()
        self.rows_widget.setObjectName('naiConnectionRows')
        self.rows_layout = QtWidgets.QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 5, 0)
        self.rows_layout.setSpacing(10)
        self.rows_layout.setAlignment(QtCore.Qt.AlignTop)
        self.scroll.setWidget(self.rows_widget)
        box.addWidget(self.scroll, 1)
        self.status = QtWidgets.QLabel('密钥仅保存在本机 apikeys.json，不写入绘图参数和导出文件。')
        self.status.setObjectName('naiConnectionStatus')
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.PlainText)
        box.addWidget(self.status)
        links = QtWidgets.QHBoxLayout()
        for text, url in [('打开官网 ↗', 'https://novelai.net/'), ('令牌获取说明 ↗', 'https://docs.novelai.net/en/text/usersettings/account/')]:
            button = QtWidgets.QPushButton(text)
            button.setProperty('naiConnectionLink', True)
            button.setAutoDefault(False)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            button.clicked.connect(lambda unused=False, link=url: QtGui.QDesktopServices.openUrl(QtCore.QUrl(link)))
            links.addWidget(button)
        links.addStretch()
        box.addLayout(links)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        self.save_button = buttons.button(buttons.Save)
        self.save_button.setText('保存连接')
        self.save_button.setObjectName('naiSaveConnection')
        buttons.button(buttons.Cancel).setText('取消')
        for button in buttons.buttons():
            button.setProperty('naiConnectionAction', True)
            button.setMinimumSize(88, 36)
            button.setCursor(QtCore.Qt.PointingHandCursor)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        for value in tokens_for(owner) or ['']:
            self.add_row(value, existing=True)
        self.apply_theme(self._mode)

    def add_row(self, value='', *, existing=False):
        if not existing and len(self._rows) >= self.MAX_KEYS:
            self.status.setText(f'最多可添加 {self.MAX_KEYS} 个连接；已有连接不会被截断。')
            return None
        row = _AccountTokenRow(value, self._mode, self.rows_widget)
        row.removeRequested.connect(self.remove_row)
        row.moveRequested.connect(self.move_row)
        row.queryRequested.connect(self.request_query)
        row.tokenChanged.connect(self._invalidate_query)
        self._rows.append(row)
        self.rows_layout.addWidget(row)
        self._update_positions()
        if not existing:
            row.token.setFocus()
            self._scroll_target = row
            self._scroll_timer.start(20)
        return row

    def remove_row(self, row):
        if row not in self._rows:
            return
        row.close_queries()
        self._invalidate_query(row)
        self._rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._update_positions()
        if not self._rows:
            self.status.setText('所有连接已移除；点击保存后生效。')

    def move_row(self, row, delta):
        if row not in self._rows:
            return
        index = self._rows.index(row)
        target = index + delta
        if not 0 <= target < len(self._rows):
            return
        self._rows.pop(index)
        self._rows.insert(target, row)
        self.rows_layout.removeWidget(row)
        self.rows_layout.insertWidget(target, row)
        self._update_positions()
        self._scroll_target = row
        self._scroll_timer.start(20)

    def _show_target_row(self):
        # Allow native Qt to finish the new card and scroll-area layouts first.
        row, self._scroll_target = self._scroll_target, None
        if row in self._rows:
            self.scroll.ensureWidgetVisible(row)

    def _update_positions(self):
        for index, row in enumerate(self._rows):
            row.set_position(index, len(self._rows))
        self.count.setText(f'{len(self._rows)} 个连接')
        self.add_button.setEnabled(len(self._rows) < self.MAX_KEYS)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closed and not self._initial_query_started:
            self._initial_query_started = True
            self._initial_query_timer.start(0)

    def _invalidate_query(self, row):
        self._pending_queries = [entry for entry in self._pending_queries if entry[0] is not row]
        row._queued = False
        row.check.setEnabled(not row._closed and row.job is None)
        self._update_query_status()

    def request_query(self, row):
        if self._closed or row not in self._rows or row._closed:
            return
        try:
            token = row.query_token()
        except ValueError as error:
            row.status.setText(str(error))
            row.account_strip.render(status='error', error=str(error))
            return
        if not token:
            row.status.setText('填写 Token 后可查询账户。')
            row.account_strip.render(status='unconfigured')
            return
        if (row.job is not None and row.job._probe_revision == row._revision
                and row.job._probe_token == token):
            return
        entry = (row, row._revision, token)
        if entry in self._pending_queries:
            return
        self._pending_queries = [value for value in self._pending_queries if value[0] is not row]
        self._pending_queries.append(entry)
        row.set_waiting()
        self._update_query_status()
        self._query_timer.start(0)

    def refresh_all(self):
        if self._closed:
            return
        # A manual click before the first show timer fires must not query twice.
        self._initial_query_timer.stop()
        self._initial_query_started = True
        for row in self._rows:
            self.request_query(row)
        self._update_query_status()

    def _query_finished(self):
        job = self.sender()
        if job in self._query_jobs:
            job._account_query_finished = True
            self._query_timer.start(0)

    def _drain_queries(self):
        awaiting_exit = False
        for job in tuple(self._query_jobs):
            if not getattr(job, '_account_query_finished', False):
                continue
            if job.thread is not None and job.thread.is_alive():
                awaiting_exit = True
                continue
            self._query_jobs.remove(job)
            job.deleteLater()
        if self._closed:
            if awaiting_exit:
                self._query_timer.start(10)
            return
        while len(self._query_jobs) < self.MAX_ACCOUNT_QUERIES and self._pending_queries:
            # An edited row can have an old request finishing. Other rows may
            # use spare capacity without starting two requests for that row.
            offset = next((i for i, entry in enumerate(self._pending_queries)
                           if entry[0] not in self._rows or entry[0].job is None), None)
            if offset is None:
                break
            row, revision, token = self._pending_queries.pop(offset)
            if (row not in self._rows or row._closed or row._revision != revision
                    or row.token.text().strip() != token):
                continue
            job = row.prepare_probe(token, self)
            self._query_jobs.add(job)
            job.finished.connect(self._query_finished)
            job.start()
        self._update_query_status()
        if awaiting_exit:
            self._query_timer.start(10)

    def _update_query_status(self):
        if self._closed:
            return
        active, waiting = len(self._query_jobs), len(self._pending_queries)
        if active or waiting:
            self.query_status.setText(f'正在查询 {active} 个 · 等待 {waiting} 个 · 最多同时查询 2 个')
        else:
            self.query_status.setText('账户额度分别显示；可刷新全部或单独查询。')

    def save(self):
        from aetherloom_core.api_manager_ui import persist_credentials
        from aetherloom_core.paths import current_dir
        values = []
        for index, row in enumerate(self._rows):
            value = row.token.text().strip()
            if any(c.isspace() for c in value):
                self.status.setText(f'连接 {index + 1} 的 Token 包含空格或换行，请检查复制内容。')
                row.token.setFocus()
                self.scroll.ensureWidgetVisible(row)
                return
            if value and value not in values:
                values.append(value)
        path = getattr(self.owner, '_apikeys_file', None) or os.path.join(current_dir, 'apikeys.json')
        try:
            updates = {'novelai': {'api_key': values[0], 'api_keys': values}} if values else {}
            merged = persist_credentials(path, updates, {'novelai'})
        except (OSError, ValueError) as error:
            message = str(error)
            for value in values:
                message = message.replace(value, '[Token 已隐藏]')
            self.status.setText('连接保存失败：' + message)
            return
        self.owner._apikeys = merged
        self.accept()

    def done(self, result):
        self._closed = True
        self._initial_query_timer.stop()
        self._query_timer.stop()
        self._pending_queries.clear()
        for job in self._query_jobs:
            job.cancel()
        self._scroll_timer.stop()
        self._scroll_target = None
        for row in self._rows:
            row.close_queries()
        super().done(result)

    def apply_theme(self, mode):
        from .styles import workspace_palette as palette
        self._mode = mode
        p = palette(mode)
        self.setStyleSheet(f"""
            QDialog#novelaiAccountDialog {{background:{p['canvas']};color:{p['text']};}}
            QDialog#novelaiAccountDialog QLabel {{color:{p['text']};background:transparent;border:none;}}
            QDialog#novelaiAccountDialog QLabel#naiConnectionTitle {{font-size:20px;font-weight:700;}}
            QDialog#novelaiAccountDialog QLabel#naiConnectionHint,
            QDialog#novelaiAccountDialog QLabel#naiConnectionStatus {{color:{p['muted']};font-size:12px;}}
            QDialog#novelaiAccountDialog QScrollArea#naiConnectionsScroll,
            QDialog#novelaiAccountDialog QWidget#naiConnectionRows {{background:transparent;border:none;}}
            QDialog#novelaiAccountDialog QFrame#naiTokenCard {{background:{p['surface']};border:1px solid {p['border']};border-radius:9px;}}
            QDialog#novelaiAccountDialog QLabel#naiTokenLabel {{font-size:12px;font-weight:600;}}
            QDialog#novelaiAccountDialog QLineEdit#naiConnectionToken {{background:{p['input']};color:{p['text']};border:1px solid {p['border']};border-radius:6px;padding:6px 9px;font-size:13px;selection-background-color:{p['accent']};selection-color:{'#242333' if mode != 'light' else '#ffffff'};}}
            QDialog#novelaiAccountDialog QLineEdit#naiConnectionToken:focus {{border-color:{p['accent']};}}
            QDialog#novelaiAccountDialog QPushButton[naiConnectionAction="true"],
            QDialog#novelaiAccountDialog QToolButton[naiConnectionAction="true"] {{background:{p['surface']};color:{p['text']};border:1px solid {p['border']};border-radius:6px;padding:5px 9px;font-size:12px;}}
            QDialog#novelaiAccountDialog QPushButton[naiConnectionAction="true"]:hover,
            QDialog#novelaiAccountDialog QToolButton[naiConnectionAction="true"]:hover {{background:{p['hover']};border-color:{p['muted']};}}
            QDialog#novelaiAccountDialog QPushButton[naiConnectionAction="true"]:focus,
            QDialog#novelaiAccountDialog QToolButton[naiConnectionAction="true"]:focus {{border-color:{p['accent']};}}
            QDialog#novelaiAccountDialog QToolButton:checked,
            QDialog#novelaiAccountDialog QToolButton#naiQueryAccount {{background:{p['accent_soft']};color:{p['accent']};}}
            QDialog#novelaiAccountDialog QPushButton#naiSaveConnection {{background:{p['accent']};color:{'#242333' if mode != 'light' else '#ffffff'};border-color:{p['accent']};font-weight:600;}}
            QDialog#novelaiAccountDialog QPushButton[naiConnectionAction="true"]:disabled,
            QDialog#novelaiAccountDialog QToolButton[naiConnectionAction="true"]:disabled {{background:{p['input']};color:{p['muted']};border-color:{p['border']};}}
            QDialog#novelaiAccountDialog QPushButton[naiConnectionLink="true"] {{background:transparent;color:{p['muted']};border:none;padding:3px 0;font-size:12px;text-align:left;}}
            QDialog#novelaiAccountDialog QPushButton[naiConnectionLink="true"]:hover {{color:{p['accent']};}}
            QDialog#novelaiAccountDialog QScrollBar:vertical {{background:transparent;width:10px;margin:2px;}}
            QDialog#novelaiAccountDialog QScrollBar::handle:vertical {{background:{p['border']};min-height:28px;border-radius:4px;}}
            QDialog#novelaiAccountDialog QScrollBar::add-line:vertical,
            QDialog#novelaiAccountDialog QScrollBar::sub-line:vertical {{height:0;}}
            QDialog#novelaiAccountDialog QScrollBar::add-page:vertical,
            QDialog#novelaiAccountDialog QScrollBar::sub-page:vertical {{background:transparent;}}
        """)
        for row in self._rows:
            row.account_strip.apply_theme(mode)


class TagsDialog(QtWidgets.QDialog):
    def __init__(self, token, model, parent=None, *, dataset_mode='anime'):
        super().__init__(parent)
        self.token, self.model, self.job, self.selected_tag = token, model, None, ''
        self.dataset_mode = dataset_mode
        self.setWindowTitle('NovelAI 标签建议')
        self.resize(480, 430)
        box = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.query = QtWidgets.QLineEdit()
        self.query.setPlaceholderText('输入标签开头，例如 blue hair')
        self.search = QtWidgets.QPushButton('查询')
        self.search.clicked.connect(self.lookup)
        self.query.returnPressed.connect(self.lookup)
        row.addWidget(self.query, 1)
        row.addWidget(self.search)
        box.addLayout(row)
        self.results = QtWidgets.QListWidget()
        self.results.itemDoubleClicked.connect(self.choose)
        box.addWidget(self.results, 1)
        self.status = QtWidgets.QLabel('双击插入选定标签。')
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.PlainText)
        box.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(buttons.Ok).setText('插入标签')
        buttons.accepted.connect(lambda: self.choose(self.results.currentItem()))
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def lookup(self):
        query = self.query.text().strip()
        if not query or self.job is not None:
            return
        from .client import suggest_tags
        self.search.setEnabled(False)
        self.status.setText('正在读取官方建议…')
        token, model, mode = self.token, self.model, self.dataset_mode
        job = self.job = Job(lambda unused: suggest_tags(token, model, query, timeout=12,
                                                        dataset_mode=mode), self)
        job.succeeded.connect(self.loaded)
        job.failed.connect(lambda error: self.status.setText(str(error)))
        job.finished.connect(self.finished_lookup)
        job.start()

    def loaded(self, tags):
        self.results.clear()
        for tag in tags[:100]:
            text = tag.get('tag', '') if isinstance(tag, dict) else str(tag)
            if text:
                item = QtWidgets.QListWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, text)
                self.results.addItem(item)
        self.status.setText(f'{self.results.count()} 个建议 · 双击插入')

    def finished_lookup(self):
        job, self.job = self.job, None
        self.search.setEnabled(True)
        if job is not None:
            job.deleteLater()

    def choose(self, item):
        if item is not None:
            self.selected_tag = item.data(QtCore.Qt.UserRole)
            self.accept()

    def done(self, result):
        if self.job is not None:
            self.job.cancel()
        super().done(result)


class ChunksDialog(QtWidgets.QDialog):
    def __init__(self, chunks, parent=None):
        super().__init__(parent)
        self.setWindowTitle('本地提示词块')
        self.resize(670, 430)
        self.chunks = copy.deepcopy(chunks)
        box = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel('用 !macro:名称! 引用提示词块，支持嵌套；本地保存，可导入或导出 JSON。')
        tip.setWordWrap(True)
        box.addWidget(tip)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(['名称', '提示词内容'])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        box.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        for label, handler in [('添加', self.add), ('删除', self.remove), ('导入', self.import_file), ('导出', self.export_file)]:
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(handler)
            row.addWidget(button)
        row.addStretch()
        box.addLayout(row)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.populate()

    def populate(self):
        self.table.setRowCount(0)
        for name, text in self.chunks.items():
            self.add(name, text)

    def add(self, name='', text=''):
        if self.table.rowCount() >= 500:
            return
        i = self.table.rowCount()
        self.table.insertRow(i)
        self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(name if isinstance(name, str) else ''))
        self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(text))

    def remove(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def values(self):
        result = {}
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text().strip()
            text = self.table.item(row, 1).text()
            if not name and not text:
                continue
            if not name or '!' in name or len(name) > 100 or name in result or len(text) > 30000:
                raise ValueError('名称必须非空且不重复，不含 !，最多 100 字；内容最多 30000 字。')
            result[name] = text
        return result

    def save(self):
        try:
            self.chunks = self.values()
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, '无法保存', str(error))
            return
        self.accept()

    def import_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '导入提示词块', '', 'JSON (*.json)')
        if not path:
            return
        data = storage.read_json(path, None)
        if not isinstance(data, dict) or len(data) > 500 or any(not isinstance(v, str) for v in data.values()):
            QtWidgets.QMessageBox.warning(self, '导入失败', '需要“名称: 内容”的 JSON 对象，最多 500 项。')
            return
        self.chunks = data
        self.populate()

    def export_file(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出提示词块', 'NovelAI-chunks.json', 'JSON (*.json)')
        if path:
            try:
                storage.atomic_json(path, self.values())
            except (OSError, ValueError) as error:
                QtWidgets.QMessageBox.warning(self, '导出失败', str(error))
