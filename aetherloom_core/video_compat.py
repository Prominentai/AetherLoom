"""Bounded, silent video preview for machines without usable Qt video codecs."""
import threading

from PyQt5 import QtCore, QtGui


_workers = set()


class _Decoder(QtCore.QThread):
    opened = QtCore.pyqtSignal(int, float)
    frame = QtCore.pyqtSignal(object, int)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, path, frame_size=(1280, 720)):
        super().__init__()
        self.path = path
        self.frame_size = frame_size
        self.condition = threading.Condition()
        self.command = None
        self.stopped = False

    def request(self, position=-1):
        with self.condition:
            self.command = position
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify()

    def run(self):
        import cv2
        capture = None
        try:
            capture = cv2.VideoCapture(self.path)
            if not capture.isOpened():
                raise ValueError('无法解码此视频')
            fps = capture.get(cv2.CAP_PROP_FPS)
            if not 0 < fps <= 1000:
                fps = 25.0
            width, height = capture.get(cv2.CAP_PROP_FRAME_WIDTH), capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
            if width * height > 32_000_000:
                raise ValueError('视频分辨率过大，请使用系统打开')
            duration = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) / fps * 1000))
            self.opened.emit(duration, fps)
            while True:
                with self.condition:
                    while self.command is None and not self.stopped:
                        self.condition.wait()
                    if self.stopped:
                        return
                    position, self.command = self.command, None
                if position >= 0:
                    capture.set(cv2.CAP_PROP_POS_MSEC, position)
                ok, frame = capture.read()
                if not ok:
                    self.frame.emit(QtGui.QImage(), duration)
                    continue
                h, w = frame.shape[:2]
                scale = min(1.0, self.frame_size[0] / w, self.frame_size[1] / h)
                if scale < 1:
                    frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = QtGui.QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QtGui.QImage.Format_RGB888).copy()
                timestamp = int(capture.get(cv2.CAP_PROP_POS_FRAMES) / fps * 1000)
                if not self.stopped:
                    self.frame.emit(image, min(duration, timestamp))
        except Exception as exc:
            if not self.stopped:
                self.failed.emit(str(exc))
        finally:
            if capture is not None:
                capture.release()


class SilentVideoPlayer(QtCore.QObject):
    """At most one decoded frame in flight; no original video is buffered."""
    StoppedState, PlayingState, PausedState = 0, 1, 2
    LoadedMedia, EndOfMedia, InvalidMedia = 3, 7, 8
    stateChanged = QtCore.pyqtSignal(int)
    durationChanged = QtCore.pyqtSignal(int)
    positionChanged = QtCore.pyqtSignal(int)
    seekableChanged = QtCore.pyqtSignal(bool)
    mediaStatusChanged = QtCore.pyqtSignal(int)
    failed = QtCore.pyqtSignal(str)
    frameReady = QtCore.pyqtSignal(object)

    def __init__(self, parent=None, frame_size=(1280, 720)):
        super().__init__(parent)
        self.worker = None
        self.frame_size = frame_size
        self._position = self._duration = self._state = 0
        self._status = 0
        self._busy = False
        self._seek = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._read)
        self.start_timer = QtCore.QTimer(self)
        self.start_timer.setInterval(25)
        self.start_timer.timeout.connect(self._start_pending)
        self._pending_path = ''

    def open(self, path):
        self.release()
        self._pending_path = path
        self._start_pending()

    def _start_pending(self):
        if len(_workers) >= 2:
            self.start_timer.start()
            return
        self.start_timer.stop()
        if not self._pending_path:
            return
        self.worker = worker = _Decoder(self._pending_path, self.frame_size)
        self._pending_path = ''
        _workers.add(worker)
        worker.finished.connect(lambda: _workers.discard(worker))
        worker.opened.connect(self._opened)
        worker.frame.connect(self._frame)
        worker.failed.connect(self._failed)
        worker.start()

    def _opened(self, duration, fps):
        if self.sender() is not self.worker:
            return
        self._duration = duration
        self.timer.setInterval(max(1, round(1000 / fps)))
        self.durationChanged.emit(duration)
        self.seekableChanged.emit(True)
        self._status = self.LoadedMedia
        self.mediaStatusChanged.emit(self._status)
        self.play()

    def _read(self):
        if self.worker is not None and not self._busy:
            self._busy = True
            position, self._seek = self._seek, None
            self.worker.request(-1 if position is None else position)

    def _frame(self, image, position):
        if self.sender() is not self.worker:
            return
        self._busy = False
        if self._seek is not None:
            self._read()
            return
        self._position = position
        self.positionChanged.emit(position)
        if image.isNull():
            self.pause()
            self._status = self.EndOfMedia
            self.mediaStatusChanged.emit(self._status)
        else:
            self.frameReady.emit(image)

    def _failed(self, message):
        if self.sender() is self.worker:
            self.pause()
            self.failed.emit(message)

    def play(self):
        if self._status == self.EndOfMedia:
            self._seek = 0
        self._status = self.LoadedMedia
        self._state = self.PlayingState
        self.stateChanged.emit(self._state)
        self.timer.start()
        self._read()

    def pause(self):
        self.timer.stop()
        self._state = self.PausedState
        self.stateChanged.emit(self._state)

    def setPosition(self, position):
        self._seek = min(max(0, position), max(0, self._duration - 1))
        self._read()

    def state(self): return self._state
    def position(self): return self._position
    def duration(self): return self._duration
    def mediaStatus(self): return self._status
    def isSeekable(self): return self._duration > 0
    def setVolume(self, value): pass

    def release(self):
        self.timer.stop()
        self.start_timer.stop()
        self._pending_path = ''
        if self.worker:
            self.worker.stop()
            self.worker = None
        self._busy = False
        self._seek = None
        self._state = self.StoppedState
        self._position = self._duration = self._status = 0
        self.stateChanged.emit(self._state)


def shutdown_decoders():
    workers = list(_workers)
    for worker in workers:
        worker.stop()
    for worker in workers:
        worker.wait()
