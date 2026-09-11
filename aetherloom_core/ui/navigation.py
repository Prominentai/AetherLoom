"""A fixed icon rail with one in-window, interactive navigation reveal."""
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.rh_ui import palette


class NavigationReveal(QtWidgets.QAbstractButton):
    def __init__(self, owner):
        super().__init__(owner.centralWidget())
        self.owner = owner
        self.target = None
        self.pending = None
        self.keyboard = False
        self.setObjectName('navigationReveal')
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.hide()
        self.open_timer = QtCore.QTimer(self, singleShot=True, interval=180)
        self.open_timer.timeout.connect(self._open)
        self.close_timer = QtCore.QTimer(self, singleShot=True, interval=150)
        self.close_timer.timeout.connect(self._close_if_outside)
        self.animation = QtCore.QPropertyAnimation(self, b'geometry', self)
        self.animation.setDuration(130)
        self.animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self.clicked.connect(self._activate)
        for button in owner._sidebar_buttons:
            button.setProperty('navDescription', button.toolTip())
            button.setToolTip('')
            button.setFocusPolicy(QtCore.Qt.StrongFocus)
            button.clicked.connect(self.dismiss)
        owner.sidebar_scroll.verticalScrollBar().valueChanged.connect(self.dismiss)
        QtWidgets.QApplication.instance().installEventFilter(self)

    def request(self, button, keyboard=False):
        self.close_timer.stop()
        self.keyboard = keyboard
        if button is self.target and self.isVisible():
            return
        self.pending = button
        self.open_timer.start(0 if keyboard or self.isVisible() else 180)

    def _open(self):
        button = self.pending
        if button is None or not button.isVisible() or not self.owner.isActiveWindow():
            return
        if not self.keyboard and not button.rect().contains(button.mapFromGlobal(QtGui.QCursor.pos())):
            return
        host = self.parentWidget()
        origin = button.mapTo(host, QtCore.QPoint())
        # Overlap the icon's hit area: moving to the label never crosses a gap.
        width = min(236, host.width() - origin.x() - 8)
        rect = QtCore.QRect(origin.x(), max(4, min(origin.y() - 6, host.height() - 64)), width, 58)
        already_open = self.isVisible()
        self.animation.stop()
        self.target = button
        self.pending = None
        self.setAccessibleName(button.accessibleName() or button.text().strip())
        self.setAccessibleDescription(button.property('navDescription') or '')
        self.setGeometry(rect)
        self.show()
        self.raise_()
        if not already_open:
            self.animation.setStartValue(QtCore.QRect(rect.x(), rect.y(), button.width(), rect.height()))
            self.animation.setEndValue(rect)
            self.animation.start()
        self.update()

    def dismiss(self, *_):
        self.open_timer.stop()
        self.close_timer.stop()
        self.animation.stop()
        self.pending = self.target = None
        self.hide()

    def _close_if_outside(self):
        if self.keyboard and self.target is QtWidgets.QApplication.focusWidget():
            return
        cursor = QtGui.QCursor.pos()
        if self.isVisible() and self.rect().contains(self.mapFromGlobal(cursor)):
            return
        if self.target is not None and self.target.rect().contains(self.target.mapFromGlobal(cursor)):
            return
        self.dismiss()

    def _activate(self):
        button = self.target
        self.dismiss()
        if button is not None and button.isEnabled():
            button.click()

    def eventFilter(self, watched, event):
        kind = event.type()
        buttons = self.owner._sidebar_buttons
        if watched in buttons:
            if kind == QtCore.QEvent.KeyPress and event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                watched.click()
                return True
            if kind == QtCore.QEvent.KeyPress and event.key() in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
                step = -1 if event.key() == QtCore.Qt.Key_Up else 1
                button = buttons[(buttons.index(watched) + step) % len(buttons)]
                self.owner.sidebar_scroll.ensureWidgetVisible(button)
                button.setFocus(QtCore.Qt.TabFocusReason)
                return True
            if kind == QtCore.QEvent.Enter:
                self.request(watched)
            elif kind == QtCore.QEvent.FocusIn and event.reason() in (QtCore.Qt.TabFocusReason, QtCore.Qt.BacktabFocusReason, QtCore.Qt.ShortcutFocusReason):
                self.request(watched, keyboard=True)
            elif kind in (QtCore.QEvent.Leave, QtCore.QEvent.FocusOut):
                self.close_timer.start()
            elif kind == QtCore.QEvent.ToolTip:
                return True
        elif watched is self:
            if kind == QtCore.QEvent.Enter:
                self.close_timer.stop()
            elif kind == QtCore.QEvent.Leave:
                self.close_timer.start()
        if kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape and self.isVisible():
            self.dismiss()
            return True
        if (watched is self.owner and kind in (QtCore.QEvent.WindowDeactivate, QtCore.QEvent.Hide, QtCore.QEvent.Close, QtCore.QEvent.Resize)
                or watched is self.parentWidget() and kind == QtCore.QEvent.Resize):
            self.dismiss()
        elif kind == QtCore.QEvent.MouseButtonPress and watched is not self:
            self.dismiss()
        return False

    def paintEvent(self, event):
        if self.target is None:
            return
        p = palette(getattr(self.owner, '_theme_mode', 'dark'))
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QColor(p['border']))
        painter.setBrush(QtGui.QColor(p['surface']))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)
        accent = QtGui.QColor(p['accent'])
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(p['accent_soft'] if self.target.isChecked() else p['hover']))
        painter.drawRoundedRect(QtCore.QRectF(5, 7, 38, 44), 7, 7)
        self.target.icon().paint(painter, QtCore.QRect(13, 18, 22, 22), QtCore.Qt.AlignCenter,
                                 QtGui.QIcon.Normal, QtGui.QIcon.On if self.target.isChecked() else QtGui.QIcon.Off)
        font = QtGui.QFont(self.font());font.setPixelSize(13);font.setBold(True)
        painter.setFont(font)
        painter.setPen(accent if self.target.isChecked() else QtGui.QColor(p['text']))
        painter.drawText(QtCore.QRect(55, 9, self.width()-65, 20), QtCore.Qt.AlignVCenter, self.accessibleName())
        font.setBold(False);font.setPixelSize(11);painter.setFont(font)
        painter.setPen(QtGui.QColor(p['muted']))
        hint = painter.fontMetrics().elidedText(self.accessibleDescription(), QtCore.Qt.ElideRight, max(0, self.width()-65))
        painter.drawText(QtCore.QRect(55, 30, self.width()-65, 18), QtCore.Qt.AlignVCenter, hint)
