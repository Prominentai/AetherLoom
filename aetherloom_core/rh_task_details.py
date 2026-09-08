"""On-demand, redacted views of one task's frozen request and actual POST."""
import copy
import json

from PyQt5 import QtCore, QtWidgets, sip


_SECRET_KEYS = frozenset({'apikey', 'apikeys', 'password', 'passwd', 'pwd', 'secret',
                          'authorization', 'cookie', 'accesstoken', 'refreshtoken',
                          'token', 'sessionid', 'passcode', 'acceptedapikey', 'credential', 'credentials',
                          '密码', '口令', '密钥', '访问令牌'})


def _key(value):
    return ''.join(character for character in str(value).lower() if character.isalnum())


def _is_secret(value):
    key = _key(value)
    return key in _SECRET_KEYS or key.endswith(('password', 'apikey', 'apikeys', 'accesstoken', 'refreshtoken', 'secret'))


def redacted(value):
    """Also protect legacy in-memory snapshots that predate public task documents."""
    if isinstance(value, dict):
        secret_field = _is_secret(value.get('fieldName', ''))
        return {key: ('[已隐藏]' if _is_secret(key) or secret_field and key == 'fieldValue'
                      else redacted(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redacted(item) for item in value]
    return copy.deepcopy(value)


def task_view(record, document=None):
    """Do not reconstruct a supposed POST from the pre-upload input snapshot."""
    document = document if isinstance(document, dict) else {}
    snapshot = document.get('request') or record.get('snapshot') or {}
    request = {key: snapshot[key] for key in (
        'webapp_id', 'app_name', 'base_url', 'nodes', 'input_dir', 'output_dir',
        'decode_settings', 'retry_max', 'retry_delay', 'retry_concurrency',
    ) if key in snapshot}
    if isinstance(document.get('decode_settings'), dict):
        request['decode_settings'] = document['decode_settings']
    post = document.get('post') or {}
    phase = str(post.get('phase') or 'pending')
    phases = {'pending': '尚未生成实际请求', 'submitting': '已开始提交，等待响应',
              'accepted': '服务端已接受', 'rejected': '本次提交未被接受',
              'unknown': '提交结果未知'}
    if phase == 'pending':
        post_view = {'说明': '尚未生成实际 POST；发起参数中的本地文件尚可能需要上传。'}
    else:
        post_view = {key: post[key] for key in (
            'phase', 'attempt', 'endpoint', 'body', 'credential_ref', 'response_code',
        ) if key in post}
    association = {
        'run_id': record.get('run_id'), 'task_id': record.get('task_id'),
        'task_document': record.get('task_document'),
        'origin': document.get('origin') or record.get('origin') or {},
        'queue': document.get('queue') or {},
        'state': document.get('state') or {'status': record.get('status')},
        'results': document.get('results') or record.get('results') or [],
    }
    return redacted({'request': request, 'post': post_view, 'association': association,
                     'phase_label': phases.get(phase, '已记录实际请求')})


class TaskDetailsDialog(QtWidgets.QDialog):
    def __init__(self, owner, service, run_id):
        super().__init__(owner)
        self.service, self.run_id = service, run_id
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setWindowTitle('本次任务参数')
        self.setMinimumSize(390, 360)
        self.resize(720, 650)
        layout = QtWidgets.QVBoxLayout(self)
        self.heading = QtWidgets.QLabel()
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        hint = QtWidgets.QLabel('参数来自发起本次任务时保存的配置。修改 App 设置不会改变它。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.recovery = QtWidgets.QGroupBox('补齐本次任务的解码信息')
        self.recovery_form = QtWidgets.QFormLayout(self.recovery)
        self._recovery_kind = None
        self.recovery.hide()
        layout.addWidget(self.recovery)
        self.tabs = QtWidgets.QTabWidget()
        self.editors = {}
        for key, title in (('request', '发起参数'), ('post', '实际 POST'), ('association', '关联与结果')):
            editor = QtWidgets.QPlainTextEdit()
            editor.setReadOnly(True)
            editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            self.editors[key] = editor
            self.tabs.addTab(editor, title)
        layout.addWidget(self.tabs, 1)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QtWidgets.QHBoxLayout()
        refresh = QtWidgets.QPushButton('刷新任务记录')
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        copy_button = QtWidgets.QPushButton('复制当前页')
        copy_button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.tabs.currentWidget().toPlainText()))
        buttons.addWidget(copy_button)
        buttons.addStretch(1)
        close = QtWidgets.QPushButton('关闭')
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def _refresh_recovery(self, record):
        decode = (record.get('snapshot') or {}).get('decode_settings') or {}
        waiting = record.get('status') == 'WAITING_FOR_SECRET' and not record.get('cancel_requested')
        kind = ('configuration' if decode.get('settings_missing') else 'password') if waiting else None
        self.recovery.setVisible(bool(kind))
        if kind == self._recovery_kind:
            return
        self._recovery_kind = kind
        while self.recovery_form.count():
            child = self.recovery_form.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()
        if kind is None:
            return
        self.recovery_password = QtWidgets.QLineEdit()
        self.recovery_password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.recovery_password.setPlaceholderText('只用于本次任务，不修改 App 设置')
        if kind == 'configuration':
            from aetherloom_core.rh_parameters import RhEnumComboBox, RhNumberSpinBox
            self.recovery_enabled = QtWidgets.QCheckBox('启用本次任务的本地解码')
            self.recovery_enabled.setChecked(bool(decode.get('enabled', True)))
            self.recovery_form.addRow(self.recovery_enabled)
            self.recovery_mode = RhEnumComboBox()
            self.recovery_mode.addItem('请选择本次任务的解码方式', None)
            self.recovery_mode.addItem('GRC', 'grc')
            self.recovery_mode.addItem('SST', 'sst')
            if decode.get('mode') in ('grc', 'sst'):
                self.recovery_mode.setCurrentIndex(self.recovery_mode.findData(decode['mode']))
            self.recovery_form.addRow('解码方式', self.recovery_mode)
            self.recovery_grid = RhNumberSpinBox(integer=True)
            self.recovery_grid.configure({'min': 4, 'max': 256, 'step': 1})
            self.recovery_grid.setValue(decode.get('grid_cols', 32))
            self.recovery_form.addRow('网格列数', self.recovery_grid)
            self.recovery_delete = QtWidgets.QCheckBox('解码成功后删除原图')
            self.recovery_delete.setChecked(bool(decode.get('delete_original', False)))
            self.recovery_form.addRow(self.recovery_delete)

            def enabled_fields():
                enabled = self.recovery_enabled.isChecked()
                self.recovery_mode.setEnabled(enabled)
                self.recovery_grid.setEnabled(enabled and self.recovery_mode.currentData() == 'grc')
                self.recovery_password.setEnabled(enabled and self.recovery_mode.currentData() == 'sst')
                self.recovery_delete.setEnabled(enabled)

            self.recovery_mode.currentIndexChanged.connect(enabled_fields)
            self.recovery_enabled.toggled.connect(enabled_fields)
            enabled_fields()
        self.recovery_form.addRow('解码密码', self.recovery_password)
        self.recovery_button = QtWidgets.QPushButton('仅用于本任务并继续本地解码')
        self.recovery_button.clicked.connect(self._provide_recovery)
        self.recovery_form.addRow(self.recovery_button)

    def _provide_recovery(self):
        record = self.service.get(self.run_id) or {}
        if record.get('status') != 'WAITING_FOR_SECRET' or record.get('cancel_requested'):
            self.refresh()
            return
        try:
            if self._recovery_kind == 'configuration':
                enabled = self.recovery_enabled.isChecked()
                mode = self.recovery_mode.currentData()
                if enabled and mode not in ('grc', 'sst'):
                    self.message.setText('请明确选择本次任务的解码方式。')
                    return
                if enabled and mode == 'grc' and not self.recovery_grid.commit():
                    return
                self.service.provide_decode_settings(self.run_id, {
                    'enabled': enabled, 'mode': mode or 'grc',
                    'grid_cols': int(self.recovery_grid.value()),
                    'password': self.recovery_password.text() if enabled and mode == 'sst' else '',
                    'delete_original': self.recovery_delete.isChecked(),
                })
            else:
                if not self.recovery_password.text():
                    self.message.setText('请输入本次任务缺失的解码密码。')
                    return
                self.service.provide_decode_password(self.run_id, self.recovery_password.text())
            self.recovery_password.clear()
            self.refresh()
            self.message.setText('已补齐本次任务信息，将处理已生成的结果；不会重新提交生成请求。')
        except (OSError, ValueError, RuntimeError):
            self.message.setText('任务信息暂未保存成功，请检查本地任务记录后重试。')

    def refresh(self):
        record = self.service.get(self.run_id) or {'run_id': self.run_id}
        document = None
        repository = getattr(self.service, 'task_documents', None) or getattr(self.service, 'documents', None)
        try:
            if repository is not None:
                document = repository.get('applications', self.run_id)
            self.message.setText('' if document is not None else '此任务没有独立参数记录，显示本次会话保存的发起参数。')
        except (OSError, ValueError, RuntimeError):
            # Keep the fallback useful without exposing paths/credentials from
            # an arbitrary exception string in an imported document reader.
            self.message.setText('任务记录暂时无法读取，显示本次会话保存的发起参数。')
        view = task_view(record, document)
        self.heading.setText(f"{record.get('app_name') or record.get('webapp_id') or '任务'} · {view['phase_label']}\n"
                             f"Task ID：{record.get('task_id') or '尚未返回'}")
        for key, editor in self.editors.items():
            editor.setPlainText(json.dumps(view[key], ensure_ascii=False, indent=2))
        self._refresh_recovery(record)


def show_task_details(owner, service, run_id):
    existing = getattr(owner, '_rh_task_details_dialog', None)
    if existing is not None and not sip.isdeleted(existing):
        if existing.run_id == run_id:
            existing.refresh()
            existing.show()
            existing.raise_()
            return existing
        existing.close()
    dialog = TaskDetailsDialog(owner, service, run_id)
    owner._rh_task_details_dialog = dialog
    dialog.show()
    return dialog
