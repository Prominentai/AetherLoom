"""Account status shared by the workspace and its connection dialog."""
import copy
import hashlib
import time
from datetime import datetime

from PyQt5 import QtCore, QtGui, QtWidgets

from .styles import workspace_palette as palette
from . import client
from .jobs import Job
from .quota_model import parse_account, number, duration


class AccountMonitor(QtCore.QObject):
    """One GET in flight, short cache, and no stale responses after changing tokens."""
    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = None
        self.error = ''
        self.updated_at = None
        self.status = 'unconfigured'
        self._fingerprint = ''
        self._job = None
        self._pending = None
        self._last_attempt = 0.
        self._closed = False

    @property
    def busy(self):
        return self._job is not None

    def snapshot_for(self, token):
        """Return public account evidence only when it belongs to this exact key.

        A retained balance after a failed refresh must not authorize paid lanes.
        Freshness and expiry are checked again by the queue before dispatch.
        """
        token = str(token or '').strip()
        identity = hashlib.sha256(token.encode('utf8')).hexdigest() if token else ''
        if (not identity or identity != self._fingerprint or self.status != 'ready'
                or not isinstance(self.data, dict) or self.updated_at is None):
            return None
        return {'data': copy.deepcopy(self.data), 'updated_at': self.updated_at}

    def refresh(self, token, *, force=False):
        if self._closed:
            return
        token = str(token or '').strip()
        identity = hashlib.sha256(token.encode('utf8')).hexdigest() if token else ''
        if identity != self._fingerprint:
            self._fingerprint = identity
            self.data, self.updated_at = None, None
            self.error, self._last_attempt = '', 0.
            self._pending = None
            self.status = 'loading' if token else 'unconfigured'
            self.changed.emit()
        if not token:
            return
        if self._job is not None:
            if force or identity != getattr(self._job, '_identity', ''):
                self._pending = (token, True)
            return
        if not force and self._last_attempt and time.monotonic() - self._last_attempt < 60:
            return
        self._last_attempt = time.monotonic()
        self.status, self.error = 'loading', ''
        job = self._job = Job(lambda unused: client.account(token, timeout=15), self)
        job._identity = identity
        job.succeeded.connect(lambda raw: self._loaded(identity, raw))
        job.failed.connect(lambda error: self._failed(identity, error))
        job.finished.connect(self._finished)
        self.changed.emit()
        job.start()

    def _loaded(self, identity, raw):
        if self._closed or identity != self._fingerprint:
            return
        self.data = parse_account(raw)
        self.updated_at, self.error, self.status = time.time(), '', 'ready'
        self.changed.emit()

    def _failed(self, identity, error):
        if self._closed or identity != self._fingerprint:
            return
        self.error, self.status = str(error), 'error'
        self.changed.emit()

    def _finished(self):
        job, self._job = self._job, None
        if job is not None:
            job.deleteLater()
        pending, self._pending = self._pending, None
        if self._closed:
            return
        self.changed.emit()
        if pending:
            self.refresh(pending[0], force=pending[1])

    def shutdown(self):
        self._closed = True
        self._pending = None
        if self._job is not None:
            self._job.cancel()


def entitled(data, now=None):
    if not data or data.get('tier') is None:
        return None
    if data['tier'] <= 0:
        return False
    expiry = data.get('expires_at')
    if expiry is None:
        return None
    return expiry > (time.time() if now is None else now)


def subscription_state(data):
    available = entitled(data)
    if available is True:
        return '有效（未续订）' if data.get('active') is False else '有效'
    if available is False:
        return '未订阅或已到期'
    return '有效期未返回'


def quota_text(data):
    if data is None:
        return 'V5 免费额度 · —'
    if data.get('tier') != 3:
        return 'V5 免费额度 · ' + ('不适用' if data.get('tier') is not None else '未返回')
    if data.get('negative') is True:
        value = '0% · 已用尽'
    elif data.get('percent') is None:
        value = '未返回'
    else:
        value = f'{data["percent"]:g}%'
    available = entitled(data)
    if available is False:
        value += '（订阅未生效）'
    elif available is None:
        value += '（有效期未知）'
    return 'V5 免费额度 · ' + value


