"""NovelAI's three-column illustration workspace, scoped to its own page."""
import re
from aetherloom_core.rh_ui import app_stylesheet as _base_stylesheet, palette as _base_palette


def workspace_palette(mode='dark'):
    colors = _base_palette(mode)
    if mode == 'light':
        colors.update(canvas='#f4f2ed', surface='#fffefa', input='#f5f3ee',
                      border='#dedbd2', text='#272939', muted='#6d6d7d',
                      accent='#806d21', accent_soft='#f3edcc', hover='#eeeadd')
    else:
        colors.update(canvas='#131224', surface='#1b1c30', input='#141628',
                      border='#34354b', text='#eeeeef', muted='#a6acc4',
                      accent='#f3efa3', accent_soft='#35352f', hover='#292a41')
    return colors


def workspace_stylesheet(mode='dark', target='#rhAppPage'):
    old, new = _base_palette(mode), workspace_palette(mode)
    replacements = {old[key].lower(): new[key] for key in old}
    css = re.sub(r'#[0-9a-fA-F]{6}\b', lambda m: replacements.get(m[0].lower(), m[0]), _base_stylesheet(mode))
    return css.replace('#rhAppPage', target)


def page_stylesheet(mode):
    p = workspace_palette(mode)
    return workspace_stylesheet(mode, '#novelaiPage') + f"""
        QWidget#novelaiPage {{background:{p['canvas']};color:{p['text']};}}
        QWidget#novelaiPage QFrame#novelaiSidebar {{background:{p['surface']};border:none;border-right:1px solid {p['border']};border-radius:0;}}
        QWidget#novelaiPage QFrame#novelaiWorkspace {{background:{p['canvas']};border:none;border-radius:0;}}
        QWidget#novelaiPage QLabel#novelaiBrand {{font-size:14px;font-weight:600;color:{p['text']};}}
        QWidget#novelaiPage QLabel#novelaiMuted {{color:{p['muted']};font-size:11px;}}
        QWidget#novelaiPage QToolBar#novelaiResultActions {{background:transparent;border:none;spacing:3px;padding:0;}}
        QWidget#novelaiPage QToolButton {{padding:4px 7px;min-height:19px;border:1px solid transparent;
            border-radius:4px;background:transparent;color:{p['text']};}}
        QWidget#novelaiPage QToolButton:hover {{background:{p['hover']};border-color:{p['border']};}}
        QWidget#novelaiPage QToolButton:checked {{background:{p['accent_soft']};color:{p['accent']};}}
        QWidget#novelaiPage QToolButton:disabled {{color:{p['muted']};background:transparent;border-color:transparent;}}
        QWidget#novelaiPage QToolButton#novelaiViewTab {{padding:4px 12px;border-radius:5px;}}
        QWidget#novelaiPage QToolButton#novelaiViewTab:checked {{background:{p['accent_soft']};color:{p['accent']};border-color:{p['border']};}}
        QWidget#novelaiPage QToolButton#novelaiPanelToggle:checked {{background:transparent;color:{p['muted']};border-color:{p['border']};}}
        QWidget#novelaiPage QFrame#novelaiActionBar {{background:{p['surface']};border:none;border-top:1px solid {p['border']};border-radius:0;}}
        QWidget#novelaiPage QPushButton#novelaiPrimary {{background:{p['accent']};color:{'#242333' if mode != 'light' else '#ffffff'};
            border:1px solid transparent;border-radius:4px;padding:9px 10px;font-size:13px;font-weight:600;}}
        QWidget#novelaiPage QPushButton#novelaiPrimary:hover {{border-color:{p['text']};}}
        QWidget#novelaiPage QPushButton#novelaiPrimary:disabled {{background:{p['border']};color:{p['muted']};}}
        QWidget#novelaiPage QPushButton#novelaiStop {{background:transparent;color:{p['muted']};border:none;padding:4px;}}
        QWidget#novelaiPage QPushButton#novelaiStop:enabled {{color:{p['danger']};}}
        QWidget#novelaiPage QProgressBar#novelaiGenerationProgress {{background:{p['input']};border:none;border-radius:1px;max-height:3px;}}
        QWidget#novelaiPage QProgressBar#novelaiGenerationProgress::chunk {{background:{p['accent']};}}
        QWidget#novelaiPage QGraphicsView#novelaiPreview {{border:none;border-radius:0;background:{p['canvas']};}}
        QWidget#novelaiPage QWidget#novelaiPreviewEmpty {{background:transparent;border:none;}}
        QWidget#novelaiPage QLabel#novelaiEmptyTitle {{font-size:17px;font-weight:600;color:{p['text']};}}
        QWidget#novelaiPage QLabel#novelaiEmptyText {{font-size:12px;color:{p['muted']};}}
        QWidget#novelaiPage QLabel#novelaiEmptyHint {{font-size:11px;color:{p['muted']};}}
        QWidget#novelaiPage QWidget#novelaiHistory {{background:{p['surface']};border:none;border-radius:0;}}
        QWidget#novelaiPage QLabel#novelaiHistoryTitle {{font-size:12px;font-weight:600;}}
        QWidget#novelaiPage QLabel#novelaiHistoryCount {{color:{p['muted']};font-size:11px;}}
        QWidget#novelaiPage QListWidget {{border:0;background:transparent;}}
        QWidget#novelaiPage QListWidget::item {{border-radius:3px;padding:4px;}}
        QWidget#novelaiPage QListWidget::item:selected {{background:{p['accent_soft']};}}
        QWidget#novelaiPage QMenu {{background:{p['surface']};color:{p['text']};border:1px solid {p['border']};
            border-radius:4px;padding:4px;font-size:12px;}}
        QWidget#novelaiPage QMenu::item {{padding:7px 20px 7px 10px;border-radius:3px;}}
        QWidget#novelaiPage QMenu::item:selected {{background:{p['accent_soft']};color:{p['accent']};}}
        QWidget#novelaiPage QMenu::separator {{height:1px;background:{p['border']};margin:4px 8px;}}
        QWidget#novelaiPage QToolTip {{background:{p['surface']};color:{p['text']};border:1px solid {p['border']};padding:6px;}}
        QWidget#novelaiPage QSplitter::handle {{background:{p['border']};}}
    """
