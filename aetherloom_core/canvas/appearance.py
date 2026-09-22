"""Small, vector-only canvas accents; no effects, image assets or animation timers."""
from PyQt5 import QtCore, QtGui

_DARK={'app':'#93a4ff','image':'#668cff','video':'#d895cc','audio':'#dfb87e',
       'text':'#83b6f6','select':'#b5a0ee','preview':'#82c7dc',
       'number':'#b5a0ee','scalar':'#dfb87e','file':'#82c7dc','any':'#9baec4'}
_DARK['mask']='#81c784'
_DARK['bounding']='#e5b476'
_DARK['text_file'] = _DARK['text']
_LIGHT={'app':'#656ac8','image':'#315dcc','video':'#a35895','audio':'#9d732f',
        'text':'#387cbc','select':'#8564b7','preview':'#267f98',
        'number':'#8564b7','scalar':'#9d732f','file':'#267f98','any':'#718398'}
_LIGHT['mask']='#388e3c'
_LIGHT['bounding']='#9c671c'
_LIGHT['text_file'] = _LIGHT['text']
_DARK.update(llm_model='#9eafff', vision_model='#75cbd2', image_model='#d9a1ee', edit_model='#efb68d')
_LIGHT.update(llm_model='#6868bd', vision_model='#297f89', image_model='#9956b0', edit_model='#a06a39')
_DARK.update(filename='#a6c9a2', rename='#e4bf80')
_LIGHT.update(filename='#547d4e', rename='#97712c')
_DARK.update(int='#f29c8e', float='#b5a0ee', boolean='#dfb87e', enum='#d3a278')
_LIGHT.update(int='#b55345', float='#8564b7', boolean='#9d732f', enum='#996339')
_DARK['archive'] = '#82c7dc'
_LIGHT['archive'] = '#267f98'
_DARK.update(list2batch=_DARK['any'], batch=_DARK['any'], image_input=_DARK['image'], video_input=_DARK['video'], audio_input=_DARK['audio'])
_LIGHT.update(list2batch=_LIGHT['any'], batch=_LIGHT['any'], image_input=_LIGHT['image'], video_input=_LIGHT['video'], audio_input=_LIGHT['audio'])
_DARK['batch2list'] = '#a6c9a2'
_LIGHT['batch2list'] = '#547d4e'
_DARK.update(merge_batch=_DARK['any'], merge_list='#a6c9a2')
_LIGHT.update(merge_batch=_LIGHT['any'], merge_list='#547d4e')
_DARK['list_select'] = '#b5a0ee'
_LIGHT['list_select'] = '#8564b7'
_DARK.update(batch_select='#c4a1e8', rebatch='#86c9bc')
_LIGHT.update(batch_select='#9160a9', rebatch='#347f71')


def tint(color, alpha):
    value=QtGui.QColor(color);value.setAlpha(alpha);return value


def canvas_palette(base):
    """Quiet neutral surfaces shared by nodes, controls and their inspector."""
    colors = dict(base)
    light = QtGui.QColor(base['canvas']).lightness() > 128
    colors.update(dict(surface='#fcfdff', input='#f2f5f9', border='#d8dfe9',
                       muted='#65748a', hover='#eaf0f8') if light else
                  dict(surface='#242932', input='#1b2028', border='#3b4350',
                       muted='#a5b0c1', hover='#303845'))
    return colors


def kind_color(kind, colors):
    if kind.endswith('_input'):kind = kind[:-6]
    if kind not in _DARK:
        kind = 'image' if kind.startswith('image_') else 'mask' if kind.startswith('mask_') else 'text' if kind.startswith(('text_', 'prompt_')) else kind
    light=QtGui.QColor(colors['canvas']).lightness()>128
    return (_LIGHT if light else _DARK).get(kind,colors['accent'])


def bypass_colors(colors):
    """Distinct bypass identity in both themes, separate from execution colors."""
    if QtGui.QColor(colors['canvas']).lightness() > 128:
        return '#893bb4', '#eedcf8', '#ffffff'
    return '#d09aef', '#392449', '#24132f'


def draw_kind_icon(painter, rect, kind, color):
    kind = {'image_resize':'image', 'image_crop':'image', 'image_crop_mask':'image', 'image_paste_bounding':'image', 'image_pad':'image', 'image_compare':'preview',
            'media_info':'int', 'text_template':'text', 'text_split':'text', 'text_join':'text',
            'note':'text', 'text_file':'text', 'reroute':'select'}.get(kind, kind)
    kind = {'llm_model': 'text', 'vision_model': 'preview', 'image_model': 'image', 'edit_model': 'image', 'filename': 'text', 'rename': 'text'}.get(kind, kind)
    kind = 'image' if kind.startswith('image_') else 'mask' if kind.startswith('mask_') else 'text' if kind.startswith(('text_', 'prompt_')) else kind
    painter.save();painter.translate(rect.topLeft())
    painter.scale(rect.width()/24,rect.height()/24)
    painter.setPen(QtGui.QPen(QtGui.QColor(color),1.6,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap,QtCore.Qt.RoundJoin))
    painter.setBrush(QtCore.Qt.NoBrush)
    if kind in ('int', 'float'):
        font = QtGui.QFont('Microsoft YaHei UI');font.setPixelSize(11);font.setBold(True)
        painter.setFont(font)
        painter.drawText(QtCore.QRectF(0, 0, 24, 24), QtCore.Qt.AlignCenter, '123' if kind == 'int' else '1.0')
    elif kind in ('list_select', 'batch_select'):
        painter.drawRoundedRect(QtCore.QRectF(2,8,20,8),2,2)
        for y in (4,12,20):
            painter.drawLine(QtCore.QPointF(6,y),QtCore.QPointF(18,y))
    elif kind in ('merge_batch', 'merge_list', 'rebatch'):
        for y in (5,12,19):
            painter.drawLine(QtCore.QPointF(3,y),QtCore.QPointF(8,y))
            painter.drawLine(QtCore.QPointF(8,y),QtCore.QPointF(13,12))
        if kind in ('merge_batch', 'rebatch'):
            painter.drawRoundedRect(QtCore.QRectF(14,7,7,10),1.5,1.5)
        else:
            for y in (8,12,16):
                painter.drawLine(QtCore.QPointF(15,y),QtCore.QPointF(22,y))
    elif kind == 'batch2list':
        for y in (5,12,19):
            painter.drawRoundedRect(QtCore.QRectF(3,y-2,4,4),1,1)
            painter.drawLine(QtCore.QPointF(11,y),QtCore.QPointF(21,y))
    elif kind in ('list2batch', 'batch'):
        painter.drawRoundedRect(QtCore.QRectF(7,3,14,14),2,2)
        painter.drawRoundedRect(QtCore.QRectF(3,7,14,14),2,2)
    elif kind=='app':
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
