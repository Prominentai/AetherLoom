"""Shared dialog boundaries and readable, theme-aware transient hints."""
import html
import re

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_ui import palette
from aetherloom_core.platform_utils import _set_native_titlebar_dark


def owns(owner, widget):
    while widget is not None:
        if widget is owner:
            return True
        widget = widget.parent()
    return False


def themed_palette(mode, base):
    p = palette(mode)
    result = QtGui.QPalette(base)
    roles = {'Window': 'canvas', 'Base': 'input', 'AlternateBase': 'surface',
             'Button': 'surface', 'WindowText': 'text', 'Text': 'text',
             'ButtonText': 'text', 'ToolTipBase': 'surface', 'ToolTipText': 'text',
             'Highlight': 'accent', 'HighlightedText': None, 'PlaceholderText': 'muted'}
    for role, key in roles.items():
        result.setColor(getattr(QtGui.QPalette, role), QtGui.QColor(p[key] if key else '#ffffff'))
    for role in (QtGui.QPalette.Text, QtGui.QPalette.WindowText, QtGui.QPalette.ButtonText):
        result.setColor(QtGui.QPalette.Disabled, role, QtGui.QColor(p['muted']))
    return result


class DialogBoundary(QtCore.QObject):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.handle = self.screen = None
        self.scroll = None
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.fit)
        self.settle_timer = QtCore.QTimer(self)
        self.settle_timer.setSingleShot(True)
        self.settle_timer.setInterval(16)
        self.settle_timer.timeout.connect(lambda: self.fit(settled=True))

    def schedule(self, *args):
        if self.dialog.isVisible():
            self.timer.start()

    def bind(self):
        handle = self.dialog.windowHandle()
        if handle is not self.handle:
            if self.handle is not None:
                try:
                    self.handle.screenChanged.disconnect(self.schedule)
                except (RuntimeError, TypeError):
                    pass
            self.handle = handle
            if handle is not None:
                handle.screenChanged.connect(self.schedule)
        screen = handle.screen() if handle else self.dialog.screen()
        if screen not in QtWidgets.QApplication.screens():
            screen = QtWidgets.QApplication.primaryScreen()
        if screen is not self.screen:
            for name in ('availableGeometryChanged', 'logicalDotsPerInchChanged'):
                if self.screen is not None:
                    try:
                        getattr(self.screen, name).disconnect(self.schedule)
                    except (RuntimeError, TypeError):
                        pass
                if screen is not None:
                    getattr(screen, name).connect(self.schedule)
            self.screen = screen
        return screen

    def fit(self, settled=False):
        dialog = self.dialog
        if not dialog.isVisible():
            return
        screen = self.bind()
        if screen is None or dialog.isMaximized() or dialog.isMinimized() or dialog.isFullScreen():
            return
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            self.schedule()
            return
        margins = self.handle.frameMargins() if self.handle else QtCore.QMargins()
        area = screen.availableGeometry().adjusted(8 + margins.left(), 8 + margins.top(),
                                                   -8 - margins.right(), -8 - margins.bottom())
        if area.isEmpty():
            return
        layout = dialog.layout()
        # Last-resort scrolling for fixed-height legacy forms on a short screen.
        # Normal-size dialogs retain their original layout and action bar.
        qt_managed = isinstance(dialog, (QtWidgets.QMainWindow, QtWidgets.QMessageBox, QtWidgets.QInputDialog,
                                        QtWidgets.QFileDialog, QtWidgets.QColorDialog, QtWidgets.QFontDialog))
        if not qt_managed and self.scroll is None and layout is not None and (
                layout.minimumSize().width() > area.width() or layout.minimumSize().height() > area.height()):
            content = QtWidgets.QWidget()
            content.setObjectName('popupOverflowContent')
            content.setLayout(layout)
            self.scroll = QtWidgets.QScrollArea(dialog)
            self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.scroll.setWidgetResizable(True)
            self.scroll.setWidget(content)
            outer = QtWidgets.QVBoxLayout(dialog)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(self.scroll)
            outer.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        dialog.setMinimumSize(min(dialog.minimumWidth(), area.width()), min(dialog.minimumHeight(), area.height()))
        width, height = min(dialog.width(), area.width()), min(dialog.height(), area.height())
        geom = dialog.geometry()
        fitted = QtCore.QRect(max(area.left(), min(geom.x(), area.right() - width + 1)),
                             max(area.top(), min(geom.y(), area.bottom() - height + 1)), width, height)
        if geom != fitted:
            dialog.setGeometry(fitted)
            offset = fitted.topLeft() - dialog.geometry().topLeft()
            if not offset.isNull():
                dialog.move(dialog.pos() + offset)
            # Windows can reposition a native dialog after processing its resize.
            # Recheck once after that event, using the current screen and margins.
            if not settled:
                self.settle_timer.start()


