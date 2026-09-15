"""Bounded, display-only image/paint/mask composition for input surfaces."""
import os
from PyQt5 import QtCore, QtGui, QtWidgets, sip

_pool = None
_jobs = set()


def render_input(path, mask=None):
    from PIL import Image, ImageOps
    from . import mask_assets as assets
    base = None
    usable = mask if mask and (not path or assets.matches(mask, path)) else None
    if path and os.path.isfile(path):
        with Image.open(path) as source:
            if source.width * source.height > assets.MAX_PIXELS:
                raise ValueError('图像超过预览尺寸限制')
            base = ImageOps.exif_transpose(source).convert('RGBA')
        if usable and usable.get('orientation'):
            base = base.transpose(Image.Transpose(int(usable['orientation']) - 1))
        base.thumbnail((768, 768), Image.Resampling.LANCZOS)
    coverage = assets.read(usable, base.size if base else None) if usable else assets.image_mask(base) if base else None
    if base is None:
        if coverage is None:return QtGui.QImage()
        coverage.thumbnail((768, 768), Image.Resampling.LANCZOS)
        base = coverage.convert('RGBA')
    else:
        base = base.convert('RGB').convert('RGBA')
        paint = assets.read_paint(usable, base.size) if usable else None
        if paint is not None:base = Image.alpha_composite(base, paint)
        if coverage is not None:
            overlay = Image.new('RGBA', base.size, (88, 180, 255, 0))
            overlay.putalpha(coverage.point(lambda value: round(value * .5)))
            base = Image.alpha_composite(base, overlay)
    data = base.tobytes()
    return QtGui.QImage(data, base.width, base.height, base.width * 4, QtGui.QImage.Format_RGBA8888).copy()


class _Signals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object, str)


class _Render(QtCore.QRunnable):
    def __init__(self, signals, path, mask):
        super().__init__();self.signals, self.path, self.mask = signals, path, mask

    def run(self):
        try:self.signals.finished.emit(render_input(self.path, self.mask), '')
        except Exception:self.signals.finished.emit(QtGui.QImage(), '无法预览，文件可能已移动或格式不受支持')


class ImageInputPreview(QtWidgets.QLabel):
    browse_requested = QtCore.pyqtSignal()
    edit_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('imageInputSurface')
        self.setMinimumSize(80, 220)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._image = QtGui.QImage();self._request = None;self._loaded = None;self._busy = False
        self._last_path = '';self._orig_pixmap = None;self._error = ''
        self.setToolTip('拖入或粘贴图像 / 文件夹 · 空白处单击导入 · 双击编辑遮罩\n蓝色半透明区域表示遮罩；原图与上传内容不会被预览修改。')

    def sizeHint(self):return QtCore.QSize(320, 240)

    def set_colors(self, colors):
        self._surface_colors = dict(colors);self.update()

    def set_input(self, path='', mask=None):
        path = str(path or '')
        try:revision = (os.stat(path).st_mtime_ns, os.stat(path).st_size)
        except OSError:revision = None
        request = (path, mask, revision)
        if request == self._request:return
        self._request = request;self._last_path = path;self._error = ''
        self._image = QtGui.QImage();self._orig_pixmap = None
        self.update();self._start()

    def _start(self):
        global _pool
        if self._busy or not self.isVisible() or self._request == self._loaded:return
        if not self._request:return
        request = self._request
        if not request[0] and not request[1]:self._loaded = request;return
        if os.path.isdir(request[0]):self._error = '文件夹输入 · 运行时读取匹配图像';self._loaded = request;self.update();return
        if _pool is None:
            _pool = QtCore.QThreadPool(QtWidgets.QApplication.instance());_pool.setMaxThreadCount(2)
        self._busy = True
        signals = _Signals();_jobs.add(signals)
        def finish(image, error):
            _jobs.discard(signals);signals.deleteLater()
            if sip.isdeleted(self):return
            self._busy = False
            if self._request == request:
                self._loaded = request;self._image = image;self._error = error
                self._orig_pixmap = QtGui.QPixmap.fromImage(image) if not image.isNull() else None
                self.update()
            self._start()
        signals.finished.connect(finish)
        _pool.start(_Render(signals, request[0], request[1]))

    def showEvent(self, event):
        super().showEvent(event);self._start()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self);painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        colors = getattr(self, '_surface_colors', {})
        painter.setBrush(QtGui.QColor(colors['input']) if colors else self.palette().color(QtGui.QPalette.Base))
        painter.setPen(QtGui.QPen(QtGui.QColor(colors['border']) if colors else self.palette().color(QtGui.QPalette.Mid), 1))
        painter.drawRoundedRect(rect, 8, 8)
        if not self._image.isNull():
            area = rect.adjusted(8, 8, -8, -8)
            size = QtCore.QSizeF(self._image.size());size.scale(area.size(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRectF(QtCore.QPointF(), size);target.moveCenter(area.center())
            painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform);painter.drawImage(target, self._image)
        else:
            painter.setPen(QtGui.QColor(colors['muted']) if colors else self.palette().color(QtGui.QPalette.PlaceholderText))
            text = self._error or ('正在预览…' if self._busy else '拖入图像或文件夹\n点击导入 · Ctrl+V 粘贴')
            painter.drawText(rect.adjusted(12, 12, -12, -12), QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, text)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._image.isNull() and not self._last_path:self.browse_requested.emit()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._last_path:
            self.edit_requested.emit();event.accept();return
        super().mouseDoubleClickEvent(event)
