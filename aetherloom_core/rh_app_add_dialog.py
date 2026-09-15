"""Compact application import form; installation stays with the RH workspace."""
from pathlib import Path
from urllib.parse import urlsplit

from PyQt5 import QtCore, QtWidgets

from .rh_app_reference import application_reference
from .rh_connections import ensure_connections
from .rh_ui import palette


def _label(text, name='rhAddMuted'):
    widget = QtWidgets.QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(QtCore.Qt.PlainText)
    widget.setWordWrap(True)
    widget.setMinimumWidth(0)
    return widget


class _CountInput(QtWidgets.QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class AddAppDialog(QtWidgets.QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.connections = ensure_connections(owner)
        self.setObjectName('rhAddAppDialog')
        self.setWindowTitle('添加应用')
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.setMinimumSize(360, 360)
        self.resize(580, 460)
        self._theme_source = self.stylesheet
        self.setStyleSheet(self.stylesheet())

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)
        heading = QtWidgets.QVBoxLayout()
        heading.setSpacing(6)
        heading.addWidget(_label('添加应用', 'rhAddHeading'))
        heading.addWidget(_label('将 RunningHub 应用添加到你的工作区'))
        root.addLayout(heading)

        self.tabs = QtWidgets.QTabBar()
        self.tabs.setObjectName('rhAddModes')
        self.tabs.setExpanding(True)
        self.tabs.setDrawBase(False)
        self.tabs.addTab('链接 / 编号')
        self.tabs.addTab('按作者添加')
        root.addWidget(self.tabs)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName('rhAddForm')
        body.addWidget(self.pages)

        link_page = QtWidgets.QWidget()
        link_layout = QtWidgets.QVBoxLayout(link_page)
        link_layout.setContentsMargins(18, 18, 18, 18)
        link_layout.setSpacing(10)
        self.edit = self._input('粘贴应用链接或 App 编号')
        self.edit.setToolTip('支持中英文路径及分享参数；纯编号使用连接设置中当前选定的站点，完整链接使用链接自身站点。')
        title = _label('应用链接或编号', 'rhAddField')
        title.setBuddy(self.edit)
        link_layout.addWidget(title)
        link_layout.addLayout(self._input_row(self.edit))
        link_layout.addWidget(_label('支持官网分享链接、中英文网址，也可直接输入 App 编号。'))
        link_layout.addStretch(1)
        self.pages.addWidget(link_page)

        author_page = QtWidgets.QWidget()
        author_layout = QtWidgets.QVBoxLayout(author_page)
        author_layout.setContentsMargins(18, 18, 18, 18)
        author_layout.setSpacing(10)
        self.author_uid = self._input('输入作者 UID')
        self.author_uid.setToolTip('作者的数字 UID，例如 1911823721911500801')
        title = _label('作者 UID', 'rhAddField')
        title.setBuddy(self.author_uid)
        author_layout.addWidget(title)
        author_layout.addLayout(self._input_row(self.author_uid))
        count_row = QtWidgets.QHBoxLayout()
        count_row.setSpacing(12)
        count_row.addWidget(_label('添加数量上限', 'rhAddField'))
        count_row.addStretch(1)
        self.author_limit = _CountInput()
        self.author_limit.setRange(1, 200)
        self.author_limit.setValue(15)
        self.author_limit.setSuffix(' 个')
        self.author_limit.setFixedWidth(116)
        self.author_limit.setMinimumHeight(36)
        self.author_limit.setAccessibleName('添加应用数量上限')
        count_row.addWidget(self.author_limit)
        author_layout.addLayout(count_row)
        author_layout.addWidget(_label('从当前站点批量获取该作者的应用，最多 200 个。'))
        author_layout.addStretch(1)
        self.pages.addWidget(author_page)

        self.destination = _label('', 'rhAddDestination')
        body.addWidget(self.destination)
        self.message = _label('', 'rhAddError')
        self.message.hide()
        root.addWidget(self.message)
        body.addStretch(1)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        self.cancel = QtWidgets.QPushButton('取消')
        self.cancel.setAutoDefault(False)
        self.cancel.clicked.connect(self.reject)
        self.ok = QtWidgets.QPushButton('添加应用')
        self.ok.setObjectName('rhAddPrimary')
        self.ok.setDefault(True)
        for button in (self.cancel, self.ok):
            button.setMinimumSize(92, 36)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            actions.addWidget(button)
        root.addLayout(actions)

        self.tabs.currentChanged.connect(self._change_mode)
        self.edit.textChanged.connect(self._refresh)
        self.author_uid.textChanged.connect(self._refresh)
        self.connections.changed.connect(self._refresh)
        self._refresh()
        self.edit.setFocus()

    @property
    def by_author(self):
        return self.tabs.currentIndex() == 1

    @staticmethod
    def _input(placeholder):
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumWidth(0)
        edit.setMinimumHeight(38)
        edit.setClearButtonEnabled(True)
        edit.setAccessibleName(placeholder)
        return edit

    def _input_row(self, edit):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(edit, 1)
        paste = QtWidgets.QPushButton('粘贴')
        paste.setAutoDefault(False)
        paste.setMinimumHeight(38)
        paste.setCursor(QtCore.Qt.PointingHandCursor)
        paste.setAccessibleName('粘贴' + edit.placeholderText().replace('粘贴', ''))

        def paste_text():
            value = QtWidgets.QApplication.clipboard().text().strip()
            if value:
                edit.setText(value)
                edit.setCursorPosition(len(value))
            edit.setFocus()

        paste.clicked.connect(paste_text)
        row.addWidget(paste)
        return row

    def _change_mode(self, index):
        self.pages.setCurrentIndex(index)
        self.ok.setText('批量添加' if self.by_author else '添加应用')
        self._refresh()
        (self.author_uid if self.by_author else self.edit).setFocus()

    def _refresh(self, *_):
        self.message.hide()
        value = (self.author_uid if self.by_author else self.edit).text().strip()
        self.ok.setEnabled(bool(value))
        base = self.connections.host
        app_id = ''
        if not self.by_author and value:
            try:
                reference = application_reference(value, default_base=base)
                base, app_id = reference['base_url'], reference['webapp_id']
            except ValueError:
                self.destination.setText('输入有效链接或编号后，将显示对应站点。')
                return
        site = '国际站' if urlsplit(base).hostname == 'www.runninghub.ai' else '中文站'
        self.destination.setText('导入站点：' + site + ' · ' + urlsplit(base).netloc.removeprefix('www.')
                                 + ('\nApp 编号：' + app_id if app_id else ''))

    def show_error(self, message):
        self.message.setText(message)
        self.message.show()
        (self.author_uid if self.by_author else self.edit).setFocus()

    def stylesheet(self):
        mode = getattr(self.owner, '_theme_mode', 'dark')
        p = palette(mode)
        icons = Path(__file__).resolve().parents[1] / 'icons'
        up = (icons / f'ui-chevron-up-{mode}.svg').as_posix()
        down = (icons / f'ui-chevron-down-{mode}.svg').as_posix()
        return f'''
            QDialog#rhAddAppDialog {{ background: {p['canvas']}; }}
            QDialog#rhAddAppDialog QWidget {{ color: {p['text']}; font-size: 13px; background: transparent; }}
            QDialog#rhAddAppDialog QLabel {{ border: none; padding: 0; }}
            QDialog#rhAddAppDialog QLabel#rhAddHeading {{ font-size: 22px; font-weight: 600; }}
            QDialog#rhAddAppDialog QLabel#rhAddField {{ font-weight: 600; }}
            QDialog#rhAddAppDialog QLabel#rhAddMuted,
            QDialog#rhAddAppDialog QLabel#rhAddDestination {{ color: {p['muted']}; font-size: 12px; }}
            QDialog#rhAddAppDialog QLabel#rhAddError {{ color: {p['danger']}; font-size: 12px; }}
            QDialog#rhAddAppDialog QTabBar::tab {{ color: {p['muted']}; background: {p['surface']};
                border: 1px solid {p['border']}; border-radius: 7px; padding: 10px 12px; margin: 0 2px; }}
            QDialog#rhAddAppDialog QTabBar::tab:selected {{ background: {p['accent_soft']};
                color: {p['accent']}; border-color: {p['accent']}; }}
            QDialog#rhAddAppDialog QTabBar::tab:hover {{ color: {p['text']}; }}
            QDialog#rhAddAppDialog QStackedWidget#rhAddForm {{ background: {p['surface']};
                border: 1px solid {p['border']}; border-radius: 12px; }}
            QDialog#rhAddAppDialog QLineEdit, QDialog#rhAddAppDialog QSpinBox {{
                background: {p['input']}; border: 1px solid {p['border']}; border-radius: 7px;
                padding: 6px 9px; selection-background-color: {p['accent']}; selection-color: white; }}
            QDialog#rhAddAppDialog QLineEdit:focus, QDialog#rhAddAppDialog QSpinBox:focus {{ border-color: {p['accent']}; }}
            QDialog#rhAddAppDialog QSpinBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
            QDialog#rhAddAppDialog QSpinBox {{ padding-right: 30px; }}
            QDialog#rhAddAppDialog QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
                width: 26px; border: none; border-left: 1px solid {p['border']}; border-top-right-radius: 7px; }}
            QDialog#rhAddAppDialog QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
                width: 26px; border: none; border-left: 1px solid {p['border']}; border-bottom-right-radius: 7px; }}
            QDialog#rhAddAppDialog QSpinBox::up-button:hover,
            QDialog#rhAddAppDialog QSpinBox::down-button:hover {{ background: {p['hover']}; }}
            QDialog#rhAddAppDialog QSpinBox::up-arrow {{ image: url("{up}"); width: 12px; height: 12px; }}
            QDialog#rhAddAppDialog QSpinBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
            QDialog#rhAddAppDialog QPushButton {{ background: {p['surface']}; border: 1px solid {p['border']};
                border-radius: 7px; padding: 5px 14px; font-weight: 500; }}
            QDialog#rhAddAppDialog QPushButton:hover {{ background: {p['hover']}; border-color: {p['muted']}; }}
            QDialog#rhAddAppDialog QPushButton:focus {{ border-color: {p['accent']}; }}
            QDialog#rhAddAppDialog QPushButton#rhAddPrimary {{ background: {p['accent']}; color: white; border-color: {p['accent']}; }}
            QDialog#rhAddAppDialog QPushButton#rhAddPrimary:hover {{ background: {p['accent']}; border-color: {p['text']}; }}
            QDialog#rhAddAppDialog QPushButton#rhAddPrimary:disabled {{ background: {p['surface']}; color: {p['muted']}; border-color: {p['border']}; }}
        '''
