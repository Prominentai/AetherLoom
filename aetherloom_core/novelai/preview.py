"""Zoomable, bounded image preview with a half-opacity mask overlay."""
import io
import os
from pathlib import Path
from PIL import Image, ImageOps
from PyQt5 import QtCore, QtGui, QtWidgets
from .jobs import Job


def preview_bytes(path, mask=None):
    if Path(path).stat().st_size > 64 * 1024 * 1024:
        raise ValueError('图片超过 64 MB')
    with Image.open(path) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError('图片超过 4000 万像素')
        source.draft('RGB', (2200, 2200))
        image = ImageOps.exif_transpose(source).convert('RGBA')
    if mask:
        from aetherloom_core import mask_assets
        orientation = int(mask.get('orientation', 0))
        if orientation:
            image = image.transpose(Image.Transpose(orientation - 1))
        paint = mask_assets.read_paint(mask, image.size)
        if paint is not None:
            image = Image.alpha_composite(image, paint)
        selection = mask_assets.read(mask, image.size)
        layer = Image.new('RGBA', image.size, (60, 171, 239, 0))
        layer.putalpha(selection.point(lambda value: round(value * .5)))
        image = Image.alpha_composite(image, layer)
    image.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


class ImagePreview(QtWidgets.QGraphicsView):
    filesDropped = QtCore.pyqtSignal(list)
    statusChanged = QtCore.pyqtSignal(str)
    imageChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('novelaiPreview')
        self.setAccessibleName('NovelAI 图像预览')
        self.setToolTip('拖入或粘贴图片作为底图；滚轮缩放，拖动平移，双击打开图片。')
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setMinimumSize(180, 140)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self._picture = None
        self._empty = QtWidgets.QWidget(self.viewport())
        self._empty.setObjectName('novelaiPreviewEmpty')
        self._empty.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        empty_layout = QtWidgets.QVBoxLayout(self._empty)
        empty_layout.setContentsMargins(20, 16, 20, 16)
        empty_layout.setSpacing(12)
        empty_layout.addStretch(1)
        self._empty_mark = QtWidgets.QLabel()
        self._empty_mark.setAlignment(QtCore.Qt.AlignCenter)
        empty_layout.addWidget(self._empty_mark)
        self._empty_title = QtWidgets.QLabel('预览你的作品')
        self._empty_title.setObjectName('novelaiEmptyTitle')
        self._empty_text = QtWidgets.QLabel()
        self._empty_text.setObjectName('novelaiEmptyText')
        self._empty_hint = QtWidgets.QLabel('滚轮缩放 · 拖动平移 · 双击打开')
        self._empty_hint.setObjectName('novelaiEmptyHint')
        for label in (self._empty_title, self._empty_text, self._empty_hint):
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setWordWrap(True)
            empty_layout.addWidget(label)
        empty_layout.addStretch(1)
        self._path = ''
        self._version = 0
        self._closing = False
        self._jobs = set()
        self._pending_load = None
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._load_next)
        self._fit = True
        self._mode = 'dark'
        self.set_empty()
        from aetherloom_core.image_import import ImageDropFilter
        self._drop_filter = ImageDropFilter(self.viewport(), self.filesDropped.emit)

    def set_empty(self, message='从左侧开始生成，或拖入图片作为底图\n也可使用 Ctrl+V 粘贴图片'):
        self._version += 1
        self._pending_load = None
        self._debounce.stop()
        self._fit = True
        self.scene().clear()
        self._picture = None
        self._path = ''
        self.resetTransform()
        self.scene().setSceneRect(QtCore.QRectF())
        self._empty_text.setText(message)
        self._empty.setGeometry(self.viewport().rect())
        self._empty.show()
        self._empty.raise_()
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.viewport().update()
        self.imageChanged.emit()

    def snapshot_pixmap(self):
        """Share the already bounded GUI image; never read or decode the file again."""
        return self._picture.pixmap() if self._picture is not None else QtGui.QPixmap()

    def load_path(self, path, mask=None):
        if self._closing:
            return
        path = os.fspath(path)
        if self._picture is None or path != self._path:
            self.set_empty('正在读取图片…')
        self._version += 1
        self._pending_load = (self._version, path, mask)
        self._debounce.start(60)

    def _load_next(self):
        if self._closing or self._jobs or self._pending_load is None:
            return
        version, path, mask = self._pending_load
        self._pending_load = None
        job = Job(lambda unused: preview_bytes(path, mask), self)
        self._jobs.add(job)
        def done(raw):
            if not self._closing and version == self._version:
                self.set_bytes(raw, path)
        def failed(error):
            if not self._closing and version == self._version:
                self.set_empty('无法读取图片，请选择其他图片。')
                self.statusChanged.emit(str(error))
        job.succeeded.connect(done)
        job.failed.connect(failed)
        job.finished.connect(lambda: (self._jobs.discard(job), job.deleteLater(), self._load_next()))
        job.start()

    def set_bytes(self, raw, path=''):
        if self._closing:
            return
        self._version += 1
        self._pending_load = None
        buffer = QtCore.QBuffer()
        buffer.setData(raw)
        buffer.open(QtCore.QIODevice.ReadOnly)
        reader = QtGui.QImageReader(buffer)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 40_000_000:
            return
        if max(size.width(), size.height()) > 2200:
            reader.setScaledSize(size.scaled(2200, 2200, QtCore.Qt.KeepAspectRatio))
        image = reader.read()
        if image.isNull() or image.width() * image.height() > 40_000_000:
            return
        if max(image.width(), image.height()) > 2200:
            image = image.scaled(2200, 2200, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        pixmap = QtGui.QPixmap.fromImage(image)
        # Empty-path frames are updates to the current stream. A new source or
        # the first image after set_empty starts with the whole image visible.
        new_image = self._picture is None or path != self._path
        if self._picture is None:
            self.scene().clear()
            self._picture = self.scene().addPixmap(pixmap)
        else:
            self._picture.setPixmap(pixmap)
        self._path = path
        self._empty.hide()
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.scene().setSceneRect(self._picture.boundingRect())
        if new_image or self._fit:
            self.fit()
        self.imageChanged.emit()

    def fit(self):
        self._fit = True
        if self._picture is not None:
            self.fitInView(self.sceneRect().adjusted(-12, -12, 12, 12), QtCore.Qt.KeepAspectRatio)

    def actual(self):
        if self._picture is None:
            return
        self._fit = False
        self.resetTransform()

    def wheelEvent(self, event):
        if self._picture is None:
            event.accept()
            return
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not delta:
            event.accept()
            return
        factor = 1.15 ** (delta / 120)
        scale = self.transform().m11() * factor
        if .025 < scale < 16:
            self._fit = False
            self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._empty.setGeometry(self.viewport().rect())
        self._empty_hint.setVisible(self.viewport().height() >= 250)
        self._empty_mark.setVisible(self.viewport().height() >= 300)
        if self._fit:
            self.fit()

    def drawBackground(self, painter, rect):
        from .styles import workspace_palette
        painter.fillRect(rect, QtGui.QColor(workspace_palette(self._mode)['canvas']))
        if self._picture is not None:
            # Only transparent image pixels need a checkerboard, not the whole workspace.
            painter.save()
            painter.setClipRect(self._picture.boundingRect())
            painter.fillRect(rect, self.backgroundBrush())
            painter.restore()

    def mouseDoubleClickEvent(self, event):
        if self._path and os.path.isfile(self._path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._path))
        else:
            self.fit()
        event.accept()

    def apply_theme(self, mode):
        from .styles import workspace_palette as palette
        self._mode = mode
        p = palette(mode)
        tile = QtGui.QPixmap(24, 24)
        tile.fill(QtGui.QColor(p['canvas']))
        painter = QtGui.QPainter(tile)
        painter.fillRect(0, 0, 12, 12, QtGui.QColor(p['input']))
        painter.fillRect(12, 12, 12, 12, QtGui.QColor(p['input']))
        painter.end()
        self.setBackgroundBrush(QtGui.QBrush(tile))
        mark = QtGui.QPixmap(56, 56)
        mark.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(mark)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(p['accent']), 1.6))
        painter.setBrush(QtGui.QColor(p['accent_soft']))
        painter.drawRoundedRect(QtCore.QRectF(5, 7, 46, 42), 9, 9)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawEllipse(QtCore.QPointF(36, 21), 4, 4)
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(13, 40), QtCore.QPointF(23, 28),
                                              QtCore.QPointF(30, 35), QtCore.QPointF(35, 30),
                                              QtCore.QPointF(43, 40)]))
        painter.end()
        self._empty_mark.setPixmap(mark)
        self.viewport().update()

    def shutdown(self):
        self._closing = True
        self._version += 1
        self._pending_load = None
        self._debounce.stop()
        self.scene().clear()
        self._picture = None
        self._path = ''
        for job in self._jobs:
            job.cancel()
