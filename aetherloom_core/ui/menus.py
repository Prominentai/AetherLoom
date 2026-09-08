"""One theme for application menus, including Qt's built-in text menus."""

from PyQt5 import QtCore, QtGui, QtWidgets


def colors(mode):
    if mode == 'light':
        return dict(background='#ffffff', border='#d4daec', text='#1f2430',
                    selected='#dce6f9', selected_text='#0f1626',
                    disabled='#9aa5c0', separator='#e3e7f4')
    return dict(background='#131a27', border='#39404c', text='#f0f5ff',
                selected='#1e476c', selected_text='#ffffff',
                disabled='#656a72', separator='#303642')


def stylesheet(mode, font=None):
    p = colors(mode)
    font_css = ''
    if font is not None:
        family = font.family().replace('\\', '\\\\').replace('"', '\\"')
        size = (f'{font.pointSizeF():g}pt' if font.pointSizeF() > 0
                else f'{max(1, font.pixelSize())}px')
        font_css = f'font-family: "{family}"; font-size: {size}; font-weight: normal; font-style: normal;'
    return f'''
        QMenu {{ background: {p['background']}; color: {p['text']};
            border: 1px solid {p['border']}; border-radius: 8px; padding: 6px; {font_css} }}
        QMenu::item {{ padding: 6px 16px; border-radius: 6px; background: transparent; }}
        QMenu::item:selected {{ background: {p['selected']}; color: {p['selected_text']}; }}
        QMenu::item:disabled {{ color: {p['disabled']}; }}
        QMenu::separator {{ height: 1px; background: {p['separator']}; margin: 4px 8px; }}
    '''


class MenuTheme(QtCore.QObject):
    """Style owned popups locally so broad page/card CSS cannot override them."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._applying = False
        QtWidgets.QApplication.instance().installEventFilter(self)

    def _owns(self, menu):
        parent = menu.parent()
        while parent is not None:
            if parent is self.owner:
                return True
            parent = parent.parent()
        return False

    def apply(self, menu):
        if self._applying or not self._owns(menu):
            return
        self._applying = True
        try:
            mode = getattr(self.owner, '_theme_mode', 'dark')
            font = QtGui.QFont(self.owner.font())
            font.setBold(False)
            font.setItalic(False)
            css = stylesheet(mode, font)
            if menu.font() != font:
                menu.setFont(font)
            if menu.styleSheet() != css:
                menu.setStyleSheet(css)
        finally:
            self._applying = False

    def refresh(self):
        for widget in QtWidgets.QApplication.allWidgets():
            if isinstance(widget, QtWidgets.QMenu):
                self.apply(widget)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Polish, QtCore.QEvent.Show) and isinstance(watched, QtWidgets.QMenu):
            self.apply(watched)
        return False
