"""Official base-model enum, shared by filters and editable creation selectors."""
import json
import threading
import time
from pathlib import Path

from PyQt5 import QtCore, QtWidgets
from .rh_model_favorites import SITES
from .rh_parameters import RhEnumComboBox

FALLBACK=('SD 1.5','SDXL 1.0','SD3.5','IL-XL','NoobAI-XL','Pony-XL','WAN2.1','WAN2.2',
          'Qwen-image','Qwen-Image-2512','Z-image-turbo','Z-image-base','LTX2.3','anima','krea2','Other')


def read_base_models(site):
    from .rh_model_http import post_json
    import requests
    with requests.Session() as session:
        values=post_json(session,site,'/api/resource/baseModels',{})
    if not isinstance(values,list) or not values or len(values)>512 or any(not isinstance(x,str) or len(x)>200 for x in values):
        raise ValueError('基础模型列表格式无效')
    return list(dict.fromkeys(x for x in values if x.strip()))


class BaseModels(QtCore.QObject):
    changed=QtCore.pyqtSignal(str)
    received=QtCore.pyqtSignal(str,object)

    def __init__(self, owner):
        super().__init__(owner)
        from .paths import current_dir
        self.path=Path(current_dir)/'model_library'/'base_models.json'
        self.values={};self.checked={};self.pending=set()
        try:
            if self.path.stat().st_size<300000:
                data=json.loads(self.path.read_text(encoding='utf8'))
                self.values={site:[v for v in data.get(site,[])[:512] if isinstance(v,str) and len(v)<=200] for site in SITES}
        except (OSError,ValueError,TypeError):pass
        self.received.connect(self._received,QtCore.Qt.QueuedConnection)

    def options(self, site):return self.values.get(site) or list(FALLBACK)

    def refresh(self, site, *, force=False):
        if site not in SITES or site in self.pending or not force and time.monotonic()-self.checked.get(site,-10000)<3600:return
        self.pending.add(site)
        def work():
            try:values=read_base_models(site)
            except Exception:values=None
            try:self.received.emit(site,values)
            except RuntimeError:pass
        threading.Thread(target=work,name='rh-base-models',daemon=True).start()

    def _received(self, site, values):
        self.pending.discard(site);self.checked[site]=time.monotonic()
        if values:
            self.values[site]=values
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                file=QtCore.QSaveFile(str(self.path))
                if file.open(QtCore.QIODevice.WriteOnly):
                    file.write(json.dumps(self.values,ensure_ascii=False).encode('utf8'));file.commit()
            except OSError:pass
            self.changed.emit(site)


def base_models(owner):
    result=getattr(owner,'_rh_base_models',None)
    if result is None:result=owner._rh_base_models=BaseModels(owner)
    return result


class BaseModelCombo(RhEnumComboBox):
    def __init__(self, model, site, value='', parent=None):
        super().__init__(parent)
        self.model,self.site=model,site;self.setEditable(True);self.setInsertPolicy(self.NoInsert)
        self.setMinimumWidth(0);self.setMaxVisibleItems(12)
        self.lineEdit().setPlaceholderText('搜索或选择基础模型，也可填写新名称')
        self._populate(value);model.changed.connect(self._changed);model.refresh(site)

    def _populate(self, value):
        with QtCore.QSignalBlocker(self):
            self.clear();self.addItem('');self.addItems(self.model.options(self.site));self.setEditText(value)
        self.completer().setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.completer().setFilterMode(QtCore.Qt.MatchContains)
        self.completer().setCompletionMode(QtWidgets.QCompleter.PopupCompletion)

    def _changed(self, site):
        if site==self.site:self._populate(self.currentText())
