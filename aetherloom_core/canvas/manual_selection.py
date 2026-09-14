"""Paged explicit selection for one immutable workflow execution."""
from PyQt5 import QtCore,QtWidgets
from . import model,preview_data


class SelectionDialog(QtWidgets.QDialog):
 def __init__(self,page,node,parent=None):
  super().__init__(parent or page.owner)
  self.page=page;self.node_id=node['id'];self.canvas_id=page.document['id']
  self.round=node.get('selection_round');self.token=node.get('selection_token')
  self.results=node.get('results',[]);self.selected=set();self.page_index=0
  self.setWindowTitle('选择结果并继续下游');self.resize(660,620)
  screen=self.screen().availableGeometry();self.resize(min(660,screen.width()-40),min(620,screen.height()-60))
  layout=QtWidgets.QVBoxLayout(self)
  self.status=QtWidgets.QLabel('勾选需要交给下游的结果；Batch 作为一整组选择。');self.status.setWordWrap(True);layout.addWidget(self.status)
  from .graphics import ThumbnailCache
  from .result_browser import MediaPreview
  self.cache=ThumbnailCache(self,limit=4);self.preview=MediaPreview(self.cache,self);layout.addWidget(self.preview,1)
  self.text=QtWidgets.QPlainTextEdit();self.text.setReadOnly(True);self.text.setMaximumHeight(150);self.text.hide();layout.addWidget(self.text)
  self.listing=QtWidgets.QListWidget();self.listing.setMinimumHeight(120);self.listing.setMaximumHeight(220);layout.addWidget(self.listing)
  self.listing.itemChanged.connect(self.check_item);self.listing.currentItemChanged.connect(self.show_item)
  row=QtWidgets.QHBoxLayout();self.previous=QtWidgets.QPushButton('上一页');self.counter=QtWidgets.QLabel();self.next=QtWidgets.QPushButton('下一页')
  row.addWidget(self.previous);row.addWidget(self.counter,1);row.addWidget(self.next);layout.addLayout(row)
  self.previous.clicked.connect(lambda:self.turn(-1));self.next.clicked.connect(lambda:self.turn(1))
  self.confirm=QtWidgets.QPushButton('确认选择并继续');self.confirm.clicked.connect(self.submit);layout.addWidget(self.confirm)
  self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.refresh_status);self.timer.start(300)
  self.populate();self.refresh_status()

 def turn(self,delta):self.page_index+=delta;self.populate()
 def populate(self):
  with QtCore.QSignalBlocker(self.listing):
   self.listing.clear()
   for i in range(self.page_index*60,min(len(self.results),(self.page_index+1)*60)):
    result=self.results[i];kind=model.result_type(result)
    title=('Batch · %d 项'%len(model.batch_items(result))) if kind=='batch' else model.port_type_name(kind)+' · '+preview_data.title_of(result)
    item=QtWidgets.QListWidgetItem('%d. %s'%(i+1,title[:160]));item.setData(QtCore.Qt.UserRole,i)
    item.setFlags(item.flags()|QtCore.Qt.ItemIsUserCheckable);item.setCheckState(QtCore.Qt.Checked if i in self.selected else QtCore.Qt.Unchecked);self.listing.addItem(item)
  self.previous.setEnabled(self.page_index>0);self.next.setEnabled((self.page_index+1)*60<len(self.results))
  self.counter.setText('%d / %d · 已选 %d'%(self.page_index+1,max(1,(len(self.results)+59)//60),len(self.selected)))
  if self.listing.count():self.listing.setCurrentRow(0)

 def check_item(self,item):
  i=item.data(QtCore.Qt.UserRole)
  if item.checkState()==QtCore.Qt.Checked:self.selected.add(i)
  else:self.selected.discard(i)
  self.counter.setText('%d / %d · 已选 %d'%(self.page_index+1,max(1,(len(self.results)+59)//60),len(self.selected)))

 def show_item(self,item,*_):
  if item is None:return
  value=self.results[item.data(QtCore.Qt.UserRole)]
  if model.result_type(value)=='batch':value=model.batch_items(value)[0]
  text=model.result_type(value) in model.VALUE_TYPES|{'text'}
  self.preview.setVisible(not text);self.text.setVisible(text)
  if text:
   from .advanced_nodes import scalar
   try:self.text.setPlainText(str(scalar(value))[:4000])
   except Exception:self.text.setPlainText('结果文件不可读')
  else:self.preview.value=value;self.preview.update()

 def refresh_status(self):
  with self.page.engine._condition:
   doc=self.page.engine._documents.get(self.canvas_id,{})
   state=doc.get('run',{}).get('nodes',{}).get(self.node_id,{})
   valid=state.get('status')=='AWAITING_SELECTION' and state.get('selection_token')==self.token
  self.confirm.setEnabled(valid)
  if not valid:self.status.setText('此选择请求已完成、取消或过期。关闭后可重新打开当前任务。')

 def submit(self):
  try:self.page.engine.choose_results(self.canvas_id,self.node_id,self.round,self.token,sorted(self.selected))
  except ValueError as error:self.status.setText(str(error));return
  self.accept()

 def done(self,result):
  self.timer.stop();self.cache.close();super().done(result)


def open_selection(panel,node):
 page=getattr(panel.model_owner,'canvas_page',None)
 if page is None:return
 current=next((n for n in page.document['nodes'] if n['id']==node['id']),node)
 if current.get('status')!='AWAITING_SELECTION':panel.message.emit('运行到此节点后才能选择候选结果');return
 dialog=SelectionDialog(page,current,panel.dialog_parent);dialog.exec_();dialog.deleteLater()
