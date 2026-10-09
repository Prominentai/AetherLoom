"""Small brand accents contained within the home page's existing logo area."""
from PyQt5 import QtCore, QtGui, QtWidgets


class BrandGlow(QtWidgets.QWidget):
    """A soft logo halo; no page-wide panel, divider, or interactive surface."""

    def __init__(self, header, logo):
        super().__init__(header)
        self.logo = logo
        self._color = QtGui.QColor('#8bceef')
        self._light = False
        self.setObjectName('homeBrandGlow')
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setStyleSheet('background: transparent; border: none;')
        header.installEventFilter(self)
        logo.installEventFilter(self)
        self._fit()
        self.show()

    def _fit(self):
        # The unmanaged background child cannot affect the header's layout.
        self.setGeometry(self.parentWidget().rect())
        self.lower()

    def set_theme(self, mode, accent):
        self._light = mode == 'light'
        self._color = QtGui.QColor(accent)
        self.update()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (
                QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self._fit()
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move,
                            QtCore.QEvent.Show, QtCore.QEvent.Hide,
                            QtCore.QEvent.StyleChange):
            self.update()
        return False

    def paintEvent(self, event):
        if self.logo.isHidden():
            return
        center = QtCore.QPointF(self.logo.mapTo(self.parentWidget(), self.logo.rect().center()))
        radius = max(self.logo.width(), self.logo.height()) * .68
        glow = QtGui.QRadialGradient(center, radius)
        color = QtGui.QColor(self._color)
        color.setAlpha(10 if self._light else 16)
        glow.setColorAt(0, color)
        color.setAlpha(5 if self._light else 8)
        glow.setColorAt(.65, color)
        color.setAlpha(0)
        glow.setColorAt(1, color)
        painter = QtGui.QPainter(self)
        try:
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center, radius, radius)
        finally:
            painter.end()
