"""Bounded two-image comparison; no full-resolution pixmap or player cache."""
from PyQt5 import QtCore, QtGui, QtWidgets


class CompareSurface(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.images = [];self.position = .5;self.side_by_side = False;self.zoom = 1.0
        self.empty_message = '连接两张图像并运行后对比'
        self.setToolTip('拖动分隔线对比；Ctrl + 滚轮同步缩放，双击恢复适应尺寸。')
        self.setMinimumHeight(160)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if len(self.images) != 2:
            painter.setPen(self.palette().text().color());painter.drawText(self.rect().adjusted(10,10,-10,-10), QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, self.empty_message);return
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        for index, image in enumerate(self.images):
            rect = QtCore.QRectF(self.rect())
            if self.side_by_side:rect = QtCore.QRectF(index*self.width()/2, 0, self.width()/2, self.height())
            size = QtCore.QSizeF(image.size());size.scale(rect.size(), QtCore.Qt.KeepAspectRatio)
            size *= self.zoom
            target = QtCore.QRectF(rect.center()-QtCore.QPointF(size.width()/2,size.height()/2), size)
            painter.save()
            painter.setClipRect(rect)
            if not self.side_by_side:
                split = self.width()*self.position
                painter.setClipRect(QtCore.QRectF(0,0,split,self.height()) if index == 0
                                    else QtCore.QRectF(split,0,self.width()-split,self.height()), QtCore.Qt.IntersectClip)
            from .result_browser import paint_checkerboard
            paint_checkerboard(painter,target,{'input':self.palette().base().color().name()})
            painter.drawImage(target, image);painter.restore()
        if not self.side_by_side:
            painter.setPen(QtGui.QPen(self.palette().highlight().color(),2));painter.drawLine(int(self.width()*self.position),0,int(self.width()*self.position),self.height())

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.LeftButton:self.set_position(event.x()/max(1,self.width()))

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:self.set_position(event.x()/max(1,self.width()))

    def set_position(self, value):
        self.position = max(0,min(1,value));self.update()

    def wheelEvent(self, event):
        if not event.modifiers() & QtCore.Qt.ControlModifier:event.ignore();return
        self.zoom = max(.25,min(8,self.zoom * (1.15 if event.angleDelta().y()>0 else 1/1.15)))
        self.update();event.accept()

    def mouseDoubleClickEvent(self, event):
        self.zoom = 1.0;self.position = .5;self.update();event.accept()


class ImageCompare(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        from aetherloom_core.rh_parameters import RhEnumComboBox
        self.results = [];self.signature = None
        from .graphics import ThumbnailCache
        application = QtWidgets.QApplication.instance()
        self.cache = getattr(application, '_canvas_compare_previews', None)
        if self.cache is None:
            self.cache = ThumbnailCache(application, limit=8, image_size=(1024,1024))
            application._canvas_compare_previews = self.cache
            application.aboutToQuit.connect(self.cache.close)
        self.cache.ready.connect(self._ready)
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(5)
        row = QtWidgets.QHBoxLayout()
        self.pairs = RhEnumComboBox();self.pairs.setToolTip('选择对应批量输入的图像对')
        self.mode = RhEnumComboBox();self.mode.addItems(['滑动对比', '并排对比'])
        row.addWidget(self.pairs,1);row.addWidget(self.mode,1);layout.addLayout(row)
        self.surface = CompareSurface(self);layout.addWidget(self.surface,1)
        self.pairs.currentIndexChanged.connect(self.load_pair)
        self.mode.currentIndexChanged.connect(self.change_mode)

    def change_mode(self, value):
        self.surface.side_by_side = bool(value);self.surface.update()

    def set_results(self, results):
        signature = tuple((r.get('path'),r.get('index')) for r in results)
        if signature == self.signature:return
        self.signature = signature;self.results = results
        old = self.pairs.currentIndex()
        with QtCore.QSignalBlocker(self.pairs):
            self.pairs.clear()
            self.pairs.addItems(['第 %s 对 · A / B' % (i+1) for i in range(len(results)//2)])
            self.pairs.setCurrentIndex(max(0,min(old,self.pairs.count()-1)))
        if self.isVisible():self.load_pair()

    def showEvent(self, event):
        super().showEvent(event);self.load_pair()

    def hideEvent(self, event):
        self.surface.images = [];super().hideEvent(event)

    def _ready(self):
        if self.isVisible():self.load_pair()

    def load_pair(self, *_):
        if not self.isVisible():return
        import os
        index = max(0,self.pairs.currentIndex())*2
        pair = self.results[index:index+2]
        def revision(value):
            path = str(value.get('path') or '')
            try:
                stat = os.stat(path);return path,stat.st_size,stat.st_mtime_ns
            except OSError:return path,None,None
        revision_key = tuple(revision(value) for value in pair)
        if len(self.surface.images)==2 and getattr(self,'_loaded_pair',None)==revision_key:return
        self._loaded_pair = revision_key
        self.surface.images = []
        self.surface.empty_message = '连接两张图像并运行后对比'
        for result in pair:
            path = str(result.get('path') or '')
            image = self.cache.get(path)
            if image is None:
                self.surface.empty_message = ('对比文件已移动或删除' if not os.path.isfile(path) else
                    '无法生成对比预览，请检查文件或先缩小图像' if self.cache.failed(path,'image') else '正在加载对比预览…')
                continue
            self.surface.images.append(image.toImage())
        self.surface.update()
