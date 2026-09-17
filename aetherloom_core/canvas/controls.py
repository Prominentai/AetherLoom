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


class NodeLibrary(QtWidgets.QTreeWidget):
    """Category folders with lightweight rows, search and the existing drag MIME."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('canvasNodeLibrary')
        self.setHeaderHidden(True);self.setIndentation(14)
        self.setUniformRowHeights(True);self.setAnimated(False)
        self.setDragEnabled(True);self.setSelectionMode(self.SingleSelection)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setTextElideMode(QtCore.Qt.ElideRight)
        self._leaves, self._folders = [], {}
        self._expanded = set(NODE_CATEGORIES)
        self._filtering = False
        self.scope = 'all'
        self._preferences = {}
        self._ordered_scope = None
        self.itemExpanded.connect(lambda item:self._remember(item, True))
        self.itemCollapsed.connect(lambda item:self._remember(item, False))

    def drawBranches(self, painter, rect, index):
        if not self.model().hasChildren(index):return
        painter.save();painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(self.palette().color(QtGui.QPalette.Text), 1.3))
        x,y = rect.right()-7,rect.center().y()
        offsets = [(-3,-2),(0,1),(3,-2)] if self.isExpanded(index) else [(-2,-3),(1,0),(-2,3)]
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(x+dx,y+dy) for dx,dy in offsets]))
        painter.restore()

    def _remember(self, item, expanded):
        key = item.data(0, QtCore.Qt.UserRole + 1)
        if key and not self._filtering:
            if expanded:self._expanded.add(key)
            else:self._expanded.discard(key)

    def clear(self):
        super().clear();self._leaves = [];self._folders = {}

    def add_choice(self, label, group, value, tooltip):
        folder = self._folders.get(group)
        if folder is None:
            folder = QtWidgets.QTreeWidgetItem([NODE_CATEGORIES[group]])
            folder.setData(0, QtCore.Qt.UserRole + 1, group)
            folder.setFlags(QtCore.Qt.ItemIsEnabled)
            font = self.font();font.setWeight(QtGui.QFont.DemiBold);folder.setFont(0,font)
            folder.setSizeHint(0,QtCore.QSize(0,30))
            self.addTopLevelItem(folder);self._folders[group] = folder
            folder.setExpanded(group in self._expanded)
        item = QtWidgets.QTreeWidgetItem(folder,[label])
        item.setData(0, QtCore.Qt.UserRole,(group,value));item.setToolTip(0,tooltip)
        item.setData(0, QtCore.Qt.UserRole + 2, label)
        item.setSizeHint(0,QtCore.QSize(0,30));self._leaves.append(item)

    def set_preferences(self, preferences):
        self._preferences = preferences
        favorites = set(preferences.get('favorites', []))
        for item in self._leaves:
            group, value = item.data(0, QtCore.Qt.UserRole)
            label = item.data(0, QtCore.Qt.UserRole + 2)
            item.setText(0, ('★ ' if str(group) + ':' + str(value) in favorites else '') + label)
        self._order_choices()

    def _order_choices(self):
        favorites = set(self._preferences.get('favorites', []))
        recent = {key: index for index, key in enumerate(self._preferences.get('recent', []))}
        current = self.currentItem()
        for folder in self._folders.values():
            children = folder.takeChildren()
            def rank(item):
                group, value = item.data(0, QtCore.Qt.UserRole)
                key = str(group) + ':' + str(value)
                return (False if self.scope == 'recent' else key not in favorites,
                        recent.get(key, 999), item.data(0, QtCore.Qt.UserRole + 2).casefold())
            folder.addChildren(sorted(children, key=rank))
        if current is not None:self.setCurrentItem(current)
        self._ordered_scope = self.scope

    def filter(self, text):
        if self._ordered_scope != self.scope:self._order_choices()
        text = text.strip().casefold();visible = []
        self._filtering = True
        try:
            for group, folder in self._folders.items():
                matches = 0
                for index in range(folder.childCount()):
                    item = folder.child(index)
                    value = item.data(0,QtCore.Qt.UserRole)[1]
                    match = text in (item.text(0)+' '+item.toolTip(0)+' '+str(value)).casefold()
                    key = str(group) + ':' + str(value)
                    if self.scope in ('favorites', 'recent'):
                        match = match and key in self._preferences.get(self.scope, [])
                    item.setHidden(not match)
                    if match:visible.append(item);matches += 1
                folder.setHidden(not matches)
                folder.setText(0,NODE_CATEGORIES[group] + '  ' + str(matches))
                folder.setExpanded(bool(text) if text else group in self._expanded)
        finally:self._filtering = False
        current = self.currentItem()
        if current not in visible:self.setCurrentItem(visible[0] if visible else None)
        return visible

    def startDrag(self, supported):
        item=self.currentItem()
        if item is None or item.isHidden():return
        choice=item.data(0,QtCore.Qt.UserRole)
        if not choice:return
        group,value=choice
        mime=QtCore.QMimeData();mime.setData(NODE_MIME,json.dumps({'group':group,'value':value}).encode('utf8'))
        drag=QtGui.QDrag(self);drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(self.visualItemRect(item)))
        drag.setHotSpot(QtCore.QPoint(24,15))
        try:drag.exec_(QtCore.Qt.CopyAction)
        finally:
            if not sip.isdeleted(drag):drag.deleteLater()
