"""Shared local mask painter. ComfyUI MASK = 1 - PNG alpha; RGB is preserved."""
import os
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from PyQt5 import QtCore, QtGui, QtWidgets

MAX_PIXELS = 32_000_000
HISTORY_BYTES = 64 * 1024 * 1024


def _qimage(image):
    rgba = image.convert('RGBA')
    return QtGui.QImage(rgba.tobytes(), rgba.width, rgba.height, rgba.width * 4,
                        QtGui.QImage.Format_RGBA8888).copy()


def mask_alpha(image):
    rgba = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    bits = rgba.constBits();bits.setsize(rgba.byteCount())
    return Image.frombytes('RGBA', (rgba.width(), rgba.height()), bytes(bits),
                           'raw', 'RGBA', rgba.bytesPerLine()).getchannel('A')


class MaskCanvas(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.filename = Path(path).stem + '.png'
        with Image.open(path) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('图像超过 3200 万像素，请先缩小后绘制遮罩。')
            if getattr(source, 'n_frames', 1) > 1:
                raise ValueError('遮罩编辑仅支持静态图像，请先导出需要编辑的帧。')
            original = ImageOps.exif_transpose(source).convert('RGBA')
            self.rgb = original.convert('RGB')
            layer = Image.new('RGBA', original.size, (55, 191, 233, 255))
            layer.putalpha(ImageOps.invert(original.getchannel('A')))
            self.mask = _qimage(layer).convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        preview = self.rgb.copy();preview.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        self.base = _qimage(preview)
        self.scale = 1.0
        self.offset = QtCore.QPointF()
        self.diameter = 40
        self.erasing = False
        self.opacity = .5
        self._space = False
        self._pan = None
        self._last = None
        self._cursor = None
        self._before = None
        self._dirty = QtCore.QRect()
        self.undo_stack, self.redo_stack = [], []
        self.setMinimumSize(280, 220)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setObjectName('maskCanvas')

    def fit(self):
        self.scale = max(.01, min((self.width()-32)/self.mask.width(), (self.height()-32)/self.mask.height()))
        self.center();self.changed.emit()

    def center(self):
        self.offset = QtCore.QPointF((self.width()-self.mask.width()*self.scale)/2,
                                    (self.height()-self.mask.height()*self.scale)/2)
        self.update()

    def actual_size(self):
        self.scale = 1.;self.center();self.changed.emit()

    def image_point(self, position):
        return (QtCore.QPointF(position) - self.offset) / self.scale

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), getattr(self,'background',self.palette().color(QtGui.QPalette.Base)))
        rect = QtCore.QRectF(self.offset, QtCore.QSizeF(self.mask.width()*self.scale, self.mask.height()*self.scale))
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        painter.drawImage(rect, self.base)
        painter.setOpacity(self.opacity);painter.drawImage(rect, self.mask);painter.setOpacity(1)
        painter.setPen(QtGui.QPen(QtGui.QColor('#7a8a99'), 1));painter.drawRect(rect)
        if self._cursor is not None and not self._space:
            radius = self.diameter*self.scale/2
            painter.setPen(QtGui.QPen(QtGui.QColor('#111111'), 3))
            painter.drawEllipse(self._cursor, radius, radius)
            painter.setPen(QtGui.QPen(QtGui.QColor('#ffffff'), 1))
            painter.drawEllipse(self._cursor, radius, radius)

    def begin_stroke(self, point, erase=False):
        self._before = self.mask.copy()  # Only one full-size backup during a stroke.
        self._dirty = QtCore.QRect()
        self._last = point
        self._stroke_erase = erase
        self.stroke_to(point)

    def stroke_to(self, point):
        if self._before is None:return
        # Bound coordinates even when dragging outside the widget/image.
        radius = self.diameter/2 + 2
        point = QtCore.QPointF(max(-radius, min(self.mask.width()+radius, point.x())),
                              max(-radius, min(self.mask.height()+radius, point.y())))
        dirty = QtCore.QRectF(self._last, point).normalized().adjusted(-radius, -radius, radius, radius).toAlignedRect()
        self._dirty = self._dirty.united(dirty.intersected(self.mask.rect()))
        painter = QtGui.QPainter(self.mask)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        if self._stroke_erase:painter.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
        pen = QtGui.QPen(QtGui.QColor(55, 191, 233, 255), self.diameter, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        if self._last == point:painter.drawPoint(point)
        else:painter.drawLine(self._last, point)
        painter.end()
        self._last = point;self.update()

    def finish_stroke(self):
        if self._before is None:return
        if not self._dirty.isEmpty():
            rect = self._dirty
            self.undo_stack.append((rect, self._before.copy(rect), self.mask.copy(rect)))
            self.redo_stack.clear()
            self._trim_history()
        self._before = self._last = None
        self.changed.emit();self.update()

    def _trim_history(self):
        def size():return sum(a.byteCount()+b.byteCount() for _, a, b in self.undo_stack + self.redo_stack)
        while len(self.undo_stack)>30 or size()>HISTORY_BYTES:
            if self.undo_stack:self.undo_stack.pop(0)
            elif self.redo_stack:self.redo_stack.pop(0)
            else:break

    def _restore(self, stack, destination, after):
        self.finish_stroke()
        if not stack:return
        patch = stack.pop();rect, before, later = patch
        painter = QtGui.QPainter(self.mask);painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.drawImage(rect.topLeft(), later if after else before);painter.end()
        destination.append(patch);self.changed.emit();self.update()

    def undo(self):self._restore(self.undo_stack, self.redo_stack, False)
    def redo(self):self._restore(self.redo_stack, self.undo_stack, True)

    def transform_mask(self, invert=False):
        self.finish_stroke();self._before=self.mask.copy();self._dirty=self.mask.rect()
        if invert:
            layer=Image.new('RGBA', self.rgb.size, (55,191,233,255))
            layer.putalpha(ImageOps.invert(mask_alpha(self.mask)))
            self.mask=_qimage(layer).convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
        else:self.mask.fill(QtCore.Qt.transparent)
        self.finish_stroke()

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button()==QtCore.Qt.MiddleButton or (event.button()==QtCore.Qt.LeftButton and self._space):
            self._pan=event.localPos();return
        if event.button() in (QtCore.Qt.LeftButton, QtCore.Qt.RightButton):
            point=self.image_point(event.localPos())
            if QtCore.QRectF(self.mask.rect()).contains(point):
                self.begin_stroke(point, self.erasing or event.button()==QtCore.Qt.RightButton)

    def mouseMoveEvent(self, event):
        self._cursor=event.localPos()
        if self._pan is not None:
            self.offset+=event.localPos()-self._pan;self._pan=event.localPos()
        elif self._before is not None:self.stroke_to(self.image_point(event.localPos()))
        self.update()

    def mouseReleaseEvent(self, event):
        self._pan=None;self.finish_stroke()

    def leaveEvent(self, event):self._cursor=None;self.update()

    def wheelEvent(self, event):
        self.finish_stroke()
        point=self.image_point(event.posF())
        self.scale=max(.01,min(16.,self.scale*(1.15 if event.angleDelta().y()>0 else 1/1.15)))
        self.offset=event.posF()-point*self.scale
        self.changed.emit();self.update();event.accept()

    def keyPressEvent(self, event):
        if event.key()==QtCore.Qt.Key_Space:self._space=True;self.update();event.accept()
        else:super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key()==QtCore.Qt.Key_Space:self._space=False;self.update();event.accept()
        else:super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._space=False;self._pan=None;self.finish_stroke();super().focusOutEvent(event)

class MaskEditor(QtWidgets.QDialog):
    def __init__(self, path, parent=None, directory=None, mask=None):
        super().__init__(parent)
        self.setObjectName('maskEditor');self.setWindowTitle('图像输入 · 遮罩与绘画')
        self.directory=directory
        self.source_path=path
        self.initial_mask=dict(mask or {})
        self.imported_mask_path=self.initial_mask.get('import_path','')
        self.imported_paint_path=self.initial_mask.get('paint_import_path','')
        self.result_mask=None
        self.result_path=''
        from .mask_panel import build
        build(self,path,mask)
        dark=getattr(parent,'_theme_mode','dark')!='light'
        if parent is not None:
            root=parent.window();dark=getattr(root,'_theme_mode','dark')!='light'
        self._popup_theme = self.apply_theme
        self.apply_theme('dark' if dark else 'light')
        available=self.screen().availableGeometry()
        self.resize(min(1080,int(available.width()*.9)),min(800,int(available.height()*.9)))
        QtCore.QTimer.singleShot(0,self.canvas.fit)

    def apply_theme(self, mode):
        from .paths import current_dir
        from .rh_ui import palette as theme_palette
        dark = mode != 'light'
        colors = theme_palette(mode)
        bg,base,text,border,hover = (colors[key] for key in ('canvas','input','text','border','hover'))
        mode='dark' if dark else 'light'
        up=(Path(current_dir)/'icons'/f'ui-chevron-up-{mode}.svg').as_posix()
        down=(Path(current_dir)/'icons'/f'ui-chevron-down-{mode}.svg').as_posix()
        palette=self.palette();palette.setColor(QtGui.QPalette.Base,QtGui.QColor(base));self.canvas.setPalette(palette)
        self.canvas.background=QtGui.QColor(base)
        self.setStyleSheet(f'QDialog#maskEditor {{background:{bg};color:{text};}} QDialog#maskEditor QLabel {{color:{text};background:transparent;}} '
            f'QDialog#maskEditor QSlider {{background:transparent;}} QDialog#maskEditor QAbstractSpinBox {{background:{base};color:{text};border:1px solid {border};border-radius:5px;padding:0px;min-height:30px;}} '
            f'QDialog#maskEditor QAbstractSpinBox QLineEdit {{background:{base};color:{text};border:none;padding:2px;}} '
            f'QDialog#maskEditor QAbstractSpinBox::up-button {{subcontrol-origin:border;subcontrol-position:top right;width:22px;height:16px;background:{hover};border:none;}} '
            f'QDialog#maskEditor QAbstractSpinBox::down-button {{subcontrol-origin:border;subcontrol-position:bottom right;width:22px;height:16px;background:{hover};border:none;}} '
            f'QDialog#maskEditor QAbstractSpinBox::up-arrow {{image:url("{up}");width:12px;height:12px;}} QDialog#maskEditor QAbstractSpinBox::down-arrow {{image:url("{down}");width:12px;height:12px;}} '
            f'QDialog#maskEditor QPushButton {{background:{bg};color:{text};border:1px solid {border};border-radius:6px;padding:7px 11px;}} '
            f'QDialog#maskEditor QPushButton:hover {{background:{hover};}} QDialog#maskEditor QPushButton:checked {{background:#286b8c;color:white;border-color:#48bce4;}} '
            f'QDialog#maskEditor QPushButton:disabled {{color:#8190a1;}} '
            f'QDialog#maskEditor QTabBar::tab {{background:{base};color:{text};padding:9px 20px;border:1px solid {border};border-radius:5px;}} '
            f'QDialog#maskEditor QTabBar::tab:selected {{background:{hover};border-bottom:2px solid #48bce4;}} '
            f'QDialog#maskEditor QLineEdit {{background:{base};color:{text};border:1px solid {border};border-radius:4px;padding:3px;}}')
        self.canvas.update()

    def refresh(self):
        self.undo_button.setEnabled(bool(self.canvas.undo_stack));self.redo_button.setEnabled(bool(self.canvas.redo_stack))
        self.status.setText(f'{self.canvas.mask.width()} × {self.canvas.mask.height()}  ·  {self.canvas.scale*100:.0f}%')
        if hasattr(self,'brush_sliders'):
            with QtCore.QSignalBlocker(self.size):self.size.setValue(self.canvas.diameter)
            with QtCore.QSignalBlocker(self.brush_sliders['hardness']):self.brush_sliders['hardness'].setValue(round(self.canvas.hardness*100))
            with QtCore.QSignalBlocker(self.layer_selector):self.layer_selector.setCurrentIndex(1 if self.canvas.active_layer=='paint' else 0)

    def save(self):
        from .mask_assets import draft
        self.canvas.finish_stroke()
        try:
            self.result_mask=draft(mask_alpha(self.canvas.mask),self.source_path,rgb=self.canvas.rgb)
            if hasattr(self.canvas,'settings'):self.canvas.settings(self.result_mask)
            from .paths import current_dir
            owner=self.parentWidget().window() if self.parentWidget() else None
            directory=Path(getattr(owner,'input_dir',Path(current_dir)/'input'))/'masks'
            self.result_mask['path']=str(directory/(self.result_mask['sha256']+'.png'))
            if self.result_mask.get('paint_sha256'):
                self.result_mask['paint_path']=str(directory.parent/'paintings'/(self.result_mask['paint_sha256']+'.png'))
                if self.imported_paint_path:self.result_mask['paint_import_path']=self.imported_paint_path
            if self.imported_mask_path:self.result_mask['import_path']=self.imported_mask_path
            if self.initial_mask and all(self.result_mask.get(key)==self.initial_mask.get(key) for key in ('source','sha256','orientation','paint_png','import_path','paint_import_path')):
                self.result_mask=dict(self.initial_mask)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self,'遮罩保存失败',str(error));return
        self.accept()

    def import_mask(self):
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,'导入遮罩','','图像 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif);;所有文件 (*)')
        if not path:return
        labels=['自动（含 Alpha 时取反 Alpha，其余取亮度）','亮度（白色为选区）','Alpha（透明区域为选区）','红色通道','绿色通道','蓝色通道']
        label,ok=QtWidgets.QInputDialog.getItem(self,'遮罩通道','尺寸不同时自动缩放至原图，保留灰度、软边缘和笔触不透明度。',labels,0,False)
        if not ok:return
        try:
            from .mask_assets import import_mask
            mask=import_mask(path,self.canvas.rgb.size,['auto','luminance','alpha','red','green','blue'][labels.index(label)])
            self.canvas.finish_stroke()
            before=self.canvas.mask.copy()
            layer=Image.new('RGBA',mask.size,(55,191,233,255));layer.putalpha(mask.convert('L'))
            self.canvas.mask=_qimage(layer)
            self.imported_mask_path=path
            self.mask_path_display.setText(path)
            self.mask_path_display.setToolTip(path)
            self.canvas._overlay=None
            self.canvas.undo_stack.append((self.canvas.mask.rect(),before,self.canvas.mask.copy()))
            self.canvas.redo_stack.clear();self.canvas._trim_history();self.canvas.update();self.canvas.changed.emit()
        except Exception as error:QtWidgets.QMessageBox.warning(self,'遮罩导入失败',str(error))

    def import_paint(self):
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,'导入 RGBA 绘画层','','图像 (*.png *.webp *.jpg *.jpeg *.bmp *.tif *.tiff *.avif);;所有文件 (*)')
        if not path:return
        try:
            from .mask_assets import import_paint
            image=import_paint(path,self.canvas.rgb.size)
            self.canvas.replace_paint(image);self.imported_paint_path=path
            self.paint_path_display.setText(path);self.paint_path_display.setToolTip(path)
        except Exception as error:QtWidgets.QMessageBox.warning(self,'绘画图层导入失败',str(error))


