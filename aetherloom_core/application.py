"""Application initialization; imported by the stable root launcher."""
import sys
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core import __version__
from aetherloom_core.paths import resource_path
from aetherloom_core.startup import close_boot_splash, report_startup_failure, startup_theme
from aetherloom_core.ui.startup_splash import StartupSplash

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
    # Preserve Windows fractional scaling (125%, 150%, 175%) on each monitor.
    QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName('AetherLoom')
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    splash = None
    w = None
    try:
        splash = StartupSplash(startup_theme())
        splash.show()
        # At this point only the splash exists. Flush its first native frame before
        # heavy imports; never pump events from MainWindow's stage callbacks.
        app.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
        close_boot_splash()
        splash.set_status('正在加载界面组件')
        from aetherloom_core.ui.main_window import MainWindow

        icon = QtGui.QIcon(resource_path('app_icon.ico'))
        if icon.isNull():
            icon = QtGui.QIcon(resource_path('icons', 'home_emblem.svg'))
        app.setWindowIcon(icon)
        w = MainWindow(startup_progress=splash.set_status)
        w.setWindowIcon(icon)
        splash.set_status('正在打开工作区')
        splash.finish(w)
        w.show()
        app.setQuitOnLastWindowClosed(True)
    except Exception as error:
        if splash is not None:
            splash.close()
        close_boot_splash()
        report_startup_failure(error)
        return 1

    try:
        return app.exec_()
    finally:
        splash.close()
        close_boot_splash()
        # ensure settings saved on exit
        try:
            w._save_settings()
        except Exception:
            pass
