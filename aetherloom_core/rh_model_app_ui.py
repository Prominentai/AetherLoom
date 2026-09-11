"""Add Standard Model/LLM applications and separate dashboard categories."""
import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path
import requests
from PyQt5 import QtCore, QtWidgets
from . import rh_model_apps as models

_reads = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='rh-model-list')


def _model_names(names):
    if not isinstance(names, list):raise ValueError('Invalid model list')
    return sorted({name.strip() for name in names if isinstance(name, str) and 0 < len(name.strip()) <= 512})[:2000]


def _cache_path(root, site):
    region = 'cn' if models.official_site(site).endswith('.cn') else 'ai'
    return Path(root).parent / '.rh_model_cache' / ('llm_' + region + '.json')


def _cached_models(root, site):
    try:
        path = _cache_path(root, site)
        if path.stat().st_size > 2 * 1024 * 1024:return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('site') != site:return None
        return _model_names(data['models']), data['updated_at']
    except (OSError, ValueError, KeyError, TypeError, AttributeError):return None


def _local_time(value):
    try:return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):return '时间未知'


def read_llm_models(base_url, key):
    from .rh_model_runtime import llm_base
    # Both regional catalogs are public; avoid an unrelated/consumer key
    # preventing model discovery. Retry authenticated only when required.
    url = llm_base(base_url) + '/models'
    with requests.get(url, timeout=15, allow_redirects=False) as response:
        if response.status_code in (401, 403) and key:
            with requests.get(url, headers={'Authorization': 'Bearer ' + key}, timeout=15, allow_redirects=False) as private:
                private.raise_for_status();data = private.json()
        else:
            response.raise_for_status();data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise ValueError('Invalid model catalog response')
    return _model_names([item.get('id') for item in data['data'] if isinstance(item, dict)])


class _AddApplicationDialog(QtWidgets.QDialog):
    def __init__(self, owner, root, title, size):
        super().__init__(owner)
        self.owner, self.root = owner, root
        self.setWindowTitle(title);self.resize(*size)
        self.setStyleSheet('QLabel { background: transparent; } QListWidget::item { min-height: 26px; padding: 3px 6px; }')
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(20, 18, 20, 18);layout.setSpacing(12)
        hint = QtWidgets.QLabel('模型应用与普通应用共用运行页、任务队列和输出卡片。运行需要当前站点的企业级共享 API Key。')
        hint.setWordWrap(True);layout.addWidget(hint)
        hint.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self.add_model_controls(layout)
        self.name = QtWidgets.QLineEdit();self.name.setPlaceholderText('应用名称（留空使用模型名称，可添加多个独立配置）');layout.addWidget(self.name)
        self.status = QtWidgets.QLabel();self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum);layout.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(buttons.Ok).setText('添加到应用');buttons.accepted.connect(self.add);buttons.rejected.connect(self.reject)
        buttons.button(buttons.Cancel).setText('取消')
        layout.addWidget(buttons)

    def add(self):
        try:
            connection = self.owner._rh_connection_snapshot()
            application = self.application(connection['base_url'])
            models.install(self.root, application)
            self.owner._rh_reload_apps()
            if getattr(self.owner, 'canvas_page', None):self.owner.canvas_page.refresh_apps()
            tabs = getattr(self.owner, '_rh_application_tabs', None)
            if tabs:tabs.setCurrentIndex(2 if self.kind == 'rh_llm' else 1)
            self.accept()
        except Exception as error:self.status.setText(str(error))


