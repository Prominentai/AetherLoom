"""Named provider connections and direct credential editing for API cards."""
import copy
import os
import uuid
from PyQt5 import QtCore, QtWidgets
from . import api_manager
from .api_credentials import get_credentials
from .api_manager_ui import credential_events, persist_credentials
from .translation import DEFAULT_PROMPT
from .agent_catalog import AGENTS, supports
from . import image_model_catalog

PROTOCOL_OPTIONS = {
    'llm': [('自动识别（兼容旧配置）', 'auto'), ('OpenAI · Chat Completions', 'protocol_openai'),
            ('OpenAI · Responses', 'protocol_responses'), ('Anthropic · Messages', 'protocol_claude'),
            ('Ollama · Chat', 'protocol_ollama'), ('Ollama · Generate', 'protocol_ollama_generate')],
    'translator': [('自动识别（兼容旧配置）', 'auto'), ('Google Translation v2', 'google_translate'),
                   ('百度翻译', 'baidu_translate')],
}
PROTOCOL_OPTIONS['vision'] = PROTOCOL_OPTIONS['llm']
PROTOCOL_EXAMPLES = {
    'protocol_openai': 'https://你的网关/v1/chat/completions',
    'protocol_responses': 'https://你的网关/v1/responses',
    'protocol_claude': 'https://你的网关/v1/messages',
    'protocol_ollama': 'http://localhost:11434/api/chat',
    'protocol_ollama_generate': 'http://localhost:11434/api/generate',
    'google_translate': 'https://translation.googleapis.com/language/translate/v2',
    'baidu_translate': 'https://fanyi-api.baidu.com/api/trans/vip/translate',
}


def register_profiles(profiles):
    for category, values in (profiles or {}).items():
        if not isinstance(values, dict):
            continue
        for identity, config in values.items():
            if not identity.startswith('user_') or not isinstance(config, dict):
                continue
            template = config.get('template', 'custom')
            if template in AGENTS and not supports(template, category):
                continue
            entry = api_manager.find_provider(category, template) or {}
            api_manager.PROVIDERS[identity] = dict(name=config.get('name') or '自定义供应商', endpoint=config.get('endpoint') or entry.get('endpoint', ''), template=template)
            existing = api_manager.CATEGORY_PROVIDERS.setdefault(category, [])
            existing[:] = [v for v in existing if v.get('key') != identity]
            existing.append(dict(key=identity, models=entry.get('models', [])))


