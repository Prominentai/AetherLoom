"""Flat execution graph, collapsible reusable visual groups with port aliases.

Member IDs never change when a group folds; tasks, snapshots and App cards keep
their original identity. Aliases point to real sockets rather than proxy tasks.
"""
import copy
import json
from pathlib import Path
from PyQt5 import QtCore,QtGui,QtWidgets
from . import model


def members(document,ids):
 found=set(ids)
 for node in document['nodes']:
  if node['id'] in found and node['kind']=='subgraph':found.update(node.get('members',[]))
 return found


def group_selected(page):
 from .graphics import NodeItem
 ids={i.node['id'] for i in page.scene.selectedItems() if isinstance(i,NodeItem)}
 ids=members(page.document,ids)
 selected=[n for n in page.document['nodes'] if n['id'] in ids and n['kind']!='subgraph']
 if not selected:page._message('先框选需要组合的节点');return
 page._checkpoint()
 # Flatten existing groups rather than create recursive visual ownership.
 page.document['nodes']=[n for n in page.document['nodes'] if n['kind']!='subgraph' or not set(n.get('members',[]))&ids]
 group=model.new_node('subgraph',members=[n['id'] for n in selected],params={'collapsed':True},
  x=min(n.get('x',0) for n in selected)-340,y=min(n.get('y',0) for n in selected))
 page.document['nodes'].append(group);page._edited(rebuild=True,select=group['id'])


def toggle(page,group):page._node_changed(group['id'],'params.collapsed',not group.get('params',{}).get('collapsed',True))


def unpack(page,group):
 page._checkpoint();page.document['nodes']=[n for n in page.document['nodes'] if n['id']!=group['id']]
 page._edited(rebuild=True)


def export_group(page,group):
 path,_=QtWidgets.QFileDialog.getSaveFileName(page.owner,'保存组合节点模板',group.get('title','组合节点')+'.json','JSON (*.json)')
 if not path:return
 ids=members(page.document,{group['id']});doc=model.new_document(group.get('title','组合节点'))
 doc['nodes']=[copy.deepcopy(n) for n in page.document['nodes'] if n['id'] in ids]
 doc['edges']=[copy.deepcopy(e) for e in page.document['edges'] if e['source'] in ids and e['target'] in ids]
 page.store.export_workflow(doc,path);page._message('已保存组合节点模板，可在其他画布导入')


def import_group(page):
 path,_=QtWidgets.QFileDialog.getOpenFileName(page.owner,'导入组合节点模板','','JSON (*.json)')
 if not path:return
 try:
  if Path(path).stat().st_size>8*1024*1024:raise ValueError('模板超过 8 MiB')
  doc=page.store.load_workflow(path)
  if len(doc.get('nodes',[]))>1000:raise ValueError('模板节点过多')
  model.validate_document(doc)
  if not any(n['kind']=='subgraph' for n in doc['nodes']):raise ValueError('请选择包含组合节点的模板')
  old=page._clipboard;page._clipboard={'nodes':model.workflow_document(doc)['nodes'],'edges':doc['edges']}
  try:page.paste_nodes()
  finally:page._clipboard=old
 except (OSError,ValueError,TypeError) as error:page._message('导入失败：'+str(error))


def build(panel,node):
 page=getattr(panel.model_owner,'canvas_page',None)
 if page is None:return
 row=QtWidgets.QHBoxLayout()
 for text,fn in [('展开 / 收起',lambda:toggle(page,node)),('解散组合',lambda:unpack(page,node))]:
  button=QtWidgets.QPushButton(text);button.clicked.connect(fn);row.addWidget(button)
 panel.form.addLayout(row)
 button=QtWidgets.QPushButton('保存为组合节点模板');button.clicked.connect(lambda:export_group(page,node));panel.form.addWidget(button)
 from aetherloom_core.rh_parameters import RhEnumComboBox
 combo=RhEnumComboBox();panel.form.addWidget(combo)
 selected=[n for n in page.document['nodes'] if n['id'] in node.get('members',[]) and n['kind']!='subgraph']
 for member in selected:combo.addItem(model.node_title(member),member['id'])
 holder=QtWidgets.QVBoxLayout();panel.form.addLayout(holder)
 def display():
  while holder.count():
   item=holder.takeAt(0)
   if item.widget():item.widget().deleteLater()
  member=next((n for n in page.document['nodes'] if n['id']==combo.currentData()),None)
  if member is None:return
  from .editors import Inspector
  editor=Inspector(member,page.document['id'],page.document['edges'],page.histories,parent=panel,model_owner=panel.model_owner,embedded=True)
  editor.changed.connect(lambda path,value:page._node_changed(member['id'],path,value));editor.message.connect(page._message)
  panel.group_editor=editor;holder.addWidget(editor)
 combo.currentIndexChanged.connect(display);display()


