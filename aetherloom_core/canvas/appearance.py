"""Small, vector-only canvas accents; no effects, image assets or animation timers."""
from PyQt5 import QtCore, QtGui

_DARK={'app':'#93a4ff','image':'#68c9ac','video':'#d895cc','audio':'#dfb87e',
       'text':'#83b6f6','select':'#b5a0ee','preview':'#82c7dc',
       'number':'#b5a0ee','scalar':'#dfb87e','file':'#82c7dc','any':'#9baec4'}
_LIGHT={'app':'#656ac8','image':'#26876d','video':'#a35895','audio':'#9d732f',
        'text':'#387cbc','select':'#8564b7','preview':'#267f98',
        'number':'#8564b7','scalar':'#9d732f','file':'#267f98','any':'#718398'}
_DARK.update(llm_model='#9eafff', vision_model='#75cbd2', image_model='#d9a1ee', edit_model='#efb68d')
_LIGHT.update(llm_model='#6868bd', vision_model='#297f89', image_model='#9956b0', edit_model='#a06a39')
_DARK.update(filename='#a6c9a2', rename='#e4bf80')
_LIGHT.update(filename='#547d4e', rename='#97712c')


def tint(color, alpha):
    value=QtGui.QColor(color);value.setAlpha(alpha);return value


def kind_color(kind, colors):
    light=QtGui.QColor(colors['canvas']).lightness()>128
    return (_LIGHT if light else _DARK).get(kind,colors['accent'])


def draw_kind_icon(painter, rect, kind, color):
    kind = {'llm_model': 'text', 'vision_model': 'preview', 'image_model': 'image', 'edit_model': 'image', 'filename': 'text', 'rename': 'text'}.get(kind, kind)
    painter.save();painter.translate(rect.topLeft())
    painter.scale(rect.width()/24,rect.height()/24)
    painter.setPen(QtGui.QPen(QtGui.QColor(color),1.6,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap,QtCore.Qt.RoundJoin))
    painter.setBrush(QtCore.Qt.NoBrush)
    if kind=='app':
        for x,y in ((4,4),(14,4),(4,14),(14,14)):
            painter.drawRoundedRect(QtCore.QRectF(x,y,6,6),1.5,1.5)
    elif kind in ('image','preview'):
        painter.drawRoundedRect(QtCore.QRectF(3,4,18,16),2,2)
        painter.drawEllipse(QtCore.QPointF(16,9),1.7,1.7)
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(5,17),QtCore.QPointF(10,11),QtCore.QPointF(14,16),QtCore.QPointF(17,13),QtCore.QPointF(20,17)]))
    elif kind=='video':
        painter.drawRoundedRect(QtCore.QRectF(3,4,18,16),2,2)
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(10,8),QtCore.QPointF(16,12),QtCore.QPointF(10,16)]))
    elif kind=='audio':
        for x,height in ((4,4),(8,10),(12,16),(16,10),(20,4)):
            painter.drawLine(QtCore.QPointF(x,12-height/2),QtCore.QPointF(x,12+height/2))
    elif kind=='text':
        painter.drawLine(QtCore.QPointF(5,5),QtCore.QPointF(19,5))
        painter.drawLine(QtCore.QPointF(12,5),QtCore.QPointF(12,20))
        painter.drawLine(QtCore.QPointF(8,20),QtCore.QPointF(16,20))
    else:
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(4,5),QtCore.QPointF(20,5),QtCore.QPointF(14,12),QtCore.QPointF(14,19),QtCore.QPointF(10,21),QtCore.QPointF(10,12),QtCore.QPointF(4,5)]))
    painter.restore()