def edit_mask(path, parent=None, mask=None):
    if not path or not Path(path).is_file():
        QtWidgets.QMessageBox.information(parent,'绘制遮罩','请先选择一张本地图像，再绘制遮罩。')
        return ''
    dialog=None
    try:
        dialog=MaskEditor(path,parent,mask=mask)
        return dialog.result_mask if dialog.exec_()==QtWidgets.QDialog.Accepted else None
    except Exception as error:
        QtWidgets.QMessageBox.warning(parent,'无法打开遮罩编辑器',str(error));return ''
    finally:
        if dialog is not None:dialog.deleteLater()


def add_mask_button(layout, editor, parent=None, mask=None, on_change=None):
    """Bind to the input's existing persistence/preview path, never task state."""
    button=QtWidgets.QPushButton('遮罩 / 绘画');button.setObjectName('rhMaskButton')
    button.setToolTip('编辑遮罩与 RGBA 绘画层；节点执行或应用提交时才保存图像文件')
    editor._aetherloom_mask=mask
    editor._mask_button=button
    def open_editor():
        value=edit_mask(editor.text().strip(),parent,getattr(editor,'_aetherloom_mask',None))
        if value:
            editor._aetherloom_mask=value
            if on_change:on_change(value)
            editor.editingFinished.emit()
    button.clicked.connect(open_editor);layout.addWidget(button)
    return button