def details_text(data, updated_at=None, error=''):
    if data is None:
        return error or '尚未读取账户信息。请配置 Token 后刷新。'
    status = subscription_state(data)
    lines = [
        f'订阅：{data.get("tier_name", "未知")} · {status}',
        f'Anlas 总余额：{number(data.get("total"))}',
        f'订阅 Anlas：{number(data.get("fixed"))}　购买 Anlas：{number(data.get("purchased"))}',
        '', quota_text(data),
    ]
    if data.get('tier') == 3:
        if data.get('refill_seconds') is not None and data['refill_seconds'] > 0:
            lines.append('每恢复 1 个百分点：' + duration(data['refill_seconds']))
            if data.get('daily_refill') is not None:
                lines.append(f'按当前速率：约 {data["daily_refill"]:g} 个百分点 / 天')
        elif data.get('refill_seconds') == 0:
            lines.append('当前额度恢复已暂停 / 达到上限。')
        else:
            lines.append('额度恢复速率：接口未返回。')
        lines.extend([
            '',
            '此额度用于符合免费条件的 V5 生成，会随时间逐渐恢复；不是每天或每月固定重置。',
            'V4.5 不受这条额度限制，但仍需符合 Opus 免费条件。高分辨率、多图、图生图、参考图等可能消耗 Anlas，最终以服务端计费为准。',
            '恢复间隔不是下一整数百分比的倒计时；不据此估算剩余出图张数。',
        ])
    expiry = data.get('expires_at')
    if expiry is not None:
        try:
            lines.extend(['', '订阅到期：' + datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M')])
        except (ValueError, OverflowError, OSError):
            pass
    if updated_at:
        lines.append('最近查询：' + datetime.fromtimestamp(updated_at).strftime('%m-%d %H:%M:%S'))
    if error:
        lines.extend(['', '刷新失败，以上为上次查询结果：', str(error)])
    return '\n'.join(lines)


class QuotaProgressBar(QtWidgets.QProgressBar):
    """A compact quota meter; its label preserves values above the visual cap."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = 'dark'
        self.setRange(0, 1000)
        self.setValue(0)
        self.setFormat('—')
        self.setFixedHeight(22)
        self.setMinimumWidth(120)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setAccessibleName('V5 免费剩余额度')

    def apply_theme(self, mode):
        self._mode = mode
        self.update()

    def paintEvent(self, event):
        colors = palette(self._mode)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        radius = rect.height() / 2
        track = QtGui.QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QtGui.QColor(colors['input']))
        painter.setPen(QtGui.QPen(QtGui.QColor(colors['border']), 1))
        painter.drawPath(track)
        fill = QtGui.QPainterPath()
        fraction = max(0, min(self.value(), self.maximum())) / self.maximum()
        if fraction > 0:
            filled = QtCore.QRectF(rect)
            filled.setWidth(rect.width() * fraction)
            fill.addRoundedRect(filled, min(radius, filled.width() / 2), radius)
            gradient = QtGui.QLinearGradient(rect.topLeft(), rect.topRight())
            if self.isEnabled():
                gradient.setColorAt(0, QtGui.QColor(colors['accent']).darker(120))
                gradient.setColorAt(1, QtGui.QColor(colors['accent']))
            else:
                gradient.setColorAt(0, QtGui.QColor(colors['border']))
                gradient.setColorAt(1, QtGui.QColor(colors['muted']).darker(145))
            painter.fillPath(fill, gradient)
        if not self.isTextVisible():
            painter.end()
            return
        font = self.font()
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(colors['text'] if self.isEnabled() else colors['muted']))
        painter.drawText(rect, QtCore.Qt.AlignCenter, self.text())
        if fraction > 0:
            painter.setClipPath(fill)
            fill_text = colors['canvas'] if QtGui.QColor(colors['accent']).lightness() > 155 else '#ffffff'
            painter.setPen(QtGui.QColor(fill_text))
            painter.drawText(rect, QtCore.Qt.AlignCenter, self.text())
        painter.end()


class AccountStrip(QtWidgets.QFrame):
    refreshRequested = QtCore.pyqtSignal()

    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self.setObjectName('novelaiAccountStrip')
        self._data = None
        self._updated_at = None
        self._error = ''
        self._mode = 'dark'
        self._compact = compact
        self._sidebar_compact = False
        self._sidebar_header = None
        self._sidebar_quota_heading = None
        self.setMinimumWidth(0)
        self.box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight, self)
        self.box.setContentsMargins(14, 10, 14, 10)
        self.box.setSpacing(16)
        self.tier_box = QtWidgets.QWidget()
        self.tier_box.setMinimumWidth(112)
        tier_layout = QtWidgets.QVBoxLayout(self.tier_box)
        tier_layout.setContentsMargins(0, 0, 0, 0)
        tier_layout.setSpacing(3)
        self.tier = QtWidgets.QLabel('账户未连接')
        self.tier.setWordWrap(True)
        self.stamp = QtWidgets.QLabel('连接账户后自动读取')
        self.stamp.setObjectName('quotaMuted')
        self.stamp.setWordWrap(True)
        tier_layout.addWidget(self.tier)
        tier_layout.addWidget(self.stamp)
        self.box.addWidget(self.tier_box)
        self.quota_box = QtWidgets.QWidget()
        quota_layout = self.quota_layout = QtWidgets.QVBoxLayout(self.quota_box)
        quota_layout.setContentsMargins(0, 0, 0, 0)
        quota_layout.setSpacing(5)
        self.quota = QtWidgets.QLabel('V5 免费额度')
        self.quota.setObjectName('quotaHeading')
        self.quota.setWordWrap(True)
        self.bar = QuotaProgressBar()
        self.recovery = QtWidgets.QLabel('连接账户后读取剩余额度')
        self.recovery.setObjectName('quotaMuted')
        self.recovery.setWordWrap(True)
        quota_layout.addWidget(self.quota)
        quota_layout.addWidget(self.bar)
        quota_layout.addWidget(self.recovery)
        self.box.addWidget(self.quota_box, 2)
        self.balance_box = QtWidgets.QWidget()
        balance_layout = QtWidgets.QVBoxLayout(self.balance_box)
        balance_layout.setContentsMargins(0, 0, 0, 0)
        balance_layout.setSpacing(3)
        self.balance = QtWidgets.QLabel('Anlas · —')
        self.breakdown = QtWidgets.QLabel('订阅 — · 购买 —')
        self.breakdown.setObjectName('quotaMuted')
        self.breakdown.setWordWrap(True)
        balance_layout.addWidget(self.balance)
        balance_layout.addWidget(self.breakdown)
        self.box.addWidget(self.balance_box, 1)
        self.buttons = QtWidgets.QWidget()
        button_layout = QtWidgets.QHBoxLayout(self.buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(5)
        self.details = QtWidgets.QToolButton(text='详情')
        self.details.clicked.connect(self.open_details)
        button_layout.addWidget(self.details)
        self.refresh = QtWidgets.QToolButton(text='刷新')
        self.refresh.setToolTip('重新查询账户余额与 V5 免费额度；不会发起生成')
        self.refresh.clicked.connect(self.refreshRequested)
        button_layout.addWidget(self.refresh)
        self.box.addWidget(self.buttons)
        self.refresh.setVisible(not compact)
        self.apply_theme('dark')

    def set_sidebar_compact(self, enabled=True):
        """Compact workspace account strip; connection-row mode stays unchanged."""
        enabled = bool(enabled)
        if enabled == self._sidebar_compact:
            return
        self._sidebar_compact = enabled
        self.setProperty('naiSidebarAccount', enabled)
        if self._sidebar_header is None:
            self._sidebar_header = QtWidgets.QWidget(self)
            self._sidebar_header.setObjectName('naiAccountTopline')
            self._sidebar_header_layout = QtWidgets.QHBoxLayout(self._sidebar_header)
            self._sidebar_header_layout.setContentsMargins(0, 0, 0, 0)
            self._sidebar_header_layout.setSpacing(8)
            self._sidebar_quota_heading = QtWidgets.QWidget(self.quota_box)
            self._sidebar_heading_layout = QtWidgets.QHBoxLayout(self._sidebar_quota_heading)
            self._sidebar_heading_layout.setContentsMargins(0, 0, 0, 0)
            self._sidebar_heading_layout.setSpacing(7)
            self.quota_percent = QtWidgets.QLabel(self.bar.text())
            self.quota_percent.setObjectName('quotaPercent')
            self.quota_percent.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.quota_percent.setMinimumWidth(0)
        for widget in (self.tier_box, self.quota_box, self.balance_box, self.buttons, self._sidebar_header):
            self.box.removeWidget(widget)
        self.quota_layout.removeWidget(self.quota)
        self.quota_layout.removeWidget(self._sidebar_quota_heading)
        if enabled:
            self.box.setDirection(QtWidgets.QBoxLayout.TopToBottom)
            self.box.setContentsMargins(10, 8, 10, 9)
            self.box.setSpacing(5)
            self.tier_box.setMinimumWidth(0)
            self.balance_box.setMinimumWidth(0)
            self.tier.setWordWrap(False)
            self.tier.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            self.balance.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
            self._sidebar_header_layout.addWidget(self.tier_box, 1)
            self._sidebar_header_layout.addWidget(self.balance_box)
            self.box.addWidget(self._sidebar_header)
            self._sidebar_heading_layout.addWidget(self.quota)
            self._sidebar_heading_layout.addWidget(self.quota_percent)
            self._sidebar_heading_layout.addStretch(1)
            self._sidebar_heading_layout.addWidget(self.buttons)
            self.quota_layout.insertWidget(0, self._sidebar_quota_heading)
            self.box.addWidget(self.quota_box)
            self.bar.setFixedHeight(8)
            self.bar.setTextVisible(False)
            self.quota.setText('V5 免费额度')
            self.quota.setWordWrap(False)
            self.quota_percent.show()
        else:
            self._sidebar_header_layout.removeWidget(self.tier_box)
            self._sidebar_header_layout.removeWidget(self.balance_box)
            for widget in (self.quota, self.quota_percent, self.buttons):
                self._sidebar_heading_layout.removeWidget(widget)
            while self._sidebar_heading_layout.count():
                self._sidebar_heading_layout.takeAt(0)
            self.box.setContentsMargins(14, 10, 14, 10)
            self.tier_box.setMinimumWidth(112)
            self.tier.setWordWrap(True)
            self.tier.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
            self.quota.setWordWrap(True)
            self.quota_layout.insertWidget(0, self.quota)
            self.box.addWidget(self.tier_box)
            self.box.addWidget(self.quota_box, 2)
            self.box.addWidget(self.balance_box, 1)
            self.box.addWidget(self.buttons)
            self.bar.setFixedHeight(22)
            self.bar.setTextVisible(True)
            self.quota_percent.hide()
        self._sidebar_header.setVisible(enabled)
        self._sidebar_quota_heading.setVisible(enabled)
        self.stamp.setVisible(not enabled)
        self.breakdown.setVisible(not enabled)
        self.recovery.setVisible(not enabled)
        self.tier_box.setVisible(enabled or not self._compact)
        self.refresh.setVisible(enabled or not self._compact)
        self._adjust_layout()
        self.apply_theme(self._mode)
        self.updateGeometry()

    def render(self, data=None, *, status='unconfigured', busy=False, updated_at=None, error=''):
        self._data, self._updated_at, self._error = data, updated_at, str(error)
        active = entitled(data)
        tier = data.get('tier_name', '未知') if data else '账户未连接'
        if data and (active is not True or data.get('active') is False):
            tier += ' · ' + subscription_state(data)
        self.tier.setText(tier)
        if status == 'loading':
            stamp = '正在更新…'
        elif status == 'error':
            stamp = '刷新失败 · 显示缓存' if data else '额度读取失败'
        elif updated_at:
            stamp = '更新于 ' + datetime.fromtimestamp(updated_at).strftime('%H:%M:%S')
        else:
            stamp = '连接账户后自动读取'
        self.stamp.setText(stamp)
        self.stamp.setToolTip(str(error) if error else stamp)
        known = data is not None and data.get('tier') == 3 and (data.get('percent') is not None or data.get('negative') is True)
        self.bar.setEnabled(known and active is True and not data.get('negative'))
        if known:
            value = 0 if data.get('negative') else data.get('percent', 0)
            self.bar.setValue(round(min(100., max(0., value)) * 10))
            self.bar.setFormat(f'{value:g}%')
            seconds = data.get('refill_seconds')
            if seconds is not None and seconds > 0:
                recovery = '恢复 1% · ' + duration(seconds)
            elif seconds == 0:
                recovery = '额度恢复已暂停 / 达到上限'
            else:
                recovery = '恢复速度未返回'
            if active is False:
                recovery = '订阅未生效 · 当前额度不可用'
            elif active is None:
                recovery = '订阅有效期未知 · ' + recovery
            elif data.get('negative'):
                recovery = '已用尽 · ' + recovery
        else:
            self.bar.setValue(0)
            self.bar.setFormat('—')
            if data and data.get('tier') is not None and data.get('tier') != 3:
                self.bar.setFormat('不适用')
                recovery = 'Opus 专属额度'
            elif data:
                recovery = '接口未返回免费额度'
            elif status == 'loading':
                recovery = '正在读取剩余额度…'
            elif status == 'error':
                recovery = '读取失败 · 请刷新重试'
            else:
                recovery = '连接账户后读取剩余额度'
        self.recovery.setText(recovery)
        self.bar.setAccessibleDescription(quota_text(data) + '；' + recovery)
        if self._sidebar_quota_heading is not None:
            self.quota_percent.setText(self.bar.text())
        self.balance.setText('Anlas · ' + number(data.get('total') if data else None))
        self.breakdown.setText('订阅 ' + number(data.get('fixed') if data else None) + ' · 购买 ' + number(data.get('purchased') if data else None))
        tooltip = details_text(data, updated_at, error)
        for widget in (self.quota_box, self.balance_box):
            widget.setToolTip(tooltip)
        self.tier.setToolTip(tier + " · " + stamp)
        self.refresh.setEnabled(not busy)
        self.details.setEnabled(bool(data or error))

    def open_details(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('NovelAI 账户额度')
        dialog.resize(540, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        text = QtWidgets.QTextBrowser()
        text.setOpenExternalLinks(False)
        text.setPlainText(details_text(self._data, self._updated_at, self._error))
        layout.addWidget(text, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.button(buttons.Close).setText('关闭')
        official = buttons.addButton('官方额度说明', QtWidgets.QDialogButtonBox.HelpRole)
        official.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl('https://docs.novelai.net/en/faq/#opus-usage-limits')))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        p = palette(self._mode)
        dialog.setStyleSheet(f'QDialog {{background:{p["surface"]};color:{p["text"]};}} QTextBrowser {{background:{p["input"]};color:{p["text"]};border:1px solid {p["border"]};padding:10px;}}')
        dialog.exec_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_layout()

    def _adjust_layout(self):
        if self._sidebar_compact:
            self.box.setDirection(QtWidgets.QBoxLayout.TopToBottom)
            self.box.setSpacing(5)
            return
        narrow = self.width() < (460 if self._compact else 620)
        self.box.setDirection(QtWidgets.QBoxLayout.TopToBottom if narrow else QtWidgets.QBoxLayout.LeftToRight)
        self.box.setSpacing(8 if narrow else 16)
        if self._compact:
            self.tier_box.setVisible(False)

    def apply_theme(self, mode):
        self._mode = mode
        p = palette(mode)
        self.bar.apply_theme(mode)
        self.setStyleSheet(f"""
            QFrame#novelaiAccountStrip {{background:{p['surface']};border:1px solid {p['border']};border-radius:9px;}}
            QFrame#novelaiAccountStrip QLabel {{color:{p['text']};border:none;background:transparent;font-size:12px;}}
            QFrame#novelaiAccountStrip QLabel#quotaMuted {{color:{p['muted']};font-size:11px;}}
            QFrame#novelaiAccountStrip QLabel#quotaHeading {{font-weight:600;}}
            QFrame#novelaiAccountStrip QToolButton {{padding:5px 8px;border:none;border-radius:5px;color:{p['muted']};background:transparent;}}
            QFrame#novelaiAccountStrip QToolButton:hover {{color:{p['accent']};background:{p['hover']};}}
            QFrame#novelaiAccountStrip[naiSidebarAccount="true"] {{border:none;border-bottom:1px solid {p['border']};border-radius:0;background:transparent;}}
            QFrame#novelaiAccountStrip[naiSidebarAccount="true"] QLabel {{font-size:11px;}}
            QFrame#novelaiAccountStrip[naiSidebarAccount="true"] QLabel#quotaHeading {{font-size:10px;font-weight:400;color:{p['muted']};}}
            QFrame#novelaiAccountStrip[naiSidebarAccount="true"] QLabel#quotaPercent {{color:{p['accent']};font-size:10px;font-weight:600;}}
            QFrame#novelaiAccountStrip[naiSidebarAccount="true"] QToolButton {{padding:3px 5px;font-size:10px;}}
        """)
