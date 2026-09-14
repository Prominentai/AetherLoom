"""Bounded two-image comparison; no full-resolution pixmap or player cache."""
from PyQt5 import QtCore, QtGui, QtWidgets


class CompareSurface(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.images = [];self.position = .5;self.side_by_side = False;self.zoom = 1.0
        self.setToolTip('拖动分隔线对比；Ctrl + 滚轮同步缩放，双击恢复适应尺寸。')
        self.setMinimumHeight(160)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if len(self.images) != 2:
            painter.setPen(self.palette().text().color());painter.drawText(self.rect(), QtCore.Qt.AlignCenter, '连接两张图像并运行后对比');return
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

    def load_pair(self, *_):
        self.surface.images = []
        index = max(0,self.pairs.currentIndex())*2
        for result in self.results[index:index+2]:
            reader = QtGui.QImageReader(str(result.get('path') or ''));reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.width()*size.height() > 40_000_000:
                self.surface.images = [];self.surface.setToolTip('对比预览上限为 4000 万像素，请先缩放图像');break
            if size.isValid():reader.setScaledSize(size.scaled(1200,1200,QtCore.Qt.KeepAspectRatio))
            image = reader.read()
            if image.isNull():self.surface.images = [];break
            self.surface.images.append(image)
        self.surface.update()
