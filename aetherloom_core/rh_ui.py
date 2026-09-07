"""Shared presentation for the RunningHub workspace, without task behavior."""

from pathlib import Path


def palette(mode='dark'):
    """Return a fresh set of semantic colors for the requested app theme."""
    if mode == 'light':
        return {
            'canvas': '#f3f6fa', 'surface': '#ffffff', 'input': '#f8fafc',
            'border': '#dce3ec', 'text': '#1c2a3d', 'muted': '#637389',
            'accent': '#2869d8', 'accent_soft': '#eaf1ff', 'hover': '#edf2f8',
            'success': '#258463', 'warning': '#a66a14', 'danger': '#c45059',
        }
    return {
        'canvas': '#101720', 'surface': '#182230', 'input': '#111b28',
        'border': '#2b3a4c', 'text': '#e8eef6', 'muted': '#a1afc1',
        'accent': '#4c8dff', 'accent_soft': '#203858', 'hover': '#243348',
        'success': '#55c99a', 'warning': '#e7b96c', 'danger': '#f08286',
    }


def navigation_button_stylesheet(mode='dark', active=False, status=None):
    """Keep navigation neutral while making task state visible at its edge."""
    p = palette(mode)
    marker = p['accent']
    if active:
        marker = p['warning']
    elif status == 'SUCCESS':
        marker = p['success']
    elif status in ('FAILED', 'CANCELED'):
        marker = p['danger']
    return f'''
        QPushButton {{ background: {p['surface']}; color: {p['text']};
            border: 1px solid {p['border']}; border-left: 3px solid {marker};
            border-radius: 9px; padding: 12px 12px 12px 14px;
            text-align: left; font-weight: 600; }}
        QPushButton:hover {{ background: {p['hover']}; border-color: {marker}; }}
        QPushButton:pressed, QPushButton:checked {{ background: {p['accent_soft']}; }}
        QPushButton:focus {{ border: 1px solid {p['accent']};
            border-left: 3px solid {marker}; }}
        QPushButton:disabled {{ color: {p['muted']}; }}
    '''