class AddStandardModelApplication(_AddApplicationDialog):
    kind = 'rh_standard'

    def __init__(self, owner, root):
        super().__init__(owner, root, '添加标准模型', (720, 600))
        self.future = None
        self.timer = QtCore.QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.finish_fetch)
        self.finished.connect(self.stop_fetch)

    def add_model_controls(self, layout):
        self.catalog_data = models.catalog_document(self.root)
        self.catalog_info = QtWidgets.QLabel()
        self.catalog_info.setWordWrap(True)
        self.catalog_info.setToolTip(models.CATALOG_SOURCE)
        self.show_catalog_info()
        layout.addWidget(self.catalog_info)
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit();self.search.setPlaceholderText('搜索名称、功能或模型接口');row.addWidget(self.search, 1)
        self.read = QtWidgets.QPushButton('更新目录');self.read.setToolTip('从 RH 官方 GitHub 更新表单目录；已添加的应用保留当前配置')
        self.read.clicked.connect(self.fetch);row.addWidget(self.read);layout.addLayout(row)
        self.entries = self.catalog_data['models']
        self.list = QtWidgets.QListWidget();self.list.setUniformItemSizes(True);layout.addWidget(self.list, 1)
        self.detail = QtWidgets.QLabel();self.detail.setWordWrap(True);layout.addWidget(self.detail)
        self.search.textChanged.connect(self.filter)
        self.list.currentItemChanged.connect(self.describe)
        self.filter()

    def application(self, base_url):
        item = self.list.currentItem()
        if item is None:raise ValueError('请选择标准模型')
        return models.standard_application(self.entries[item.data(QtCore.Qt.UserRole)], self.name.text(), base_url)

    def filter(self, *_, selected_endpoint=None):
        if selected_endpoint is None and self.list.currentItem() is not None:
            selected_endpoint = self.entries[self.list.currentItem().data(QtCore.Qt.UserRole)]['endpoint']
        self.list.clear()
        query = self.search.text().strip().casefold()
        selected = None
        for index, entry in enumerate(self.entries):
            title = str(entry.get('name_cn') or entry['display_name'])
            if query not in (title + ' ' + entry['endpoint']).casefold():continue
            item = QtWidgets.QListWidgetItem(title);item.setData(QtCore.Qt.UserRole, index)
            item.setToolTip(entry['endpoint']);self.list.addItem(item)
            if entry['endpoint'] == selected_endpoint:selected = item
        if selected is not None:self.list.setCurrentItem(selected)
        elif self.list.count() and selected_endpoint is None:self.list.setCurrentRow(0)

    def describe(self, current, previous=None):
        if current is None:self.detail.clear();return
        entry = self.entries[current.data(QtCore.Qt.UserRole)]
        self.detail.setText(f"{entry['endpoint']}\n输出：{entry['output_type']} · 参数：{len(entry['params'])} 项")

    def show_catalog_info(self):
        data = self.catalog_data
        stamp = ('最近获取：' + _local_time(data['fetched_at'])) if data.get('fetched_at') else ('本地目录更新：' + str(data.get('bundled_at') or '未记录'))
        origin = 'GitHub 缓存目录' if data.get('fetched_at') else '内置目录'
        self.catalog_info.setText(f'来源：RH 官方插件 · {origin} · {len(data["models"])} 个模型\n{stamp}')

    def fetch(self):
        if self.future:return
        self.future = _reads.submit(models.fetch_catalog)
        self.read.setEnabled(False);self.status.setText('正在从 GitHub 获取最新表单目录…');self.timer.start()

    def finish_fetch(self):
        if not self.future or not self.future.done():return
        self.timer.stop();future, self.future = self.future, None;self.read.setEnabled(True)
        try:
            data = future.result()
            models.save_catalog(self.root, data)
            item = self.list.currentItem()
            endpoint = self.entries[item.data(QtCore.Qt.UserRole)]['endpoint'] if item is not None else None
            # Clear the old selection before replacing its index-to-definition mapping.
            self.list.clear()
            self.catalog_data = data;self.entries = data['models']
            self.filter(selected_endpoint=endpoint);self.show_catalog_info()
            self.status.setText(f'已更新 {len(self.entries)} 个模型表单；已添加应用的配置保持不变。')
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else '网络不可用或本地缓存无法保存'
            self.status.setText('更新失败，保留现有目录。' + reason)

    def stop_fetch(self, *_):
        self.timer.stop()
        if self.future:self.future.cancel()


