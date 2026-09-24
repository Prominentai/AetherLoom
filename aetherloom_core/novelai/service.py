"""Application-owned NovelAI queue shared by editors and canvas execution."""
import os
from pathlib import Path

from PyQt5 import QtCore, QtWidgets, sip

from aetherloom_core.paths import current_dir
from . import storage
from .queue import QueueService


def _gui_thread(owner):
    app = QtCore.QCoreApplication.instance()
    if app is None or QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError('NovelAI service must be accessed on the GUI thread.')
    if not isinstance(owner, QtCore.QObject) or sip.isdeleted(owner) or owner.thread() != app.thread():
        raise RuntimeError('NovelAI service requires an application owner on the GUI thread.')


def existing_service(owner):
    """Return the current queue without creating one or loading settings."""
    service = getattr(owner, '_novelai_service', None)
    return service if isinstance(service, QueueService) and not sip.isdeleted(service) else None


def get_service(owner, data_dir=None):
    """Get the GUI-thread queue; callers must enqueue/cancel on that thread too."""
    _gui_thread(owner)
    service = existing_service(owner)
    if service is not None:
        if service._closed:
            raise RuntimeError('NovelAI service has been shut down.')
        if data_dir is not None and os.path.normcase(str(Path(data_dir).resolve())) != os.path.normcase(service.data_dir):
            raise ValueError('The shared NovelAI service already uses another data directory.')
        return service
    directory = str(Path(data_dir or current_dir).resolve())
    settings = storage.load_settings(directory).get('queue')
    settings = settings if isinstance(settings, dict) else {}
    service = QueueService(owner)
    service.data_dir = directory
    # Page construction must not reset a queue that the canvas already uses.
    try:
        service.set_concurrency(settings.get('concurrency', 3))
    except (ValueError, TypeError, OverflowError):
        service.set_concurrency(3)
    try:
        service.set_retry_interval(settings.get('retry_interval', 5))
    except (ValueError, TypeError, OverflowError):
        service.set_retry_interval(5)
    owner._novelai_service = service
    return service


def can_close_service(owner, parent=None):
    """Ask about received, unsaved images even when no editor was opened."""
    service = existing_service(owner)
    if service is None or not service.has_unsaved:
        return True
    _gui_thread(owner)
    parent = parent if parent is not None else owner if isinstance(owner, QtWidgets.QWidget) else None
    prompt = QtWidgets.QMessageBox(parent)
    prompt.setWindowTitle('还有尚未保存的 NovelAI 图片')
    prompt.setIcon(QtWidgets.QMessageBox.Warning)
    prompt.setText('部分图片已生成，但尚未成功保存。')
    prompt.setInformativeText('退出会丢失这些图片。可以返回任务队列，重新选择目录保存，无需再次生成。')
    keep = prompt.addButton('返回保存', QtWidgets.QMessageBox.RejectRole)
    discard = prompt.addButton('放弃图片并退出', QtWidgets.QMessageBox.DestructiveRole)
    prompt.setDefaultButton(keep)
    prompt.exec_()
    return prompt.clickedButton() is discard


def shutdown_service(owner):
    """End the shared queue once, from application shutdown rather than a view."""
    service = existing_service(owner)
    if service is not None and not service._closed:
        _gui_thread(owner)
        service.shutdown()
