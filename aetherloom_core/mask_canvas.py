"""Layered mask tools, matching ComfyUI's editing and navigation conventions."""
import base64
import io
import math
import hashlib
import cv2
import numpy as np
from PIL import Image
from PyQt5 import QtCore,QtGui
from .mask_editor import MaskCanvas, _qimage, mask_alpha


def pil(image):
    rgba=image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    bits=rgba.constBits();bits.setsize(rgba.byteCount())
    return Image.frombytes('RGBA',(rgba.width(),rgba.height()),bytes(bits),'raw','RGBA',rgba.bytesPerLine())


def oriented(image,orientation):
    return image.transpose(Image.Transpose(orientation-1)) if orientation else image.copy()


def next_orientation(current,operation):
    source=Image.fromarray(np.arange(6,dtype=np.uint8).reshape(2,3))
    target=oriented(source,current).transpose(operation)
    return next(value for value in range(8) if oriented(source,value).size==target.size and oriented(source,value).tobytes()==target.tobytes())


class AdvancedMaskCanvas(MaskCanvas):
    def __init__(self,path,parent=None):
        super().__init__(path,parent)
        self.tool='mask';self.active_layer='mask';self.paint_layer=QtGui.QImage()
        self.orientation=0;self.hardness=1.;self.brush_opacity=1.;self.spacing=.1;self.shape='round'
        self.color=QtGui.QColor('#ef4444');self.mask_color=QtGui.QColor('#37bfe9')
        self.show_base=True;self.show_paint=True;self.show_mask=True;self.blend='color'
        self.tolerance=30;self.color_mode='HSL';self._adjust=None;self._dab=None;self._overlay=None

    def restore_config(self,config):
        from .mask_assets import read,read_paint
        self.orientation=int(config.get('orientation',0))
        self.rgb=oriented(self.rgb,self.orientation)
        mask=read(config,self.rgb.size).convert('L')
        layer=Image.new('RGBA',mask.size,(55,191,233,255));layer.putalpha(mask);self.mask=_qimage(layer)
        paint=read_paint(config,self.rgb.size)
        if paint is not None:self.paint_layer=_qimage(paint)
        self._refresh_base()

    def settings(self,config):
        if self.orientation:config['orientation']=self.orientation
        if not self.paint_layer.isNull():
            stream=io.BytesIO();pil(self.paint_layer).save(stream,format='PNG')
            config['paint_png']=base64.b64encode(stream.getvalue()).decode('ascii')
            config['paint_sha256']=hashlib.sha256(stream.getvalue()).hexdigest()

    def replace_paint(self,image):
        self.finish_stroke()
        if self.paint_layer.isNull():
            self.paint_layer=QtGui.QImage(self.mask.size(),QtGui.QImage.Format_ARGB32_Premultiplied);self.paint_layer.fill(QtCore.Qt.transparent)
        before=self.paint_layer.copy()
        self.paint_layer=_qimage(image)
        self.undo_stack.append((self.mask.rect(),before,self.paint_layer.copy(),'paint'))
        self.redo_stack.clear();self._trim_history();self.changed.emit();self.update()

    def clear_paint(self):
        if not self.paint_layer.isNull():self.replace_paint(Image.new('RGBA',self.rgb.size,(0,0,0,0)))

    def _refresh_base(self):
        preview=self.rgb.copy();preview.thumbnail((1800,1800),Image.Resampling.LANCZOS)
        self.base=_qimage(preview);self._overlay=None;self.update()

    def _target(self):return self.paint_layer if self._stroke_layer=='paint' else self.mask

    def begin_stroke(self,point,erase=False):
        self._stroke_layer='paint' if self.tool=='paint' or self.tool=='erase' and self.active_layer=='paint' else 'mask'
        if self._stroke_layer=='paint' and self.paint_layer.isNull():
            self.paint_layer=QtGui.QImage(self.mask.size(),QtGui.QImage.Format_ARGB32_Premultiplied);self.paint_layer.fill(QtCore.Qt.transparent)
        self._before=self._target().copy();self._dirty=QtCore.QRect();self._last=point;self._dab=None
        self._stroke_erase=erase;self.stroke_to(point)

    def stroke_to(self,point):
        if self._before is None:return
        radius=self.diameter/2+2
        point=QtCore.QPointF(max(-radius,min(self.mask.width()+radius,point.x())),max(-radius,min(self.mask.height()+radius,point.y())))
        dirty=QtCore.QRectF(self._last,point).normalized().adjusted(-radius,-radius,radius,radius).toAlignedRect()
        self._dirty=self._dirty.united(dirty.intersected(self.mask.rect()))
        start=self._dab or self._last;delta=point-start;distance=math.hypot(delta.x(),delta.y());step=max(1.,self.diameter*self.spacing)
        points=[point] if self._dab is None else [start+delta*(i*step/distance) for i in range(1,int(distance/step)+1)] if distance else []
        painter=QtGui.QPainter(self._target());painter.setRenderHint(QtGui.QPainter.Antialiasing);painter.setPen(QtCore.Qt.NoPen)
        if self._stroke_erase:painter.setCompositionMode(QtGui.QPainter.CompositionMode_DestinationOut)
        color=QtGui.QColor(self.color if self._stroke_layer=='paint' else self.mask_color);color.setAlphaF(color.alphaF()*self.brush_opacity)
        radius=self.diameter/2
        for position in points:
            if self.hardness<.999:
                gradient=QtGui.QRadialGradient(position,radius);gradient.setColorAt(0,color);gradient.setColorAt(max(.001,self.hardness),color)
                transparent=QtGui.QColor(color);transparent.setAlpha(0);gradient.setColorAt(1,transparent);painter.setBrush(gradient)
            else:painter.setBrush(color)
            if self.shape=='square':painter.drawRect(QtCore.QRectF(position.x()-radius,position.y()-radius,self.diameter,self.diameter))
            else:painter.drawEllipse(position,radius,radius)
            self._dab=position
        painter.end();self._last=point;self._overlay=None;self.update()

    def finish_stroke(self):
        if self._before is None:return
        rect=self._dirty
        if not rect.isEmpty():
            self.undo_stack.append((rect,self._before.copy(rect),self._target().copy(rect),self._stroke_layer))
            self.redo_stack.clear();self._trim_history()
        self._before=self._last=None;self._overlay=None;self.changed.emit();self.update()

    def _restore(self,stack,destination,after):
        self.finish_stroke()
        if not stack:return
        from .mask_history import restore
        entry=stack[-1];rect,before,later=entry[:3];layer=entry[3] if len(entry)>3 else 'mask'
        try:value=restore(later if after else before)
        except (OSError,MemoryError) as error:
            self.history_warning='无法读取撤销记录：'+str(error);self.changed.emit();return
        if layer=='all':
            self.rgb,self.mask,self.paint_layer,self.orientation=value[0].copy(),value[1].copy(),value[2].copy(),value[3]
            self._refresh_base();self.fit()
        else:
            target=self.paint_layer if layer=='paint' else self.mask
            if rect==target.rect():
                if layer=='paint':self.paint_layer=value.copy()
                else:self.mask=value.copy()
            else:
                painter=QtGui.QPainter(target);painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source);painter.drawImage(rect.topLeft(),value);painter.end()
        stack.pop();destination.append(entry);self.history_warning='';self._overlay=None;self.changed.emit();self.update()

    def transform_mask(self,invert=False):
        self.finish_stroke();self._stroke_layer='mask';super().transform_mask(invert);self._overlay=None

    def transform_all(self,operation):
        self.finish_stroke()
        before=(self.rgb.copy(),self.mask.copy(),self.paint_layer.copy(),self.orientation)
        self.rgb=self.rgb.transpose(operation);self.mask=_qimage(pil(self.mask).transpose(operation))
        if not self.paint_layer.isNull():self.paint_layer=_qimage(pil(self.paint_layer).transpose(operation))
        self.orientation=next_orientation(self.orientation,operation)
        after=(self.rgb.copy(),self.mask.copy(),self.paint_layer.copy(),self.orientation)
        self.undo_stack.append((None,before,after,'all'));self.redo_stack.clear();self._trim_history();self._refresh_base();self.fit();self.changed.emit()

    def color_select(self,point,connected=False,erase=False):
        x,y=int(point.x()),int(point.y());rgb=self.rgb.convert('RGBA')
        if not self.paint_layer.isNull():rgb=Image.alpha_composite(rgb,pil(self.paint_layer))
        pixels=np.asarray(rgb.convert('RGB')).copy();height,width=pixels.shape[:2]
        if not(0<=x<width and 0<=y<height):return
        tolerance=int(self.tolerance)
        if connected:
            area=np.zeros((height+2,width+2),np.uint8)
            cv2.floodFill(pixels,area,(x,y),(0,0,0),(tolerance,)*3,(tolerance,)*3,
                          4|cv2.FLOODFILL_FIXED_RANGE|cv2.FLOODFILL_MASK_ONLY|(255<<8))
            selected=area[1:-1,1:-1]>0
        else:
            if self.color_mode in ('HSL','LAB'):
                pixels=cv2.cvtColor(pixels,cv2.COLOR_RGB2HLS if self.color_mode=='HSL' else cv2.COLOR_RGB2LAB)
            reference=pixels[y,x].astype(np.int16);selected=np.empty((height,width),bool)
            for row in range(0,height,128):
                delta=np.abs(pixels[row:row+128].astype(np.int16)-reference)
                if self.color_mode=='HSL':delta[:,:,0]=np.minimum(delta[:,:,0],180-delta[:,:,0])*255//90
                selected[row:row+128]=np.max(delta,axis=2)<=tolerance
        before=self.mask.copy();alpha=np.array(mask_alpha(self.mask));erase=erase or connected and alpha[y,x]>=128
        alpha[selected]=0 if erase else 255
        layer=Image.new('RGBA',self.rgb.size,(55,191,233,255));layer.putalpha(Image.fromarray(alpha));self.mask=_qimage(layer)
        self.undo_stack.append((self.mask.rect(),before,self.mask.copy(),'mask'));self.redo_stack.clear();self._trim_history()
        self._overlay=None;self.changed.emit();self.update()

    def paintEvent(self,event):
        painter=QtGui.QPainter(self);painter.fillRect(self.rect(),getattr(self,'background',self.palette().color(QtGui.QPalette.Base)))
        rect=QtCore.QRectF(self.offset,QtCore.QSizeF(self.mask.width()*self.scale,self.mask.height()*self.scale))
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        painter.fillRect(rect,QtGui.QBrush(QtGui.QColor('#81909d'),QtCore.Qt.Dense6Pattern))
        if self.show_base:painter.drawImage(rect,self.base)
        if self.show_paint and not self.paint_layer.isNull():painter.drawImage(rect,self.paint_layer)
        if self.show_mask:
            if self._overlay is None:
                color=self.mask_color if self.blend=='color' else QtGui.QColor('black' if self.blend=='black' else 'white')
                overlay=self.mask.scaled(self.base.size(),QtCore.Qt.KeepAspectRatio,QtCore.Qt.FastTransformation)
                p=QtGui.QPainter(overlay);p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn);p.fillRect(overlay.rect(),color);p.end();self._overlay=overlay
            if self.blend=='negative':painter.setCompositionMode(QtGui.QPainter.CompositionMode_Difference)
            painter.setOpacity(self.opacity);painter.drawImage(rect,self._overlay);painter.setOpacity(1);painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setPen(QtGui.QPen(QtGui.QColor('#7a8a99'),1));painter.drawRect(rect)
        if self._cursor is not None and not self._space:
            radius=self.diameter*self.scale/2
            for color,width in [('#111111',3),('#ffffff',1)]:
                painter.setPen(QtGui.QPen(QtGui.QColor(color),width));painter.setBrush(QtCore.Qt.NoBrush)
                if self.shape=='square':painter.drawRect(QtCore.QRectF(self._cursor.x()-radius,self._cursor.y()-radius,2*radius,2*radius))
                else:painter.drawEllipse(self._cursor,radius,radius)

    def mousePressEvent(self,event):
        if event.button()==QtCore.Qt.RightButton and event.modifiers() & QtCore.Qt.AltModifier:
            self._adjust=(event.localPos(),self.diameter,self.hardness);return
        if self.tool in ('fill','color') and event.button() in (QtCore.Qt.LeftButton,QtCore.Qt.RightButton) and not self._space:
            self.setFocus();self.color_select(self.image_point(event.localPos()),self.tool=='fill',event.button()==QtCore.Qt.RightButton);return
        self.erasing=self.tool=='erase';super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self._adjust:
            origin,size,hardness=self._adjust;delta=event.localPos()-origin
            if abs(delta.x())>=abs(delta.y()):self.diameter=max(1,min(1024,int(size+delta.x())))
            else:self.hardness=max(0.,min(1.,hardness-delta.y()/150))
            self.changed.emit();self.update();return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):self._adjust=None;super().mouseReleaseEvent(event)

    def focusOutEvent(self,event):self._adjust=None;super().focusOutEvent(event)

    def wheelEvent(self,event):
        if event.modifiers() & QtCore.Qt.ControlModifier:super().wheelEvent(event)
        else:
            self.diameter=max(1,min(1024,self.diameter+(5 if event.angleDelta().y()>0 else -5)))
            self.changed.emit();self.update();event.accept()