class PopupTheme(QtCore.QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._applying = False
        self.hint_timer = QtCore.QTimer(self)
        self.hint_timer.setSingleShot(True)
        self.hint_timer.timeout.connect(self._refresh_tooltip)
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.screenRemoved.connect(self._topology_changed)
        app.primaryScreenChanged.connect(self._topology_changed)

    def apply(self, dialog):
        if self._applying or getattr(self.owner, '_closing', False) or not dialog.isWindow() or not owns(self.owner, dialog):
            return
        self._applying = True
        try:
            mode = self.owner._theme_mode
            p = palette(mode)
            dialog.setPalette(themed_palette(mode, dialog.palette()))
            font = QtGui.QFont(self.owner.font()); font.setPixelSize(13)
            dialog.setFont(font)
            callback = getattr(dialog, '_popup_theme', None)
            if callback:
                callback(mode)
            source = getattr(dialog, '_theme_source', None)
            current = dialog.styleSheet()
            if current != getattr(dialog, '_popup_applied_css', None):
                dialog._popup_original_css = current
            original = source() if source else getattr(dialog, '_popup_original_css', current)
            css = f'''QDialog {{ background: {p['canvas']}; color: {p['text']}; }}
                QWidget#popupOverflowContent {{ background: {p['canvas']}; }}
                QDialog QPlainTextEdit, QDialog QListView {{ background: {p['input']}; color: {p['text']};
                    border: 1px solid {p['border']}; border-radius: 7px; padding: 6px;
                    selection-background-color: {p['accent']}; selection-color: white; }}
                QDialog QPlainTextEdit:focus {{ border-color: {p['accent']}; }}
                QDialog QAbstractSpinBox {{ background: {p['input']}; color: {p['text']};
                    border: 1px solid {p['border']}; border-radius: 6px; padding: 5px 8px; }}
                QDialogButtonBox QPushButton {{ background: {p['surface']}; color: {p['text']};
                    border: 1px solid {p['border']}; border-radius: 6px; padding: 7px 14px; }}
                QDialogButtonBox QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
                QDialogButtonBox QPushButton:default {{ background: {p['accent']}; color: white; }}
                QDialogButtonBox QPushButton:disabled {{ color: {p['muted']}; }}
            ''' + original
            dialog._popup_applied_css = css
            if dialog.styleSheet() != css:
                dialog.setStyleSheet(css)
            if not hasattr(dialog, '_popup_boundary'):
                dialog._popup_boundary = DialogBoundary(dialog)
            dialog._popup_boundary.schedule()
            if dialog.isVisible():
                _set_native_titlebar_dark(dialog, mode == 'dark')
        finally:
            self._applying = False

    def refresh(self):
        QtWidgets.QToolTip.hideText()
        font = QtGui.QFont(self.owner.font()); font.setPixelSize(12)
        QtWidgets.QToolTip.setFont(font)
        QtWidgets.QToolTip.setPalette(themed_palette(self.owner._theme_mode, self.owner.palette()))
        for widget in self.owner.findChildren(QtWidgets.QDialog):
            self.apply(widget)

    def _topology_changed(self, *args):
        for dialog in self.owner.findChildren(QtWidgets.QDialog):
            boundary = getattr(dialog, '_popup_boundary', None)
            if boundary:
                boundary.schedule()
        QtWidgets.QToolTip.hideText()

    def _refresh_tooltip(self):
        p = palette(self.owner._theme_mode)
        for tip in QtWidgets.QApplication.topLevelWidgets():
            if not isinstance(tip, QtWidgets.QLabel) or tip.objectName() != 'qtooltip_label' or not tip.isVisible():
                continue
            screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or tip.screen()
            if screen is None:
                continue
            area = screen.availableGeometry().adjusted(6, 6, -6, -6)
            maximum = max(80, min(420, area.width()))
            text = tip.text()
            if text != getattr(tip, '_popup_hint_rendered', None):
                # Paths and model names are plain text, including '<' and '&'.
                tip._popup_hint_plain = text[:4000] + ('…' if len(text) > 4000 else '')
            # Native tooltips are top-level labels and do not inherit the shell font.
            font = QtGui.QFont(self.owner.font()); font.setPixelSize(12)
            tip.setFont(font)
            tip.setPalette(themed_palette(self.owner._theme_mode, tip.palette()))
            tip.setStyleSheet(f'QToolTip, QLabel {{ background: {p["surface"]}; color: {p["text"]}; '
                             f'border: 1px solid {p["border"]}; border-radius: 6px; padding: 7px 9px; font-size: 12px; }}')
            tip.setWordWrap(True)
            limit = min(320, area.height())
            tip.setMaximumSize(maximum, limit)
            plain = tip._popup_hint_plain
            for _ in range(5):
                text = re.sub(r'([^\s]{32})', lambda m: m[0] + '\u200b', plain)
                text = '<qt>' + html.escape(text).replace('\n', '<br>') + '</qt>'
                tip.setText(text)
                width = max(80, min(maximum, tip.sizeHint().width()))
                height = max(24, tip.heightForWidth(width))
                if height <= limit:
                    break
                plain = plain[:max(1, int(len(plain) * limit / height * .85))] + '…'
            tip._popup_hint_rendered = text
            tip.resize(width, min(limit, height))
            tip.move(max(area.left(), min(tip.x(), area.right() - tip.width() + 1)),
                     max(area.top(), min(tip.y(), area.bottom() - tip.height() + 1)))

    def _style_choices(self, widget):
        mode = self.owner._theme_mode
        p = palette(mode)
        widget.setPalette(themed_palette(mode, widget.palette()))
        font = QtGui.QFont(self.owner.font()); font.setPixelSize(13)
        widget.setFont(font)
        widget.setStyleSheet(f'''QAbstractItemView {{ background: {p['surface']}; color: {p['text']};
            border: 1px solid {p['border']}; selection-background-color: {p['accent_soft']};
            selection-color: {p['accent']}; outline: none; font-size: 13px; }}
            QAbstractItemView::item {{ min-height: 26px; padding: 3px 8px; }}
            QAbstractItemView::item:hover {{ background: {p['hover']}; }}''')

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QtCore.QEvent.ToolTip and isinstance(watched, QtWidgets.QWidget) and owns(self.owner, watched):
            self.hint_timer.start(0)
        elif kind == QtCore.QEvent.Show and isinstance(watched, QtWidgets.QDialog):
            # Let native dialogs finish constructing their private show layout
            # before applying local styles and measuring their available space.
            if watched.isWindow() and owns(self.owner, watched):
                timer = getattr(watched, '_popup_apply_timer', None)
                if timer is None:
                    timer = watched._popup_apply_timer = QtCore.QTimer(watched)
                    timer.setSingleShot(True)
                    timer.timeout.connect(lambda dialog=watched: self.apply(dialog))
                timer.start(0)
        elif kind == QtCore.QEvent.Show and isinstance(watched, QtWidgets.QLabel) and watched.objectName() == 'qtooltip_label':
            self.hint_timer.start(0)
        elif (kind == QtCore.QEvent.Show and isinstance(watched, QtWidgets.QWidget) and
              watched.isWindow() and watched.windowType() == QtCore.Qt.Popup and
              (isinstance(watched, QtWidgets.QAbstractItemView) or watched.metaObject().className() == 'QComboBoxPrivateContainer') and
              (owns(self.owner, watched) or owns(self.owner, QtWidgets.QApplication.focusWidget()))):
            self._style_choices(watched)
        elif kind in (QtCore.QEvent.WindowStateChange, QtCore.QEvent.WinIdChange) and isinstance(watched, QtWidgets.QDialog):
            boundary = getattr(watched, '_popup_boundary', None)
            if boundary:
                boundary.schedule()
        return False
