"""Responsive API cards and immutable background model probes."""

import copy
import json
import os
import threading
import tempfile
import time

from PyQt5 import QtCore, QtWidgets



def persist_credentials(path, store, managed_keys):
    """Replace managed records atomically while preserving unrelated credentials."""
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            merged = json.load(stream)
    except FileNotFoundError:
        merged = {}
    if not isinstance(merged, dict):
        raise ValueError('Credential file must contain an object')
    for key in managed_keys:
        merged.pop(key, None)
    merged.update(store)
    descriptor, temporary = tempfile.mkstemp(prefix='.apikeys-', suffix='.tmp', dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(merged, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return merged


class CollapsibleApiCard(QtWidgets.QFrame):
    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        self.setObjectName('apiModelCard')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 16)
        layout.setSpacing(8)
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setObjectName('apiCardToggle')
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(QtCore.Qt.RightArrow)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setMinimumHeight(38)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.toggle.setCursor(QtCore.Qt.PointingHandCursor)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(self.toggle, 1)
        self.state_badge = QtWidgets.QLabel('未测试')
        self.state_badge.setObjectName('apiStateBadge')
        self.state_badge.setAlignment(QtCore.Qt.AlignCenter)
        header.addWidget(self.state_badge)
        layout.addLayout(header)
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('apiMuted')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.PlainText)
        layout.addWidget(self.summary)
        self.body = QtWidgets.QWidget()
        self.body_layout = QtWidgets.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 10, 0, 0)
        self.body_layout.setSpacing(14)
        hint = QtWidgets.QLabel(subtitle)
        hint.setObjectName('apiMuted')
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)
        layout.addWidget(self.body)
        self.body.setVisible(False)
        self.toggle.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self.body.setVisible(checked)
        self.toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        self.setProperty('expanded', checked)
        self.style().unpolish(self)
        self.style().polish(self)


