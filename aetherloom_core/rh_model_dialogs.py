"""Shared model-library dialogs with scrollable content and a stable action bar."""
from PyQt5 import QtCore, QtWidgets


def label(text, name='rhModelMuted'):
    widget=QtWidgets.QLabel(text)
    widget.setTextFormat(QtCore.Qt.PlainText);widget.setWordWrap(True)
    widget.setObjectName(name);widget.setMinimumWidth(0)
    return widget


def section(title):
    frame=QtWidgets.QFrame();frame.setObjectName('rhModelSection')
    layout=QtWidgets.QVBoxLayout(frame);layout.setContentsMargins(16,14,16,16);layout.setSpacing(12)
    layout.addWidget(label(title,'rhModelSectionTitle'))
    return frame,layout


class _MessageLabel(QtWidgets.QLabel):
    def __init__(self):
        super().__init__()
        self.setObjectName('rhModelMessage');self.setWordWrap(True)
        self.setTextFormat(QtCore.Qt.PlainText);self.setText('')

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))


class ModelDialog(QtWidgets.QDialog):
    def __init__(self, picker, title, subtitle, size=(660,700)):
        super().__init__(picker)
        self.picker=picker;self._first_show=True
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose);self.setObjectName('rhModelDialog')
        self.setWindowTitle(title);self.resize(*size);self.setMinimumSize(360,340)
        self.root=QtWidgets.QVBoxLayout(self);self.root.setContentsMargins(20,18,20,16);self.root.setSpacing(12)
        self.heading=label(title,'rhModelHeading');self.root.addWidget(self.heading)
        self.subtitle=label(subtitle);self.root.addWidget(self.subtitle)
        self.scroll=QtWidgets.QScrollArea();self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.content=QtWidgets.QWidget();self.content.setObjectName('rhModelDialogContent')
        self.body=QtWidgets.QVBoxLayout(self.content);self.body.setContentsMargins(0,0,6,0);self.body.setSpacing(14)
        self.scroll.setWidget(self.content);self.root.addWidget(self.scroll,1)
        self.message=_MessageLabel();self.root.addWidget(self.message)
        self.actions=QtWidgets.QHBoxLayout();self.actions.setContentsMargins(0,6,0,0)
        self.actions.addStretch();self.root.addLayout(self.actions)

    def button(self, text, callback, primary=False):
        button=QtWidgets.QPushButton(text)
        button.setObjectName('rhModelPrimary' if primary else 'rhModelSecondary')
        button.setAutoDefault(False);button.setMinimumHeight(36)
        button.clicked.connect(callback);self.actions.addWidget(button)
        return button

    def showEvent(self, event):
        if self._first_show:
            self._first_show=False
            screen=self.parentWidget().screen()
            if screen:
                area=screen.availableGeometry().adjusted(16,32,-16,-16)
                self.resize(min(self.width(),area.width()),min(self.height(),area.height()))
                center=self.parentWidget().mapToGlobal(self.parentWidget().rect().center())
                self.move(max(area.left(),min(center.x()-self.width()//2,area.right()-self.width())),
                          max(area.top(),min(center.y()-self.height()//2,area.bottom()-self.height())))
        super().showEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'actions'):
            buttons=[self.actions.itemAt(i).widget() for i in range(self.actions.count()) if self.actions.itemAt(i).widget()]
            needed=sum(button.sizeHint().width() for button in buttons)+max(0,len(buttons)-1)*self.actions.spacing()
            self.actions.setDirection(QtWidgets.QBoxLayout.TopToBottom if needed>self.width()-40 else QtWidgets.QBoxLayout.LeftToRight)


class ModelDetailsDialog(ModelDialog):
    def __init__(self, picker, card):
        import re
        from html import unescape
        from .rh_model_cards import model_title
        record,version=card.record,dict(card.version)
        super().__init__(picker,'模型详情',model_title(record),(640,670))
        self.origin,self.resource_type=picker.settings.host,picker.resource_type
        self.subtitle.setObjectName('rhModelName')
        frame,layout=section('版本信息');self.body.addWidget(frame)
        meta=' · '.join(str(x) for x in (record.get('resourceType'),version.get('version') or '默认版本',version.get('baseModel') or '基础模型未标注') if x)
        layout.addWidget(label(meta,'rhModelBadge'))
        owner=record.get('owner') if isinstance(record.get('owner'),dict) else {}
        local=record.get('_local_favorite') or {}
        source=local.get('source') or ('custom' if local.get('custom') else 'catalog')
        labels={'custom':'手动登记','website':'导入上传记录'} if local.get('bucket')=='uploads' else {'custom':'自建收藏','catalog':'客户端收藏','website':'导入收藏'}
        layout.addWidget(label('来源：'+(labels.get(source,'本地记录') if local else '公共模型')+'    作者：'+str(owner.get('name') or '未标注')))
        layout.addWidget(label('Model name','rhModelFieldLabel'))
        self.model_name=QtWidgets.QLineEdit(str(version.get('node_token') or ''));self.model_name.setReadOnly(True)
        self.model_name.setCursorPosition(0);self.model_name.setMinimumWidth(0);layout.addWidget(self.model_name)
        frame,layout=section('使用说明');self.body.addWidget(frame)
        layout.addWidget(label('触发词','rhModelFieldLabel'))
        self.trigger=QtWidgets.QPlainTextEdit(str(version.get('triggerWords') or '无'))
        self.trigger.setReadOnly(True);self.trigger.setFixedHeight(76);layout.addWidget(self.trigger)
        raw=str(version.get('desc') or record.get('desc') or '')
        description=unescape(re.sub('<[^>]*>','',raw))[:8000]
        layout.addWidget(label('模型介绍','rhModelFieldLabel'))
        self.description=QtWidgets.QPlainTextEdit(description or '暂无模型介绍')
        self.description.setReadOnly(True);self.description.setMinimumHeight(140);layout.addWidget(self.description)
        if local.get('notes'):
            layout.addWidget(label('本地备注','rhModelFieldLabel'));layout.addWidget(label(str(local['notes'])))
        self.body.addStretch()
        self.button('关闭',self.reject)
        self.button('打开官网',card.open_website).setEnabled(bool(card.website_url))
        self.button('复制 model name',self.copy_name,primary=picker.library).setEnabled(bool(version.get('node_token')))
        if not picker.library:
            self.button('使用此版本',self.use_version,primary=True).setEnabled(card.use.isEnabled())

    def use_version(self):
        if (self.picker.settings.host,self.picker.resource_type)!=(self.origin,self.resource_type):
            self.message.setText('模型站点或类型已切换，请重新选择模型。');return
        token=self.model_name.text()
        if token:
            self.accept();self.picker.close();self.picker.selected.emit(token)

    def copy_name(self):
        QtWidgets.QApplication.clipboard().setText(self.model_name.text())
        self.message.setText('已复制 model name')