class ProviderEditor(QtCore.QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.pending = owner, {}
        self.timer = QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(500)
        self.timer.timeout.connect(self.save_keys)
        self.prompts = {}
        self.image_hints = {}
        self.protocol_rows = {}
        for category, fields in owner.api_config_fields.items():
            card = owner._api_model_cards[category]
            if category in PROTOCOL_OPTIONS:
                box = QtWidgets.QWidget();layout = QtWidgets.QVBoxLayout(box);layout.setContentsMargins(0,0,0,0)
                combo = QtWidgets.QComboBox()
                for label, value in PROTOCOL_OPTIONS[category]:combo.addItem(label, value)
                owner._install_combo_wheel_blocker(combo)
                hint = QtWidgets.QLabel();hint.setWordWrap(True);hint.setObjectName('apiMuted')
                layout.addWidget(combo);layout.addWidget(hint)
                fields['form'].insertRow(1, '协议类型', box)
                fields['api_protocol'] = combo;self.protocol_rows[category] = (box,hint)
                combo.currentIndexChanged.connect(lambda _, cat=category:self.protocol_changed(cat))
            row = QtWidgets.QHBoxLayout()
            add = QtWidgets.QPushButton('＋ 添加供应商');add.setObjectName('apiSecondaryButton')
            add.clicked.connect(lambda _, cat=category:self.add_dialog(cat));row.addWidget(add)
            rename = QtWidgets.QPushButton('重命名');rename.setObjectName('apiSecondaryButton')
            rename.clicked.connect(lambda _, cat=category:self.rename(cat));row.addWidget(rename)
            duplicate = QtWidgets.QPushButton('复制配置');duplicate.setObjectName('apiSecondaryButton')
            duplicate.setToolTip('创建独立副本，复制地址、协议、模型和超时；密钥或登录授权需另行填写。')
            duplicate.clicked.connect(lambda _, cat=category:self.duplicate(cat));row.addWidget(duplicate)
            save = QtWidgets.QPushButton('保存密钥');save.setObjectName('apiSecondaryButton')
            save.clicked.connect(self.save_keys);row.addWidget(save);row.addStretch()
            fields['save_key_button'] = save;fields['rename_button'] = rename
            fields['duplicate_button'] = duplicate
            card.body_layout.insertLayout(1, row)
            if fields.get('model_row') is not None:
                browse = QtWidgets.QPushButton('浏览模型');browse.setObjectName('apiSecondaryButton')
                browse.setToolTip('搜索当前候选列表，或手动指定模型 ID；打开窗口不调用 API。')
                browse.clicked.connect(lambda _, cat=category:self.browse_models(cat))
                fields['model_row'].layout().insertWidget(1,browse)
                fields['browse_models_button'] = browse
            if category in image_model_catalog.IMAGE_CATEGORIES:
                hint = QtWidgets.QLabel();hint.setWordWrap(True);hint.setObjectName('apiMuted')
                hint.setTextFormat(QtCore.Qt.PlainText)
                card.body_layout.insertWidget(3, hint);self.image_hints[category] = hint
                fields['model'].currentTextChanged.connect(lambda _, cat=category:self.image_model_changed(cat))
            fields['provider'].currentIndexChanged.connect(lambda _, cat=category:self.refresh(cat))
            for name in ('api_key','baidu_appid','baidu_secret'):
                fields[name].textEdited.connect(lambda _, cat=category:self.key_edited(cat))
                fields[name].editingFinished.connect(self.save_keys)
            if category == 'translator':
                prompt = QtWidgets.QPlainTextEdit();prompt.setObjectName('apiTranslationPrompt')
                prompt.setPlaceholderText(DEFAULT_PROMPT);prompt.setMinimumHeight(88);prompt.setMaximumHeight(150)
                prompt.setPlainText((owner.api_settings.get(category) or {}).get('translation_prompt') or DEFAULT_PROMPT)
                label = QtWidgets.QLabel('大语言模型翻译提示词 · {target_lang} 为目标语言')
                label.setWordWrap(True);card.body_layout.insertWidget(3,label);card.body_layout.insertWidget(4,prompt)
                fields['translation_prompt'] = prompt;self.prompts[category] = (label,prompt)
                prompt.textChanged.connect(self.settings_changed)
        credential_events().saved.connect(self.credentials_saved)
        from .agent_ui import AgentAccounts
        self.agents = AgentAccounts(self)
        self.refresh_all()

    def identity(self, category):
        value = self.owner.api_config_fields[category]['provider'].currentData()
        return 'custom_'+category if value == 'custom' else value

    def protocol(self, category):
        identity = self.identity(category)
        return api_manager.effective_protocol(category, identity, self.owner._get_api_provider_profile(category, identity))

    def protocol_changed(self, category):
        identity = self.identity(category)
        profile = self.owner._get_api_provider_profile(category, identity)
        selected = self.owner.api_config_fields[category]['api_protocol'].currentData()
        if not api_manager.is_custom_connection(identity, profile):return
        # Preserve edits made to credentials before changing the form's auth fields.
        self.save_keys()
        profile['api_protocol'] = selected
        self.owner._set_api_provider_profile(category, identity, profile)
        self.refresh(category)
        self.owner._api_probe_controllers[category]._configuration_changed()
        self.settings_changed()

    def refresh_all(self):
        for category in self.owner.api_config_fields:self.refresh(category)

    def refresh(self, category):
        f = self.owner.api_config_fields[category];identity=self.identity(category)
        protocol = self.protocol(category);special=identity in ('free_translate','llm_translate')
        config = self.owner.api_settings.get(category)
        if isinstance(config, dict):config['protocol'] = protocol
        agent = protocol in AGENTS
        if category in self.protocol_rows:
            box,hint = self.protocol_rows[category]
            profile = self.owner._get_api_provider_profile(category, identity)
            visible = api_manager.is_custom_connection(identity, profile)
            box.setVisible(visible);f['form'].labelForField(box).setVisible(visible)
            profile = self.owner._get_api_provider_profile(category, identity)
            choice = profile.get('api_protocol','auto') if visible else 'auto'
            combo = f['api_protocol']
            with QtCore.QSignalBlocker(combo):
                if combo.findData(choice)<0:combo.addItem('不支持的协议（请重新选择）',choice)
                combo.setCurrentIndex(combo.findData(choice))
            example = PROTOCOL_EXAMPLES.get(choice,'https://你的服务/完整接口路径')
            f['endpoint'].setPlaceholderText(example if visible else '')
            hint.setText('按地址及供应商模板识别；新配置建议明确选择协议。' if choice=='auto' else
                         '使用所选协议的鉴权与请求格式；请核对接口地址。示例：'+example)
        creds = get_credentials(self.owner._apikeys,identity,category)
        for name, field in (('api_key','api_key'),('appid','baidu_appid'),('secret','baidu_secret')):
            w=f[field]
            with QtCore.QSignalBlocker(w):w.setText('' if agent else creds.get(name,''))
            w.setReadOnly(False);w.setEnabled(not special)
        f['api_key'].setToolTip('可直接输入并保存；与 API 密钥管理使用同一份本地密钥。')
        f['api_key'].setPlaceholderText('输入 API Key，修改后自动保存到本地')
        if protocol in ('volcengine', 'byteplus_ap', 'byteplus_eu'):
            region = {'volcengine': '国内火山方舟·北京', 'byteplus_ap': 'BytePlus·亚太柔佛',
                      'byteplus_eu': 'BytePlus·欧洲都柏林'}[protocol]
            f['api_key'].setPlaceholderText(f'输入 {region} API Key')
            f['api_key'].setToolTip('仅使用所选站点及区域的 API Key；国内、国际亚太、国际欧洲分别保存，不互相借用密钥。')
        f['api_key_stack'].setCurrentIndex(1 if protocol=='baidu_translate' else 0)
        f['endpoint'].setReadOnly(agent);f['endpoint'].setEnabled(not special)
        if agent:
            with QtCore.QSignalBlocker(f['endpoint']):f['endpoint'].setText(
                image_model_catalog.endpoint(protocol, category) or AGENTS[protocol]['endpoint'])
            from .agent_catalog import llm_name
            model = f['model']
            old = model.currentText()
            with QtCore.QSignalBlocker(model):
                for i in range(model.count()-1, -1, -1):
                    if not llm_name(protocol, model.itemText(i)):model.removeItem(i)
                model.setEditText(llm_name(protocol, old))
            model.lineEdit().setPlaceholderText('登录后刷新并选择 LLM，或输入 LLM 名称')
            if old and not llm_name(protocol, old):
                profile = self.owner._get_api_provider_profile(category, identity)
                profile['model'] = ''
                self.owner._set_api_provider_profile(category, identity, profile)
                self.owner.api_settings.setdefault(category, {})['model'] = ''
        f['model'].setEnabled(not special)
        docs=f.get('model_docs_btn')
        if docs is not None:
            docs.setVisible(not agent)
            url=api_manager.get_model_list_url(protocol)
            docs.setEnabled(bool(url));docs.setProperty('model_docs_url',url)
        f['save_key_button'].setVisible(not special and not agent)
        f['rename_button'].setVisible(bool(identity and identity.startswith('user_')))
        f['duplicate_button'].setVisible(not special)
        if f.get('browse_models_button') is not None:f['browse_models_button'].setVisible(not special and category!='translator')
        form=f.get('form')
        if form is not None:form.setVerticalSpacing(3 if special else 8 if agent else 12)
        for w in (f['endpoint'],f.get('model_row'),f.get('key_row'),f['timeout']):
            if w is not None and form is not None:
                visible=not special or w is f['timeout'] and identity=='free_translate'
                if agent and w in (f['endpoint'], f.get('key_row')):visible=False
                w.setVisible(visible)
                label=form.labelForField(w)
                if label is not None:label.setVisible(visible)
        if form is not None and f.get('model_row') is not None:
            label = form.labelForField(f['model_row'])
            if label is not None:label.setText('Agent LLM' if agent else '模型名称')
        if category in self.prompts:
            for w in self.prompts[category]:w.setVisible(identity=='llm_translate')
            self.owner._api_model_cards[category].summary.setText(
                '免费翻译 · 无需 API Key' if identity=='free_translate' else
                '大语言模型翻译 · 使用大语言模型区的当前供应商和模型' if identity=='llm_translate' else
                f['provider'].currentText()+' · '+f['model'].currentText())
        self.owner._api_probe_controllers[category].refresh_button.setEnabled(not special)
        probe = self.owner._api_probe_controllers[category]
        probe.refresh_button.setVisible(not special and category not in image_model_catalog.IMAGE_CATEGORIES)
        if category in image_model_catalog.IMAGE_CATEGORIES:
            can_list = agent or protocol in ('openai', 'grok', 'gemini', 'siliconflow_cn', 'siliconflow_com')
            probe.refresh_button.setVisible(can_list)
            probe.test_button.setVisible(agent)
            for w in (probe.status_label, probe.card.state_badge):w.setVisible(agent or can_list)
            probe.test_button.setText('测试图像生成（调用一次）' if category == 'text2img' else '测试图像编辑（调用一次）')
            self.image_model_changed(category)
        self.agents.refresh(category)

    def image_model_changed(self, category):
        fields = self.owner.api_config_fields[category]
        protocol = self.protocol(category);model = fields['model'].currentText().strip()
        if protocol not in AGENTS:
            old = fields['endpoint'].text().strip()
            endpoint = image_model_catalog.follow_model_endpoint(protocol, category, model, old)
            if endpoint != old:fields['endpoint'].setText(endpoint)
        hint = self.image_hints[category]
        hint.setVisible(protocol not in AGENTS)
        hint.setText(image_model_catalog.model_note(protocol, category, model))
        docs = fields.get('model_docs_btn')
        if docs is not None and protocol not in AGENTS:
            url = image_model_catalog.docs_url(protocol, category)
            docs.setProperty('model_docs_url', url);docs.setEnabled(bool(url))

    def key_edited(self, category):
        identity=self.identity(category);f=self.owner.api_config_fields[category]
        if identity in ('free_translate','llm_translate') or self.protocol(category) in AGENTS:return
        data = ({'appid':f['baidu_appid'].text().strip(),'secret':f['baidu_secret'].text().strip()}
                if self.protocol(category)=='baidu_translate' else {'api_key':f['api_key'].text().strip()})
        self.pending[identity]=data;self.owner._apikeys[identity]=copy.deepcopy(data)
        # Mirror known-key rows so the older key manager cannot overwrite edits.
        self.sync_key_rows({identity:data});self.timer.start()

    def sync_key_rows(self, values):
        for identity, data in values.items():
            row=getattr(self.owner,'apikey_rows',{}).get(identity,{})
            for name, field in (('api_key','key_edit'),('appid','appid'),('secret','secret')):
                w=row.get(field)
                if w is not None:
                    with QtCore.QSignalBlocker(w):w.setText(get_credentials({identity:data},identity).get(name,''))

    def save_keys(self, *_):
        self.timer.stop()
        if not self.pending:return
        try:
            updates=copy.deepcopy(self.pending)
            self.owner._apikeys=persist_credentials(self.owner._apikeys_file,updates,updates)
            self.pending.clear()
        except (OSError,ValueError):
            for card in self.owner._api_model_cards.values():card.summary.setText('密钥保存失败，请检查本地文件写入权限后重试。')

    def credentials_saved(self, path, data):
        if os.path.normcase(os.path.abspath(self.owner._apikeys_file))!=path:return
        merged=copy.deepcopy(data);merged.update(self.pending)
        self.owner._apikeys=merged;self.sync_key_rows(merged);self.refresh_all()

    def settings_changed(self):
        self.owner._collect_api_settings_from_ui();self.owner._save_settings()

    def add_connection(self, category, name, template='custom', api_protocol=None):
        name=str(name).strip()
        if not name:raise ValueError('请填写供应商名称')
        if template in AGENTS and not supports(template, category):raise ValueError('此 Agent 不支持当前模型类别')
        if api_protocol is None:
            api_protocol = ('protocol_openai' if category in ('llm','vision') else 'google_translate') if template=='custom' and category in PROTOCOL_OPTIONS else 'auto'
        if api_protocol != 'auto' and (template != 'custom' or api_protocol not in dict((v,k) for k,v in PROTOCOL_OPTIONS.get(category,[]))):
            raise ValueError('此类别或供应商不支持所选协议')
        identity='user_'+uuid.uuid4().hex
        entry=api_manager.find_provider(category,template) or {}
        self.owner._set_api_provider_profile(category,identity,dict(name=name,template=template,
            endpoint=entry.get('endpoint',''),model='',timeout=30,api_protocol=api_protocol))
        register_profiles(self.owner.api_provider_profiles)
        combo=self.owner.api_config_fields[category]['provider']
        combo.addItem(name,identity);combo.setCurrentIndex(combo.findData(identity))
        self.refresh(category);self.settings_changed()
        return identity

    def browse_models(self, category):
        from .api_model_selector import ModelSelector
        f = self.owner.api_config_fields[category];combo = f['model']
        identity = self.identity(category)
        context = (identity,self.protocol(category),f['endpoint'].text())
        names = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i).strip()]
        dialog = ModelSelector(self.owner,f['provider'].currentText(),names,combo.currentText())
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            if context == (self.identity(category),self.protocol(category),f['endpoint'].text()):
                # insert() retains the line editor's undo history; no edit on cancel.
                edit = combo.lineEdit()
                if edit is not None:edit.selectAll();edit.insert(dialog.selected_model)
                else:combo.setCurrentText(dialog.selected_model)
                self.settings_changed()
        dialog.deleteLater()

    def duplicate(self, category):
        f = self.owner.api_config_fields[category];identity = self.identity(category)
        if identity in ('free_translate','llm_translate'):return
        self.save_keys();self.owner._collect_api_settings_from_ui()
        profile = self.owner._get_api_provider_profile(category,identity)
        template = profile.get('template') or ('custom' if str(identity).startswith('custom') else identity)
        config = dict(endpoint=f['endpoint'].text(),model=f['model'].currentText(),timeout=f['timeout'].value())
        if 'web_search' in profile:config['web_search'] = profile['web_search']
        if f.get('translation_prompt') is not None:config['translation_prompt'] = f['translation_prompt'].toPlainText()
        existing = {r['name'] for r in api_manager.get_providers(category)}
        base = f['provider'].currentText() + ' 副本';name=base;number=2
        while name in existing:name=f'{base} {number}';number+=1
        new_id = self.add_connection(category,name,template,profile.get('api_protocol','auto') if template=='custom' else 'auto')
        self.owner._set_api_provider_profile(category,new_id,config)
        self.agents.refresh(category)
        f['endpoint'].setText(config['endpoint']);f['model'].setEditText(config['model']);f['timeout'].setValue(config['timeout'])
        self.settings_changed()
        self.owner._api_probe_controllers[category]._set_status('已创建独立副本，请填写此连接的密钥或登录授权。')
        return new_id

    def add_dialog(self, category):
        dialog=QtWidgets.QDialog(self.owner);dialog.setWindowTitle('添加供应商')
        dialog.setStyleSheet(self.owner.api_page.styleSheet().replace('#api_page_root',''))
        form=QtWidgets.QFormLayout(dialog);form.setContentsMargins(22,20,22,20);form.setSpacing(12)
        name=QtWidgets.QLineEdit();name.setPlaceholderText('例如：工作账户 / 本地网关')
        template=QtWidgets.QComboBox();template.addItem('自定义接口','custom')
        for entry in api_manager.get_providers(category):
            if entry['key'] not in ('free_translate','llm_translate') and not entry['key'].startswith('user_'):
                template.addItem(entry['name'],entry['key'])
        self.owner._install_combo_wheel_blocker(template)
        form.addRow('供应商名称',name);form.addRow('接口模板',template)
        protocol = QtWidgets.QComboBox()
        if category in PROTOCOL_OPTIONS:
            for label,value in PROTOCOL_OPTIONS[category]:protocol.addItem(label,value)
            self.owner._install_combo_wheel_blocker(protocol);form.addRow('协议类型',protocol)
            def update_protocol():
                enabled = template.currentData() == 'custom'
                protocol.setEnabled(enabled)
                protocol.setVisible(enabled);form.labelForField(protocol).setVisible(enabled)
                value = ('protocol_openai' if category in ('llm','vision') else 'google_translate') if template.currentData()=='custom' else 'auto'
                protocol.setCurrentIndex(protocol.findData(value))
            template.currentIndexChanged.connect(update_protocol);update_protocol()
        hint=QtWidgets.QLabel('名称由你指定；添加后可独立配置地址、密钥和模型。');hint.setWordWrap(True);form.addRow(hint)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save|QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(buttons.Save).setText('添加');buttons.button(buttons.Cancel).setText('取消')
        def save():
            if not name.text().strip():name.setFocus();return
            self.add_connection(category,name.text(),template.currentData(),protocol.currentData() if category in PROTOCOL_OPTIONS else 'auto');dialog.accept()
        buttons.accepted.connect(save);buttons.rejected.connect(dialog.reject);form.addRow(buttons)
        dialog.resize(460,240);dialog.exec_();dialog.deleteLater()

    def rename(self, category):
        identity=self.identity(category)
        if not identity.startswith('user_'):
            self.owner._api_model_cards[category].summary.setText('预置供应商可通过“添加供应商”创建独立命名的配置。');return
        profile=self.owner._get_api_provider_profile(category,identity)
        name,ok=QtWidgets.QInputDialog.getText(self.owner,'重命名供应商','名称',text=profile.get('name',''))
        if ok and name.strip():
            profile['name']=name.strip();self.owner._set_api_provider_profile(category,identity,profile)
            combo=self.owner.api_config_fields[category]['provider'];combo.setItemText(combo.currentIndex(),name.strip())
            register_profiles(self.owner.api_provider_profiles);self.settings_changed()
