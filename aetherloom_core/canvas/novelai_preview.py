"""Bounded, visible-only NovelAI stream previews, never serialized as results."""
from collections import OrderedDict
import weakref

from PyQt5 import QtCore, QtGui

from aetherloom_core.novelai.jobs import Job


class CanvasFrames(QtCore.QObject):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.pending = OrderedDict()
        self.job = None
        self.latest = {}
        self.retained = OrderedDict()

    def close(self):
        self.pending.clear()
        self.latest.clear()
        self.retained.clear()
        if self.job is not None:self.job.cancel()

    def _item(self, source):
        page = self.page
        if (page._closed or not page.isVisible() or source.get('canvas_id') != page.document.get('id')
                or source.get('round_id') != page.document.get('run', {}).get('id')):
            return None
        item = page.scene.nodes.get(source.get('node_id'))
        if item is None or item.node.get('status') not in ('RUNNING','DOWNLOADING','DOWNLOAD_FAILED'):return None
        if item.node['id'] not in page.scene.thumbnail_nodes:return None
        return item

    @QtCore.pyqtSlot(object, object)
    def receive(self, source, value):
        item = self._item(source)
        raw = value.get('bytes') if isinstance(value,dict) else None
        if item is None or not isinstance(raw,bytes) or not 0 < len(raw) <= 8*1024*1024:return
        identity = (source['canvas_id'],source['round_id'],source['node_id'])
        index = source.get('item_index',0)
        # Parallel requests may finish out of order; an older item's late frame
        # must not steal the preview from a newer item in the same node.
        if self.latest.get(identity,-1) > index:return
        self.latest[identity] = index
        if len(self.latest)>64:self.latest = {identity:index}
        self.pending.pop(identity,None)
        self.pending[identity] = (dict(source),raw)
        while len(self.pending)>4 or sum(len(v[1]) for v in self.pending.values())>16*1024*1024:
            self.pending.popitem(last=False)
        self._next()

    def _next(self):
        if self.job is not None or not self.pending or self.page._closed:return
        identity,(source,raw) = self.pending.popitem(last=False)
        def decode(unused):
            buffer = QtCore.QBuffer()
            buffer.setData(raw)
            buffer.open(QtCore.QIODevice.ReadOnly)
            reader = QtGui.QImageReader(buffer)
            reader.setAutoTransform(True)
            size=reader.size()
            if not size.isValid() or size.width()*size.height()>40_000_000:return QtGui.QImage()
            reader.setScaledSize(size.scaled(640,640,QtCore.Qt.KeepAspectRatio))
            image=reader.read()
            if max(image.width(),image.height())>640:
                image=image.scaled(640,640,QtCore.Qt.KeepAspectRatio,QtCore.Qt.SmoothTransformation)
            return image
        job=self.job=Job(decode,self)
        def done(image):
            item=self._item(source)
            if item is None or image.isNull() or self.latest.get(identity)!=source.get('item_index',0):return
            item._novelai_frame=QtGui.QPixmap.fromImage(image)
            item._novelai_frame_round=source['round_id']
            self.retained.pop(identity,None)
            self.retained[identity]=weakref.ref(item)
            while len(self.retained)>8:
                _,reference=self.retained.popitem(last=False)
                previous=reference()
                if previous is not None:
                    previous._novelai_frame=None
                    try:previous.update()
                    except RuntimeError:pass
            item.layout_inline()
            item.update()
        def finished():
            self.job=None
            job.deleteLater()
            self._next()
        job.succeeded.connect(done)
        job.finished.connect(finished)
        job.start()