def app_stylesheet(mode='dark'):
    """Styles stay below rhAppPage so other tools retain their own layout."""
    p = palette(mode)
    icon_dir = Path(__file__).resolve().parents[1] / 'icons'
    icon_theme = 'light' if mode == 'light' else 'dark'
    up_arrow = (icon_dir / f'ui-chevron-up-{icon_theme}.svg').as_posix()
    down_arrow = (icon_dir / f'ui-chevron-down-{icon_theme}.svg').as_posix()
    check = (icon_dir / 'ui-check.svg').as_posix()
    return f'''
        QWidget#rhAppPage {{ background: {p['canvas']}; color: {p['text']}; }}
        QWidget#rhAppPage QWidget {{ background: transparent; color: {p['text']}; }}
        QWidget#rhAppPage QLabel {{ background: transparent; border: none; padding: 0; }}
        QWidget#rhAppPage QLabel#rhPageTitle {{ font-size: 24px; font-weight: 700; }}
        QWidget#rhAppPage QLabel#rhSubtitle,
        QWidget#rhAppPage QLabel#rhMuted {{ color: {p['muted']}; font-size: 12px; }}
        QWidget#rhAppPage QLabel#rhSectionTitle {{ font-size: 15px; font-weight: 700; }}
        QWidget#rhAppPage QWidget#rhParameterPanel,
        QWidget#rhAppPage QWidget#rhResultPanel {{ background: {p['surface']};
            border: 1px solid {p['border']}; border-radius: 14px; }}
        QWidget#rhAppPage QFrame#nodeCard {{ background: {p['surface']};
            border: 1px solid {p['border']}; border-radius: 11px; }}
        QWidget#rhAppPage QFrame#nodeCard:hover {{ border-color: {p['muted']}; }}
        QWidget#rhAppPage QFrame#nodePreviewCard {{ background: {p['input']};
            border: 1px solid {p['border']}; border-radius: 10px; }}
        QWidget#rhAppPage QLabel#rhResultTitle {{ font-size: 13px; font-weight: 600; }}
        QWidget#rhAppPage QLabel#rhNodeTitle {{ font-size: 14px; font-weight: 600; }}
        QWidget#rhAppPage QLabel#rhNodeSubtitle {{ color: {p['muted']}; font-size: 12px; }}
        QWidget#rhAppPage QLabel#rhTypeBadge {{ background: {p['accent_soft']};
            color: {p['accent']}; border-radius: 5px; padding: 3px 7px;
            font-size: 10px; font-weight: 600; }}
        QWidget#rhAppPage QLineEdit, QWidget#rhAppPage QTextEdit,
        QWidget#rhAppPage QPlainTextEdit, QWidget#rhAppPage QAbstractSpinBox,
        QWidget#rhAppPage QComboBox {{ background: {p['input']};
            color: {p['text']}; border: 1px solid {p['border']};
            border-radius: 7px; padding: 7px 9px; selection-background-color: {p['accent']};
            selection-color: #ffffff; }}
        QWidget#rhAppPage QLineEdit:focus, QWidget#rhAppPage QTextEdit:focus,
        QWidget#rhAppPage QPlainTextEdit:focus, QWidget#rhAppPage QAbstractSpinBox:focus,
        QWidget#rhAppPage QComboBox:focus {{ border-color: {p['accent']}; }}
        QWidget#rhAppPage QAbstractSpinBox QLineEdit {{ background: transparent;
            border: none; border-radius: 0; padding: 0; }}
        QWidget#rhAppPage QAbstractSpinBox {{ padding-right: 9px; }}
        QWidget#rhAppPage QAbstractSpinBox::up-button {{ subcontrol-origin: border;
            subcontrol-position: top right; width: 26px; height: 17px;
            background: {p['hover']}; border: none; border-left: 1px solid {p['border']};
            border-bottom: 1px solid {p['border']}; border-top-right-radius: 7px; }}
        QWidget#rhAppPage QAbstractSpinBox::down-button {{ subcontrol-origin: border;
            subcontrol-position: bottom right; width: 26px; height: 17px;
            background: {p['hover']}; border: none; border-left: 1px solid {p['border']};
            border-bottom-right-radius: 7px; }}
        QWidget#rhAppPage QAbstractSpinBox::up-button:hover,
        QWidget#rhAppPage QAbstractSpinBox::down-button:hover {{ background: {p['accent_soft']}; }}
        QWidget#rhAppPage QAbstractSpinBox::up-arrow {{ image: url("{up_arrow}"); width: 12px; height: 12px; }}
        QWidget#rhAppPage QAbstractSpinBox::down-arrow {{ image: url("{down_arrow}"); width: 12px; height: 12px; }}
        QWidget#rhAppPage QAbstractSpinBox::up-button:disabled,
        QWidget#rhAppPage QAbstractSpinBox::down-button:disabled {{ background: {p['input']}; }}
        QWidget#rhAppPage QComboBox {{ padding-right: 9px; }}
        QWidget#rhAppPage QComboBox::drop-down {{ subcontrol-origin: border;
            subcontrol-position: top right; border: none; width: 30px;
            border-left: 1px solid {p['border']}; }}
        QWidget#rhAppPage QComboBox::down-arrow {{ image: url("{down_arrow}"); width: 14px; height: 14px; }}
        QWidget#rhAppPage QComboBox QAbstractItemView {{ background: {p['surface']};
            color: {p['text']}; border: 1px solid {p['border']};
            selection-background-color: {p['accent_soft']}; selection-color: {p['text']}; }}
        QWidget#rhAppPage QPushButton, QWidget#rhAppPage QToolButton {{
            background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']};
            border-radius: 7px; padding: 7px 11px; font-size: 12px; font-weight: 500; }}
        QWidget#rhAppPage QPushButton:hover, QWidget#rhAppPage QToolButton:hover {{
            background: {p['hover']}; border-color: {p['muted']}; }}
        QWidget#rhAppPage QPushButton:focus, QWidget#rhAppPage QToolButton:focus {{
            border-color: {p['accent']}; }}
        QWidget#rhAppPage QPushButton#rhPrimaryButton {{ background: {p['accent']};
            color: #ffffff; border: 1px solid {p['accent']}; font-weight: 600;
            padding: 9px 18px; border-radius: 8px; }}
        QWidget#rhAppPage QPushButton#rhPrimaryButton:hover {{ background: #377bea;
            border-color: #377bea; }}
        QWidget#rhAppPage QToolButton#rhToolButton,
        QWidget#rhAppPage QPushButton#rhToolButton {{ background: transparent;
            color: {p['muted']}; border: 1px solid transparent; padding: 4px 7px; }}
        QWidget#rhAppPage QToolButton#rhToolButton:hover,
        QWidget#rhAppPage QPushButton#rhToolButton:hover {{ background: {p['hover']};
            color: {p['text']}; border-color: {p['border']}; }}
        QWidget#rhAppPage QPushButton:disabled, QWidget#rhAppPage QToolButton:disabled {{
            background: {p['hover']}; color: {p['muted']}; border-color: {p['border']}; }}
        QWidget#rhAppPage QScrollArea, QWidget#rhAppPage QScrollArea#rhNodesScroll {{
            background: transparent; border: none; }}
        QWidget#rhAppPage QLabel#rhInputPreview {{ background: {p['input']};
            border: 1px dashed {p['border']}; border-radius: 8px; }}
        QWidget#rhAppPage QWidget#rhRunBar {{ background: {p['surface']};
            border: none; border-top: 1px solid {p['border']}; }}
        QWidget#rhAppPage QLabel#rhEmptyTitle {{ color: {p['text']};
            font-size: 17px; font-weight: 600; }}
        QWidget#rhAppPage QLabel#rhEmptyHint {{ color: {p['muted']}; font-size: 12px; }}
        QWidget#rhAppPage QSplitter::handle {{ background: transparent; }}
        QWidget#rhAppPage QSplitter::handle:hover {{ background: {p['border']}; }}
        QWidget#rhAppPage QCheckBox {{ spacing: 8px; }}
        QWidget#rhAppPage QCheckBox::indicator {{ width: 16px; height: 16px;
            background: {p['input']}; border: 1px solid {p['muted']}; border-radius: 4px; }}
        QWidget#rhAppPage QCheckBox::indicator:hover,
        QWidget#rhAppPage QCheckBox::indicator:focus {{ border-color: {p['accent']}; }}
        QWidget#rhAppPage QCheckBox::indicator:checked {{ background: {p['accent']};
            border-color: {p['accent']}; image: url("{check}"); }}
        QWidget#rhAppPage QCheckBox::indicator:disabled {{ background: {p['hover']};
            border-color: {p['border']}; }}
        QWidget#rhAppPage QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
        QWidget#rhAppPage QScrollBar::handle:vertical {{ background: {p['border']};
            min-height: 32px; border-radius: 3px; }}
        QWidget#rhAppPage QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
        QWidget#rhAppPage QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
        QWidget#rhAppPage QScrollBar::handle:horizontal {{ background: {p['border']};
            min-width: 32px; border-radius: 3px; }}
        QWidget#rhAppPage QScrollBar::handle:horizontal:hover {{ background: {p['muted']}; }}
        QWidget#rhAppPage QScrollBar::add-line, QWidget#rhAppPage QScrollBar::sub-line {{
            width: 0; height: 0; }}
        QWidget#rhAppPage QScrollBar::add-page, QWidget#rhAppPage QScrollBar::sub-page {{
            background: transparent; }}
    '''
