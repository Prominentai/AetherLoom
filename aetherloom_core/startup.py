"""Early startup helpers; keep heavyweight application imports out of this file."""
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime

from aetherloom_core.paths import current_dir


def startup_theme():
    try:
        with open(os.path.join(current_dir, 'settings.json'), encoding='utf-8') as handle:
            settings = json.load(handle)
        return 'light' if settings.get('theme_mode') == 'light' else 'dark'
    except (OSError, ValueError, AttributeError):
        return 'dark'


def close_boot_splash():
    """A source launch has no bootloader; its absence must never block startup."""
    if not getattr(sys, 'frozen', False):
        return
    try:
        import pyi_splash
        if pyi_splash.is_alive():
            pyi_splash.close()
    except Exception:
        pass


def report_startup_failure(error):
    """Keep a bounded local diagnostic and an actionable window for noconsole EXEs."""
    from PyQt5 import QtWidgets

    detail = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    log_path = None
    for folder in (os.path.join(current_dir, 'logs'),
                   os.path.join(tempfile.gettempdir(), 'AetherLoom')):
        try:
            os.makedirs(folder, exist_ok=True)
            candidate = os.path.join(folder, 'startup-error.log')
            with open(candidate, 'w', encoding='utf-8') as handle:
                handle.write(datetime.now().isoformat(timespec='seconds') + '\n' + detail)
            log_path = candidate
            break
        except OSError:
            continue
    if sys.stderr is not None:
        try:
            sys.stderr.write(detail)
        except (OSError, UnicodeError):
            pass
    message = QtWidgets.QMessageBox()
    message.setWindowTitle('AetherLoom · 启动失败')
    message.setIcon(QtWidgets.QMessageBox.Critical)
    message.setText('启动未能完成')
    reason = str(error).strip() or type(error).__name__
    message.setInformativeText(reason + ('\n\n诊断日志：' + log_path if log_path else ''))
    message.setDetailedText(detail)
    message.setStandardButtons(QtWidgets.QMessageBox.Close)
    message.exec_()
