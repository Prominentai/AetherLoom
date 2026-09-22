"""Theme-scoped styling for the public model gallery and version cards."""
from .rh_ui import palette
from aetherloom_core.paths import resource_path


def stylesheet(mode):
    p = palette(mode)
    from pathlib import Path
    check = Path(resource_path('icons', 'ui-check.svg')).as_posix()
    return f'''
        QDialog#rhModelPicker {{ background: {p['canvas']}; }}
        QDialog#rhModelPicker QWidget {{ color: {p['text']}; font-size: 13px; }}
        QDialog#rhModelPicker QLabel {{ border: none; background: transparent; padding: 0; }}
        QDialog#rhModelPicker QLabel#rhModelHeading {{ font-size: 20px; font-weight: 700; }}
        QDialog#rhModelPicker QLabel#rhModelSectionTitle {{ color: {p['text']}; font-size: 14px; font-weight: 600; }}
        QDialog#rhModelPicker QLabel#rhModelFieldLabel {{ color: {p['muted']}; font-size: 12px; }}
        QDialog#rhModelPicker QLabel#rhModelBadge {{ background: {p['accent_soft']}; color: {p['accent']}; padding: 7px 10px; border-radius: 6px; }}
        QDialog#rhModelPicker QLabel#rhModelMessage {{ color: {p['muted']}; padding: 3px 0; }}
        QDialog#rhModelPicker QFrame#rhModelSection {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; }}
        QDialog#rhModelPicker QWidget#rhModelDialogContent {{ background: {p['canvas']}; }}
        QDialog#rhModelPicker QTabWidget#rhModelDialogTabs::pane {{ border: none; background: {p['canvas']}; }}
        QDialog#rhModelPicker QTabWidget#rhModelDialogTabs QTabBar::tab {{ padding: 9px 16px; margin-bottom: 8px; }}
        QDialog#rhModelPicker QLabel#rhImportSteps {{ background: {p['accent_soft']}; color: {p['accent']}; padding: 12px; border-radius: 8px; }}
        QDialog#rhModelPicker QLabel#rhImportPreview {{ background: {p['input']}; color: {p['muted']}; padding: 12px; border-radius: 8px; min-height: 70px; }}
        QDialog#rhModelPicker QProgressBar {{ border: none; background: {p['input']}; border-radius: 2px; }}
        QDialog#rhModelPicker QProgressBar::chunk {{ background: {p['accent']}; border-radius: 2px; }}
        QDialog#rhModelPicker QLabel#rhModelName {{ font-size: 14px; font-weight: 600; }}
        QDialog#rhModelPicker QLabel#rhModelMuted {{ color: {p['muted']}; }}
        QDialog#rhModelPicker QLabel#rhCoverDrop {{ background: {p['input']}; border: 1px dashed {p['accent']}; border-radius: 9px; color: {p['muted']}; padding: 6px; }}
        QDialog#rhModelPicker QWidget#rhModelOverlay {{ background: transparent; }}
        QDialog#rhModelPicker QWidget#rhModelOverlay QLabel#rhModelName {{ color: white; }}
        QDialog#rhModelPicker QWidget#rhModelOverlay QLabel#rhModelMuted {{ color: #c1ccda; }}
        QDialog#rhModelPicker QScrollArea, QWidget#rhModelContent {{ background: {p['canvas']}; border: none; }}
        QDialog#rhModelPicker QFrame#rhModelCard {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 12px; }}
        QDialog#rhModelPicker QFrame#rhModelCard:hover {{ border-color: {p['accent']}; }}
        QDialog#rhModelPicker QFrame#rhModelFilter {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 8px; }}
        QDialog#rhModelPicker QLineEdit, QDialog#rhModelPicker QListWidget,
        QDialog#rhModelPicker QPlainTextEdit {{ background: {p['input']}; color: {p['text']};
            border: 1px solid {p['border']}; border-radius: 7px; padding: 8px; selection-background-color: {p['accent']}; }}
        QDialog#rhModelPicker QLineEdit:focus {{ border-color: {p['accent']}; }}
        QDialog#rhModelPicker QListWidget#rhModelBaseList {{ padding: 4px; }}
        QDialog#rhModelPicker QListWidget#rhModelBaseList::item {{ min-height: 32px; padding: 3px 7px; border: none; }}
        QDialog#rhModelPicker QListWidget#rhModelBaseList::item:hover {{ background: {p['hover']}; border-radius: 5px; }}
        QDialog#rhModelPicker QListWidget#rhModelBaseList::indicator {{ width: 17px; height: 17px; border: 1px solid {p['muted']}; border-radius: 4px; background: {p['input']}; }}
        QDialog#rhModelPicker QListWidget#rhModelBaseList::indicator:checked {{ border-color: {p['accent']}; background: {p['accent']}; image: url("{check}"); }}
        QDialog#rhModelPicker QPlainTextEdit:focus, QDialog#rhModelPicker QComboBox:focus {{ border-color: {p['accent']}; }}
        QDialog#rhModelPicker QLineEdit:read-only {{ color: {p['muted']}; }}
        QDialog#rhModelPicker QComboBox QAbstractItemView {{ background: {p['surface']}; color: {p['text']};
            border: 1px solid {p['border']}; selection-background-color: {p['accent_soft']}; selection-color: {p['accent']}; outline: none; }}
        QDialog#rhModelPicker QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 3px 8px; }}
        QDialog#rhModelPicker QCheckBox {{ spacing: 8px; padding: 4px 0; }}
        QDialog#rhModelPicker QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 0; }}
        QDialog#rhModelPicker QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 4px; min-height: 26px; }}
        QDialog#rhModelPicker QScrollBar::add-line:vertical, QDialog#rhModelPicker QScrollBar::sub-line:vertical {{ height: 0; }}
        QDialog#rhModelPicker QScrollBar::add-page:vertical, QDialog#rhModelPicker QScrollBar::sub-page:vertical {{ background: transparent; }}
        QDialog#rhModelPicker QPushButton {{ border: 1px solid {p['border']}; border-radius: 7px; padding: 7px 12px; }}
        QDialog#rhModelPicker QPushButton#rhModelPrimary {{ background: {p['accent']}; color: white; border-color: {p['accent']}; }}
        QDialog#rhModelPicker QPushButton#rhModelPrimary:hover {{ background: {p['accent']}; border-color: {p['text']}; }}
        QDialog#rhModelPicker QPushButton:focus {{ border-color: {p['accent']}; }}
        QDialog#rhModelPicker QPushButton#rhModelSecondary {{ background: {p['surface']}; color: {p['text']}; }}
        QDialog#rhModelPicker QPushButton#rhModelFavorite {{ background: {p['surface']}; color: #eab64a; padding: 6px 2px; font-size: 18px; }}
        QDialog#rhModelPicker QComboBox {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 7px; }}
        QDialog#rhModelPicker QDialog {{ background: {p['canvas']}; }}
        QDialog#rhModelPicker QPushButton#rhModelSecondary:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
        QDialog#rhModelPicker QPushButton#rhModelPrimary:disabled,
        QDialog#rhModelPicker QPushButton#rhModelSecondary:disabled {{ background: {p['input']}; color: {p['muted']}; border-color: {p['border']}; }}
        QDialog#rhModelPicker QTabBar {{ background: transparent; }}
        QDialog#rhModelPicker QTabBar::tab {{ background: {p['input']}; color: {p['muted']}; border: none;
            border-radius: 5px; padding: 6px 11px; margin-right: 5px; font-size: 12px; }}
        QDialog#rhModelPicker QTabBar::tab:selected {{ background: {p['accent_soft']}; color: {p['accent']}; }}
        QDialog#rhModelPicker QTabBar::tab:hover {{ color: {p['text']}; }}
        QDialog#rhModelPicker QTabBar#rhModelVersions::tab {{ background: rgba(15,22,32,185); color: #dce5f1; }}
        QDialog#rhModelPicker QTabBar#rhModelVersions::tab:selected {{ background: {p['accent']}; color: white; }}
        QDialog#rhModelPicker QTabBar QToolButton {{ background: {p['surface']}; padding: 2px; }}
    '''
