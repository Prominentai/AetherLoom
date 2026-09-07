"""Viewport-based reflow without propagating page minimums to the window."""
from PyQt5 import QtCore, QtGui, QtWidgets


class ResponsivePage(QtWidgets.QScrollArea):
    def __init__(self, page, rows=(), splitters=(), home=False):
        super().__init__(page)
        self._reflowing = True
        self.setObjectName('responsivePageScroll')
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setMinimumSize(0, 0)
        content = QtWidgets.QWidget()
        # Keep the page identity, indexes, signals and descendant widgets intact.
        content.setLayout(page.layout())
        self.setWidget(content)
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self)
        self.rows = rows
        self.splitters = splitters
        self.home = home
        self.page = page
        self._nav_compact = None
        self._split_sizes = {}
        self._reflowing = False
        self.reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def reflow(self):
        if self._reflowing:
            return
        self._reflowing = True
        try:
            width = self.viewport().width()
            nav = getattr(self.page, '_app_nav_scroll', None)
            if nav is not None:
                compact = width < 900
                if compact != self._nav_compact:
                    if compact:
                        self._wide_nav_visible = getattr(self.page, '_app_nav_visible', True)
                        visible = False
                    else:
                        visible = getattr(self, '_wide_nav_visible',
                                          getattr(self.page, '_app_nav_visible', True))
                    self._nav_compact = compact
                    self.page._app_nav_visible = visible
                    nav.setVisible(visible)
                    toggle = getattr(self.page, '_app_nav_toggle', None)
                    if toggle is not None:
                        toggle.setText('\u2039' if visible else '\u203a')
                    self.page._update_app_nav_metrics()
            for row, threshold in self.rows:
                row.setDirection(QtWidgets.QBoxLayout.TopToBottom if width < threshold
                                 else QtWidgets.QBoxLayout.LeftToRight)
            for splitter, threshold in self.splitters:
                orientation = QtCore.Qt.Vertical if width < threshold else QtCore.Qt.Horizontal
                if splitter.orientation() != orientation:
                    self._split_sizes[(splitter, splitter.orientation())] = splitter.sizes()
                    splitter.setOrientation(orientation)
                    splitter.setSizes(self._split_sizes.get((splitter, orientation), [400, 400]))
            if self.home:
                title = self.widget().findChild(QtWidgets.QLabel, 'homeTitle')
                if title is not None:
                    font = QtGui.QFont(title.font())
                    font.setPixelSize(72)
                    natural = max(1, QtGui.QFontMetrics(font).horizontalAdvance(title.text()))
                    pixels = max(24, min(72, int(72 * max(100, width - 80) / natural)))
                    title.setStyleSheet('color: #ffffff; font-size: %dpx; font-weight: 900;' % pixels)
                logo = self.widget().findChild(QtWidgets.QLabel, 'homeLogo')
                if logo is not None and hasattr(logo, '_source_pixmap'):
                    size = max(80, min(180, self.viewport().height() // 5))
                    logo.setPixmap(logo._source_pixmap.scaled(size, size, QtCore.Qt.KeepAspectRatio,
                                                           QtCore.Qt.SmoothTransformation))
        finally:
            self._reflowing = False


def make_responsive(page, **options):
    if hasattr(page, '_responsive_scroll'):
        return page._responsive_scroll
    page._responsive_scroll = ResponsivePage(page, **options)
    return page._responsive_scroll


class SidebarScroll(QtWidgets.QScrollArea):
    """Keep every navigation action reachable on short displays."""
    def __init__(self, sidebar):
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setWidget(sidebar)
        sidebar.installEventFilter(self)
        self.setMinimumHeight(0)
        self._sync_width()

    def _sync_width(self):
        # Reserve the vertical scrollbar so showing it cannot clip button labels.
        self.setFixedWidth(self.widget().width() + self.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent))

    def eventFilter(self, obj, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self._sync_width()
        return super().eventFilter(obj, event)
