"""Single-link and login-assisted, staged collection import dialogs."""
import json
import tempfile
import threading
import time
from collections import deque

from PyQt5 import QtCore, QtWidgets

from .rh_model_cards import model_title, model_versions
from .rh_model_favorites import SITES
from .rh_model_import import import_values, read_model_link
from .rh_parameters import RhEnumComboBox
from .rh_model_dialogs import ModelDialog, label


class ModelImportDialog(ModelDialog):
    resolved=QtCore.pyqtSignal(int,object,str)
    imported=QtCore.pyqtSignal(object)

    def __init__(self, picker):
        target='我的上传' if picker.destination()=='uploads' else '本地收藏'
        super().__init__(picker,'导入模型','保存到'+target+' · 已有版本保留本地设置',(660,720))
        self.bucket=picker.destination();self.store=picker.store_for(self.bucket)
        self.target='我的上传' if self.bucket=='uploads' else '本地收藏'
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose);self.setWindowTitle('导入到'+self.target)
        self.picker=picker;self.generation=0;self.single=None;self.browser=None;self.reading=False;self._closed=False
        self.worker=None;self.session_mode=None
        self.stage=tempfile.TemporaryFile(mode='w+t',encoding='utf8');self.staged=0
        self.pending=deque();self.importing=False;self.import_count=0;self.skip_count=0;self.invalid_count=0;self.last_value=None
        self.timer=QtCore.QTimer(self);self.timer.setInterval(10);self.timer.timeout.connect(self._import_chunk)
        self.tabs=QtWidgets.QTabWidget();self.tabs.setObjectName('rhModelDialogTabs');self.body.addWidget(self.tabs)
        link_page=QtWidgets.QWidget();link_box=QtWidgets.QVBoxLayout(link_page);link_box.setSpacing(12)
        hint=QtWidgets.QLabel('粘贴模型详情页链接，选择版本后保存到'+self.target+'。');hint.setWordWrap(True);link_box.addWidget(hint)
        link_box.addWidget(label('模型网页地址','rhModelFieldLabel'))
        self.url=QtWidgets.QLineEdit();self.url.setPlaceholderText('https://www.runninghub.cn/model/public/…');self.url.setMaxLength(2048)
        self.url.textChanged.connect(self._link_changed);link_box.addWidget(self.url)
        self.read_link=QtWidgets.QPushButton('读取模型');self.read_link.setObjectName('rhModelSecondary');self.read_link.clicked.connect(self._read_link)
        self.url.returnPressed.connect(self._read_link);link_box.addWidget(self.read_link)
        self.model_label=QtWidgets.QLabel();self.model_label.setTextFormat(QtCore.Qt.PlainText);self.model_label.setWordWrap(True);link_box.addWidget(self.model_label);self.model_label.hide()
        self.versions=RhEnumComboBox();link_box.addWidget(self.versions);self.versions.hide()
        self.save_one=QtWidgets.QPushButton('导入此版本');self.save_one.setObjectName('rhModelPrimary');self.save_one.setEnabled(False)
        self.save_one.clicked.connect(self._save_one);link_box.addStretch()
        self.tabs.addTab(link_page,'网页链接')
        bulk_page=QtWidgets.QWidget();bulk=QtWidgets.QVBoxLayout(bulk_page);bulk.setSpacing(12)
        steps=label('登录官网  →  读取列表  →  确认导入','rhImportSteps');bulk.addWidget(steps)
        hint=QtWidgets.QLabel('已在默认浏览器登录可直接读取；未登录时先点击登录。中文站和国际站的登录状态独立。')
        hint.setWordWrap(True);bulk.addWidget(hint)
        self.site=RhEnumComboBox();self.site.addItem('中文站',SITES[0]);self.site.addItem('国际站',SITES[1])
        self.site.setCurrentIndex(SITES.index(picker.settings.host))
        self.login=QtWidgets.QPushButton('在默认浏览器登录');self.login.setObjectName('rhModelSecondary');self.login.clicked.connect(self._open_browser)
        connection=QtWidgets.QHBoxLayout();connection.addWidget(self.site,1);connection.addWidget(self.login);bulk.addLayout(connection)
        self.login_state=QtWidgets.QLabel('点击读取时检查默认浏览器登录状态');self.login_state.setWordWrap(True);self.login_state.setObjectName('rhModelMuted');bulk.addWidget(self.login_state)
        self.read_all=QtWidgets.QPushButton('读取我的上传' if self.bucket=='uploads' else '读取我的收藏');self.read_all.setObjectName('rhModelSecondary')
        reading_row=QtWidgets.QHBoxLayout();self.read_all.clicked.connect(self._read_all);reading_row.addWidget(self.read_all,1)
        self.stop=QtWidgets.QPushButton('停止读取');self.stop.setObjectName('rhModelSecondary');self.stop.setEnabled(False)
        self.stop.clicked.connect(self._stop_read);reading_row.addWidget(self.stop);bulk.addLayout(reading_row)
        self.read_progress=QtWidgets.QProgressBar();self.read_progress.setRange(0,1);self.read_progress.setValue(0);self.read_progress.setTextVisible(False);self.read_progress.setFixedHeight(5);bulk.addWidget(self.read_progress)
        self.preview_title=label('待导入模型','rhModelSectionTitle');bulk.addWidget(self.preview_title)
        self.preview=QtWidgets.QPlainTextEdit();self.preview.setReadOnly(True);self.preview.setPlaceholderText('读取后在此预览模型，确认导入才会保存到本地。')
        self.preview.setMinimumHeight(140);self.preview.setMaximumHeight(180);self.preview.setObjectName('rhImportPreview');bulk.addWidget(self.preview)
        self.all_versions=QtWidgets.QCheckBox('导入所有可用版本');bulk.addWidget(self.all_versions)
        bulk.addWidget(label('默认只导入每个模型的首个可用版本。'))
        self.save_all=QtWidgets.QPushButton('确认导入到'+self.target);self.save_all.setObjectName('rhModelPrimary');self.save_all.setEnabled(False)
        self.save_all.clicked.connect(self._begin_import);bulk.addStretch()
        self.tabs.addTab(bulk_page,'从官网读取')
        self.status=self.message
        self.button('关闭',self.reject)
        self.actions.addWidget(self.save_one);self.actions.addWidget(self.save_all)
        self.tabs.currentChanged.connect(self._tab_changed);self._tab_changed(0)
        for button in self.findChildren(QtWidgets.QPushButton):button.setAutoDefault(False)
        self.resolved.connect(self._resolved,QtCore.Qt.QueuedConnection)
        self.site.currentIndexChanged.connect(self._site_changed)

    def _tab_changed(self, index):
        self.save_one.setVisible(index==0);self.save_all.setVisible(index==1)

    def _link_changed(self):
        self.generation+=1;self.single=None;self.save_one.setEnabled(False);self.versions.clear();self.model_label.clear()
        self.versions.hide();self.model_label.hide()

    def _read_link(self):
        if not self.read_link.isEnabled() or self.reading:return
        self.generation+=1;generation=self.generation;url=self.url.text()
        self.single=None;self.save_one.setEnabled(False);self.read_link.setEnabled(False);self.status.setText('正在读取模型网页…')
        from .rh_model_import import model_link
        try:site,unused,visibility=model_link(url)
        except ValueError as error:self.read_link.setEnabled(True);self.status.setText(str(error));return
        if visibility=='self':
            self._ensure_browser(site)
            self.site.setEnabled(False);self.login.setEnabled(False)
            self.session_mode=('single',generation,url);self.browser.capture_session();return
        self._link_worker(generation,url)

    def _link_worker(self, generation, url, authorization=None):
        def work():
            try:result,error=read_model_link(url,authorization=authorization),''
            except ValueError as exc:result,error=None,str(exc)
            except Exception:result,error=None,'无法读取模型网页，请稍后重试'
            try:self.resolved.emit(generation,result,error)
            except RuntimeError:pass
        threading.Thread(target=work,name='rh-model-link',daemon=True).start()

    def _resolved(self, generation, result, error):
        if self._closed:return
        self.read_link.setEnabled(True)
        self.site.setEnabled(True);self.login.setEnabled(True)
        if generation!=self.generation:return
        if error:self.status.setText(error);return
        self.single=result;site,record=result
        self.model_label.setText(model_title(record)+' · '+record['resourceType']+' · '+('国际站' if site.endswith('.ai') else '中文站'))
        self.versions.clear()
        for version in model_versions(record)[:50]:
            if version.get('node_token'):self.versions.addItem(str(version.get('version') or '默认版本'),version)
        self.save_one.setEnabled(self.versions.count()>0);self.status.setText('请选择版本后导入；已有收藏会保留原设置。')
        self.model_label.show();self.versions.setVisible(self.versions.count()>0)

    def _save_value(self, value):
        value['bucket']=self.bucket
        if self.store.lookup(value['site'],value['resource_type'],value['model_name']):
            self.skip_count+=1;return False
        self.store.save(value);self.import_count+=1;self.last_value=value;return True

    def _save_one(self):
        if not self.single or not self.versions.currentData():return
        from .rh_model_favorites import favorite_data
        value=dict(favorite_data(self.single[0],self.single[1],self.versions.currentData()),source='website')
        try:
            if self._save_value(value):self.status.setText('已导入到'+('国际站' if value['site'].endswith('.ai') else '中文站')+' · '+self.target+'。')
            else:self.status.setText('该版本已在本地收藏中，保留现有设置。')
            self.imported.emit(value)
        except Exception:self.status.setText('保存收藏失败，请检查本地收藏目录的写入权限')

    def _ensure_browser(self, site):
        from .rh_model_browser import ModelBrowser
        if self.browser and not self.browser.closed and self.browser.site==site:return
        if self.browser:self.browser.close()
        self.browser=ModelBrowser(site,self)
        self.browser.state.connect(self._browser_state);self.browser.error.connect(self._browser_error)
        self.browser.session_ready.connect(self._start_http)

    def _open_browser(self):
        if self.reading or self.importing:return
        self._ensure_browser(self.site.currentData())
        self.status.setText('请在默认浏览器的普通窗口登录所选站点，完成后返回客户端点击读取。')
        self.browser.open()

    def _site_changed(self):
        if self.browser:self.browser.close();self.browser=None
        self.session_mode=None
        self.stage.seek(0);self.stage.truncate();self.staged=0
        self.save_all.setEnabled(False);self.preview.clear()
        self.preview_title.setText('待导入模型')
        self.login_state.setText('点击读取时检查默认浏览器登录状态')

    def _browser_state(self, state):
        if self._closed or self.sender() is not self.browser:return
        self.login_state.setText('已打开默认浏览器，登录后返回此处点击读取')

    def _browser_error(self, message):
        if self._closed or self.sender() is not self.browser:return
        self.login_state.setText('未能确认默认浏览器登录状态')
        if self.worker and self.reading:return
        mode=self.session_mode
        if isinstance(mode,tuple) and mode[0]=='single':
            self.site.setCurrentIndex(SITES.index(self.browser.site))
        self.read_link.setEnabled(True);self.session_mode=None;self.read_progress.setRange(0,1)
        self.reading=False;self.read_all.setEnabled(True);self.stop.setEnabled(False);self.login.setEnabled(True)
        self.site.setEnabled(True);self.save_all.setEnabled(self.staged>0);self.status.setText(message)

    def _read_all(self):
        if self.reading or self.importing or not self.read_link.isEnabled():return
        self._ensure_browser(self.site.currentData())
        self.stage.seek(0);self.stage.truncate();self.staged=0;self.reading=True
        self.stage_site=self.browser.site
        self.read_all.setEnabled(False);self.save_all.setEnabled(False);self.login.setEnabled(False);self.stop.setEnabled(True)
        self.site.setEnabled(False);self.preview.clear()
        self.preview_title.setText('正在读取模型…')
        self.session_mode='bulk';self.read_progress.setRange(0,0)
        self.status.setText('正在确认登录会话…');self.browser.capture_session()

    def _start_http(self, session):
        if self._closed or self.sender() is not self.browser:
            session.clear();return
        mode=self.session_mode;self.session_mode=None
        self.login_state.setText('已读取默认浏览器登录状态，正在向官网验证…')
        if isinstance(mode,tuple) and mode[0]=='single':
            authorization=session.get('authorization');session.clear()
            if mode[1]==self.generation:self._link_worker(mode[1],mode[2],authorization)
            else:
                self.read_link.setEnabled(True);self.site.setEnabled(True);self.login.setEnabled(True)
            return
        if not self.reading:session.clear();return
        from .rh_model_http import CollectionRead
        self.worker=CollectionRead(self.stage_site,self.bucket,session.get('authorization'),self);session.clear()
        self.worker.progress.connect(self._read_progress,QtCore.Qt.QueuedConnection)
        self.worker.finished.connect(self._read_finished,QtCore.Qt.QueuedConnection)
        self.worker.start()

    def _read_progress(self, count, kind):
        if self._closed or self.sender() is not self.worker:return
        self.status.setText(f'正在读取 {kind} · 已获取 {count} 个模型')

    def _read_finished(self, result):
        if self._closed or self.sender() is not self.worker:
            if result.get('stage'):result['stage'].close()
            return
        self.stage.close();self.stage=result.get('stage') or tempfile.TemporaryFile(mode='w+t',encoding='utf8')
        self.staged=result['count'];self.stage_site=result['site'];self.reading=False
        self.stop.setEnabled(False);self.login.setEnabled(True);self.read_all.setEnabled(self.browser and not self.browser.closed)
        self.site.setEnabled(True)
        self.login_state.setText('读取完成' if not result.get('error') else '读取未完成；若登录失效，请在默认浏览器重新登录后读取')
        self.read_progress.setRange(0,1);self.read_progress.setValue(1);self.save_all.setEnabled(self.staged>0)
        labels=[]
        for unused in range(min(5,self.staged)):
            value=json.loads(self.stage.readline());labels.append(model_title(value)+' · '+str(value.get('resourceType') or ''))
        self.stage.seek(0);self.preview.setPlainText('\n'.join(labels) or '没有读取到模型')
        self.preview_title.setText(f'已读取 {self.staged} 个模型'+(' · 预览前 5 项' if self.staged>5 else ''))
        self.status.setText(f'已读取 {self.staged} 个模型。'+(result.get('error') or '点击确认导入后保存到'+self.target+'，已有版本保留原设置。'))
        self.worker.deleteLater();self.worker=None

    def _stop_read(self):
        if self.worker:self.worker.cancel();self.status.setText('正在停止读取，已读取部分会保留…')
        else:
            if self.browser:self.browser.cancel_capture()
            self.session_mode=None
            self.reading=False;self.stop.setEnabled(False);self.login.setEnabled(True);self.read_progress.setRange(0,1)
            self.site.setEnabled(True);self.read_all.setEnabled(True);self.status.setText('已停止读取登录状态')

    def _begin_import(self):
        if self.importing or self.reading or not self.staged:return
        self.stage.seek(0);self.pending.clear();self.importing=True;self.import_count=self.skip_count=self.invalid_count=0
        self.import_site=self.stage_site;self.import_all_versions=self.all_versions.isChecked();self.last_value=None
        self.tabs.setEnabled(False);self.save_all.setEnabled(False);self.timer.start()

    def _import_chunk(self):
        deadline=time.monotonic()+.012;done=False
        try:
            with QtCore.QSignalBlocker(self.store):
                for unused in range(20):
                    if not self.pending:
                        line=self.stage.readline()
                        if not line:done=True;break
                        self.pending.extend(import_values(self.import_site,json.loads(line),self.import_all_versions))
                        if not self.pending:self.invalid_count+=1;continue
                    self._save_value(self.pending.popleft())
                    if time.monotonic()>=deadline:break
            self.status.setText(f'已导入 {self.import_count} 个版本，跳过 {self.skip_count} 个已有版本。')
            if done:
                self._finish_import()
                if self.invalid_count:self.status.setText(self.status.text()+f'另有 {self.invalid_count} 个模型没有可用版本。')
        except Exception:
            self._finish_import();self.status.setText(f'导入中断，已保存 {self.import_count} 个版本。请检查收藏目录后重试，已保存项会自动跳过。')

    def _finish_import(self):
        self.timer.stop();self.importing=False;self.tabs.setEnabled(True);self.save_all.setEnabled(self.staged>0);self.store.changed.emit()
        if self.last_value:self.imported.emit(self.last_value)

    def done(self, result):
        self._closed=True;self.reading=False
        self.generation+=1
        if self.importing:self._finish_import()
        if self.browser:self.browser.close()
        if self.worker:self.worker.close()
        self.stage.close();super().done(result)
