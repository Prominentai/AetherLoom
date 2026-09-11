"""Shared bulk options, applied once to the compatible selected nodes."""
from PyQt5 import QtCore,QtWidgets
from . import model


def options(nodes):
    for label,path,kinds,default in [
        ('忽略节点（旁路）','bypass',None,False),
        ('过滤重复运行','filter_repeats',{'app'}|set(model.MODEL_KINDS),False),
        ('本地解码','decode_settings.enabled',{'app'},False),
        ('保存结果','params.save_enabled',{'preview'},False),
        ('重名覆盖','params.overwrite',{'preview'},False)]:
        eligible=[node for node in nodes if kinds is None or node['kind'] in kinds]
        if path == 'decode_settings.enabled':
            eligible = [node for node in eligible if model.supports_local_decode(node)]
        if not eligible:continue
        keys=path.split('.')
        values=[bool((node.get(keys[0],{}) if len(keys)>1 else node).get(keys[-1],default)) for node in eligible]
        yield dict(label=label,path=path,ids=[node['id'] for node in eligible],checked=all(values),mixed=any(values) and not all(values))


class BatchInspector(QtWidgets.QWidget):
    changed=QtCore.pyqtSignal(object,str,object)
    message=QtCore.pyqtSignal(str)

    def __init__(self,nodes,parent=None):
        super().__init__(parent);self.setObjectName('canvasBatchInspector')
        layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(14,14,14,14);layout.setSpacing(12)
        title=QtWidgets.QLabel(f'批量设置 · {len(nodes)} 个节点');title.setObjectName('canvasSectionTitle');layout.addWidget(title)
        note=QtWidgets.QLabel('开关仅修改适用的选中节点；半选表示状态不同。每次批量修改可整体撤销。')
        note.setWordWrap(True);layout.addWidget(note)
        for option in options(nodes):
            label=option['label']+(f" · {len(option['ids'])} 个适用" if len(option['ids'])!=len(nodes) else '')
            check=QtWidgets.QCheckBox(label);check.setObjectName('batch_'+option['path'])
            if option['mixed']:check.setTristate(True);check.setCheckState(QtCore.Qt.PartiallyChecked)
            else:check.setChecked(option['checked'])
            check.clicked.connect(lambda checked=False,o=option,c=check:self.changed.emit(o['ids'],o['path'],c.checkState()!=QtCore.Qt.Unchecked))
            layout.addWidget(check)
        layout.addStretch()

    def focus_other_settings(self):self.setFocus()
