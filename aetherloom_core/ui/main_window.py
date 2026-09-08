"""Main window lifecycle and coordination."""
from aetherloom_core.ui.compare import CompareWindow
from aetherloom_core.resources import DEFAULT_EXPAND_SYSTEM_PROMPT
from aetherloom_core.resources import DEFAULT_IMAGE_REVERSE_PROMPT
from aetherloom_core.tasks.media import FileInfoJob
from aetherloom_core.resources import IMAGE_EXTS
from PIL import Image
from aetherloom_core.tasks.media import PreviewJob
from PyQt5.QtCore import Qt
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.resources import VIDEO_EXTS
from aetherloom_core.tasks.decoding import Worker
from aetherloom_core.ui.widgets import _ComboWheelBlocker
from aetherloom_core.platform_utils import _api_debug
from aetherloom_core.services.decoding import _mode_for_label
from aetherloom_core.platform_utils import _move_to_trash
from aetherloom_core import api_manager
from aetherloom_core import __version__
from aetherloom_core.prompt_history import clear_histories
from aetherloom_core.paths import current_dir, SOURCE_ROOT
import cv2
from aetherloom_core.services.decoding import grc
import os
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from aetherloom_core.ui.layout import MainLayoutMixin
from aetherloom_core.ui.local_browser import LocalBrowserMixin
from aetherloom_core.ui.presentation import PresentationMixin
from aetherloom_core.ui.settings import SettingsMixin


