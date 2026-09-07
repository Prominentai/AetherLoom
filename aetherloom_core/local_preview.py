"""Non-modal local media preview with bounded background image decoding."""

import os
from threading import Event

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.rh_ui import palette
from aetherloom_core.media_limits import VIDEO_EXTENSIONS, MediaTooLargeError, load_media_frame


def read_preview(path, target):
    """Decode a bounded frame; large unsupported sources retain system-open access."""
    try:
        frame, original, video = load_media_frame(path, (target.width(), target.height()))
    except MediaTooLargeError:
        raise
    except Exception as exc:
        raise ValueError('无法预览此文件，可尝试系统打开。') from exc
    try:
        data = frame.tobytes()
        image = QtGui.QImage(data, frame.width, frame.height, frame.width * 3,
                             QtGui.QImage.Format_RGB888).copy()
        kind = '视频 · 首帧预览' if video else '图片'
        return image, f'{kind} · {original[0]} × {original[1]}'
    finally:
        frame.close()


class _PreviewSignals(QtCore.QObject):
    ready = QtCore.pyqtSignal(int, str, object, str, str)


class PreviewJob(QtCore.QRunnable):
    def __init__(self, token, path, target, loader=read_preview):
        super().__init__()
        self.token, self.path, self.target, self.loader = token, path, target, loader
        self.signals = _PreviewSignals()
        self.cancelled = Event()

    def run(self):
        try:
            if self.cancelled.is_set():
                self.signals.ready.emit(self.token, self.path, QtGui.QImage(), '', '已取消')
                return
            image, detail = self.loader(self.path, self.target)
            if self.cancelled.is_set():
                image, detail = QtGui.QImage(), ''
            self.signals.ready.emit(self.token, self.path, image, detail, '')
        except Exception as exc:
            self.signals.ready.emit(self.token, self.path, QtGui.QImage(), '', str(exc))


