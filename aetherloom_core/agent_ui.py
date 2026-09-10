"""GUI-only subscription account controls; network work uses cancellable workers."""
import threading

from PyQt5 import QtCore, QtGui, QtWidgets
from api_calls.provider_client import ProviderAPIError
from . import agent_auth
from .agent_catalog import AGENTS, credential_ref
from . import agent_search


class AgentAccounts(QtCore.QObject):
    announced = QtCore.pyqtSignal(int, str, str)
    completed = QtCore.pyqtSignal(int, object, str)

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.panels = {}
        self.search_controls = {}
        self.attempt = None
        self.serial = 0
        self.announced.connect(self._announced, QtCore.Qt.QueuedConnection)
        self.completed.connect(self._completed, QtCore.Qt.QueuedConnection)
        # A daemon must never write credentials after the GUI has closed.
        self.cancel = threading.Event()
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.shutdown)
        cancel = self.cancel
        self.destroyed.connect(lambda *_: cancel.set())
        for category, fields in editor.owner.api_config_fields.items():
            panel = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0)
            status = QtWidgets.QLabel(); status.setObjectName('apiMuted'); status.setWordWrap(True)
            status.setTextFormat(QtCore.Qt.PlainText)
            layout.addWidget(status)
            row = QtWidgets.QHBoxLayout()
            for text, callback in [('浏览器登录', self.login), ('退出此账户', self.logout)]:
                button = QtWidgets.QPushButton(text); button.setObjectName('apiSecondaryButton')
                button.clicked.connect(lambda _, c=category, fn=callback: fn(c)); row.addWidget(button)
            row.addStretch(); layout.addLayout(row)
            if category in ('llm', 'vision'):
                search = QtWidgets.QCheckBox('联网搜索 · 按需检索网页')
                search.setToolTip('默认开启，由 Agent 判断是否需要搜索。搜索由供应商执行，可能消耗额外额度；需账户和模型支持。')
                layout.addWidget(search)
                hint = QtWidgets.QLabel(''); hint.setObjectName('apiMuted'); hint.setWordWrap(True)
                layout.addWidget(hint)
                self.search_controls[category] = (search, hint)
                fields['agent_web_search'] = search
                search.toggled.connect(lambda checked, c=category: self.search_changed(c, checked))
            fields['form'].insertRow(1, panel)
            self.panels[category] = (panel, status)

    def shutdown(self):
        self.cancel.set()
        if self.attempt: self.attempt['cancel'].set()

    def refresh(self, category):
        panel, label = self.panels[category]
        provider = self.editor.protocol(category)
        panel.setVisible(provider in AGENTS)
        if category in self.search_controls:
            checkbox, hint = self.search_controls[category]
            profile = self.editor.owner._get_api_provider_profile(category, self.editor.identity(category))
            with QtCore.QSignalBlocker(checkbox):
                checkbox.setChecked(agent_search.enabled(provider, profile))
            checkbox.setEnabled(provider in agent_search.PROVIDERS)
            hint.setText('测试响应会要求实际搜索，并区分“已搜索”和“未调用搜索”；结果保留来源链接。'
                         if provider in agent_search.PROVIDERS else '此 Agent 当前接口尚未接入联网搜索。')
        if provider not in AGENTS: return
        try:
            session = agent_auth.saved_session(provider, credential_ref(self.editor.identity(category)))
            if session: agent_auth.validate_session(provider, session)
            label.setText(('已登录 · ' + str(session.get('account') or session.get('emailAddress') or '订阅账户')
                           if session else '未登录 · 使用订阅账户授权，无需 API Key') +
                          ('\n选择负责调用图像工具的 LLM，无需填写图像模型名称。登录后刷新列表；测试会实际发起一次 Agent 请求，消耗 LLM 与图像工具额度。'
                           if category in ('text2img', 'image_edit') else '\n登录后点击“刷新模型”，再测试所选模型的响应。'))
        except ProviderAPIError as error:
            label.setText(str(error))

    def search_changed(self, category, checked):
        provider, identity = self.editor.protocol(category), self.editor.identity(category)
        if provider not in agent_search.PROVIDERS:
            return
        profile = self.editor.owner._get_api_provider_profile(category, identity)
        profile['web_search'] = bool(checked)
        self.editor.owner._set_api_provider_profile(category, identity, profile)
        self.editor.settings_changed()
        self.editor.owner._api_probe_controllers[category]._configuration_changed()

    def changed(self):
        for category in self.panels:
            self.refresh(category)
            self.editor.owner._api_probe_controllers[category]._configuration_changed()

    def logout(self, category):
        try:
            agent_auth.logout(self.editor.protocol(category), credential_ref(self.editor.identity(category)))
            if self.attempt and self.attempt['identity'] == self.editor.identity(category):
                self.attempt['cancel'].set(); self.attempt['dialog'].reject()
            self.changed()
        except (OSError, ProviderAPIError):
            self.panels[category][1].setText('无法删除此账户授权，请检查本地文件权限。')

    def login(self, category):
        if self.attempt:
            self.attempt['dialog'].raise_(); return
        provider, identity = self.editor.protocol(category), self.editor.identity(category)
        if provider not in AGENTS: return
        self.serial += 1
        token = self.serial
        dialog = QtWidgets.QDialog(self.editor.owner); dialog.setWindowTitle('登录 ' + AGENTS[provider]['name'])
        dialog.setModal(True); dialog.resize(540, 250)
        dialog.setStyleSheet(self.editor.owner.api_page.styleSheet().replace('#api_page_root', ''))
        layout = QtWidgets.QVBoxLayout(dialog); layout.setContentsMargins(22, 20, 22, 20); layout.setSpacing(14)
        message = QtWidgets.QLabel('正在准备安全登录…'); message.setWordWrap(True); layout.addWidget(message)
        url = QtWidgets.QLineEdit(); url.setReadOnly(True); url.setPlaceholderText('授权链接准备中'); layout.addWidget(url)
        code = QtWidgets.QLineEdit(); code.setReadOnly(True); code.setPlaceholderText('设备码（仅 GitHub 登录需要）'); code.hide(); layout.addWidget(code)
        row = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton('打开默认浏览器'); open_button.setEnabled(False)
        open_button.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(url.text())))
        copy_button = QtWidgets.QPushButton('复制链接')
        copy_button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(url.text()))
        close = QtWidgets.QPushButton('取消'); close.clicked.connect(dialog.reject)
        for button in (open_button, copy_button, close): row.addWidget(button)
        layout.addLayout(row)
        cancel = threading.Event()
        self.attempt = dict(token=token, category=category, identity=identity, provider=provider, dialog=dialog,
                            cancel=cancel, message=message, url=url, code=code, open=open_button, close=close)
        dialog.finished.connect(lambda _: self._closed(token))
        emit, announce, shutting_down = self.completed.emit, self.announced.emit, self.cancel
        def worker():
            result, error = None, ''
            try:
                result = agent_auth.login(provider, cancel, lambda u, c: announce(token, u, c))
            except Exception as exc:
                error = str(exc) if isinstance(exc, ProviderAPIError) else '登录未完成，请检查网络后重新登录。'
            if cancel.is_set() or shutting_down.is_set(): return
            try: emit(token, result, error)
            except RuntimeError: pass
        threading.Thread(target=worker, name='agent-oauth', daemon=True).start()
        dialog.show()

    def _closed(self, token):
        if self.attempt and self.attempt['token'] == token:
            self.attempt['cancel'].set()
            self.attempt['dialog'].deleteLater(); self.attempt = None

    def _announced(self, token, url, code):
        if not self.attempt or self.attempt['token'] != token or self.cancel.is_set(): return
        a = self.attempt; a['url'].setText(url); a['code'].setText(code); a['code'].setVisible(bool(code)); a['open'].setEnabled(True)
        a['message'].setText('请在默认浏览器登录目标账户并完成授权，然后回到此窗口。' +
                             (' 请在网页输入下方设备码。' if code else ''))
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _completed(self, token, session, error):
        if not self.attempt or self.attempt['token'] != token or self.cancel.is_set(): return
        a = self.attempt
        if a['cancel'].is_set(): return
        if session:
            try:
                agent_auth.save_session(a['provider'], credential_ref(a['identity']), session)
                self.changed(); a['dialog'].accept(); return
            except Exception:
                error = '授权返回成功，但无法保存本地账户，请检查文件权限。'
        a['message'].setText(error); a['close'].setText('关闭')
