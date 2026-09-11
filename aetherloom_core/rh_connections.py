"""One owner-scoped RunningHub connection model shared by every page.

Only apikeys.json contains key rings. Execution callers must capture snapshot()
on the GUI thread; workers never access this QObject or its input controls.
"""
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from .api_manager_ui import credential_events, persist_credentials, _rh_keys
from .rh_tasks import normalize_base_url


SITES = (('https://www.runninghub.cn', 'runninghub_cn', '中文站 · runninghub.cn'),
         ('https://www.runninghub.ai', 'runninghub_ai', '国际站 · runninghub.ai'))


def _site(value):
    base = normalize_base_url(value)
    hostname = urlsplit(base).hostname or ''
    for origin, name, unused in SITES:
        suffix = urlsplit(origin).hostname.removeprefix('www.')
        if hostname in (suffix, 'www.' + suffix, 'api.' + suffix):
            return origin
    return base


def _record_name(host):
    site = _site(host)
    return next((name for origin, name, unused in SITES if origin == site), None)


class RhConnectionSettings(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)

    def __init__(self, owner, path=None):
        super().__init__(owner if isinstance(owner, QtCore.QObject) else None)
        self.owner = owner
        if path is None:
            path = getattr(owner, '_apikeys_file', None)
        if not path:
            from .paths import current_dir
            path = Path(current_dir) / 'apikeys.json'
        self.path = Path(path).resolve()
        self._store = {}
        self._host = SITES[0][0]
        self._rings = {host: [] for host, unused, unused_label in SITES}
        self._bindings = []
        self._bound_widgets = set()
        self._pending_primary = {}
        self._flushing = False
        try:
            self.reload()
        except (OSError, ValueError, TypeError):
            fallback = getattr(owner, '_apikeys', {})
            self._apply_store(fallback if isinstance(fallback, dict) else {})
            QtCore.QTimer.singleShot(0, lambda: self.error.emit(
                '无法读取 apikeys.json，已保留原文件。请检查文件内容和读取权限。'))
        credential_events().saved.connect(self._credentials_saved)

    @property
    def host(self):
        return self._host

    def keys_for(self, host=None):
        return list(self._rings.get(_site(host or self._host), []))

    api_keys = keys_for

    def site_keyrings(self):
        return copy.deepcopy(self._rings)

    def snapshot(self, host=None):
        self.flush_pending()
        base = _site(host or self._host)
        keys = self.keys_for(base)
        return {'base_url': base, 'api_key': keys[0] if keys else '',
                'api_keys': keys, 'site_keyrings': self.site_keyrings()}

    def reload(self):
        try:
            with self.path.open('r', encoding='utf8') as stream:
                store = json.load(stream)
        except FileNotFoundError:
            store = copy.deepcopy(getattr(self.owner, '_apikeys', {}) or {})
        if not isinstance(store, dict):
            raise ValueError('apikeys.json 必须是 JSON 对象')
        self._apply_store(store)

    def _apply_store(self, store):
        legacy_host = (getattr(self.owner, 'settings', {}) or {}).get('runninghub_host')
        host = _site(store.get('runninghub_host') or legacy_host or self._host)
        if not _record_name(host):
            host = SITES[0][0]
        rings = {origin: _rh_keys(store.get(name)) for origin, name, unused in SITES}
        changed = (host != self._host or rings != self._rings)
        self._host, self._rings = host, rings
        self._store = copy.deepcopy(store)
        for origin, name, unused in SITES:
            entry = self._store.get(name)
            entry = copy.deepcopy(entry) if isinstance(entry, dict) else {}
            entry.update(api_key=rings[origin][0] if rings[origin] else '', api_keys=list(rings[origin]))
            self._store[name] = entry
        self._sync_owner()
        if changed:
            self.changed.emit()

    def _sync_owner(self):
        # Preserve edits to unrelated provider keys that have not been saved yet.
        current = getattr(self.owner, '_apikeys', None)
        if not isinstance(current, dict):
            current = {}
            self.owner._apikeys = current
        for unused, name, unused_label in SITES:
            current[name] = copy.deepcopy(self._store[name])
        current['runninghub_host'] = self._host
        settings = getattr(self.owner, 'settings', None)
        if isinstance(settings, dict):
            settings['runninghub_host'] = urlsplit(self._host).netloc
        self._sync_controls()

    @QtCore.pyqtSlot(str, object)
    def _credentials_saved(self, path, store):
        if os.path.normcase(str(self.path)) == path:
            self._apply_store(store)

    def _commit(self, updates):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        merged = persist_credentials(str(self.path), updates, set(updates))
        # Also works when a caller deliberately blocks credential notifications.
        if self._store != merged:
            self._apply_store(merged)

    def set_host(self, host):
        host = _site(host)
        if not _record_name(host):
            raise ValueError('请选择 RunningHub 中文站或国际站')
        if host != self._host:
            self.flush_pending()
            self._commit({'runninghub_host': host})

    def set_keys(self, keys, host=None):
        host = _site(host or self._host)
        name = _record_name(host)
        if not name:
            raise ValueError('未知的 RunningHub 站点')
        if not isinstance(keys, (list, tuple)) or any(not isinstance(value, str) for value in keys):
            raise ValueError('API Keys 必须是文本列表')
        keys = _rh_keys({'api_keys': list(keys)})
        if keys == self.keys_for(host):
            self._sync_owner()
            return
        existing = self._store.get(name, {})
        entry = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        entry.update(api_key=keys[0] if keys else '', api_keys=keys)
        self._commit({name: entry, 'runninghub_host': self._host})

    def set_primary_key(self, value, host=None):
        host = _site(host or self._host)
        value = str(value or '').strip()
        self.set_keys(([value] if value else []) + self.keys_for(host)[1:], host)

    def _guard(self, operation):
        try:
            operation()
        except (OSError, ValueError, TypeError):
            # Do not put credential values or arbitrary exception text in UI/logs.
            self.error.emit('连接设置保存失败，请检查 apikeys.json 是否可写。')
            return False
        return True

    def _primary_edited(self, widget, host, text):
        self._pending_primary[id(widget)] = (widget, host or self._host, text)

    def _finish_primary(self, widget, host):
        pending = self._pending_primary.pop(id(widget), None)
        selected_host = pending[1] if pending else (host or self._host)
        value = pending[2] if pending else widget.text()
        try:
            self.set_primary_key(value, selected_host)
        except (OSError, ValueError, TypeError):
            if pending:
                self._pending_primary[id(widget)] = pending
            raise

    def flush_pending(self):
        """Capture unfocused/shortcut edits before taking an execution snapshot."""
        if self._flushing:
            return
        self._flushing = True
        try:
            for widget, host, unused in list(self._pending_primary.values()):
                if not sip.isdeleted(widget):
                    self._finish_primary(widget, host)
                else:
                    self._pending_primary.pop(id(widget), None)
        finally:
            self._flushing = False

    def bind_controls(self, host_combo=None, key_input=None):
        if host_combo is not None and id(host_combo) not in self._bound_widgets:
            self._bound_widgets.add(id(host_combo))
            self._bindings.append(('host', host_combo, None))
            host_combo.currentTextChanged.connect(self._legacy_host_changed)
        if key_input is not None and id(key_input) not in self._bound_widgets:
            self._bound_widgets.add(id(key_input))
            self._bindings.append(('key', key_input, None))
            key_input.setEchoMode(QtWidgets.QLineEdit.Password)
            key_input.textEdited.connect(
                lambda text: self._primary_edited(key_input, None, text))
            key_input.editingFinished.connect(
                lambda: self._guard(lambda: self._finish_primary(key_input, None)))
        # API manager's existing first-key editor edits the same ring. Its old
        # in-memory save callback runs first; this callback restores the full ring.
        for origin, name, unused in SITES:
            entry = (getattr(self.owner, 'apikey_rows', {}) or {}).get(name, {})
            widget = entry.get('key_edit')
            if widget is not None and id(widget) not in self._bound_widgets:
                self._bound_widgets.add(id(widget))
                self._bindings.append(('key', widget, origin))
                widget.textEdited.connect(
                    lambda text, edit=widget, host=origin: self._primary_edited(edit, host, text))
                widget.editingFinished.connect(
                    lambda edit=widget, host=origin: self._guard(
                        lambda: self._finish_primary(edit, host)))
        self._sync_controls()

    def _legacy_host_changed(self, text):
        if not self._guard(lambda: self.set_host(text)):
            self._sync_controls()

    def _sync_controls(self):
        for kind, widget, host in self._bindings:
            if sip.isdeleted(widget):
                continue
            if kind == 'key' and id(widget) in self._pending_primary:
                continue
            blocker = QtCore.QSignalBlocker(widget)
            if kind == 'host':
                index = next((index for index in range(widget.count())
                              if _site(widget.itemText(index)) == self._host), -1)
                if index < 0:
                    widget.addItem(urlsplit(self._host).netloc)
                    index = widget.count() - 1
                widget.setCurrentIndex(index)
            else:
                keys = self.keys_for(host)
                widget.setText(keys[0] if keys else '')
            del blocker