class MainWindow(MainLayoutMixin, LocalBrowserMixin, PresentationMixin, SettingsMixin, QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'AetherLoom v{__version__}')
        # threadpool and thumbnail cache for background thumbnail generation
        try:
            self._thumb_pool = QtCore.QThreadPool.globalInstance()
            # limit concurrency to avoid saturating CPU / IO on large folders
            try:
                cpu = max(2, (os.cpu_count() or 2))
                self._thumb_pool.setMaxThreadCount(min(6, cpu))
            except Exception:
                pass
            self._thumb_cache_dir = os.path.join(current_dir, '.thumb_cache')
            os.makedirs(self._thumb_cache_dir, exist_ok=True)
            try:
                self._prune_thumb_cache(max_size_mb=getattr(self, 'thumb_cache_max_mb', 300), max_files=4000, max_age_days=14, aggressive=True)
            except Exception:
                pass
            # simple in-memory LRU cache for most-recent icons
            from collections import OrderedDict
            self._thumb_mem_cache = OrderedDict()
            # maximum number of icons to keep in memory (tweakable)
            self._thumb_mem_cache_max = 400
            # track inflight thumbnail jobs: key -> cancel token
            self._thumb_jobs_inflight = {}
            # raw PNG bytes cache for results produced while scrolling (delay main-thread QPixmap creation)
            self._thumb_raw_cache = {}
            # scrolling interaction flag and idle timer
            self._in_scroll_interaction = False
            self._scroll_idle_timer = QtCore.QTimer(self)
            self._scroll_idle_timer.setSingleShot(True)
            self._scroll_idle_timer.setInterval(220)
            self._scroll_idle_timer.timeout.connect(self._on_scroll_idle)
            # interaction state: when user is actively changing thumb size or performing other operations,
            # show only low-res thumbnails and defer high-res replacements until idle.
            self._in_thumb_interaction = False
            self._pending_high_keys = set()
            # snapshots to avoid re-rendering local lists when switching modes
            self._local_snapshot_input = None
            self._local_snapshot_output = None
            # chunked population state to avoid blocking UI when folders are huge
            self._local_pending_add = {}
            self._local_add_timers = {}
            # pending file-info path to ignore stale results
            self._pending_info_paths = {'orig': None, 'output': None}
            # per-list scroll enqueue debounce timers
            self._scroll_enqueue_timers = {}
            # metadata cache for local filter logic
            self._local_meta_cache = {}
            # dynamic filter rows state
            self._local_filter_rows = []
            self._filter_dropdown_index = 0
            self._local_controls_visible = True
        except Exception:
            self._thumb_pool = None
            self._thumb_cache_dir = None
            from collections import OrderedDict
            self._thumb_mem_cache = OrderedDict()
            self._thumb_mem_cache_max = 400
            self._thumb_jobs_inflight = {}
            self._thumb_raw_cache = {}
            self._in_scroll_interaction = False
            self._scroll_idle_timer = QtCore.QTimer(self)
            self._scroll_idle_timer.setSingleShot(True)
            self._scroll_idle_timer.setInterval(220)
            try:
                self._scroll_idle_timer.timeout.connect(self._on_scroll_idle)
            except Exception:
                pass
            self._in_thumb_interaction = False
            self._pending_high_keys = set()
            self._local_pending_add = {}
            self._local_add_timers = {}
            self._pending_info_paths = {'orig': None, 'output': None}
            self._scroll_enqueue_timers = {}
            self._local_meta_cache = {}
            self._local_filter_rows = []
            self._filter_dropdown_index = 0
            self._local_controls_visible = True
        self._compare_window = None
        # settings path
        self.settings_path = os.path.join(current_dir, 'settings.json')
        # default folders
        # use local input/output folders inside the GUI directory by default
        default_input = os.path.join(current_dir, 'input')
        default_output = os.path.join(current_dir, 'output')
        default_local_decode = os.path.join(current_dir, 'decoding')

        # load settings if exist
        self.settings = self._load_settings() or {}
        try:
            self.rh_local_decode_settings = self.settings.get('rh_local_decode_settings', {}) if isinstance(self.settings, dict) else {}
            if not isinstance(self.rh_local_decode_settings, dict):
                self.rh_local_decode_settings = {}
        except Exception:
            self.rh_local_decode_settings = {}
        try:
            favs = self.settings.get('rh_favorites', []) if isinstance(self.settings, dict) else []
            if isinstance(favs, (list, tuple, set)):
                self.rh_favorites = set([str(x) for x in favs if x is not None])
            else:
                self.rh_favorites = set()
        except Exception:
            self.rh_favorites = set()
        # ensure certain prompts have sane defaults so settings.json doesn't contain null
        try:
            if isinstance(self.settings, dict):
                if not self.settings.get('expand_system_prompt'):
                    self.settings['expand_system_prompt'] = DEFAULT_EXPAND_SYSTEM_PROMPT
                if not self.settings.get('image_reverse_prompt'):
                    self.settings['image_reverse_prompt'] = DEFAULT_IMAGE_REVERSE_PROMPT
                if not self.settings.get('image_reverse_prompt'):
                    self.settings['image_reverse_prompt'] = '''你是一位专业的图像分析专家，请将提供的图片转换为适合AI绘图模型使用的自然语言。你的描述需要准确、详细，并符合Stable Diffusion等模型的提示词特点。

分析重点：
1. 主体描述（按重要性排序）：
   - 人物/物体的具体类型和特征
   - 准确的外观描述（发型、服装、表情等）
   - 清晰的姿势和动作
   - 关键细节特征

2. 场景要素：
   - 具体的场景类型
   - 环境细节
   - 空间关系
   - 天气和时间状态

3. 视觉风格：
   - 整体艺术风格
   - 画面质感
   - 特殊效果

4. 技术特征：
   - 构图方式
   - 光影效果
   - 色彩特点
   - 渲染风格

输出要求：
1. 使用AI绘图模型常用的描述方式
2. 按重要性顺序组织描述
3. 包含必要的艺术风格和技术标签
4. 避免使用模型难以理解的抽象描述
5. 保持描述的可执行性和清晰度

示例输出：
一位穿着白色连衣裙的年轻动漫女孩，金色长发飘逸，面带甜美笑容。她站在阳光明媚的春日花园中，周围绽放着粉色和白色的花朵。画面采用温暖的色调，细腻的动漫风格渲染，半身构图，柔和的自然光效果。背景经过适度模糊处理，突出人物主体。高质量插画风格，注重细节刻画。

注意事项：
1. 使用具体而非抽象的描述
2. 包含AI模型能够理解的标准术语
3. 按照"主体 > 场景 > 风格"的顺序组织描述
4. 确保每个重要视觉元素都有明确描述

请直接以及仅输出符合AI绘图要求的自然语言描述，确保描述既流畅自然，又包含足够的细节供模型准确理解和生成。不生成其他任何内容。'''
        except Exception:
            pass
        # persist defaults immediately so settings.json won't contain null on first run
        try:
            try:
                self._save_settings()
            except Exception:
                pass
        except Exception:
            pass
        self.input_dir = self.settings.get('input_dir', default_input)
        self.output_dir = self.settings.get('output_dir', default_output)
        self.local_decode_dir = self.settings.get('local_decode_dir', default_local_decode)
        raw_custom_cache = self.settings.get('custom_api_settings', {}) if isinstance(self.settings, dict) else {}
        self.api_custom_cache = raw_custom_cache if isinstance(raw_custom_cache, dict) else {}
        raw_provider_profiles = self.settings.get('api_provider_profiles', {}) if isinstance(self.settings, dict) else {}
        self.api_provider_profiles = raw_provider_profiles if isinstance(raw_provider_profiles, dict) else {}
        try:
            self.thumb_cache_max_mb = int(max(50, min(5000, int(self.settings.get('thumb_cache_max_mb', 300)))))
        except Exception:
            self.thumb_cache_max_mb = 300
        try:
            self.app_page_cache_limit = int(max(1, min(1000, int(self.settings.get('app_page_cache_limit', 20)))))
        except Exception:
            self.app_page_cache_limit = 20
        # RunningHub busy retries are FIFO; first submissions retain a concurrency cap.
        try:
            self.rh_retry_max = int(self.settings.get('rh_retry_max', 100))
        except Exception:
            self.rh_retry_max = 100
        try:
            self.rh_retry_delay = int(self.settings.get('rh_retry_delay', 5))
        except Exception:
            self.rh_retry_delay = 5
        try:
            self.rh_retry_head_count = max(1, min(16, int(self.settings.get('rh_retry_head_count', 1))))
        except (TypeError, ValueError):
            self.rh_retry_head_count = 1
        # concurrency is fixed default (not user-configurable in settings)
        try:
            self.rh_retry_concurrency = 25
        except Exception:
            self.rh_retry_concurrency = 25
        # runtime counters for active submission requests
        try:
            import threading as _threading
            self._rh_retry_active = 0
            self._rh_retry_lock = _threading.Lock()
        except Exception:
            self._rh_retry_active = 0
            self._rh_retry_lock = None
        # queue for tasks waiting to retry and cancellation flag
        try:
            self._rh_retry_queue = []
            self._rh_retry_cancel_all = False
        except Exception:
            self._rh_retry_queue = []
            self._rh_retry_cancel_all = False
        from aetherloom_core.rh_submission_queue import get_submission_queue
        get_submission_queue(self).set_admission_limit(self.rh_retry_head_count)
        # api categories and defaults
        if api_manager and hasattr(api_manager, 'get_categories'):
            try:
                cats = api_manager.get_categories()
                self.api_categories = [(c.get('key'), c.get('name'), c.get('desc')) for c in cats]
                _api_debug(f"loaded categories from api_manager: {[c[0] for c in self.api_categories]}")
            except Exception as _e:
                _api_debug(f"failed to load categories: {_e}")
                self.api_categories = []
        else:
            self.api_categories = []
            _api_debug("api_manager missing or no get_categories")
        if not self.api_categories:
            self.api_categories = [
                ('translator', '翻译模型', '翻译/ASR/字幕等接口入口'),
                ('llm', '大语言模型', '通用对话/推理模型'),
                ('vision', '视觉模型', '图像理解/多模态识别'),
                ('text2img', '文生图模型', '文本生成图片模型接口'),
                ('image_edit', '图像编辑', '修图、抠图、重绘等'),
                ('runninghub', 'RunningHub', 'RunningHub 站点 API 调用'),
            ]
        try:
            if api_manager and hasattr(api_manager, 'API_CATALOG'):
                self.api_catalog = api_manager.API_CATALOG
                _api_debug("api_catalog from api_manager.API_CATALOG")
            elif api_manager and hasattr(api_manager, 'get_providers'):
                # fallback: build from getter
                self.api_catalog = {c[0]: api_manager.get_providers(c[0]) for c in self.api_categories}
                _api_debug("api_catalog built via get_providers")
            else:
                self.api_catalog = {}
                _api_debug("api_catalog empty: api_manager missing")
        except Exception as _e:
            self.api_catalog = {}
            _api_debug(f"failed to build api_catalog: {_e}")
        try:
            cat_keys = [c[0] for c in self.api_categories]
            _api_debug(f"categories in use: {cat_keys}")
            for key in cat_keys:
                provs = self.api_catalog.get(key, []) if isinstance(self.api_catalog, dict) else []
                _api_debug(f"{key} providers count {len(provs)}")
                if provs:
                    first = provs[0]
                    _api_debug(f"{key} first provider {first}")
        except Exception:
            pass
        self._combo_wheel_blocker = _ComboWheelBlocker(self)
        self.api_settings = self.settings.get('api_settings', {}) if isinstance(self.settings, dict) else {}
        if not isinstance(self.api_settings, dict):
            self.api_settings = {}
        defaults = self._default_api_settings()
        merged_api = defaults.copy()
        for k, v in self.api_settings.items():
            if isinstance(v, dict):
                try:
                    merged = defaults.get(k, {}).copy()
                    merged.update({
                        'provider': v.get('provider', ''),
                        'endpoint': v.get('endpoint', ''),
                        'api_key': v.get('api_key', ''),
                        'model': v.get('model', ''),
                        'timeout': v.get('timeout', 30),
                    })
                    merged_api[k] = merged
                except Exception:
                    merged_api[k] = defaults.get(k, {}).copy()
        self.api_settings = merged_api
        if not isinstance(getattr(self, 'api_custom_cache', None), dict):
            self.api_custom_cache = {}
        for _cat, cfg in self.api_settings.items():
            try:
                prov = cfg.get('provider') if isinstance(cfg, dict) else None
            except Exception:
                prov = None
            if isinstance(cfg, dict) and (prov == 'custom' or (isinstance(prov, str) and prov.startswith('custom_'))):
                self.api_custom_cache.setdefault(_cat, {
                    'endpoint': cfg.get('endpoint', ''),
                    'api_key': cfg.get('api_key', ''),
                    'model': cfg.get('model', ''),
                    'timeout': cfg.get('timeout', 30),
                })
        self.api_config_fields = {}
        os.makedirs(self.input_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.local_decode_dir, exist_ok=True)

        self._themes = self._build_theme_palettes()
        self._theme_mode = self.settings.get('theme_mode', 'dark') if isinstance(self.settings, dict) else 'dark'
        if self._theme_mode not in self._themes:
            self._theme_mode = 'dark'
        self._active_screen_size = None
        self._active_screen_name = None
        self._pending_restore_maximized = False
        self._screen_signal_bound = False
        self._bound_screen = None
        base_font = self.font() or QtWidgets.QApplication.font()
        try:
            self._base_font_point = base_font.pointSizeF() if base_font is not None else 11.5
        except Exception:
            self._base_font_point = 11.5
        # Sidebar sizing: prefer fractions of window/screen width over fixed px
        # fraction of window width used for base sidebar width (e.g. 0.12 == 12%)
        self._sidebar_base_frac = 0.06
        # minimum fraction of window width for sidebar (won't shrink below this fraction)
        self._sidebar_min_frac = 0.06
        # fraction used for collapsed sidebar minimum width (icon-only)
        self._sidebar_collapsed_min_frac = 0.04
        # increase sidebar button height base so buttons are taller (interpreted as base units scaled)
        self._sidebar_button_height_base = 110
        # icon base size (scaled by UI scale)
        self._sidebar_icon_px_base = 44
        # legacy pixel fallback for environments where screen width cannot be determined
        self._sidebar_base_width = 200
        self._sidebar_collapsed_min_px = 80
        # maximum fraction of window width the sidebar may occupy (conservative default)
        self._sidebar_max_fraction = 0.12
        # absolute maximum width in pixels for sidebar
        self._sidebar_max_px = 420
        self._theme_toggle_size_base = 52
        self._theme_toggle_icon_px_base = 32
        self._sidebar_buttons = []
        self._file_list_icon_base = 120
        self._preview_min_base = QtCore.QSize(180, 120)
        self._output_min_base = QtCore.QSize(320, 210)
        self._ui_scale_factor = 1.0
        self._control_panel_font_base = 11.5
        self._control_group_css_template = None
        self.decode_control_group = None
        self._play_icon_cache = {}
        self._play_btn_size_base = 152
        self._play_icon_px_base = 100
        self._suppress_play_hide_once = False

        self._setup_ui()
        from aetherloom_core.rh_progress import ProgressMonitor
        self._rh_progress_monitor = ProgressMonitor(self)
        self._restore_window_geometry(initial=True)
        self._connect_signals()
        self._apply_theme(self._theme_mode)
        try:
            self._update_theme_toggle_icon(self._theme_mode)
            if hasattr(self, 'theme_toggle_btn') and self.theme_toggle_btn is not None:
                self.theme_toggle_btn.setToolTip('切换为日间模式' if self._theme_mode == 'dark' else '切换为夜间模式')
        except Exception:
            pass
        # apply loaded settings to UI (if any) but do NOT apply saved page index
        try:
            self._apply_settings(self.settings, apply_window_geometry=False, apply_page_index=False)
        except Exception:
            pass
        self.load_folder(self.local_decode_dir)
        # ensure local thumbnails are generated on startup (apply persisted thumb size first)
        try:
            QtCore.QTimer.singleShot(150, lambda: self._refresh_local_list())
        except Exception:
            pass


    def _rh_connection_snapshot(self):
        from aetherloom_core.rh_connections import ensure_connections
        return ensure_connections(self).snapshot()


    def _default_api_settings(self):
        base = {}
        try:
            categories = getattr(self, 'api_categories', [])
            for key, _, _ in categories:
                provider_default = ''
                endpoint_default = ''
                try:
                    provs = self.api_catalog.get(key, []) if isinstance(getattr(self, 'api_catalog', None), dict) else []
                    if provs:
                        provider_default = provs[0].get('key', '')
                        endpoint_default = provs[0].get('endpoint', '')
                except Exception:
                    pass
                base[key] = {
                    'provider': provider_default,
                    'endpoint': endpoint_default,
                    'model': '',
                    'timeout': 30,
                }
        except Exception:
            pass
        if not base:
            base = {
                'translator': {'provider': '', 'endpoint': '', 'model': '', 'timeout': 30},
                'llm': {'provider': '', 'endpoint': '', 'model': '', 'timeout': 90},
                'vision': {'provider': '', 'endpoint': '', 'model': '', 'timeout': 120},
                'text2img': {'provider': '', 'endpoint': '', 'model': '', 'timeout': 120},
                'image_edit': {'provider': '', 'endpoint': '', 'model': '', 'timeout': 150},
                'runninghub': {'provider': '', 'endpoint': '', 'model': '', 'timeout': 30},
            }
        return base


    def _provider_items_for_category(self, key):
        items = []
        try:
            providers = []
            if api_manager and hasattr(api_manager, 'get_providers'):
                providers = api_manager.get_providers(key) or []
            elif isinstance(getattr(self, 'api_catalog', None), dict):
                providers = self.api_catalog.get(key, []) or []
            for p in providers:
                name = p.get('name', p.get('key', ''))
                items.append((name, p.get('key', '')))
            if not providers:
                _api_debug(f"no providers for category {key}; api_catalog keys: {list(self.api_catalog.keys()) if isinstance(getattr(self, 'api_catalog', None), dict) else 'n/a'}")
        except Exception:
            pass
        items.append(('自定义', 'custom'))
        return items


    def _find_provider_entry(self, category_key, provider_key):
        try:
            providers = []
            if api_manager and hasattr(api_manager, 'get_providers'):
                providers = api_manager.get_providers(category_key) or []
            elif isinstance(getattr(self, 'api_catalog', None), dict):
                providers = self.api_catalog.get(category_key, []) or []
            for p in providers:
                if p.get('key') == provider_key:
                    return p
        except Exception:
            pass
        return None


    def _ensure_api_provider_profile_store(self):
        if not isinstance(getattr(self, 'api_provider_profiles', None), dict):
            self.api_provider_profiles = {}
        return self.api_provider_profiles


    def _get_api_provider_profile(self, category_key, provider_key):
        if not category_key or not provider_key:
            return {}
        store = self._ensure_api_provider_profile_store()
        cat_profiles = store.get(category_key, {})
        data = cat_profiles.get(provider_key, {})
        return data.copy() if isinstance(data, dict) else {}


    def _set_api_provider_profile(self, category_key, provider_key, data):
        if not category_key or not provider_key or not isinstance(data, dict):
            return
        store = self._ensure_api_provider_profile_store()
        cat_profiles = store.setdefault(category_key, {})
        cat_profiles[provider_key] = self._normalize_provider_profile_payload(provider_key, data)


    def _normalize_provider_profile_payload(self, provider_key, data):
        if not isinstance(data, dict):
            return {}
        sanitized = dict(data)
        # Never persist sensitive credentials into settings.json.
        # Remove API keys and any provider secrets (AppID/Secret) here.
        sanitized.pop('api_key', None)
        sanitized.pop('appid', None)
        sanitized.pop('secret', None)
        return sanitized


    def _sanitize_api_provider_profiles(self):
        store = self._ensure_api_provider_profile_store()
        for category_key, providers in list(store.items()):
            if not isinstance(providers, dict):
                store[category_key] = {}
                continue
            for provider_key, payload in list(providers.items()):
                providers[provider_key] = self._normalize_provider_profile_payload(provider_key, payload)


    def _snapshot_api_provider_fields(self, category_key, provider_key):
        if not category_key or not provider_key:
            return
        fields = getattr(self, 'api_config_fields', {}).get(category_key)
        if not isinstance(fields, dict):
            return
        try:
            endpoint_val = fields['endpoint'].text().strip()
        except Exception:
            endpoint_val = ''
        try:
            api_key_val = fields['api_key'].text().strip()
        except Exception:
            api_key_val = ''
        if fields.get('model') is not None:
            try:
                model_val = fields['model'].currentText().strip()
            except Exception:
                model_val = ''
        else:
            model_val = ''
        try:
            timeout_val = int(fields['timeout'].value()) if fields.get('timeout') else 30
        except Exception:
            timeout_val = 30
        payload = {
            'endpoint': endpoint_val,
            'api_key': api_key_val,
            'model': model_val,
            'timeout': timeout_val,
        }
        if provider_key == 'baidu_translate' and fields.get('baidu_appid') is not None:
            try:
                payload['appid'] = fields['baidu_appid'].text().strip()
            except Exception:
                payload['appid'] = payload.get('appid', '')
        if provider_key == 'baidu_translate' and fields.get('baidu_secret') is not None:
            try:
                payload['secret'] = fields['baidu_secret'].text().strip()
            except Exception:
                payload['secret'] = payload.get('secret', '')
        self._set_api_provider_profile(category_key, provider_key, payload)


    def _refresh_rh_task_credentials(self):
        """Copy host credentials on the GUI thread for background task recovery."""
        lifecycle = getattr(self, '_rh_task_lifecycle', None)
        if lifecycle is None:
            return
        from aetherloom_core.rh_connections import ensure_connections
        connection = ensure_connections(self)
        host, keys = connection.host, connection.site_keyrings()
        lifecycle.set_credentials(
            {'base_url': host, 'output_dir': str(self.output_dir)}, keys)


    def _open_api_portal(self, btn):
        try:
            url = btn.property('api_url')
            if url:
                webbrowser.open(url)
        except Exception:
            pass


    def _open_model_docs(self, btn):
        try:
            url = btn.property('model_docs_url')
            if url:
                webbrowser.open(url)
        except Exception:
            pass


    def _install_combo_wheel_blocker(self, combo):
        try:
            if hasattr(self, '_combo_wheel_blocker') and self._combo_wheel_blocker:
                combo.installEventFilter(self._combo_wheel_blocker)
        except Exception:
            pass


    def _connect_signals(self):
        self.input_btn.clicked.connect(self.select_input_folder)
        self.output_btn.clicked.connect(self.select_output_folder)
        self.local_decode_btn.clicked.connect(self.select_local_decode_folder)
        self.theme_toggle_btn.clicked.connect(self._toggle_theme_mode)
        self.input_open_btn.clicked.connect(lambda: self._open_folder_path(self.input_label.text(), create=True))
        self.output_open_btn.clicked.connect(lambda: self._open_folder_path(self.output_label.text(), create=True))
        self.local_decode_open_btn.clicked.connect(lambda: self._open_folder_path(self.local_decode_label.text(), create=True))
        self.input_label.editingFinished.connect(self.on_input_edit)
        self.output_label.editingFinished.connect(self.on_output_edit)
        self.local_decode_label.editingFinished.connect(self.on_local_decode_edit)
        self.file_list.itemSelectionChanged.connect(self.on_file_selected)
        # context menu for left file list
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self.on_file_list_context_menu)
        # enable drop callbacks
        self.file_list.drop_callback = self._on_files_dropped
        try:
            for _lbl in (self.orig_view_grc, self.orig_view_sst):
                _lbl.drop_callback = self._on_files_dropped
        except Exception:
            pass
        self.output_view.drop_callback = self._on_files_dropped
        try:
            self.output_play_btn.clicked.connect(self._on_output_play_clicked)
        except Exception:
            pass
        # context menu on previews for save-as
        try:
            for _lbl in (self.orig_view_grc, self.orig_view_sst):
                _lbl.setContextMenuPolicy(Qt.CustomContextMenu)
                _lbl.customContextMenuRequested.connect(lambda pos, lbl=_lbl: self._on_preview_context_menu('orig', pos))
            self.output_view.setContextMenuPolicy(Qt.CustomContextMenu)
            self.output_view.customContextMenuRequested.connect(lambda pos: self._on_preview_context_menu('output', pos))
        except Exception:
            pass
        # handle double-click on preview labels to open file in default app
        try:
            for _lbl in (self.orig_view_grc, self.orig_view_sst):
                _lbl.dblclick_callback = lambda lbl=_lbl: self._open_current_file('orig')
        except Exception:
            pass
        self.output_view.dblclick_callback = lambda: self._open_current_file('output')
        self.preview_btn.clicked.connect(self.on_decode_selected)
        self.batch_btn.clicked.connect(self.on_batch_restore)
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.grid_spin.valueChanged.connect(self.on_grid_changed)
        # save settings when parameters change
        try:
            self.grid_spin.valueChanged.connect(lambda v: self._save_settings())
            self.show_grid_cb.toggled.connect(lambda v: self._save_settings())
            self.overwrite_cb.toggled.connect(lambda v: self._save_settings())
            # local sort comboboxes: persist and refresh lists when changed
            try:
                if hasattr(self, 'local_sort_in_combo') and self.local_sort_in_combo is not None:
                    self.local_sort_in_combo.currentIndexChanged.connect(lambda _: self._on_local_sort_changed('in'))
                if hasattr(self, 'local_sort_out_combo') and self.local_sort_out_combo is not None:
                    self.local_sort_out_combo.currentIndexChanged.connect(lambda _: self._on_local_sort_changed('out'))
            except Exception:
                pass
        except Exception:
            pass

