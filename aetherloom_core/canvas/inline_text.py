"""Shared session text documents for the inspector and in-node editor."""
import copy
from PyQt5 import QtCore,QtGui,QtWidgets
from aetherloom_core.ui.widgets import CompletionTextEdit
from aetherloom_core.prompt_history import PromptHistory


def bind_document(editor,text,identity,histories):
    key=('text_document',)+tuple(identity)
    document=histories.get(key)
    if document is None:
        document=QtGui.QTextDocument(QtWidgets.QApplication.instance())
        document.setPlainText(str(text));histories[key]=document
    elif document.toPlainText()!=str(text):
        document.setPlainText(str(text))
    editor.setDocument(document)
    return document


class Signals(QtCore.QObject):
    done=QtCore.pyqtSignal(object,str)


class TextJob(QtCore.QRunnable):
    def __init__(self,callback):
        super().__init__();self.callback=callback;self.signals=Signals()
    def run(self):
        try:self.signals.done.emit(self.callback(),'')
        except Exception as error:self.signals.done.emit(None,str(error))


class InlineText(QtWidgets.QWidget):
    def __init__(self,item):
        super().__init__()
        self.item=item;self.page=item.canvas_scene.parent();self.settings=self.page.owner.settings
        self.setObjectName('canvasInlineText')
        layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(3,3,3,3);layout.setSpacing(3)
        row=QtWidgets.QHBoxLayout();row.setContentsMargins(0,0,0,0)
        back,forward=QtWidgets.QToolButton(),QtWidgets.QToolButton()
        back.setText('回退');forward.setText('前进');row.addWidget(back);row.addWidget(forward);row.addStretch()
        self.tools=QtWidgets.QToolButton();self.tools.setText('文本工具');self.tools.setPopupMode(self.tools.InstantPopup)
        menu=QtWidgets.QMenu(self.tools)
        for label,mode in [('翻译为中文','zh'),('翻译为英文','en'),('扩写','expand')]:
            menu.addAction(label,lambda unused=False,m=mode:self.transform(m))
        self.tools.setMenu(menu);row.addWidget(self.tools);layout.addLayout(row)
        self.editor=CompletionTextEdit(self);self.editor.setAcceptRichText(False);self.editor.setMinimumSize(50,40)
        # Popup children of an embedded widget otherwise become a second
        # graphics proxy and steal the scene's keyboard focus on completion.
        popup=self.editor._popup
        popup.setParent(self.page.owner,popup.windowFlags())
        popup.setAttribute(QtCore.Qt.WA_ShowWithoutActivating,True)
        self.editor.destroyed.connect(popup.deleteLater)
        self.setFocusPolicy(QtCore.Qt.StrongFocus);self.setFocusProxy(self.editor)
        identity=(self.page.document['id'],item.node['id'],'text')
        bind_document(self.editor,item.node.get('params',{}).get('text',''),identity,self.page.histories)
        self.history=PromptHistory(self.editor,back,forward,self.page.histories.setdefault(identity,[]))
        self.editor.textChanged.connect(lambda:self.page._node_changed(item.node['id'],'params.text',self.editor.toPlainText()))
        layout.addWidget(self.editor,1)
        self.editor.installEventFilter(self)
        self.refresh()

    def eventFilter(self,watched,event):
        if event.type()==QtCore.QEvent.FocusIn:
            if not self.item.isSelected():self.item.scene().clearSelection();self.item.setSelected(True)
        return super().eventFilter(watched,event)

    def refresh(self):
        colors=self.item.canvas_scene.colors
        self._theme_mode=getattr(self.page.owner,'_theme_mode','dark')
        if getattr(self,'_colors',None)==colors:return
        self._colors=dict(colors)
        self.setStyleSheet('QWidget#canvasInlineText{background:'+colors['surface']+';color:'+colors['text']+';}'
            'QTextEdit{background:'+colors['input']+';color:'+colors['text']+';border:1px solid '+colors['border']+';border-radius:5px;padding:4px;font-size:12px;}'
            'QToolButton{color:'+colors['text']+';background:'+colors['surface']+';border:none;padding:3px;font-size:11px;}')

    def transform(self,mode):
        text=self.editor.toPlainText()
        if not text.strip():return
        owner=self.page.owner;settings=copy.deepcopy(getattr(owner,'api_settings',{}));keys=getattr(owner,'_apikeys',{})
        if mode!='expand':
            from aetherloom_core.translation import snapshot,translate
            config=snapshot(settings,keys);callback=lambda:translate(config,text,mode)
        else:
            from aetherloom_core.api_credentials import get_credentials
            config=copy.deepcopy(settings.get('llm') or {});config.update(get_credentials(keys,config.get('provider'),'llm'))
            prompt=str(owner.settings.get('expand_system_prompt','扩写提示词'))
            def callback():
                from api_calls.call_llm import call_llm
                if not config.get('endpoint') or not config.get('model'):raise ValueError('请先配置大语言模型')
                return call_llm(config['endpoint'],config.get('api_key',''),config['model'],prompt,text,
                    temperature=.6,timeout=int(config.get('timeout') or 30),provider=config.get('protocol') or config.get('provider'),web_search=config.get('web_search'))
        self.history.record(source='before_'+mode);self.tools.setEnabled(False)
        job=TextJob(callback);self._job=job;job.signals.done.connect(self.transformed)
        QtCore.QThreadPool.globalInstance().start(job)

    @QtCore.pyqtSlot(object,str)
    def transformed(self,result,error):
        self.tools.setEnabled(True)
        if error:QtWidgets.QMessageBox.warning(self,'文本处理失败',error)
        elif isinstance(result,str):self.history.apply_result(result,'text_tool')