class ApiProbeController(QtCore.QObject):
    completed = QtCore.pyqtSignal(int, object)

    def __init__(self, window, category, fields, card, *, backend=None):
        super().__init__(card)
        self.window, self.category, self.fields, self.card = window, category, fields, card
        self.backend = backend
        self._generation = 0
        self._busy = False
        self._closed = False
        self._request = None
        self._action = None
        self.test_button = QtWidgets.QPushButton('测试响应')
        self.test_button.setObjectName('apiPrimaryButton')
        self.refresh_button = QtWidgets.QPushButton('刷新模型')
        self.refresh_button.setObjectName('apiSecondaryButton')
        for button in (self.test_button, self.refresh_button):
            button.setCursor(QtCore.Qt.PointingHandCursor)
            button.setMinimumHeight(36)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.test_button)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        card.body_layout.addLayout(row)
        self.status_label = QtWidgets.QLabel('尚未测试')
        self.status_label.setObjectName('apiProbeStatus')
        self.status_label.setTextFormat(QtCore.Qt.PlainText)
        self.status_label.setWordWrap(True)
        card.body_layout.addWidget(self.status_label)
        self.test_button.clicked.connect(lambda: self.start('test'))
        self.refresh_button.clicked.connect(lambda: self.start('models'))
        self.completed.connect(self._finished, QtCore.Qt.QueuedConnection)
        for name in ('provider', 'endpoint', 'api_key', 'model', 'timeout', 'baidu_appid', 'baidu_secret'):
            widget = fields.get(name)
            if widget is None:
                continue
            signal = (widget.currentTextChanged if isinstance(widget, QtWidgets.QComboBox)
                      else widget.valueChanged if isinstance(widget, QtWidgets.QSpinBox) else widget.textChanged)
            signal.connect(self._configuration_changed)
        self._update_summary()

    def snapshot(self):
        fields = self.fields
        return {'category': self.category, 'provider': fields['provider'].currentData() or 'custom',
                'endpoint': fields['endpoint'].text().strip(), 'api_key': fields['api_key'].text().strip(),
                'model': fields['model'].currentText().strip() if fields.get('model') is not None else '',
                'timeout': int(fields['timeout'].value()),
                'appid': fields['baidu_appid'].text().strip() if fields.get('baidu_appid') is not None else '',
                'secret': fields['baidu_secret'].text().strip() if fields.get('baidu_secret') is not None else ''}

    def _update_summary(self):
        provider = self.fields['provider'].currentText()
        model = self.fields['model'].currentText().strip() if self.fields.get('model') is not None else ''
        no_model = ('标准翻译' if self.fields['provider'].currentData() in ('baidu_translate', 'google_translate')
                    else '尚未选择模型')
        self.card.summary.setText(provider + ' · ' + (model or no_model))

    def _set_status(self, message, state='idle'):
        self.status_label.setText(message)
        self.status_label.setProperty('state', state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        badge = self.card.state_badge
        badge.setText({'idle': '未测试', 'busy': '请求中', 'error': '请求失败',
                       'success': '模型已更新' if self._action == 'models' else '连接正常'}.get(state, '未测试'))
        badge.setProperty('state', state)
        badge.style().unpolish(badge)
        badge.style().polish(badge)

    def _configuration_changed(self, *_args):
        self._generation += 1
        self._update_summary()
        self._set_status('配置已更改，当前请求结束后可重新测试' if self._busy else '尚未测试')

    def start(self, action):
        if self._busy or self._closed or getattr(self.window, '_closing', False):
            return
        snapshot = copy.deepcopy(self.snapshot())
        self._generation += 1
        token = self._generation
        self._request, self._action = snapshot, action
        self._busy = True
        self.test_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self._set_status('正在请求模型响应…' if action == 'test' else '正在刷新可用模型…', 'busy')
        backend = self.backend
        emit = self.completed.emit

        def worker():
            started = time.perf_counter()
            try:
                if backend is None:
                    from aetherloom_core import api_model_probe as module
                    fn = module.test_response if action == 'test' else module.fetch_models
                else:
                    fn = backend.test_response if action == 'test' else backend.fetch_models
                result = fn(snapshot)
                if not isinstance(result, dict):
                    raise TypeError('Probe result must be a dictionary')
                result = dict(result)
            except Exception as exc:
                result = {'ok': False, 'message': f'请求未完成（{type(exc).__name__}），请检查地址、密钥或网络连接。'}
            if not isinstance(result.get('elapsed_ms'), (int, float)):
                result['elapsed_ms'] = round((time.perf_counter() - started) * 1000)
            # Backend messages are already safe; prevent accidental raw fixture/key
            # echoes from ever appearing in a label as an additional boundary.
            message = str(result.get('message') or ('请求成功' if result.get('ok') else '请求失败'))[:1200]
            for name in ('api_key', 'appid', 'secret'):
                secret = str(snapshot.get(name) or '')
                if secret:
                    message = message.replace(secret, '[已隐藏]')
            result['message'] = message
            try:
                emit(token, result)
            except RuntimeError:
                pass  # The page was destroyed while its bounded request finished.

        threading.Thread(target=worker, name=f'api-{action}-{self.category}', daemon=True).start()

    @QtCore.pyqtSlot(int, object)
    def _finished(self, token, result):
        self._busy = False
        if self._closed or getattr(self.window, '_closing', False):
            return
        self.test_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        if token != self._generation or self.snapshot() != self._request:
            self._set_status('配置已更改，已忽略此前结果；请重新测试')
            return
        ok = bool(result.get('ok'))
        if ok and self._action == 'models':
            combo = self.fields.get('model')
            models = result.get('models')
            if combo is not None and isinstance(models, list):
                selected = combo.currentText()
                blocked = QtCore.QSignalBlocker(combo)
                combo.clear()
                combo.addItems(list(dict.fromkeys(model for model in models if isinstance(model, str) and model.strip())))
                combo.setEditText(selected)
                blocked.unblock()
                self._update_summary()
        elapsed = max(0.0, float(result.get('elapsed_ms', 0)))
        timing = f'{elapsed:.0f} ms' if elapsed < 1000 else f'{elapsed / 1000:.2f} s'
        code = result.get('status_code')
        suffix = f' · HTTP {code}' if code is not None else ''
        self._set_status(f"{result['message']} · {timing}{suffix}", 'success' if ok else 'error')

    def close(self):
        self._closed = True
        self._generation += 1
        self._request = None


def close_probes(window):
    for controller in getattr(window, '_api_probe_controllers', {}).values():
        controller.close()


def apply_theme(window, mode):
    page = getattr(window, 'api_page', None)
    if page is None:
        return
    from aetherloom_core.ui.preferences import stylesheet
    page.setStyleSheet(stylesheet('api_page_root', mode))
    for fields in getattr(window, 'api_config_fields', {}).values():
        stack = fields.get('api_key_stack')
        if stack is not None and stack.currentWidget() is not None:
            stack.setFixedHeight(stack.currentWidget().sizeHint().height())
