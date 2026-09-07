"""Application initialization; imported by the stable root launcher."""
import os
import sys
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.paths import current_dir
from aetherloom_core.resources import PLAY_BUTTON_SVG
from aetherloom_core.platform_utils import _svg_to_icon
from aetherloom_core.ui.main_window import MainWindow

try:
    from PyQt5.QtGui import QTextCursor
    try:
        QtCore.qRegisterMetaType('QTextCursor')
    except Exception:
        pass
except Exception:
    pass

def main():
    # Qt owns physical-to-logical pixel conversion; enable it before QApplication.
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)

    # Prefer an explicit app icon so Windows taskbar shows the correct icon.
    try:
        icon_candidates = []
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            icon_candidates.append(os.path.join(exe_dir, 'app_icon.ico'))
            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                icon_candidates.append(os.path.join(meipass, 'app_icon.ico'))
        else:
            icon_candidates.append(os.path.join(current_dir, 'app_icon.ico'))
            icon_candidates.append(os.path.join(current_dir, 'app_icon.png'))

        set_icon = False
        for p in icon_candidates:
            try:
                if p and os.path.exists(p):
                    app.setWindowIcon(QtGui.QIcon(p))
                    set_icon = True
                    break
            except Exception:
                pass

        # fallback to embedded SVG icon if no file found
        if not set_icon:
            ico = _svg_to_icon(PLAY_BUTTON_SVG, 256)
            if ico:
                app.setWindowIcon(ico)
    except Exception:
        pass

    w = MainWindow()
    try:
        # ensure main window also carries the icon
        try:
            win_icon = QtWidgets.QApplication.windowIcon()
            if not win_icon.isNull():
                w.setWindowIcon(win_icon)
        except Exception:
            pass
    except Exception:
        pass
    w.show()
    try:
        sys.exit(app.exec_())
    finally:
        # ensure settings saved on exit
        try:
            w._save_settings()
        except Exception:
            pass