def ensure_connections(owner):
    settings = getattr(owner, '_rh_connection_settings', None)
    if settings is None:
        settings = RhConnectionSettings(owner)
        owner._rh_connection_settings = settings
    return settings


def install_connection_settings(owner, host_combo=None, key_input=None):
    settings = ensure_connections(owner)
    settings.bind_controls(host_combo if host_combo is not None else getattr(owner, 'rh_host_combo', None),
                           key_input if key_input is not None else getattr(owner, 'rh_apikey_input', None))
    return settings


install_legacy_controls = install_connection_settings


class _HostCombo(QtWidgets.QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class SiteSwitchButton(QtWidgets.QPushButton):
    """A view of the shared site selection, never a separate per-page setting."""
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.settings = ensure_connections(owner)
        self.owner = owner
        self.setObjectName('rhSecondaryButton')
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAutoDefault(False)
        self.setMinimumHeight(34)
        self.clicked.connect(self.switch_site)
        self.settings.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        cn = self.settings.host == SITES[0][0]
        label, other = ('中文站', '国际站') if cn else ('国际站', '中文站')
        self.setText(label + ' ⇄')
        self.setAccessibleName('当前' + label + '，切换到' + other)
        self.setToolTip(f'当前：{self.settings.host}\n点击切换到{other}，使用该站点的 API Key。\n与连接设置同步；已发起任务保持原站点。')

    def switch_site(self):
        target = SITES[1][0] if self.settings.host == SITES[0][0] else SITES[0][0]
        if not self.settings._guard(lambda:self.settings.set_host(target)):
            callback = getattr(self.owner, '_show_toast', None)
            if callback:callback('站点切换未保存，请检查连接设置。', 3500)
        self.refresh()


from .rh_connection_panel import RhConnectionPanel


def open_connection_settings(owner, parent=None):
    dialog = getattr(owner, '_rh_connection_dialog', None)
    if dialog is None or sip.isdeleted(dialog):
        dialog = QtWidgets.QDialog(owner if isinstance(owner, QtWidgets.QWidget) else parent)
        dialog.setWindowTitle('RunningHub 连接设置')
        dialog.setModal(False)
        dialog.resize(620, 680)
        dialog.setMinimumSize(360, 360)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        panel = RhConnectionPanel(ensure_connections(owner), dialog)
        layout.addWidget(panel)
        dialog.panel = panel
        owner._rh_connection_dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


show_connection_dialog = open_connection_settings
