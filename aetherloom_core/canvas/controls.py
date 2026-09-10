"""Node-library interactions shared with the canvas drop target."""
import json
from PyQt5 import QtCore, QtGui, QtWidgets, sip
from .model import NODE_CATEGORIES

NODE_MIME='application/x-aetherloom-node'


def node_choice(mime):
    if not mime.hasFormat(NODE_MIME):return None
    raw=bytes(mime.data(NODE_MIME))
    if len(raw)>1024:return None
    try:
        value=json.loads(raw)
        if (isinstance(value,dict) and value.get('group') in {*NODE_CATEGORIES, 'base'} and
                isinstance(value.get('value'),str) and 0<len(value['value'])<=200):
            return {'group':value['group'],'value':value['value']}
    except (ValueError,UnicodeError):pass
    return None


class CanvasStatus(QtWidgets.QLabel):
    """Single-line feedback that never grows over the canvas controls."""
    def __init__(self, text='', parent=None):
        super().__init__('', parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self._full_text.replace('\n', ' · '), QtCore.Qt.ElideRight, max(0, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class NodeLibrary(QtWidgets.QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setSelectionMode(self.SingleSelection)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setResizeMode(self.Adjust)

    def startDrag(self, supported):
        item=self.currentItem()
        if item is None or item.isHidden():return
        group,value=item.data(QtCore.Qt.UserRole)
        mime=QtCore.QMimeData();mime.setData(NODE_MIME,json.dumps({'group':group,'value':value}).encode('utf8'))
        drag=QtGui.QDrag(self);drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(self.visualItemRect(item)))
        drag.setHotSpot(QtCore.QPoint(24,18))
        try:drag.exec_(QtCore.Qt.CopyAction)
        finally:
            if not sip.isdeleted(drag):drag.deleteLater()