class AddLlmApplication(_AddApplicationDialog):
    kind = 'rh_llm'

    def __init__(self, owner, root):
        super().__init__(owner, root, '添加 LLM', (680, 560))
        self.timer = QtCore.QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.finish_fetch)
        self.future = None
        self.catalog_site = ''
        self.names = []
        self.updated_at = None
        self.autoload = QtCore.QTimer(self);self.autoload.setSingleShot(True)
        self.autoload.timeout.connect(self.fetch)
        self.finished.connect(self.stop_fetch)
        try:self.select_site(self.owner._rh_connection_snapshot()['base_url'])
        except Exception:self.status.setText('无法读取当前站点；可在连接设置中调整后刷新。')
        self.autoload.start(0)

    def add_model_controls(self, layout):
        self.catalog_info = QtWidgets.QLabel();self.catalog_info.setWordWrap(True);layout.addWidget(self.catalog_info)
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit();self.search.setPlaceholderText('搜索 LLM 模型名称');row.addWidget(self.search, 1)
        self.read = QtWidgets.QPushButton('刷新列表');row.addWidget(self.read);layout.addLayout(row)
        self.list = QtWidgets.QListWidget();self.list.setUniformItemSizes(True);layout.addWidget(self.list, 1)
        self.model_name = QtWidgets.QLineEdit();self.model_name.setPlaceholderText('从列表选择，或手动填写 RH LLM 模型名');layout.addWidget(self.model_name)
        self.search.textChanged.connect(self.filter)
        self.list.currentItemChanged.connect(self.select_model)
        self.read.clicked.connect(self.fetch)

    def application(self, base_url):
        if self.catalog_site and models.official_site(base_url) != self.catalog_site:
            raise ValueError('连接站点已变更，请刷新列表后重新选择模型。')
        return models.llm_application(self.model_name.text(), self.name.text(), base_url)

    def select_site(self, base_url):
        site = models.official_site(base_url)
        if site == self.catalog_site:return
        self.catalog_site = site
        self.names = [];self.updated_at = None
        self.model_name.clear()
        cached = _cached_models(self.root, site)
        if cached:
            self.names, self.updated_at = cached
            self.status.setText('已加载上次列表，正在检查更新…')
        self.filter()
        self.show_catalog_info()

    def show_catalog_info(self):
        stamp = _local_time(self.updated_at) if self.updated_at else '尚未获取'
        self.catalog_info.setText(f'来源：{self.catalog_site} · {len(self.names)} 个模型\n最近获取：{stamp}')

    def filter(self, *_):
        selected = self.model_name.text().strip()
        blocker = QtCore.QSignalBlocker(self.list)
        self.list.clear()
        query = self.search.text().strip().casefold()
        for name in self.names:
            if query in name.casefold():
                item = QtWidgets.QListWidgetItem(name);item.setToolTip(name);self.list.addItem(item)
                if name == selected:self.list.setCurrentItem(item)
        del blocker

    def select_model(self, current, previous=None):
        if current is not None:self.model_name.setText(current.text())

    def stop_fetch(self, *_):
        self.autoload.stop();self.timer.stop()
        if self.future:self.future.cancel()

    def fetch(self):
        if self.future:return
        self.autoload.stop()
        try:
            connection = self.owner._rh_connection_snapshot()
            self.select_site(connection['base_url'])
            self.fetch_site = self.catalog_site
            self.future = _reads.submit(read_llm_models, connection['base_url'], connection['api_key'])
            self.read.setEnabled(False)
            self.status.setText('正在刷新模型目录，可继续使用已有列表或手填模型名…');self.timer.start()
        except Exception as error:self.status.setText(str(error))

    def finish_fetch(self):
        if not self.future or not self.future.done():return
        self.timer.stop();future, self.future = self.future, None;self.read.setEnabled(True)
        try:
            names = _model_names(future.result())
            if not names:raise ValueError('Empty model catalog')
            if models.official_site(self.owner._rh_connection_snapshot()['base_url']) != self.fetch_site:
                self.select_site(self.owner._rh_connection_snapshot()['base_url'])
                self.autoload.start(0)
                return
            self.names = names;self.updated_at = datetime.now(timezone.utc).isoformat()
            self.filter();self.show_catalog_info()
            try:
                from .rh_app_install import _atomic_write
                _atomic_write(_cache_path(self.root, self.catalog_site), {
                    'version': 1, 'site': self.catalog_site, 'models': names, 'updated_at': self.updated_at})
                self.status.setText(f'已更新 {len(names)} 个模型；支持搜索或手填模型名。')
            except OSError:self.status.setText('列表已更新，但缓存无法保存；当前仍可正常使用。')
        except Exception:
            self.status.setText('刷新失败，保留上次列表；可重试或手动填写模型名。' if self.names else '暂时无法获取列表；可刷新重试或手动填写模型名。')