class LocalPreviewDialog(QtWidgets.QDialog):
    """A reusable preview window whose navigation never leaves the supplied list."""

    def __init__(self, parent=None, *, opener=None, pool=None, loader=read_preview, mode='dark'):
        super().__init__(parent, QtCore.Qt.Window)
        self.setObjectName('localPreviewWindow')
        self.setWindowTitle('快速预览')
        self.setModal(False)
        self.setMinimumSize(540, 400)
        self.resize(980, 700)
        self.files = []
        self.index = -1
        self.current_path = ''
        self._load_token = 0
        self._closed = False
        self._jobs = {}
        self._pending_request = None
        self._image = QtGui.QImage()
        self._opener = opener
        self._pool = pool or QtCore.QThreadPool.globalInstance()
        self._loader = loader
        self._provider = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        header = QtWidgets.QHBoxLayout()
        self.filename_label = QtWidgets.QLabel()
        self.filename_label.setObjectName('localPreviewFilename')
        self.filename_label.setTextFormat(QtCore.Qt.PlainText)
        self.filename_label.setWordWrap(True)
        self.filename_label.setMinimumWidth(0)
        self.filename_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        header.addWidget(self.filename_label, 1)
        self.counter_label = QtWidgets.QLabel()
        self.counter_label.setObjectName('localPreviewMuted')
        header.addWidget(self.counter_label)
        layout.addLayout(header)

        self.image_label = QtWidgets.QLabel('选择文件开始预览')
        self.image_label.setObjectName('localPreviewImage')
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setWordWrap(True)
        self.image_label.setMinimumSize(1, 1)
        self.image_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        layout.addWidget(self.image_label, 1)

        footer = QtWidgets.QHBoxLayout()
        self.detail_label = QtWidgets.QLabel()
        self.detail_label.setObjectName('localPreviewMuted')
        self.detail_label.setWordWrap(True)
        footer.addWidget(self.detail_label, 1)
        self.previous_button = QtWidgets.QPushButton('← 上一个')
        self.previous_button.setToolTip('上一个可见文件（←）')
        self.previous_button.clicked.connect(lambda: self.navigate(-1))
        footer.addWidget(self.previous_button)
        self.next_button = QtWidgets.QPushButton('下一个 →')
        self.next_button.setToolTip('下一个可见文件（→）')
        self.next_button.clicked.connect(lambda: self.navigate(1))
        footer.addWidget(self.next_button)
        self.open_button = QtWidgets.QPushButton('系统打开')
        self.open_button.clicked.connect(self.open_current)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)
        hint = QtWidgets.QLabel('← → 切换当前列表中的文件    Esc / 空格 关闭预览')
        hint.setObjectName('localPreviewMuted')
        hint.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(hint)
        self._shortcuts = []
        for key, action in (('Left', lambda: self.navigate(-1)), ('Right', lambda: self.navigate(1)),
                            ('Escape', self.close), ('Space', self.close)):
            shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(QtCore.Qt.WindowShortcut)
            shortcut.activated.connect(action)
            self._shortcuts.append(shortcut)
        for button in (self.previous_button, self.next_button, self.open_button):
            button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
            button.setMinimumHeight(34)
            button.setAutoDefault(False)
        self.apply_theme(mode)

    def apply_theme(self, mode):
        p = palette(mode)
        self.setStyleSheet(f'''
            QDialog#localPreviewWindow {{ background: {p['canvas']}; color: {p['text']}; }}
            QDialog#localPreviewWindow QLabel {{ color: {p['text']}; background: transparent; border: none; }}
            QDialog#localPreviewWindow QLabel#localPreviewFilename {{ font-size: 16px; font-weight: 600; }}
            QDialog#localPreviewWindow QLabel#localPreviewMuted {{ color: {p['muted']}; font-size: 12px; }}
            QDialog#localPreviewWindow QLabel#localPreviewImage {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 10px; }}
            QDialog#localPreviewWindow QPushButton {{ color: {p['text']}; background: {p['surface']};
                border: 1px solid {p['border']}; border-radius: 7px; padding: 6px 12px; }}
            QDialog#localPreviewWindow QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
            QDialog#localPreviewWindow QPushButton:disabled {{ color: {p['muted']}; background: {p['canvas']}; }}
        ''')

    def set_items(self, paths, current_path=None, provider=None):
        self.files = list(dict.fromkeys(str(path) for path in paths if path))
        self._provider = provider
        self._closed = False
        self.index = self.files.index(current_path) if current_path in self.files else (0 if self.files else -1)
        self._show_current()

    def navigate(self, delta):
        if self._provider is not None:
            visible = list(dict.fromkeys(str(path) for path in self._provider() if path))
            if visible != self.files:
                current = self.current_path
                self.files = visible
                if current not in visible:
                    self.index = min(max(0, self.index), len(visible) - 1)
                    self._show_current()
                    return
                self.index = visible.index(current)
        if not self.files:
            return
        target = min(max(0, self.index + delta), len(self.files) - 1)
        if target != self.index:
            self.index = target
            self._show_current()

    def _show_current(self):
        self._pending_request = None
        for job in self._jobs.values():
            job.cancelled.set()
        self._load_token += 1
        self._image = QtGui.QImage()
        self.current_path = self.files[self.index] if 0 <= self.index < len(self.files) else ''
        self.filename_label.setText(os.path.basename(self.current_path) or '没有可预览的文件')
        self.filename_label.setToolTip(self.current_path)
        self.counter_label.setText(f'{self.index + 1} / {len(self.files)}' if self.files else '0 / 0')
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(0 <= self.index < len(self.files) - 1)
        self.open_button.setEnabled(bool(self.current_path))
        self.detail_label.clear()
        self.image_label.clear()
        self.image_label.setText('正在载入预览…' if self.current_path else '当前列表中没有可预览的文件')
        if not self.current_path:
            return
        ratio = max(1.0, float(self.devicePixelRatioF()))
        target = QtCore.QSize(min(2560, max(1200, int(self.image_label.width() * ratio))),
                             min(1600, max(800, int(self.image_label.height() * ratio))))
        self._pending_request = (self._load_token, self.current_path, target)
        self._start_pending()

    def _start_pending(self):
        """One submitted job and one replaceable request bound rapid navigation."""
        if self._closed or self._jobs or self._pending_request is None:
            return
        token, path, target = self._pending_request
        self._pending_request = None
        job = PreviewJob(token, path, target, self._loader)
        self._jobs[token] = job
        job.signals.ready.connect(self._accept_result, QtCore.Qt.QueuedConnection)
        self._pool.start(job, 1)

    @QtCore.pyqtSlot(int, str, object, str, str)
    def _accept_result(self, token, path, image, detail, error):
        self._jobs.pop(token, None)
        if self._closed or token != self._load_token or path != self.current_path:
            self._start_pending()
            return
        if error:
            self.image_label.setText(error)
            self.detail_label.setText('预览不可用')
            return
        self._image = image
        self.detail_label.setText(detail)
        self._fit_image()
        self._start_pending()

    def _fit_image(self):
        if not self._image.isNull():
            bounds = self.image_label.size() - QtCore.QSize(16, 16)
            if bounds.width() > 0 and bounds.height() > 0:
                image = self._image.scaled(bounds, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                self.image_label.setPixmap(QtGui.QPixmap.fromImage(image))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'image_label'):
            self._fit_image()

    def open_current(self):
        if self.current_path and self._opener is not None:
            self._opener(self.current_path)

    def closeEvent(self, event):
        self._closed = True
        self._load_token += 1
        self._pending_request = None
        for job in self._jobs.values():
            job.cancelled.set()
        self._image = QtGui.QImage()
        self.image_label.clear()
        super().closeEvent(event)
