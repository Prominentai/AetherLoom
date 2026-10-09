"""Lightweight startup artwork shared by Qt and the one-file bootloader."""
import argparse
import os
import sys

from PyQt5 import QtCore, QtGui, QtSvg, QtWidgets

from aetherloom_core import __version__
from aetherloom_core.paths import resource_path


ARTWORK_SIZE = QtCore.QSize(640, 360)


def _font(size, weight=QtGui.QFont.Normal):
    font = QtGui.QFont('Microsoft YaHei UI')
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


def paint_startup(painter, rect, theme='dark', status='正在启动创作工作台'):
    """Paint at logical resolution; the same composition scales with Windows DPI."""
    dark = theme != 'light'
    painter.save()
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 640, rect.height() / 360)
    foreground = QtGui.QColor('#EDF3FF' if dark else '#192D4B')
    muted = QtGui.QColor('#8E9EB7' if dark else '#5F708A')
    background = QtGui.QLinearGradient(0, 0, 640, 360)
    background.setColorAt(0, QtGui.QColor('#111B2D' if dark else '#F9FBFF'))
    background.setColorAt(1, QtGui.QColor('#0B1120' if dark else '#EAF0FA'))
    painter.fillRect(QtCore.QRectF(0, 0, 640, 360), background)

    # Restrained threads echo the existing woven emblem, without a bitmap asset.
    painter.save()
    painter.setClipRect(QtCore.QRectF(315, 0, 325, 278))
    for index in range(11):
        path = QtGui.QPainterPath(QtCore.QPointF(302 + index * 15, -32))
        path.cubicTo(354 + index * 16, 118, 615 - index * 6, 67,
                     675, 218 + index * 12)
        color = QtGui.QColor('#88B4EF' if dark else '#5A80BA')
        color.setAlpha(36 if dark else 22)
        painter.setPen(QtGui.QPen(color, 1))
        painter.drawPath(path)
    glow = QtGui.QRadialGradient(486, 137, 146)
    glow.setColorAt(0, QtGui.QColor(77, 111, 237, 34 if dark else 16))
    glow.setColorAt(1, QtGui.QColor(77, 111, 237, 0))
    painter.fillRect(QtCore.QRectF(315, 0, 325, 278), glow)
    painter.restore()

    painter.setPen(QtGui.QColor('#84A9E6' if dark else '#426AA7'))
    painter.setFont(_font(11, QtGui.QFont.DemiBold))
    painter.drawText(QtCore.QRectF(40, 34, 350, 24), QtCore.Qt.AlignVCenter,
                     'A I   C R E A T I V E   S T U D I O')
    painter.setPen(foreground)
    painter.setFont(_font(39, QtGui.QFont.DemiBold))
    painter.drawText(QtCore.QRectF(38, 99, 363, 64), QtCore.Qt.AlignVCenter,
                     'AetherLoom')
    painter.setFont(_font(17))
    painter.setPen(muted)
    painter.drawText(QtCore.QRectF(41, 172, 350, 29), QtCore.Qt.AlignVCenter,
                     '让灵感，交织成作品')
    painter.setFont(_font(12))
    painter.drawText(QtCore.QRectF(41, 221, 300, 22), QtCore.Qt.AlignVCenter,
                     '画布  /  图像  /  创作')

    renderer = QtSvg.QSvgRenderer(resource_path('icons', 'home_emblem.svg'))
    if renderer.isValid():
        renderer.render(painter, QtCore.QRectF(418, 73, 174, 174))

    painter.setPen(QtGui.QPen(QtGui.QColor('#263349' if dark else '#D7E0EF'), 1))
    painter.drawLine(QtCore.QPointF(40, 274), QtCore.QPointF(600, 274))
    accent = QtGui.QLinearGradient(40, 0, 150, 0)
    accent.setColorAt(0, QtGui.QColor('#24C0D7'))
    accent.setColorAt(1, QtGui.QColor('#8572FA'))
    painter.setPen(QtGui.QPen(QtGui.QBrush(accent), 2))
    painter.drawLine(QtCore.QPointF(40, 274), QtCore.QPointF(143, 274))

    painter.setFont(_font(13))
    painter.setPen(foreground)
    message = QtGui.QFontMetrics(painter.font()).elidedText(
        str(status), QtCore.Qt.ElideRight, 440)
    painter.drawText(QtCore.QRectF(40, 291, 448, 28), QtCore.Qt.AlignVCenter, message)
    painter.setFont(_font(12))
    painter.setPen(muted)
    painter.drawText(QtCore.QRectF(505, 291, 95, 28),
                     QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, 'v' + __version__)
    painter.setPen(QtGui.QPen(QtGui.QColor('#28354B' if dark else '#CDD9E9'), 1))
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawRect(QtCore.QRectF(0.5, 0.5, 639, 359))
    painter.restore()


