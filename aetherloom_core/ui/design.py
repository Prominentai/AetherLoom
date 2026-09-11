"""Shared page geometry in Qt logical pixels; independent of monitor resolution.

Content reflows at width breakpoints. Font sizes and hit targets never shrink
with the window. Qt owns device-pixel scaling on high-DPI displays.
"""
from PyQt5 import QtCore, QtWidgets

BODY = 13
TITLE = 24
CONTROL = 34
PAGE_MARGIN = 20
PAGE_GAP = 16
HEADER_HEIGHT = 64
CARD_MARGIN = 16
COMPACT_WIDTH = 760


class PageBaseline(QtCore.QObject):
    def __init__(self, host, layout, maximum=None):
        super().__init__(host)
        self.host, self.layout, self.maximum = host, layout, maximum
        host.installEventFilter(self)
        layout.setSpacing(PAGE_GAP)
        self.update()

    def update(self):
        width = self.host.width()
        short = self.host.height() < 560
        gutter = 12 if width < COMPACT_WIDTH else PAGE_MARGIN
        if self.maximum:
            gutter = max(gutter, (width - self.maximum) // 2)
        vertical = 10 if short else PAGE_MARGIN
        self.layout.setContentsMargins(gutter, vertical, gutter, vertical)
        self.layout.setSpacing(10 if short else PAGE_GAP)
        heading = self.host.findChild(QtWidgets.QWidget, 'pageHeader')
        if heading is not None:
            heading.setMinimumHeight(48 if short else HEADER_HEIGHT)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self.update()
        return False


def page_layout(host, layout, maximum=None):
    host._page_baseline = PageBaseline(host, layout, maximum)
    return host._page_baseline


def title(label):
    label.setStyleSheet(f'font-size: {TITLE}px; font-weight: 600; background: transparent; border: none; padding: 0;')
    label.setMinimumWidth(0)
    return label


def header(layout):
    """Wrap an existing header row without replacing widgets or signal wiring."""
    frame = QtWidgets.QWidget()
    frame.setObjectName('pageHeader')
    frame.setMinimumHeight(HEADER_HEIGHT)
    frame.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
    frame.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    return frame


def shell_stylesheet(mode):
    from aetherloom_core.rh_ui import palette
    p = palette(mode)
    return f'''
        QWidget {{ font-size: 13px; }}
        QMainWindow {{ background: {p['canvas']}; }}
        QFrame#sidebarFrame {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 12px; }}
        QToolButton#sidebarButton {{ font-size: 13px; font-weight: 500; text-align: left;
            padding: 0; border-radius: 8px; color: {p['muted']}; }}
        QToolButton#sidebarButton:checked {{ color: {p['accent']}; background: {p['accent_soft']}; }}
        QToolButton#sidebarButton:hover {{ background: {p['hover']}; color: {p['text']}; }}
        QToolButton#sidebarButton:focus {{ border: 1px solid {p['accent']}; }}
        QPushButton#themeToggleButton {{ padding: 4px; border-radius: 8px; }}
        QTabBar::tab {{ font-size: 13px; font-weight: 500; padding: 8px 14px; margin-right: 2px;
            border: none; border-bottom: 2px solid transparent; border-radius: 0; background: transparent; }}
        QTabBar::tab:selected {{ background: {p['accent_soft']}; color: {p['accent']}; border-bottom-color: {p['accent']}; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ padding: 5px 8px; }}
        QPushButton {{ font-size: 13px; font-weight: 500; padding: 7px 12px; }}
        QToolTip {{ font-size: 12px; }}
        QWidget#pageHeader {{ background: transparent; border: none; }}
    '''