# when the user switches pages, ensure visible thumbnails are requested
        try:
            self.pages.currentChanged.connect(self._on_page_changed)
        except Exception:
            pass

        # progress update slot (route worker signals through a main-thread handler)
        # use a dedicated method so we can add smoothing/logging later if needed
        self._on_progress_slot = lambda v: self.on_progress_update(v)

        # export button and handler removed

        self.worker = None


    def log(self, txt):
        try:
            if hasattr(self, '_log_emitter') and getattr(self, '_log_emitter', None) is not None:
                try:
                    self._log_emitter.sig.emit(str(txt))
                    return
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.log_text.append(str(txt))
        except Exception:
            pass


    def _on_local_sort_changed(self, which):
        """Handle when user changes sort combobox; persist selection and refresh local lists."""
        try:
            if which == 'in' and hasattr(self, 'local_sort_in_combo') and self.local_sort_in_combo is not None:
                try:
                    v = self.local_sort_in_combo.currentData()
                    if isinstance(self.settings, dict):
                        self.settings['local_sort_in'] = v
                except Exception:
                    pass
            if which == 'out' and hasattr(self, 'local_sort_out_combo') and self.local_sort_out_combo is not None:
                try:
                    v = self.local_sort_out_combo.currentData()
                    if isinstance(self.settings, dict):
                        self.settings['local_sort_out'] = v
                except Exception:
                    pass
            try:
                self._save_settings()
            except Exception:
                pass
            try:
                QtCore.QTimer.singleShot(60, lambda: self._refresh_local_list())
            except Exception:
                pass
        except Exception:
            pass


    def _sort_file_names(self, names, base_dir, option):
        """Sort a list of filenames according to option. Returns a new list.
        option keys: name_asc/name_desc, mtime_asc/mtime_desc, size_asc/size_desc, ext_asc/ext_desc
        """
        try:
            if not names:
                return names
            opt = (option or 'name_asc')
            lower = lambda s: s.lower()
            if opt == 'name_asc':
                return sorted(names, key=lower)
            if opt == 'name_desc':
                return sorted(names, key=lower, reverse=True)
            if opt in ('mtime_asc', 'mtime_desc'):
                def mtime_key(n):
                    try:
                        p = os.path.join(base_dir, n)
                        return os.path.getmtime(p) if os.path.exists(p) else 0
                    except Exception:
                        return 0
                return sorted(names, key=mtime_key, reverse=(opt == 'mtime_desc'))
            if opt in ('size_asc', 'size_desc'):
                def size_key(n):
                    try:
                        p = os.path.join(base_dir, n)
                        return os.path.getsize(p) if os.path.exists(p) else 0
                    except Exception:
                        return 0
                return sorted(names, key=size_key, reverse=(opt == 'size_desc'))
            if opt in ('ext_asc', 'ext_desc'):
                def ext_key(n):
                    try:
                        return os.path.splitext(n)[1].lower()
                    except Exception:
                        return ''
                return sorted(names, key=ext_key, reverse=(opt == 'ext_desc'))
        except Exception:
            pass
        # fallback
        try:
            return sorted(names, key=lower)
        except Exception:
            return names


    def _on_page_changed(self, idx):
        """Called when pages change — enqueue thumbnails for visible items."""
        try:
            # hide any floating selection info when switching pages
            try:
                if getattr(self, '_selection_info_frame', None) is not None and self._selection_info_frame.isVisible():
                    try:
                        self._selection_info_frame.setVisible(False)
                    except Exception:
                        pass
            except Exception:
                pass
            # treat page change as an interaction to show low-res placeholders briefly
            try:
                self._start_thumb_interaction()
            except Exception:
                pass
        except Exception:
            pass
        try:
            # if local page, ensure both in/out lists populate
            if hasattr(self, 'pages') and hasattr(self, 'local_page'):
                try:
                    if idx == self.pages.indexOf(self.local_page):
                        if hasattr(self, 'local_list_in'):
                            self._enqueue_visible_thumbnails(self.local_list_in)
                        if hasattr(self, 'local_list_out'):
                            self._enqueue_visible_thumbnails(self.local_list_out)
                        return
                except Exception:
                    pass
            # otherwise ensure left file list visible thumbs
            if hasattr(self, 'file_list'):
                try:
                    self._enqueue_visible_thumbnails(self.file_list)
                except Exception:
                    pass
        except Exception:
            pass


    def _on_list_selection_changed(self, list_widget):
        """Show selected file info in the bottom-right floating frame and in file_info_label."""
        if list_widget in (getattr(self, 'local_list_in', None), getattr(self, 'local_list_out', None)):
            self._local_active_list = list_widget
            frame = getattr(self, '_selection_info_frame', None)
            if frame is not None:
                frame.hide()
            update_summary = getattr(self, '_update_local_selection_summary', None)
            if update_summary is not None:
                update_summary(list_widget)
            list_widget.viewport().update()
            self._notify_decorations_changed(list_widget)
            return
        try:
            items = list_widget.selectedItems()
            if not items:
                try:
                    if self._selection_info_frame:
                        self._selection_info_frame.setVisible(False)
                except Exception:
                    pass
                try:
                    self.file_info_label.clear()
                except Exception:
                    pass
                # force repaint to clear any lingering selection decorations
                try:
                    if list_widget is not None:
                        wp = list_widget.viewport()
                        if wp is not None:
                            wp.update()
                        try:
                            self._notify_decorations_changed(list_widget)
                        except Exception:
                            pass
                    # also nudge other lists to repaint in case highlight persisted there
                    for lw in (getattr(self, 'file_list', None), getattr(self, 'local_list_in', None), getattr(self, 'local_list_out', None)):
                        try:
                            if lw is not None and lw is not list_widget:
                                vp = lw.viewport()
                                if vp is not None:
                                    vp.update()
                        except Exception:
                            pass
                except Exception:
                    pass
                return
            # show info for first selected item
            # if multiple selected, show a compact footnote in the selection info frame
            try:
                if len(items) > 1 and list_widget is not self.file_list:
                    # hide main detailed file info
                    try:
                        self.file_info_label.clear()
                    except Exception:
                        pass
                    try:
                        if self._selection_info_label is not None and self._selection_info_frame is not None:
                            txt = f'已选目标: {len(items)} 项'
                            self._selection_info_label.setText(txt)
                            # position bottom-right
                            try:
                                self._selection_info_frame.adjustSize()
                                fw = self._selection_info_frame.width()
                                fh = self._selection_info_frame.height()
                                margin = 16
                                x = max(8, self.width() - fw - margin)
                                y = max(8, self.height() - fh - margin)
                                self._selection_info_frame.move(x, y)
                                self._selection_info_frame.setVisible(True)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return
            except Exception:
                pass

            it = items[0]
            meta = it.data(QtCore.Qt.UserRole) or {}
            path = meta.get('path')
            if not path:
                return
            # schedule background retrieval of file info to avoid blocking UI
            try:
                # cancel any previous pending info (we'll ignore stale results)
                self._mark_pending_info('output', path)
                job = FileInfoJob(path)
                handler = lambda p, txt, target='output': self._on_file_info_ready(target, p, txt)
                try:
                    job.signals.finished.connect(handler, QtCore.Qt.QueuedConnection)
                except Exception:
                    try:
                        job.signals.finished.connect(handler)
                    except Exception:
                        pass
                try:
                    if self._thumb_pool:
                        self._thumb_pool.start(job)
                    else:
                        job.run()
                except Exception:
                    try:
                        job.run()
                    except Exception:
                        pass
            except Exception:
                try:
                    self._set_file_info(path, target='output')
                except Exception:
                    pass
            # populate floating frame (skip for left file list to avoid heavy UI)
            try:
                if list_widget is not self.file_list and self._selection_info_label is not None:
                    # build floating frame content from current main label text (updated by background job)
                    base = self.file_info_label.text() or ''
                    parts = [p.strip() for p in base.split(' | ') if p.strip()]
                    lines = parts
                    txt = "\n".join(lines)
                    self._selection_info_label.setText(txt)
                    # position bottom-right with margin
                    try:
                        if self._selection_info_frame is not None:
                            # allow the frame to expand to show full filename when possible
                            try:
                                maxw = max(400, int(self.width() * 0.6))
                                self._selection_info_frame.setFixedWidth(min(maxw, 1000))
                            except Exception:
                                pass
                            self._selection_info_frame.adjustSize()
                            fw = self._selection_info_frame.width()
                            fh = self._selection_info_frame.height()
                            margin = 16
                            # coordinates relative to main window
                            x = max(8, self.width() - fw - margin)
                            y = max(8, self.height() - fh - margin)
                            self._selection_info_frame.move(x, y)
                            self._selection_info_frame.setVisible(True)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass


    def _make_thumbnail(self, path, size=(200, 200)):
        """Create a fixed-size QPixmap thumbnail on a black background so all icons align.
        The returned QPixmap will be exactly `size`.
        """
        try:
            # load a representative frame (first frame for video/GIF)
            if path.lower().endswith(VIDEO_EXTS):
                if path.lower().endswith('.gif'):
                    img = Image.open(path)
                    frame = img.copy().convert('RGB')
                else:
                    cap = cv2.VideoCapture(path)
                    ret, frame = cap.read()
                    cap.release()
                    if not ret:
                        return None
                    frame = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                frame = Image.open(path).convert('RGB')

            # create a black background at the target size
            target_w, target_h = int(size[0]), int(size[1])
            bg = Image.new('RGB', (target_w, target_h), (0, 0, 0))

            # resize thumbnail preserving aspect ratio to fit within target
            thumb = frame.copy()
            thumb.thumbnail((target_w, target_h), Image.LANCZOS)

            # compute centered position
            x = (target_w - thumb.width) // 2
            y = (target_h - thumb.height) // 2
            bg.paste(thumb, (x, y))

            # Save to PNG bytes and load into QPixmap to avoid QImage raw buffer lifetime/stride issues
            from io import BytesIO
            buf = BytesIO()
            bg.save(buf, format='PNG')
            png_data = buf.getvalue()
            pix = QtGui.QPixmap()
            if pix.loadFromData(png_data, 'PNG'):
                return pix
            return None
        except Exception:
            return None


    def _find_restored_output(self, input_filename):
        """Return the first restored output path matching the input basename, ignoring extension."""
        try:
            name, _ = os.path.splitext(os.path.basename(input_filename))
            base = f"{name}_restored"
            if not os.path.isdir(self.output_dir):
                return None
            try:
                for fname in sorted(os.listdir(self.output_dir)):
                    try:
                        stem, _ext = os.path.splitext(fname)
                        if stem == base:
                            return os.path.join(self.output_dir, fname)
                    except Exception:
                        continue
            except Exception:
                return None
            return None
        except Exception:
            return None


    def on_file_selected(self):
        items = self.file_list.selectedItems()
        show_grid = self._current_decode_mode() == 'grc' and self.show_grid_cb.isChecked()
        try:
            if getattr(self, '_suppress_play_hide_once', False):
                self._suppress_play_hide_once = False
            else:
                self._update_output_play_button_visibility(False)
        except Exception:
            pass
        if not items:
            return
        try:
            if len(items) > 1:
                self._update_output_play_button_visibility(False)
        except Exception:
            pass
        f = items[0].text()
        path = os.path.join(self.local_decode_dir, f)
        # load previews asynchronously to avoid UI blocking
        try:
            size = (self.orig_view.width() or 800, self.orig_view.height() or 600)
        except Exception:
            size = (800, 600)
        try:
            # remember pending preview path to ignore stale results
            self._pending_preview_orig = path
            job = PreviewJob(path, 'orig', size, show_grid=show_grid, grid_cols=self.grid_spin.value())
            job.signals.finished.connect(self._on_preview_ready, QtCore.Qt.QueuedConnection)
            if getattr(self, '_thumb_pool', None):
                self._thumb_pool.start(job)
            else:
                job.run()
        except Exception:
            try:
                self.show_image_in_label(path, self.orig_view, show_grid=show_grid)
            except Exception:
                pass
        # 如果输出文件已存在，则直接在“输出”预览中显示
        try:
            output_path = self._find_restored_output(f)
            if output_path and os.path.exists(output_path):
                # 显示解码后文件（若为视频则显示第一帧） — async
                try:
                    out_size = (self.output_view.width() or 800, self.output_view.height() or 600)
                    self._pending_preview_output = output_path
                    job2 = PreviewJob(output_path, 'output', out_size, show_grid=False, grid_cols=self.grid_spin.value())
                    job2.signals.finished.connect(self._on_preview_ready, QtCore.Qt.QueuedConnection)
                    if getattr(self, '_thumb_pool', None):
                        self._thumb_pool.start(job2)
                    else:
                        job2.run()
                except Exception:
                    try:
                        self.show_image_in_label(output_path, self.output_view, show_grid=False)
                    except Exception:
                        pass
            else:
                self.output_view.clear()
                try:
                    self.file_info_label.clear()
                except Exception:
                    pass
                try:
                    self._current_pixmaps['output'] = None
                    self._current_paths['output'] = None
                except Exception:
                    pass
                try:
                    self._pending_preview_output = None
                except Exception:
                    pass
                try:
                    self._update_output_play_button_visibility(True)
                except Exception:
                    pass
        except Exception:
            self.output_view.clear()
            try:
                self.file_info_label.clear()
            except Exception:
                pass
            try:
                self._update_output_play_button_visibility(True)
            except Exception:
                pass


    def on_file_list_context_menu(self, pos):
        # pos is relative to the file_list
        # collect target filenames: either selected items or the item under cursor
        item_under = self.file_list.itemAt(pos)
        selected = self.file_list.selectedItems()
        targets = []
        if selected:
            targets = [it.text() for it in selected]
        elif item_under is not None:
            targets = [item_under.text()]
        else:
            return

        menu = QtWidgets.QMenu(self)
        act_open = menu.addAction('在默认应用中打开')
        act_open_folder = menu.addAction('在本地文件夹中打开')
        act_copy = menu.addAction('复制到剪贴板')
        act_paste = menu.addAction('粘贴')
        act_save = menu.addAction('另存为')
        act_compare = menu.addAction('加入比较')
        menu.addSeparator()
        act_del = menu.addAction('删除')
        # enable paste only for image/video content or local file URLs with image/video extensions
        try:
            cb = QtWidgets.QApplication.clipboard()
            md = cb.mimeData() if cb is not None else None
            ok_enable = False
            if md is not None:
                try:
                    # image data present
                    if md.hasImage():
                        ok_enable = True
                    # file URLs: allow if any url points to an image/video file
                    elif md.hasUrls():
                        for u in md.urls():
                            try:
                                p = u.toLocalFile() or u.toString()
                                if p:
                                    ext = os.path.splitext(p)[1].lower()
                                    if ext in ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tiff', '.mp4', '.mov', '.avi', '.mkv', '.webm'):
                                        ok_enable = True
                                        break
                            except Exception:
                                pass
                    # text path pointing to image/video
                    elif md.hasText():
                        try:
                            txt = md.text().strip()
                            if txt and os.path.exists(txt):
                                ext = os.path.splitext(txt)[1].lower()
                                if ext in ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tiff', '.mp4', '.mov', '.avi', '.mkv', '.webm'):
                                    ok_enable = True
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                act_paste.setEnabled(bool(ok_enable))
            except Exception:
                pass
        except Exception:
            pass

        act = menu.exec_(self.file_list.mapToGlobal(pos))
        if act == act_copy:
            self._copy_targets_to_clipboard(targets)
        elif act == act_save:
            self._save_as_targets(targets)
        elif act == act_compare:
            abs_paths = [os.path.join(self.local_decode_dir, t) for t in targets if t]
            self._add_to_compare(abs_paths)
        elif act == act_open:
            # open selected files in default application
            try:
                self._open_files_in_default_app(targets)
            except Exception as e:
                self.log(f'打开文件失败: {e}')
        elif act == act_open_folder:
            try:
                for name in targets:
                    try:
                        p = os.path.join(self.local_decode_dir, name)
                        folder = os.path.dirname(p) if p else p
                    except Exception:
                        folder = None
                    try:
                        if folder:
                            self._reveal_in_explorer(folder)
                    except Exception:
                        try:
                            # fallback to platform open
                            if folder:
                                if sys.platform.startswith('win'):
                                    os.startfile(folder)
                                elif sys.platform == 'darwin':
                                    subprocess.Popen(['open', folder])
                                else:
                                    subprocess.Popen(['xdg-open', folder])
                        except Exception:
                            pass
            except Exception:
                pass
        # Delete selected input files only (do not remove corresponding restored outputs)
        elif act == act_del:
            try:
                # delegate to helper that only removes input files and handles UI refresh
                self._delete_input_only(targets)
            except Exception as e:
                self.log(f'删除失败: {e}')
        elif act == act_paste:
            # Paste behavior: reuse existing drop logic when possible so behavior matches drag-and-drop
            try:
                cb = QtWidgets.QApplication.clipboard()
                md = cb.mimeData() if cb is not None else None
                if md is None:
                    self.log('剪贴板没有内容可粘贴')
                else:
                    # Priority 1: URLs -> directly feed to _on_files_dropped to match drag behavior
                    if md.hasUrls():
                        paths = []
                        for u in md.urls():
                            try:
                                p = u.toLocalFile() or u.toString()
                                if p:
                                    paths.append(p)
                            except Exception:
                                pass
                        if paths:
                            # call existing drop handler which enforces extensions/overwrite behavior
                            try:
                                self._on_files_dropped(paths)
                                return
                            except Exception as e:
                                self.log(f'粘贴导入失败: {e}')

                    # Priority 2: plain text containing one or more paths
                    if md.hasText():
                        try:
                            txt = md.text().strip()
                            # support multiple lines
                            lines = [l.strip() for l in txt.splitlines() if l.strip()]
                            candidates = []
                            for l in lines:
                                try:
                                    if os.path.exists(l):
                                        candidates.append(l)
                                except Exception:
                                    pass
                            if candidates:
                                try:
                                    self._on_files_dropped(candidates)
                                    return
                                except Exception as e:
                                    self.log(f'粘贴导入失败: {e}')
                        except Exception:
                            pass

                    # Priority 3: image data in clipboard -> save into local folder
                    if md.hasImage():
                        try:
                            img = None
                            try:
                                img = cb.image() if hasattr(cb, 'image') else None
                            except Exception:
                                img = None
                            if img is None:
                                try:
                                    img = md.imageData()
                                except Exception:
                                    img = None
                            if img is not None:
                                base = f'pasted_{int(time.time())}.png'
                                dst = os.path.join(self.local_decode_dir, base)
                                try:
                                    if isinstance(img, QtGui.QImage):
                                        img.save(dst)
                                    else:
                                        pix = img if isinstance(img, QtGui.QPixmap) else (QtGui.QPixmap.fromImage(img) if isinstance(img, QtGui.QImage) else None)
                                        if pix is not None:
                                            pix.save(dst)
                                        else:
                                            QtGui.QImage(img).save(dst)
                                    # refresh list via load_folder
                                    QtCore.QTimer.singleShot(50, lambda: self.load_folder(self.local_decode_dir))
                                    self.log('已将剪贴板图像保存到本地解码文件夹')
                                    return
                                except Exception as e:
                                    self.log(f'保存剪贴板图像失败: {e}')
                        except Exception:
                            pass

                    # nothing suitable
                    self.log('剪贴板中没有可粘贴的图像或视频文件')
            except Exception as e:
                self.log(f'粘贴失败: {e}')
        elif act == act_open_folder:
            # open parent folders for the selected entries
            try:
                abs_paths = [os.path.join(self.local_decode_dir, t) for t in targets]
                for p in abs_paths:
                    if not os.path.exists(p):
                        continue
                    try:
                        self._reveal_in_explorer(p)
                    except Exception:
                        pass
            except Exception as e:
                self.log(f'打开所在目录失败: {e}')


    def _delete_input_only(self, fnames):
        try:
            if isinstance(fnames, str):
                fnames = [fnames]
            paths = []
            for fname in fnames:
                try:
                    p = os.path.join(self.local_decode_dir, fname)
                    if os.path.exists(p):
                        paths.append(p)
                except Exception:
                    pass
            removed, errors = _move_to_trash(paths)
            if removed:
                self.log(f'已删除 {removed} 个输入文件 (已移入回收站)')
            for p, err in errors:
                self.log(f'删除输入失败: {p} 错误: {err}')
        except Exception as e:
            self.log(f'删除输入失败: {e}')
        # refresh once
        self.load_folder(self.local_decode_dir)
        # clear previews if they showed any of the removed files
        try:
            for _lbl in (self.orig_view_grc, self.orig_view_sst):
                _lbl.clear()
            try:
                if hasattr(self, 'orig_info_label') and self.orig_info_label is not None:
                    self.orig_info_label.clear()
            except Exception:
                pass
            # if output was one of removed, clear output and info
            self.output_view.clear()
            self.file_info_label.clear()
            try:
                self._current_paths['output'] = None
                self._current_pixmaps['output'] = None
                self._pending_preview_output = None
                self._suppress_play_hide_once = True
                self._update_output_play_button_visibility(True)
                try:
                    self._current_pixmaps['orig'] = None
                    self._current_paths['orig'] = None
                    self._orig_pixmaps_by_mode['grc'] = None
                    self._orig_pixmaps_by_mode['sst'] = None
                    self._orig_paths_by_mode['grc'] = None
                    self._orig_paths_by_mode['sst'] = None
                except Exception:
                    pass
                try:
                    self.output_play_btn.raise_()
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass


    def _save_as_targets(self, fnames):
        """Save one or multiple input files via Save As or choose folder for multiple files."""
        try:
            if isinstance(fnames, str):
                fnames = [fnames]
            if not fnames:
                return
            if len(fnames) == 1:
                src = os.path.join(self.local_decode_dir, fnames[0])
                if not os.path.exists(src):
                    self.log('源文件不存在，无法另存为')
                    return
                suggested = os.path.join(self.output_dir, fnames[0])
                dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存为', suggested)
                if dst:
                    try:
                        shutil.copy2(src, dst)
                        self.log(f'已另存为: {dst}')
                    except Exception as e:
                        self.log(f'另存为失败: {e}')
            else:
                # multiple files: choose destination folder
                d = QtWidgets.QFileDialog.getExistingDirectory(self, '选择目标文件夹')
                if not d:
                    return
                copied = 0
                for name in fnames:
                    try:
                        src = os.path.join(self.local_decode_dir, name)
                        if os.path.exists(src):
                            dst = os.path.join(d, name)
                            shutil.copy2(src, dst)
                            copied += 1
                    except Exception as e:
                        self.log(f'另存 {name} 失败: {e}')
                self.log(f'已另存 {copied} 个文件到 {d}')
        except Exception as e:
            self.log(f'另存为失败: {e}')


    def _copy_targets_to_clipboard(self, fnames):
        """Copy selected filenames (or single image) to system clipboard.
        - If multiple files: copy file URLs so Explorer can paste; fallback to newline paths.
        - If single file and a preview pixmap exists, copy image data and PNG bytes; also set file URL if exists.
        """
        try:
            if isinstance(fnames, str):
                fnames = [fnames]
            if not fnames:
                return
            clipboard = QtWidgets.QApplication.clipboard()
            if len(fnames) == 1:
                name = fnames[0]
                path = os.path.join(self.local_decode_dir, name)
                # if we have a pixmap for this file in current pixmaps, copy image data
                try:
                    pix = None
                    if self._current_paths.get('orig') == path and self._current_pixmaps.get('orig') is not None:
                        pix = self._current_pixmaps.get('orig')
                    elif self._current_paths.get('output') == path and self._current_pixmaps.get('output') is not None:
                        pix = self._current_pixmaps.get('output')
                    if pix is not None:
                        md = QtCore.QMimeData()
                        try:
                            md.setImageData(pix.toImage())
                        except Exception:
                            pass
                        try:
                            ba = QtCore.QByteArray()
                            buf = QtCore.QBuffer(ba)
                            buf.open(QtCore.QIODevice.WriteOnly)
                            pix.save(buf, 'PNG')
                            buf.close()
                            md.setData('image/png', ba)
                        except Exception:
                            pass
                        if os.path.exists(path):
                            try:
                                md.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(path))])
                            except Exception:
                                pass
                        clipboard.setMimeData(md)
                        self.log('已将图像复制到剪贴板')
                        return
                except Exception:
                    pass
                # otherwise try to copy the file itself as a file URL (so Explorer can paste)
                try:
                    md = QtCore.QMimeData()
                    if os.path.exists(path):
                        md.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(path))])
                        clipboard.setMimeData(md)
                        self.log('已将文件路径复制到剪贴板 (可粘贴到资源管理器)')
                        return
                except Exception:
                    pass
                # fallback: copy the absolute path as text
                clipboard.setText(os.path.abspath(path))
                self.log('已将路径复制到剪贴板')
            else:
                paths = [os.path.abspath(os.path.join(self.local_decode_dir, n)) for n in fnames]
                try:
                    md = QtCore.QMimeData()
                    md.setUrls([QtCore.QUrl.fromLocalFile(p) for p in paths])
                    clipboard.setMimeData(md)
                    self.log(f'已将 {len(paths)} 个文件复制到剪贴板 (可粘贴到资源管理器)')
                    return
                except Exception:
                    clipboard.setText('\n'.join(paths))
                    self.log(f'已将 {len(paths)} 个路径复制到剪贴板')
        except Exception as e:
            self.log(f'复制到剪贴板失败: {e}')


    def _delete_input_and_output(self, fnames):
        try:
            if isinstance(fnames, str):
                fnames = [fnames]
            paths = []
            for fname in fnames:
                try:
                    p_in = os.path.join(self.local_decode_dir, fname)
                    if os.path.exists(p_in):
                        paths.append(p_in)
                    name, ext = os.path.splitext(fname)
                    p_out = os.path.join(self.output_dir, f"{name}_restored{ext}")
                    if os.path.exists(p_out):
                        paths.append(p_out)
                except Exception:
                    pass
            removed, errors = _move_to_trash(paths)
            if removed:
                self.log(f'已删除 {removed} 个输入/输出文件 (已移入回收站)')
            for p, err in errors:
                self.log(f'删除输入/输出失败: {p} 错误: {err}')
        except Exception as e:
            self.log(f'删除输入/输出失败: {e}')
        # refresh once
        self.load_folder(self.local_decode_dir)
        try:
            self.output_view.clear()
            self.file_info_label.clear()
            try:
                self._current_paths['output'] = None
                self._current_pixmaps['output'] = None
                self._pending_preview_output = None
                self._suppress_play_hide_once = True
                self._update_output_play_button_visibility(True)
                try:
                    self.output_play_btn.raise_()
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass
            try:
                self.file_info_label.clear()
            except Exception:
                pass


    def _open_files_in_default_app(self, fnames):
        """Open one or more files in the system default application."""
        try:
            if isinstance(fnames, str):
                fnames = [fnames]
            for name in fnames:
                try:
                    path = os.path.join(self.local_decode_dir, name)
                    if not os.path.exists(path):
                        self.log(f'文件不存在: {name}')
                        continue
                    if sys.platform.startswith('win'):
                        try:
                            os.startfile(path)
                            continue
                        except Exception:
                            pass
                    if sys.platform == 'darwin':
                        subprocess.Popen(['open', path])
                    else:
                        subprocess.Popen(['xdg-open', path])
                except Exception as e:
                    self.log(f'打开文件失败: {name} 错误: {e}')
        except Exception as e:
            self.log(f'打开文件失败: {e}')


    def _on_output_play_clicked(self):
        """Start decoding for the current selection from the output overlay."""
        try:
            self._update_output_play_button_visibility(False)
        except Exception:
            pass
        try:
            self.on_decode_selected()
        except Exception as e:
            try:
                self.log(f'解码启动失败: {e}')
            except Exception:
                pass


    def _current_decode_mode(self):
        try:
            idx = self.preview_tabs.currentIndex()
            if idx == getattr(self, '_sst_tab_index', -1):
                return 'sst'
        except Exception:
            pass
        return 'grc'


    def on_decode_selected(self):
        """Decode the currently selected files and save outputs to the output folder."""
        if self.worker and self.worker.isRunning():
            return
        if self._current_decode_mode() == 'grc' and not self.grid_spin.commit():
            self.log('请先填写有效的网格列数（4–256）')
            self.grid_spin.setFocus()
            return
        items = self.file_list.selectedItems()
        if not items:
            self.log('请先选择至少一个文件以解码')
            return
        files = [it.text() for it in items]
        try:
            self._update_output_play_button_visibility(False)
        except Exception:
            pass
        # start worker to process these files
        self.current_worker_files = files
        mode_key = self._current_decode_mode()
        pwd_val = ''
        try:
            pwd_val = self.sst_pwd_edit.text() if mode_key == 'sst' else ''
        except Exception:
            pwd_val = ''
        # ensure module grid settings follow UI for GRC only
        try:
            if mode_key == 'grc':
                grc.grid_cols = int(self.grid_spin.value())
                grc.grid_rows = int(grc.grid_cols) + 2
        except Exception:
            pass
        self.worker = Worker(files, self.local_decode_dir, self.output_dir, keep_audio=True, decode_mode=mode_key, overwrite=self.overwrite_cb.isChecked(), password=pwd_val, grid_cols=self.grid_spin.value())
        # configure progress bar: if single file, show indeterminate busy state;
        # if multiple files, use determinate 0-100
        if len(files) == 1:
            self.progress.setRange(0, 0)  # indeterminate
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        self.worker.progress.connect(self._on_progress_slot)
        self.worker.log.connect(self.log)
        self.worker.finished.connect(self.on_worker_finished)
        self.batch_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self._decode_page.set_running(True)
        self.worker.start()
        # start elapsed timer display
        try:
            import time
            self._progress_start_time = time.time()
            self._elapsed_timer.start()
        except Exception:
            pass
        self.log(f'已开始解码 {len(files)} 个选中文件')


    def show_image_in_label(self, path, label, show_grid=False):
        try:
            if path.lower().endswith(VIDEO_EXTS):
                # for video/GIF show first frame
                if path.lower().endswith('.gif'):
                    img = Image.open(path)
                    frame = img.copy().convert('RGB')
                else:
                    cap = cv2.VideoCapture(path)
                    ret, frame = cap.read()
                    cap.release()
                    if not ret:
                        label.setText('无法读取视频帧')
                        return
                    frame = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                img = frame
            else:
                img = Image.open(path).convert('RGB')
            if show_grid:
                img = self._draw_grid_overlay(img)
            qim = self.pil2pixmap(img)
            # store the pixmap for responsive scaling
            if label in (getattr(self, 'orig_view_grc', None), getattr(self, 'orig_view_sst', None)):
                mode = _mode_for_label(label, getattr(self, 'orig_view_grc', None), getattr(self, 'orig_view_sst', None))
                self._orig_pixmaps_by_mode[mode] = qim
                self._orig_paths_by_mode[mode] = path
                # keep active alias caches in sync when storing to the currently selected tab
                try:
                    if mode == self._current_decode_mode():
                        self._current_pixmaps['orig'] = qim
                        self._current_paths['orig'] = path
                except Exception:
                    pass
            elif label is self.output_view:
                self._current_pixmaps['output'] = qim
                self._current_paths['output'] = path
            # set as base pixmap for zooming support
            try:
                if hasattr(label, 'set_base_pixmap'):
                    label.set_base_pixmap(qim)
                else:
                    label.setPixmap(qim.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            except Exception:
                try:
                    label.setPixmap(qim.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                except Exception:
                    pass
            # update file info when showing previews directly without background jobs
            if label is self.orig_view:
                try:
                    self._set_file_info(path, target='orig')
                except Exception:
                    pass
            elif label is self.output_view:
                try:
                    self._set_file_info(path, target='output')
                except Exception:
                    pass
        except Exception as e:
            label.setText(f'显示失败: {e}')


    def _open_current_file(self, which):
        """Open the currently shown file in the system default application.
        `which` is 'orig' or 'output'."""
        try:
            path = self._current_paths.get(which)
            if not path or not os.path.exists(path):
                self.log('未找到文件以打开')
                return
            # cross-platform open
            if sys.platform.startswith('win'):
                try:
                    os.startfile(path)
                    return
                except Exception:
                    pass
            # macOS
            if sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', path])
                return
            # linux / others
            import subprocess
            subprocess.Popen(['xdg-open', path])
        except Exception as e:
            try:
                self.log(f'打开文件失败: {e}')
            except Exception:
                pass


    def _ensure_compare_window(self):
        try:
            if self._compare_window is None:
                self._compare_window = CompareWindow(self)
                try:
                    self._compare_window.destroyed.connect(lambda: setattr(self, '_compare_window', None))
                except Exception:
                    pass
            try:
                self._compare_window.sync_theme(getattr(self, '_theme_mode', 'dark'))
            except Exception:
                pass
            self._compare_window.show()
            self._compare_window.raise_()
            self._compare_window.activateWindow()
            return self._compare_window
        except Exception:
            self.log('无法打开比较窗口')
            return None


    def _add_to_compare(self, paths):
        try:
            valid = [p for p in paths if p and os.path.exists(p)]
        except Exception:
            valid = []
        if not valid:
            self.log('没有可加入比较的文件')
            return
        window = self._ensure_compare_window()
        if window is not None:
            window.add_paths(valid)


    def _on_preview_context_menu(self, which, pos):
        """Show context menu for preview area (which: 'orig' or 'output')."""
        try:
            path = self._current_paths.get(which)
            menu = QtWidgets.QMenu(self)
            act_open = menu.addAction('在默认应用中打开')
            act_open_folder = menu.addAction('在本地文件夹中打开')
            act_copy = menu.addAction('复制到剪贴板')
            act_save = menu.addAction('另存为')
            act_compare = menu.addAction('加入比较')
            menu.addSeparator()
            act_del = menu.addAction('删除')
            if not path:
                act_compare.setEnabled(False)
                act_del.setEnabled(False)
            act = menu.exec_(QtGui.QCursor.pos())
            if act == act_open:
                try:
                    # open the file currently shown in this preview
                    self._open_current_file(which)
                except Exception as e:
                    self.log(f'打开文件失败: {e}')
                return
            if act == act_open_folder:
                try:
                    if not path:
                        return
                    try:
                        try:
                            folder = os.path.dirname(path)
                        except Exception:
                            folder = path
                        try:
                            self._reveal_in_explorer(folder)
                        except Exception:
                            pass
                    except Exception:
                        pass
                except Exception as e:
                    self.log(f'打开所在目录失败: {e}')
                return
            if act == act_copy:
                # try to copy image pixmap if available, else copy path
                try:
                    pix = None
                    if which == 'orig' and self._current_pixmaps.get('orig') is not None:
                        pix = self._current_pixmaps.get('orig')
                    elif which == 'output' and self._current_pixmaps.get('output') is not None:
                        pix = self._current_pixmaps.get('output')
                    if pix is not None:
                        try:
                            md = QtCore.QMimeData()
                            try:
                                md.setImageData(pix.toImage())
                            except Exception:
                                pass
                            try:
                                ba = QtCore.QByteArray()
                                buf = QtCore.QBuffer(ba)
                                buf.open(QtCore.QIODevice.WriteOnly)
                                pix.save(buf, 'PNG')
                                buf.close()
                                md.setData('image/png', ba)
                            except Exception:
                                pass
                            # if underlying file path exists, also provide it as a file URL
                            if path and os.path.exists(path):
                                try:
                                    md.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(path))])
                                except Exception:
                                    pass
                            QtWidgets.QApplication.clipboard().setMimeData(md)
                            self.log('已将预览图复制到剪贴板')
                        except Exception as e:
                            self.log(f'复制到剪贴板失败: {e}')
                    else:
                        # copy path text or file URL for Explorer
                        if path and os.path.exists(path):
                            try:
                                md = QtCore.QMimeData()
                                md.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(path))])
                                QtWidgets.QApplication.clipboard().setMimeData(md)
                                self.log('已将预览文件复制到剪贴板 (可粘贴到资源管理器)')
                                return
                            except Exception:
                                pass
                            QtWidgets.QApplication.clipboard().setText(os.path.abspath(path))
                            self.log('已将预览文件路径复制到剪贴板')
                        else:
                            self.log('当前无可用文件以复制')
                except Exception as e:
                    self.log(f'复制到剪贴板失败: {e}')
            elif act == act_save:
                if not path or not os.path.exists(path):
                    self.log('当前无可用文件以另存')
                    return
                # offer Save As for the single file
                dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存为', os.path.join(self.output_dir, os.path.basename(path)))
                if dst:
                    try:
                        shutil.copy2(path, dst)
                        self.log(f'已另存为: {dst}')
                    except Exception as e:
                        self.log(f'另存为失败: {e}')
            elif act == act_compare and path:
                self._add_to_compare([path])
            elif act == act_del and path:
                try:
                    removed, errors = _move_to_trash([path])
                    if removed:
                        self.log(f'已删除: {os.path.basename(path)} (已移入回收站)')
                    for p, err in errors:
                        self.log(f'删除失败: {p} 错误: {err}')
                except Exception as e:
                    self.log(f'删除失败: {e}')
                try:
                    self._current_paths[which] = None
                    self._current_pixmaps[which] = None
                    if which == 'orig':
                        try:
                            mode = self._current_decode_mode()
                            self._orig_pixmaps_by_mode[mode] = None
                            self._orig_paths_by_mode[mode] = None
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    if which == 'output':
                        self._pending_preview_output = None
                        self._suppress_play_hide_once = True
                        self._update_output_play_button_visibility(True)
                        try:
                            self.output_play_btn.raise_()
                        except Exception:
                            pass
                    else:
                        self._update_output_play_button_visibility(False)
                except Exception:
                    pass
                try:
                    if which == 'orig':
                        for _lbl in (self.orig_view_grc, self.orig_view_sst):
                            try:
                                _lbl.clear()
                            except Exception:
                                pass
                        if hasattr(self, 'orig_info_label') and self.orig_info_label is not None:
                            self.orig_info_label.clear()
                    else:
                        self.output_view.clear()
                    if hasattr(self, 'file_info_label') and self.file_info_label is not None:
                        self.file_info_label.clear()
                except Exception:
                    pass
                try:
                    self.load_folder(self.local_decode_dir)
                except Exception:
                    pass
        except Exception as e:
            try:
                self.log(f'右键菜单失败: {e}')
            except Exception:
                pass


    def closeEvent(self, event):
        connections = getattr(self, '_rh_connection_settings', None)
        if connections is not None:
            try:
                connections.flush_pending()
            except (OSError, ValueError, TypeError):
                self._show_toast('连接设置未能保存，请检查 apikeys.json 是否可写。', 5000)
        clear_histories(self)
        self._closing = True
        home_page = getattr(self, 'home_page', None)
        if home_page is not None:
            home_page.close_updates()
        execution = getattr(self, '_rh_execution_service', None)
        if execution is not None:
            execution.close()
        canvas = getattr(self, 'canvas_page', None)
        if canvas is not None:
            canvas.shutdown()
        workflow_queue = getattr(self, '_canvas_workflow_queue', None)
        if workflow_queue is not None:
            workflow_queue.close()
            workflow_queue.engine.close()
        from aetherloom_core.api_manager_ui import close_probes
        close_probes(self)
        submission_queue = getattr(self, '_rh_submission_queue', None)
        if submission_queue is not None:
            submission_queue.close()
        scheduler = getattr(self, '_thumb_scheduler', None)
        if scheduler is not None:
            scheduler.close()
        preview = getattr(self, '_local_preview_window', None)
        if preview is not None:
            preview.close()
        for timer in getattr(self, '_scroll_enqueue_timers', {}).values():
            timer.stop()
        for timer in getattr(self, '_thumb_view_timers', {}).values():
            timer.stop()
        for timer_name in ('_scroll_idle_timer', '_thumb_interaction_timer', '_local_search_timer'):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        self._thumb_raw_cache.clear()
        self._thumb_mem_cache.clear()
        self._pending_high_keys.clear()
        if hasattr(self, '_thumb_residency'):
            self._thumb_residency.clear()
        local_scan = getattr(self, '_local_scan_controller', None)
        if local_scan is not None:
            local_scan.close()
        decode_scan = getattr(self, '_decode_scan_controller', None)
        if decode_scan is not None:
            decode_scan.close()
        self._decode_scan_loading = False
        self._local_scan_loading = False
        self._local_thumb_resize_generation = getattr(self, '_local_thumb_resize_generation', 0) + 1
        lifecycle = getattr(self, '_rh_task_lifecycle', None)
        if lifecycle is not None:
            lifecycle.stop()
        for timer_name in ('_rh_app_status_timer', '_rh_card_timer'):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        self._local_pending_add.clear()
        # save settings on close
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self._prune_thumb_cache(max_size_mb=getattr(self, 'thumb_cache_max_mb', 300), max_files=4000, max_age_days=14, aggressive=True)
        except Exception:
            pass
        try:
            if getattr(self, '_compare_window', None) is not None:
                self._compare_window.close()
        except Exception:
            pass
        try:
            super().closeEvent(event)
        except Exception:
            event.accept()


    def _draw_grid_overlay(self, pil_img):
        w, h = pil_img.size
        try:
            mode = self._current_decode_mode()
        except Exception:
            mode = 'grc'
        if mode == 'sst':
            cols = 32
        else:
            cols = int(self.grid_spin.value())
        rows = cols + 2
        draw = pil_img.copy()
        import PIL.ImageDraw as ImageDraw
        d = ImageDraw.Draw(draw)
        tw = w / cols
        th = h / rows
        for i in range(1, cols):
            x = int(i * tw)
            d.line([(x, 0), (x, h)], fill=(255, 0, 0), width=1)
        for j in range(1, rows):
            y = int(j * th)
            d.line([(0, y), (w, y)], fill=(255, 0, 0), width=1)
        return draw


    def _format_bytes(self, num):
        for unit in ['B','KB','MB','GB','TB']:
            if num < 1024.0:
                return f"{num:.2f}{unit}"
            num /= 1024.0
        return f"{num:.2f}PB"


    def _set_file_info(self, path, target='output'):
        label = None
        try:
            try:
                label = (getattr(self, '_info_labels', {}) or {}).get(target)
            except Exception:
                label = self.file_info_label if target == 'output' else None

            def fmt_size_bytes(sz):
                try:
                    if sz >= 1024 * 1024:
                        return f"{sz / (1024*1024):.2f} MB"
                    else:
                        return f"{sz / 1024:.2f} KB"
                except Exception:
                    return ''

            parts = []
            # filename only
            try:
                parts.append(os.path.basename(path))
            except Exception:
                parts.append(path)

            # size
            try:
                size = os.path.getsize(path)
                sztxt = fmt_size_bytes(size)
                if sztxt:
                    parts.append(sztxt)
            except Exception:
                pass

            ext = os.path.splitext(path)[1].lower()
            # image
            if ext in IMAGE_EXTS:
                try:
                    img = Image.open(path)
                    w, h = img.size
                    parts.insert(1, f"{w}x{h} (IMAGE)")
                except Exception:
                    pass
            # gif (treat specially)
            elif ext == '.gif':
                try:
                    img = Image.open(path)
                    w, h = img.size
                    frames = int(getattr(img, 'n_frames', 1) or 1)
                    total_ms = 0
                    try:
                        if frames > 1:
                            for i in range(frames):
                                try:
                                    img.seek(i)
                                    total_ms += int(img.info.get('duration', 0) or 0)
                                except Exception:
                                    pass
                        else:
                            total_ms = int(img.info.get('duration', 0) or 0)
                    except Exception:
                        try:
                            total_ms = int(img.info.get('duration', 0) or 0)
                        except Exception:
                            total_ms = 0
                    total_s = (total_ms / 1000.0) if total_ms else 0.0
                    fps = (frames / total_s) if total_s > 0 else 0.0
                    extras = []
                    if fps > 0:
                        extras.append(f"{fps:.2f} FPS")
                    if total_s > 0:
                        extras.append(f"{total_s:.2f}s")
                    extras.append(f"{frames} frames")
                    note = (' ' + '/'.join(extras)) if extras else ''
                    parts.insert(1, f"{w}x{h} (GIF){note}")
                except Exception:
                    pass
            # video
            elif ext in VIDEO_EXTS:
                try:
                    cap = cv2.VideoCapture(path)
                    if cap.isOpened():
                        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
                        duration = frames / fps if fps > 0 else 0.0
                        extra = []
                        if fps > 0:
                            extra.append(f"{fps:.2f} FPS")
                        if duration > 0:
                            extra.append(f"{duration:.2f}s")
                        note = ''
                        if extra:
                            note = ' ' + '/'.join(extra)
                        parts.insert(1, f"{w}x{h} (VIDEO){note}")
                    cap.release()
                except Exception:
                    pass

            if label is not None:
                try:
                    label.setText(' | '.join(parts))
                except Exception:
                    pass
        except Exception:
            try:
                if label is None and target == 'output':
                    label = self.file_info_label
                if label is not None:
                    label.setText('')
            except Exception:
                pass


    def resizeEvent(self, event):
        """Handle window resize to rescale preview pixmaps responsively."""
        try:
            super().resizeEvent(event)
        except Exception:
            pass
        compact = self.width() < 1000
        if compact != getattr(self, '_sidebar_auto_collapsed', False):
            self._sidebar_auto_collapsed = compact
            self._sidebar_auto_override = None
            self._apply_ui_scale(getattr(self, '_ui_scale_factor', 1.0))
        try:
            # Debounced reflow of RunningHub app buttons when main window resizes
            if hasattr(self, '_reflow_rh_buttons'):
                try:
                    if not hasattr(self, '_rh_reflow_timer'):
                        self._rh_reflow_timer = QtCore.QTimer(self)
                        self._rh_reflow_timer.setSingleShot(True)
                        self._rh_reflow_timer.timeout.connect(lambda: self._reflow_rh_buttons())
                    # restart timer (will fire after 200ms of no further resize)
                    self._rh_reflow_timer.start(200)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            # rescale origin pixmap
            if self._current_pixmaps.get('orig') is not None:
                pix = self._current_pixmaps['orig']
                try:
                    # use DropLabel update if available
                    if hasattr(self.orig_view, 'set_base_pixmap'):
                        self.orig_view.set_base_pixmap(pix)
                    else:
                        self.orig_view.setPixmap(pix.scaled(self.orig_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                except Exception:
                    try:
                        self.orig_view.setPixmap(pix.scaled(self.orig_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    except Exception:
                        pass
            if self._current_pixmaps.get('output') is not None:
                pix = self._current_pixmaps['output']
                try:
                    if hasattr(self.output_view, 'set_base_pixmap'):
                        self.output_view.set_base_pixmap(pix)
                    else:
                        self.output_view.setPixmap(pix.scaled(self.output_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                except Exception:
                    try:
                        self.output_view.setPixmap(pix.scaled(self.output_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    except Exception:
                        pass
        except Exception:
            pass


    def pil2pixmap(self, img):
        try:
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format='PNG')
            data = buf.getvalue()
            pix = QtGui.QPixmap()
            if pix.loadFromData(data, 'PNG'):
                return pix
        except Exception:
            pass
        # fallback: try raw conversion (best-effort)
        data = img.tobytes('raw', 'RGB')
        qimg = QtGui.QImage(data, img.width, img.height, QtGui.QImage.Format_RGB888)
        return QtGui.QPixmap.fromImage(qimg)


    def _restore_frame_from_pil(self, pil_img):
        """Restore a single PIL image (one frame) using the grid mapping."""
        try:
            width, height = pil_img.size
            cols = int(self.grid_spin.value())
            rows = cols + 2
            tile_w = width // cols
            tile_h = height // rows
            restored_img = Image.new('RGB', (width, tile_h * cols))
            for row in range(cols):
                for col in range(cols):
                    reversed_row = rows - 1 - row
                    reversed_col = cols - 1 - col
                    left = reversed_col * tile_w
                    upper = reversed_row * tile_h
                    right = left + tile_w
                    lower = upper + tile_h
                    tile = pil_img.crop((left, upper, right, lower))
                    restore_x = col * tile_w
                    restore_y = row * tile_h
                    restored_img.paste(tile, (restore_x, restore_y))
            return restored_img
        except Exception:
            return None


    def on_preview_restore(self):
        items = self.file_list.selectedItems()
        if not items:
            self.log('请先选择一个文件以预览还原')
            return
        f = items[0].text()
        src = os.path.join(self.local_decode_dir, f)
        _, ext = os.path.splitext(f)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(tmp_fd)

        # update grid settings in module
        grc.grid_cols = int(self.grid_spin.value())
        grc.grid_rows = int(grc.grid_cols) + 2

        self.log(f'开始预览还原: {f} -> 临时 {tmp_path}')

        def job():
            try:
                if ext.lower() in IMAGE_EXTS:
                    ok = grc.reverse_image_grid(src, tmp_path)
                else:
                    ok = grc.restore_video_cv2(src, tmp_path)
                QtCore.QMetaObject.invokeMethod(self, 'show_restored_preview', Qt.QueuedConnection, QtCore.Q_ARG(str, tmp_path))
                self.log(f"预览结束: {f} {'OK' if ok else 'FAIL'}")
            except Exception as e:
                self.log(f'预览失败: {e}')
        # run preview job in a QThread to avoid using QObjects/timers from std threads
        thread = QtCore.QThread()
        class _PreviewJobWorker(QtCore.QObject):
            finished = QtCore.pyqtSignal()

            def __init__(self, fn):
                super().__init__()
                self.fn = fn

            @QtCore.pyqtSlot()
            def run(self):
                try:
                    self.fn()
                except Exception:
                    pass
                finally:
                    try:
                        self.finished.emit()
                    except Exception:
                        pass

        w = _PreviewJobWorker(job)
        w.moveToThread(thread)
        thread.started.connect(w.run)
        w.finished.connect(thread.quit)
        w.finished.connect(w.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()


    @QtCore.pyqtSlot(str)
    def show_restored_preview(self, path):
        self.show_image_in_label(path, self.output_view, show_grid=self.show_grid_cb.isChecked())


    def on_batch_restore(self):
        if self.worker and self.worker.isRunning():
            return
        if self._current_decode_mode() == 'grc' and not self.grid_spin.commit():
            self.log('请先填写有效的网格列数（4–256）')
            self.grid_spin.setFocus()
            return
        items = [self.file_list.item(i).text() for i in range(self.file_list.count())]
        if not items:
            self.log('文件列表为空，无法批量解码')
            return
        # update grid settings
        grc.grid_cols = int(self.grid_spin.value())
        grc.grid_rows = int(grc.grid_cols) + 2

        self.current_worker_files = items
        mode_key = self._current_decode_mode()
        # ensure module grid settings follow UI for GRC only
        try:
            if mode_key == 'grc':
                grc.grid_cols = int(self.grid_spin.value())
                grc.grid_rows = int(grc.grid_cols) + 2
        except Exception:
            pass
        pwd_val = ''
        try:
            pwd_val = self.sst_pwd_edit.text() if mode_key == 'sst' else ''
        except Exception:
            pwd_val = ''
        self.worker = Worker(items, self.local_decode_dir, self.output_dir, keep_audio=True, decode_mode=mode_key, overwrite=self.overwrite_cb.isChecked(), password=pwd_val, grid_cols=self.grid_spin.value())
        # configure progress bar: if single file, show indeterminate busy state;
        # since this is batch, use determinate range
        if len(items) == 1:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        self.worker.progress.connect(self._on_progress_slot)
        self.worker.log.connect(self.log)
        self.worker.finished.connect(self.on_worker_finished)
        self.batch_btn.setEnabled(False)
        self._decode_page.set_running(True)
        self.worker.start()
        # start elapsed timer display for batch
        try:
            import time
            self._progress_start_time = time.time()
            self._elapsed_timer.start()
        except Exception:
            pass
        self.log('已启动批量解码任务')


    def on_cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self._decode_page.set_canceling()
            self.log('正在请求取消任务...')
            # Keep elapsed/progress visible until the worker confirms it stopped.


    def on_grid_changed(self, val):
        """Handle grid column changes from the UI."""
        try:
            # grid change only affects GRC mode
            mode = self._current_decode_mode()
            if mode == 'grc':
                grc.grid_cols = int(val)
                grc.grid_rows = int(grc.grid_cols) + 2
                QtCore.QTimer.singleShot(50, self.on_file_selected)
        except Exception:
            pass


    def on_worker_finished(self):
        canceled = bool(self.worker and self.worker._is_cancelled)
        self.batch_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        # restore determinate range and mark complete
        try:
            self.progress.setRange(0, 100)
        except Exception:
            pass
        self.progress.setValue(0 if canceled else 100)
        self.log('解码任务已停止' if canceled else '解码任务处理结束')
        self._decode_page.set_running(False, canceled=canceled)
        # if we have a list of files processed, show output preview for first file if exists
        output_found = False
        try:
            files = getattr(self, 'current_worker_files', None)
            if files and len(files) > 0:
                first = files[0]
                output_path = self._find_restored_output(first)
                if output_path and os.path.exists(output_path):
                    self.show_image_in_label(output_path, self.output_view, show_grid=False)
                    output_found = True
        except Exception:
            pass
        try:
            self._update_output_play_button_visibility(not output_found)
        except Exception:
            pass
        # stop elapsed timer and show final elapsed
        try:
            self._elapsed_timer.stop()
            # ensure label shows final elapsed
            self._update_elapsed_label()
        except Exception:
            pass


    def on_progress_update(self, val):
        """Main-thread slot for updating progress bar."""
        try:
            # clamp to 0-100
            v = int(val)
            if v < 0:
                v = 0
            if v > 100:
                v = 100
            self.progress.setValue(v)
        except Exception:
            pass


    def _update_elapsed_label(self):
        try:
            import time
            if not self._progress_start_time:
                self.elapsed_label.setText('解码用时: 0.00s')
                return
            elapsed = time.time() - self._progress_start_time
            # show elapsed seconds with two decimals
            txt = f'解码用时: {elapsed:.2f}s'
            self.elapsed_label.setText(txt)
        except Exception:
            pass