def sync(scene):
 from .graphics import PortItem
 document=scene._document;owner={};mapping={}
 groups=[n for n in document['nodes'] if n['kind']=='subgraph']
 for group in groups:
  if group.get('params',{}).get('collapsed',True):
   for identity in group.get('members',[]):owner[identity]=group['id']
 for identity,item in scene.nodes.items():item.setVisible(identity not in owner)
 for group in groups:
  item=scene.nodes[group['id']]
  old=getattr(item,'group_ports',{});item.group_ports={}
  if not group.get('params',{}).get('collapsed',True):
   for port in old.values():scene.removeItem(port);port.setParentItem(None)
   continue
  member_ids=group.get('members',[]);ids=set(member_ids);sockets=[]
  internal_inputs=set();internal_sources=set();external_outputs=set()
  for edge in document['edges']:
   if edge['source'] in ids and edge['target'] in ids:
    internal_inputs.add((edge['target'],edge['input']));internal_sources.add(edge['source'])
   elif edge['source'] in ids:
    external_outputs.add((edge['source'],edge.get('output','output')))
  # The group boundary must remain connectable after folding or disconnecting.
  # Ignore the legacy expose_unconnected flag: only internally wired inputs
  # are private. Iterate member/field order so external wiring cannot move rows.
  for identity in member_ids:
   member=scene.nodes.get(identity)
   if member is None:continue
   sockets.extend((identity,key,False) for key in member.ports if (identity,key) not in internal_inputs)
   sockets.extend((identity,key,True) for key in member.outputs
                  if identity not in internal_sources or (identity,key) in external_outputs)
  counts={False:0,True:0}
  for identity,key,output in dict.fromkeys(sockets):
   member=scene.nodes[identity];original=(member.outputs if output else member.ports).get(key)
   if original is None:continue
   alias=old.pop((identity,key,output),None)
   if alias is None:alias=PortItem(member,key,original.label,original.kind,output);alias.setParentItem(item)
   alias.kind=original.kind;alias.label=original.label
   alias.content_kind=original.content_kind
   if output:alias.containers=original.containers
   alias.setPos(item.width if output else 0,64+counts[output]*24);counts[output]+=1
   alias.refresh_connection(original.connected);alias.setToolTip(model.node_title(member.node)+' · '+original.label+'\n'+alias.toolTip())
   item.group_ports[(identity,key,output)]=alias;mapping[(identity,key,output)]=alias
  for port in old.values():scene.removeItem(port);port.setParentItem(None)
  item.form_minimum_height=max(150,100+max(counts.values())*24);item.set_size(group.get('size'))
  for (_,_,output),alias in item.group_ports.items():alias.setX(item.width if output else 0)
 scene.group_aliases=mapping;scene.group_owners=owner
 for edge in scene.edges.values():
  source,target=edge.edge['source'],edge.edge['target']
  edge.setVisible(not (source in owner and owner.get(source)==owner.get(target)))
  if edge.isVisible():edge.update_path()


def paint(item,painter):
 p=item.canvas_scene.colors;body=QtCore.QRectF(0,0,item.width,item.height)
 issue=getattr(item,'dependency_issue',None)
 painter.setBrush(QtGui.QColor(p['surface']));painter.setPen(QtGui.QPen(QtGui.QColor(p[issue['severity']] if issue else p['accent'] if item.isSelected() else p['border']),2))
 painter.drawRoundedRect(body,8,8);painter.setPen(QtGui.QColor(p['text']))
 font=QtGui.QFont('Microsoft YaHei UI');font.setPixelSize(14);font.setBold(True);painter.setFont(font)
 painter.drawText(QtCore.QRectF(14,8,item.width-28,24),QtCore.Qt.AlignVCenter,model.node_title(item.node))
 font.setPixelSize(11);font.setBold(False);painter.setFont(font);painter.setPen(QtGui.QColor(p['muted']))
 painter.drawText(QtCore.QRectF(14,34,item.width-28,18),QtCore.Qt.AlignVCenter,'%d 个节点 · %s'%(len(item.node.get('members',[])),'已折叠' if item.node.get('params',{}).get('collapsed',True) else '已展开'))
 for (_,_,output),alias in getattr(item,'group_ports',{}).items():
  label=model.port_type_name(alias.content_kind) if output else alias.label
  rect=QtCore.QRectF(item.width/2 if output else 12,alias.pos().y()-10,item.width/2-24,20)
  painter.drawText(rect,QtCore.Qt.AlignRight|QtCore.Qt.AlignVCenter if output else QtCore.Qt.AlignLeft|QtCore.Qt.AlignVCenter,
                   painter.fontMetrics().elidedText(label,QtCore.Qt.ElideRight,int(rect.width())))
 if issue:painter.setPen(QtGui.QColor(p[issue['severity']]))
 painter.drawText(QtCore.QRectF(14,item.height-27,item.width-28,20),QtCore.Qt.AlignVCenter,issue['label'] if issue else '双击配置 · 右键展开 / 解散')
