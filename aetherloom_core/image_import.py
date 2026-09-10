"""Capture native/browser drag payloads before Qt destroys QMimeData."""
import base64
import io
from html.parser import HTMLParser
from pathlib import Path
import urllib.parse
import urllib.request
import uuid
import time
from PIL import Image, ImageOps
from PyQt5 import QtCore, QtGui, QtWidgets, sip
from .mask_assets import MAX_PIXELS, atomic_png

LIMIT = 32 * 1024 * 1024
_pool = QtCore.QThreadPool()
_pool.setMaxThreadCount(2)
_pending = 0
_jobs = set()


class _Images(HTMLParser):
    def __init__(self):super().__init__();self.url=''
    def handle_starttag(self, tag, attrs):
        if tag == 'img' and not self.url:self.url=dict(attrs).get('src','')


def remote_url(mime):
    urls=[url.toString() for url in mime.urls() if not url.isLocalFile()]
    if mime.hasHtml():
        parser=_Images();parser.feed(mime.html()[:2*LIMIT]);urls.insert(0,parser.url)
    if mime.hasText():urls.append(mime.text().strip())
    return next((url for url in urls if url.startswith(('https://','http://','data:image/','data:video/','data:audio/'))),'')


def accepts_mime(mime):
    return mime.hasImage() or mime.hasUrls() or bool(remote_url(mime)) or any(
        value.startswith(('image/','audio/','video/')) or 'FileContents' in value for value in mime.formats())


def _read_url(url):
    if url.startswith('data:'):
        header,body=url.split(',',1)
        if len(body)>LIMIT*2:raise ValueError('导入图像超过 32 MB')
        data=base64.b64decode(body,validate=True) if ';base64' in header else urllib.parse.unquote_to_bytes(body)
    else:
        request=urllib.request.Request(url,headers={'User-Agent':'AetherLoom/0.2','Accept':'image/*,video/*,audio/*'})
        with urllib.request.urlopen(request,timeout=15) as response:
            if urllib.parse.urlsplit(response.url).scheme not in ('https','http'):raise ValueError('不支持此图像地址')
            chunks=[];length=0;deadline=time.monotonic()+30
            while True:
                if time.monotonic()>deadline:raise TimeoutError('图像下载超时，请先保存到本地再导入')
                chunk=response.read(min(64*1024,LIMIT+1-length))
                if not chunk:break
                chunks.append(chunk);length+=len(chunk)
                if length>LIMIT:raise ValueError('导入图像超过 32 MB')
            data=b''.join(chunks)
    if len(data)>LIMIT:raise ValueError('导入图像超过 32 MB')
    return data


class _Signals(QtCore.QObject):
    finished=QtCore.pyqtSignal(object,str)


class _Import(QtCore.QRunnable):
    def __init__(self, signals, payload, directory,kind='image'):
        super().__init__();self.signals,self.payload,self.directory,self.kind=signals,payload,directory,kind
    def run(self):
        paths,error=[],''
        try:
            if isinstance(self.payload,QtGui.QImage):
                image=self.payload
                if image.width()*image.height()>MAX_PIXELS:raise ValueError('图像超过 3200 万像素')
                buffer=QtCore.QBuffer();buffer.open(QtCore.QIODevice.WriteOnly)
                if not image.save(buffer,'PNG'):raise ValueError('无法读取拖入的图像')
                data=bytes(buffer.data())
            elif isinstance(self.payload,bytes):data=self.payload
            else:data=_read_url(self.payload)
            if len(data)>LIMIT:raise ValueError('导入内容超过 32 MB')
            from .media_import import save
            paths=[save(data,self.directory,self.kind)]
        except Exception as exception:error=str(exception)
        try:self.signals.finished.emit(paths,error)
        except RuntimeError:pass


def import_mime(mime, parent, callback,kind='image'):
    global _pending
    local=[url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
    if local:
        from .canvas.media_inputs import accepts
        kinds=('image','video','audio') if kind in ('any','file') else (kind,)
        paths=[path for path in local if any(accepts(path,k) for k in kinds)]
        if paths:callback(paths);return True
    if _pending>=4:
        QtWidgets.QMessageBox.information(parent,'正在导入','请等待当前图像导入完成后再添加。');return True
    payload=None
    for fmt in mime.formats():
        if (fmt.startswith(('image/','video/','audio/')) and fmt!='image/x-qt-image') or 'FileContents' in fmt:
            data=bytes(mime.data(fmt))
            if data:payload=data;break
    if payload is None and mime.hasImage():
        value=mime.imageData()
        if isinstance(value,QtGui.QPixmap):value=value.toImage()
        if isinstance(value,QtGui.QImage) and not value.isNull():
            if value.width()*value.height()>MAX_PIXELS:
                QtWidgets.QMessageBox.warning(parent,'图像过大','图像超过 3200 万像素，请先缩小后导入。');return True
            payload=value.copy()
    if payload is None:payload=remote_url(mime)
    if not payload:
        if mime.hasUrls() or (mime.hasHtml() and '<img' in mime.html().lower()):
            QtWidgets.QMessageBox.information(parent,'无法直接读取图像','此拖动内容未提供图像数据或可下载地址。请在来源软件复制图像本身后粘贴，或先保存到本地再导入。');return True
        return False
    from .paths import current_dir
    owner=parent.window()
    directory=Path(getattr(owner,'input_dir',Path(current_dir)/'input'))/'imported'
    signals=_Signals()
    _jobs.add(signals)
    def finished(paths,error):
        global _pending
        _pending-=1
        _jobs.discard(signals);signals.deleteLater()
        if sip.isdeleted(parent):return
        if getattr(owner,'_closing',False):return
        if error:QtWidgets.QMessageBox.warning(parent,'素材导入失败',error+'\n若来源需要登录或地址仅在浏览器内有效，请复制文件内容，或先保存到本地再导入。')
        else:callback(paths)
    signals.finished.connect(finished)
    _pending+=1
    _pool.start(_Import(signals,payload,directory,kind))
    return True


class ImageDropFilter(QtCore.QObject):
    def __init__(self, widget, callback,kind='image'):
        super().__init__(widget);self.callback=callback;self.kind=kind;widget.setAcceptDrops(True);widget.installEventFilter(self)
    def eventFilter(self, widget, event):
        if event.type()==QtCore.QEvent.KeyPress and event.matches(QtGui.QKeySequence.Paste):
            mime=QtWidgets.QApplication.clipboard().mimeData()
            if mime.hasImage() or remote_url(mime):
                return import_mime(mime,widget,self.callback,self.kind)
        if event.type() in (QtCore.QEvent.DragEnter,QtCore.QEvent.DragMove):
            if accepts_mime(event.mimeData()):event.acceptProposedAction();return True
        if event.type()==QtCore.QEvent.Drop:
            if import_mime(event.mimeData(),widget,self.callback,self.kind):event.acceptProposedAction();return True
        return False