def render_startup(path, theme='dark'):
    """Export an opaque, deterministic image for PyInstaller's native splash."""
    # Qt's Windows offscreen backend does not discover system fonts itself.
    # Explicitly register installed fonts for build-time rendering only.
    if not QtGui.QFontDatabase().families():
        font_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
        for filename in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf'):
            candidate = os.path.join(font_dir, filename)
            if os.path.isfile(candidate):
                QtGui.QFontDatabase.addApplicationFont(candidate)
    if not QtGui.QFontDatabase().families():
        raise RuntimeError('启动画面渲染缺少可用字体，请使用具备系统字体的构建环境')
    image = QtGui.QImage(ARTWORK_SIZE, QtGui.QImage.Format_RGB32)
    image.fill(QtCore.Qt.black)
    painter = QtGui.QPainter(image)
    try:
        paint_startup(painter, QtCore.QRectF(image.rect()), theme)
    finally:
        painter.end()
    if not image.save(os.fspath(path), 'PNG'):
        raise OSError('无法写入启动画面: ' + os.fspath(path))


class StartupSplash(QtWidgets.QWidget):
    """Repaint stages without re-entering the partially built main window."""

    def __init__(self, theme='dark'):
        super().__init__(None, QtCore.Qt.SplashScreen | QtCore.Qt.FramelessWindowHint)
        self.setObjectName('aetherloomStartupSplash')
        self.setWindowTitle('AetherLoom · 正在启动')
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent)
        self.theme = theme
        self.status = '正在启动创作工作台'
        self._main_window = None
        self._finish_timer = QtCore.QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self.close)
        self._position_on_screen()

    def _position_on_screen(self):
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
        screen = screen or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(ARTWORK_SIZE)
            return
        available = screen.availableGeometry()
        scale = min(1.0, max(1, available.width() - 32) / 640,
                    max(1, available.height() - 32) / 360)
        size = QtCore.QSize(round(640 * scale), round(360 * scale))
        # Associate the native window with its monitor before setting logical size.
        self.winId()
        self.windowHandle().setScreen(screen)
        self.setFixedSize(size)
        self.move(available.center() - self.rect().center())

    def set_status(self, status):
        self.status = str(status)
        # QWidget.repaint is synchronous and does not pump application timers.
        self.repaint()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            paint_startup(painter, QtCore.QRectF(self.rect()), self.theme, self.status)
        finally:
            painter.end()

    def finish(self, window):
        """Close after the main window's first painted frame, without a delay."""
        self._main_window = window
        window.installEventFilter(self)
        window.destroyed.connect(self.close)

    def eventFilter(self, watched, event):
        if watched is self._main_window and event.type() in (
                QtCore.QEvent.Paint, QtCore.QEvent.Close):
            if not self._finish_timer.isActive():
                self._finish_timer.start(0)
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        self._finish_timer.stop()
        window, self._main_window = self._main_window, None
        if window is not None:
            try:
                window.removeEventFilter(self)
            except RuntimeError:
                pass
        super().closeEvent(event)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render', required=True, help='Output PNG path')
    args = parser.parse_args(argv)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
    render_startup(args.render)
    return 0


if __name__ == '__main__':
    sys.exit(main())
