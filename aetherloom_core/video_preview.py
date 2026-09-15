"""One on-demand video player per client, independent of canvas rendering."""
import os

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from .rh_ui import palette


def open_video(path, parent=None):
    app = QtWidgets.QApplication.instance()
    dialog = getattr(app, '_video_preview_window', None)
    if dialog is None or sip.isdeleted(dialog):
        dialog = VideoPreviewDialog(parent.window() if parent else None)
        app._video_preview_window = dialog
        app.aboutToQuit.connect(dialog.release)
        from .video_compat import shutdown_decoders
        if not getattr(app, '_video_decoder_shutdown_connected', False):
            app.aboutToQuit.connect(shutdown_decoders)
            app._video_decoder_shutdown_connected = True
    dialog.show()
    dialog.owner = parent
    dialog.open_path(path)
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def close_video(parent):
    dialog = getattr(QtWidgets.QApplication.instance(), '_video_preview_window', None)
    if dialog is not None and not sip.isdeleted(dialog) and getattr(dialog, 'owner', None) is parent:
        dialog.close()
        dialog.owner = None


def _clock(milliseconds):
    seconds = max(0, int(milliseconds)) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


class _FrameSurface(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QtGui.QImage()
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def set_frame(self, image):
        self.image = image
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtCore.Qt.black)
        if not self.image.isNull():
            size = self.image.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRect(QtCore.QPoint(), size)
            target.moveCenter(self.rect().center())
            painter.drawImage(target, self.image)


class VideoPreviewDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle('视频预览')
        self.setObjectName('videoPreviewWindow')
        self.resize(860, 570)
        self.setMinimumSize(360, 280)
        self.path = ''
        self.player = None
        self.video = None
        self._resume_after_seek = False
        self._backend_error = ''
        self.native_player = None
        self.compat_player = None
        self.compat_surface = None
        self._fallback_pending = False
        self._generation = 0
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        self.title = QtWidgets.QLabel()
        self.title.setTextFormat(QtCore.Qt.PlainText)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.surface = QtWidgets.QVBoxLayout()
        layout.addLayout(self.surface, 1)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QtWidgets.QHBoxLayout()
        self.play = QtWidgets.QPushButton('播放')
        self.play.clicked.connect(self.toggle)
        controls.addWidget(self.play)
        self.seek = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.setToolTip('播放位置')
        self.seek.sliderPressed.connect(self._begin_seek)
        self.seek.sliderReleased.connect(self._end_seek)
        self.seek.sliderMoved.connect(self._show_time)
        self.seek.valueChanged.connect(self._seek_by_key)
        controls.addWidget(self.seek, 1)
        self.time = QtWidgets.QLabel('00:00 / 00:00')
        controls.addWidget(self.time)
        layout.addLayout(controls)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(QtWidgets.QLabel('音量'))
        self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setMaximumWidth(130)
        self.volume.valueChanged.connect(lambda value: self.player.setVolume(value) if self.player else None)
        footer.addWidget(self.volume)
        footer.addStretch()
        self.external = QtWidgets.QPushButton('系统打开')
        self.external.clicked.connect(self.open_external)
        footer.addWidget(self.external)
        layout.addLayout(footer)
        self.shortcut = QtWidgets.QShortcut(QtGui.QKeySequence('Space'), self)
        self.shortcut.activated.connect(self.toggle)
        for button in (self.play, self.external):
            button.setAutoDefault(False)
        try:
            from PyQt5.QtMultimedia import QMediaPlayer
            from PyQt5.QtMultimediaWidgets import QVideoWidget
            self.video = QVideoWidget(self)
            self.video.setMinimumSize(1, 1)
            self.video.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
            self.surface.addWidget(self.video)
            self.player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
            self.player.setVideoOutput(self.video)
            self.player.setVolume(self.volume.value())
            self.player.stateChanged.connect(self._state_changed)
            self.player.durationChanged.connect(self._duration_changed)
            self.player.positionChanged.connect(self._position_changed)
            self.player.seekableChanged.connect(self.seek.setEnabled)
            self.player.mediaStatusChanged.connect(self._media_status)
            self.player.error.connect(self._error)
            self.native_player = self.player
        except (ImportError, RuntimeError) as exc:
            self._backend_error = f'视频播放组件不可用：{exc}'
            self.surface.addWidget(QtWidgets.QLabel('请使用系统打开播放此视频'))

    def open_path(self, path):
        self.release()
        self._fallback_pending = False
        self.player = self.native_player
        if self.compat_surface:
            self.compat_surface.hide()
        if self.video:
            self.video.show()
        self.volume.setEnabled(True)
        self.path = os.path.abspath(path)
        self.title.setText(os.path.basename(self.path))
        self.title.setToolTip(self.path)
        self.setWindowTitle(f'视频预览 · {os.path.basename(self.path)}')
        mode = 'dark' if self.palette().color(QtGui.QPalette.Window).lightness() < 128 else 'light'
        colors = palette(mode)
        self.setStyleSheet(f"QDialog#videoPreviewWindow {{background:{colors['canvas']}; color:{colors['text']};}} "
                           f"QLabel {{color:{colors['text']}; background:transparent;}} "
                           f"QPushButton {{color:{colors['text']}; background:{colors['surface']}; "
                           f"border:1px solid {colors['border']}; border-radius:6px; padding:6px 12px;}}")
        exists = os.path.isfile(self.path)
        self.external.setEnabled(exists)
        self.play.setEnabled(exists and self.player is not None)
        self.seek.setEnabled(False)
        self.status.setText('文件已移动或删除' if not exists else self._backend_error or '正在加载视频…')
        if exists and self.player:
            from PyQt5.QtMultimedia import QMediaContent
            self.player.setMedia(QMediaContent(QtCore.QUrl.fromLocalFile(self.path)))
            self.player.play()
        elif exists:
            self._start_compat()

    def toggle(self):
        if not self.player or not self.play.isEnabled():
            return
        if self.player.state() == self.player.PlayingState:
            self.player.pause()
        else:
            if self.player.mediaStatus() == self.player.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()

    def _state_changed(self, state):
        self.play.setText('暂停' if state == self.player.PlayingState else '播放')

    def _duration_changed(self, duration):
        self.seek.setMaximum(min(max(0, duration), 2147483647))
        self._show_time(self.player.position())

    def _show_time(self, position):
        self.time.setText(f'{_clock(position)} / {_clock(self.player.duration() if self.player else 0)}')

    def _position_changed(self, position):
        if not self.seek.isSliderDown():
            with QtCore.QSignalBlocker(self.seek):
                self.seek.setValue(position)
            self._show_time(position)

    def _begin_seek(self):
        if self.player:
            self._resume_after_seek = self.player.state() == self.player.PlayingState
            self.player.pause()

    def _end_seek(self):
        if self.player:
            self.player.setPosition(self.seek.value())
            if self._resume_after_seek:
                self.player.play()
        self._resume_after_seek = False

    def _seek_by_key(self, position):
        if self.player and not self.seek.isSliderDown() and self.player.isSeekable():
            self.player.setPosition(position)

    def _media_status(self, status):
        if self.sender() is not self.player:
            return
        if status == self.player.InvalidMedia:
            self._error()
        elif status in (self.player.LoadedMedia, 6):
            self.status.setText('兼容预览（无声） · 空格：播放 / 暂停' if self.player is self.compat_player else '空格：播放 / 暂停')
        elif status == self.player.EndOfMedia:
            self.status.setText('播放结束')

    def _error(self, *_):
        if self.player is self.native_player and self.player and (self.player.error() or self.player.mediaStatus() == self.player.InvalidMedia):
            reason = self.player.errorString() or '当前系统不支持此视频的编码格式'
            self.status.setText(f'{reason}，正在切换兼容预览…')
            if not self._fallback_pending:
                self._fallback_pending = True
                generation = self._generation
                QtCore.QTimer.singleShot(0, lambda: self._start_compat() if generation == self._generation else None)

    def _start_compat(self):
        if not self.isVisible() or not os.path.isfile(self.path):
            return
        if self.native_player:
            from PyQt5.QtMultimedia import QMediaContent
            self.native_player.stop()
            self.native_player.setMedia(QMediaContent())
        if self.video:
            self.video.hide()
        if self.compat_player is None:
            from .video_compat import SilentVideoPlayer
            self.compat_surface = _FrameSurface(self)
            self.surface.addWidget(self.compat_surface)
            self.compat_player = SilentVideoPlayer(self)
            self.compat_player.frameReady.connect(self.compat_surface.set_frame)
            self.compat_player.stateChanged.connect(self._state_changed)
            self.compat_player.positionChanged.connect(self._position_changed)
            self.compat_player.durationChanged.connect(self._duration_changed)
            self.compat_player.seekableChanged.connect(self.seek.setEnabled)
            self.compat_player.mediaStatusChanged.connect(self._media_status)
            self.compat_player.failed.connect(self._compat_error)
        self.player = self.compat_player
        self.volume.setEnabled(False)
        self.play.setEnabled(True)
        self.compat_surface.show()
        self.status.setText('正在加载兼容预览（无声）…')
        self.compat_player.open(self.path)

    def _compat_error(self, reason):
        self.status.setText(f'无法播放：{reason}。可尝试系统打开。')
        self.play.setEnabled(False)
        self.seek.setEnabled(False)

    def open_external(self):
        if os.path.isfile(self.path):
            self.release()
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.path))

    def release(self):
        self._generation += 1
        self._resume_after_seek = False
        if self.native_player:
            from PyQt5.QtMultimedia import QMediaContent
            self.native_player.stop()
            self.native_player.setMedia(QMediaContent())
        if self.compat_player:
            self.compat_player.release()
            self.compat_surface.set_frame(QtGui.QImage())
        self.seek.setRange(0, 0)
        self.time.setText('00:00 / 00:00')

    def hideEvent(self, event):
        self.release()
        super().hideEvent(event)
