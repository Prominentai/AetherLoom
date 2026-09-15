"""One silent, bounded hover animation shared by cards and canvas tiles."""
import os
from collections import OrderedDict

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from .media_limits import VIDEO_EXTENSIONS
from .video_compat import SilentVideoPlayer, shutdown_decoders


def animated_path(path):
    return bool(path) and os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS | {'.gif'}


def open_external(path):
    if path and os.path.exists(path):
        hover_player().stop()
        return QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.abspath(path)))
    return False


def hover_player():
    app = QtWidgets.QApplication.instance()
    if not hasattr(app, '_hover_media_preview'):
        app._hover_media_preview = HoverPlayer(app)
        app.aboutToQuit.connect(app._hover_media_preview.stop)
        if not getattr(app, '_video_decoder_shutdown_connected', False):
            app.aboutToQuit.connect(shutdown_decoders)
            app._video_decoder_shutdown_connected = True
    return app._hover_media_preview


class HoverPlayer(QtCore.QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.owner = None
        self.path = ''
        self.callback = self.valid = None
        self.failures = OrderedDict()
        self.video = SilentVideoPlayer(self, frame_size=(640, 360))
        self.video.frameReady.connect(self._frame)
        self.video.mediaStatusChanged.connect(self._video_status)
        self.video.failed.connect(self._failed)
        self.movie = QtGui.QMovie(self)
        self.movie.setCacheMode(QtGui.QMovie.CacheNone)
        self.movie.frameChanged.connect(lambda _: self._frame(self.movie.currentImage()))
        self.movie.finished.connect(self._repeat_gif)
        self.movie.error.connect(self._failed)
        self.guard = QtCore.QTimer(self)
        self.guard.setInterval(100)
        self.guard.timeout.connect(self._check)
        parent.applicationStateChanged.connect(self._application_state)

    def start(self, owner, path, callback, valid):
        if self.owner is owner and self.path == path:
            return
        self.stop()
        if not animated_path(path) or not os.path.isfile(path):
            return
        if path in self.failures and self.failures[path] == self._revision(path):
            return
        self.owner, self.path, self.callback, self.valid = owner, path, callback, valid
        self.guard.start()
        if os.path.splitext(path)[1].lower() == '.gif':
            reader = QtGui.QImageReader(path)
            size = reader.size()
            if not size.isValid() or size.width() * size.height() > 8_000_000:
                self._failed()
                return
            self.movie.setFileName(path)
            self.movie.setScaledSize(size.scaled(640, 360, QtCore.Qt.KeepAspectRatio))
            self.movie.start()
        else:
            self.video.open(path)

    def stop(self, owner=None):
        if owner is not None and owner is not self.owner:
            return
        callback = self.callback
        self.owner = self.callback = self.valid = None
        self.path = ''
        self.guard.stop()
        self.movie.stop()
        self.movie.setFileName('')
        self.video.release()
        if callback:
            try:
                callback(QtGui.QImage())
            except RuntimeError:
                pass  # The owning Qt item may already have been deleted.

    def _check(self):
        try:
            valid = self.valid and self.valid()
        except (RuntimeError, IndexError):
            valid = False
        if not valid:
            self.stop()

    @staticmethod
    def _revision(path):
        try:
            stat = os.stat(path)
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _failed(self, *_):
        if self.path:
            self.failures[self.path] = self._revision(self.path)
            self.failures.move_to_end(self.path)
            while len(self.failures) > 32:
                self.failures.popitem(last=False)
        self.stop()

    def _frame(self, image):
        self._check()
        if self.callback:
            self.callback(image)

    def _video_status(self, status):
        if self.owner is not None and status == self.video.EndOfMedia:
            self.video.play()

    def _repeat_gif(self):
        if self.owner is not None:
            self.movie.start()

    def _application_state(self, state):
        if state != QtCore.Qt.ApplicationActive:
            self.stop()


class _Overlay(QtWidgets.QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.image = QtGui.QImage()
        self.hide()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.palette().brush(QtGui.QPalette.Base))
        size = self.image.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
        target = QtCore.QRect(QtCore.QPoint(), size)
        target.moveCenter(self.rect().center())
        painter.drawImage(target, self.image)


class WidgetHover(QtCore.QObject):
    """Keep the existing first-frame QLabel/painter untouched under the overlay."""
    def __init__(self, card, preview, path_getter):
        super().__init__(card)
        self.card, self.preview, self.path_getter = card, preview, path_getter
        self.overlay = None
        card.installEventFilter(self)
        if card is not preview:
            preview.installEventFilter(self)

    def start(self):
        path = self.path_getter()
        hover_player().start(self, path, self.frame, lambda: self.visible(path))

    def dispose(self):
        hover_player().stop(self)
        for widget in (self.card, self.preview):
            if not sip.isdeleted(widget):
                widget.removeEventFilter(self)
        if self.overlay and not sip.isdeleted(self.overlay):
            self.overlay.deleteLater()
        self.deleteLater()

    def visible(self, path):
        return (not sip.isdeleted(self.card) and not sip.isdeleted(self.preview)
                and self.card.isVisible() and self.preview.isVisible()
                and self.card.visibleRegion().contains(self.card.mapFromGlobal(QtGui.QCursor.pos()))
                and self.path_getter() == path)

    def frame(self, image):
        if sip.isdeleted(self.preview):
            return
        if image.isNull():
            if self.overlay and not sip.isdeleted(self.overlay):
                self.overlay.image = QtGui.QImage()
                self.overlay.hide()
            return
        if self.overlay is None:
            self.overlay = _Overlay(self.preview)
        self.overlay.image = image
        self.overlay.setGeometry(self.preview.contentsRect())
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.update()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QtCore.QEvent.Enter:
            self.start()
        elif kind in (QtCore.QEvent.Hide, QtCore.QEvent.Close) or (kind == QtCore.QEvent.Leave and watched is self.card):
            hover_player().stop(self)
        elif kind == QtCore.QEvent.Resize and watched is self.preview and self.overlay:
            self.overlay.setGeometry(self.preview.contentsRect())
        elif kind == QtCore.QEvent.MouseButtonDblClick and event.button() == QtCore.Qt.LeftButton:
            path = self.path_getter()
            if animated_path(path):
                open_external(path)
                return True
        return False