def open_add(owner, root, initial='rh_standard'):
    dialog_type = {'rh_standard': AddStandardModelApplication, 'rh_llm': AddLlmApplication}[initial]
    dialog = dialog_type(owner, root)
    dialog.show()
    return dialog


def dashboard_tabs(owner, layout, root):
    legacy_actions = layout.itemAt(2).layout()
    tabs = QtWidgets.QTabBar();tabs.setExpanding(False)
    for label in models.LABELS.values():tabs.addTab(label)
    owner._rh_application_tabs = tabs
    layout.insertWidget(2, tabs)
    pages = QtWidgets.QStackedWidget();owner._rh_application_searches = []
    for kind in models.BACKENDS:
        page = QtWidgets.QWidget();row = QtWidgets.QHBoxLayout(page);row.setContentsMargins(0, 0, 0, 0)
        search = QtWidgets.QLineEdit();search.setPlaceholderText('检索已添加的' + models.LABELS[kind])
        owner._rh_application_searches.append(search);row.addWidget(search, 1)
        search.textChanged.connect(lambda *_:getattr(owner, '_reflow_rh_buttons', lambda:None)())
        pages.addWidget(page)
    tabs.currentChanged.connect(pages.setCurrentIndex)
    tabs.currentChanged.connect(lambda *_:getattr(owner, '_reflow_rh_buttons', lambda:None)())
    layout.insertWidget(3, pages)
    def show_actions(index):
        if legacy_actions:
            for i in range(legacy_actions.count()):
                widget = legacy_actions.itemAt(i).widget()
                if widget:widget.setVisible(index == 0)
    tabs.currentChanged.connect(show_actions)
    show_actions(0)
    return tabs


def visible_cards(owner, cards):
    tabs = getattr(owner, '_rh_application_tabs', None)
    selected = models.BACKENDS[tabs.currentIndex()] if tabs else 'rh_app'
    searches = getattr(owner, '_rh_application_searches', [])
    query = searches[tabs.currentIndex()].text().strip().casefold() if searches else ''
    result = []
    for index, card in enumerate(cards):
        if index == 0:
            card._add_title = '添加' + (' LLM' if selected == 'rh_llm' else models.LABELS[selected])
            card._add_hint = {'rh_app': '导入工作流，开始创作', 'rh_standard': '检索标准模型，添加独立配置', 'rh_llm': '选择语言模型，添加独立配置'}[selected]
            card.setText('+\n' + card._add_title)
            card.setAccessibleName(card._add_title)
            card.setAccessibleDescription(card._add_hint)
            card.setToolTip(card._add_title)
            card.update()
        visible = True if index == 0 else (getattr(card, '_rh_backend', 'rh_app') == selected
                   and query in (getattr(card, '_full_title', '') + ' ' + str(getattr(card, '_wid', ''))).casefold())
        card.setVisible(visible)
        if visible:result.append(card)
    return result
