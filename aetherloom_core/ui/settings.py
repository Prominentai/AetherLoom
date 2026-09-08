"""Window settings serialization and restoration."""
from aetherloom_core.resources import DEFAULT_EXPAND_SYSTEM_PROMPT
from aetherloom_core.resources import DEFAULT_IMAGE_REVERSE_PROMPT
from aetherloom_core.autocomplete import completion_options
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.services.decoding import grc
import os


class SettingsMixin:
    def _load_settings(self):
        try:
            import json
            if os.path.exists(self.settings_path):
                with open(self.settings_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return None


    def _save_settings(self):
        try:
            import json
            # capture current UI settings
            try:
                geom = self.normalGeometry() if self.isMaximized() else self.geometry()
            except Exception:
                geom = self.geometry()
            screen = None
            try:
                if self.windowHandle() is not None:
                    screen = self.windowHandle().screen()
            except Exception:
                screen = None
            if screen is None:
                try:
                    screen = self.screen()
                except Exception:
                    screen = None
            if screen is None:
                screen = QtWidgets.QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, geom.width(), geom.height())
            # Only persist window size and maximized state. Do not save position (x/y)
            # so the window will always be centered on startup.
            win = {
                'width': geom.width(),
                'height': geom.height(),
                'screen_width': avail.width(),
                'screen_height': avail.height(),
                'maximized': bool(self.isMaximized())
            }
            data = {
                'input_dir': self.input_dir,
                'output_dir': self.output_dir,
                'local_decode_dir': self.local_decode_dir,
                'splitter_sizes': self.settings.get('splitter_sizes') if isinstance(getattr(self, 'settings', None), dict) else None,
                'grid_cols': int(self.grid_spin.value()) if hasattr(self, 'grid_spin') else None,
                'decode_mode': self._current_decode_mode(),
                'decode_password': self.sst_pwd_edit.text() if hasattr(self, 'sst_pwd_edit') else '',
                'show_grid': bool(getattr(self, 'show_grid_cb', False) and self.show_grid_cb.isChecked()),
                'overwrite': bool(getattr(self, 'overwrite_cb', False) and self.overwrite_cb.isChecked()),
                'thumb_size': int(getattr(self, 'thumb_size_spin', None).value() if hasattr(self, 'thumb_size_spin') else (getattr(self, 'thumb_size_slider', None).value() if hasattr(self, 'thumb_size_slider') else 200)),
                'thumb_cache_max_mb': int(getattr(self, 'thumb_cache_spin', None).value() if hasattr(self, 'thumb_cache_spin') else getattr(self, 'thumb_cache_max_mb', 300)),
                'app_page_cache_limit': int(getattr(self, 'app_cache_spin', None).value() if hasattr(self, 'app_cache_spin') else getattr(self, 'app_page_cache_limit', 20)),
                'rh_retry_max': int(getattr(self, 'rh_retry_max', 100)),
                'rh_retry_delay': int(getattr(self, 'rh_retry_delay', 5)),
                'rh_retry_head_count': int(getattr(self, 'rh_retry_head_count', 1)),
                'autocomplete': completion_options(getattr(self, 'settings', None)),
                'local_mode': int(self.local_mode_group.checkedId()) if hasattr(self, 'local_mode_group') and self.local_mode_group is not None else None,
                'window': win,
                'theme_mode': getattr(self, '_theme_mode', 'dark')
            }
            try:
                favs = getattr(self, 'rh_favorites', None)
                if favs is not None:
                    data['rh_favorites'] = sorted([str(x) for x in favs if x is not None])
            except Exception:
                pass
            try:
                store = getattr(self, 'rh_local_decode_settings', None)
                if not isinstance(store, dict) and isinstance(getattr(self, 'settings', None), dict):
                    store = self.settings.get('rh_local_decode_settings')
                if isinstance(store, dict):
                    cleaned_local = {}
                    for k, v in (store or {}).items():
                        if not isinstance(v, dict):
                            continue
                        try:
                            cleaned_local[str(k)] = {
                                'enabled': bool(v.get('enabled', False)),
                                'mode': str(v.get('mode', 'grc') or 'grc'),
                                'password': str(v.get('password', '') or ''),
                                'grid_cols': int(v.get('grid_cols', 32) or 32),
                                'delete_original': bool(v.get('delete_original', True)),
                                'sidebar_visible': bool(v.get('sidebar_visible', False))
                            }
                        except Exception:
                            pass
                    data['rh_local_decode_settings'] = cleaned_local
            except Exception:
                pass
            try:
                if hasattr(self, 'rh_host_combo') and self.rh_host_combo is not None:
                    try:
                        data['runninghub_host'] = self.rh_host_combo.currentText()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                if hasattr(self, '_collect_api_settings_from_ui'):
                    api_settings = self._collect_api_settings_from_ui() or getattr(self, 'api_settings', None)
                else:
                    api_settings = getattr(self, 'api_settings', None)
                if api_settings:
                    # ensure no api_key fields are persisted into settings.json
                    try:
                        cleaned = {}
                        for k, v in (api_settings or {}).items():
                            if not isinstance(v, dict):
                                cleaned[k] = v
                                continue
                            entry = dict(v)
                            entry.pop('api_key', None)
                            cleaned[k] = entry
                        data['api_settings'] = cleaned
                    except Exception:
                        data['api_settings'] = api_settings
            except Exception:
                pass
            try:
                if isinstance(getattr(self, 'api_custom_cache', None), dict):
                    # custom cache should not contain api_key either
                    try:
                        cc = {}
                        for k, v in (self.api_custom_cache or {}).items():
                            if not isinstance(v, dict):
                                cc[k] = v
                                continue
                            ev = dict(v)
                            ev.pop('api_key', None)
                            cc[k] = ev
                        data['custom_api_settings'] = cc
                    except Exception:
                        data['custom_api_settings'] = self.api_custom_cache
            except Exception:
                pass
            try:
                self._sanitize_api_provider_profiles()
                profiles = self._ensure_api_provider_profile_store()
                if profiles:
                    data['api_provider_profiles'] = profiles
            except Exception:
                pass

            # Persist only actual custom-key edits. Loading a model configuration
            # can briefly clear its field and must never overwrite stored keys.
            try:
                dirty_custom_keys = any(
                    fields['provider'].currentData() == 'custom' and
                    getattr(fields.get('api_key'), '_api_key_dirty', False)
                    for fields in getattr(self, 'api_config_fields', {}).values())
                if dirty_custom_keys and hasattr(self, '_write_apikeys_file'):
                    self._write_apikeys_file(show_feedback=False)
            except Exception:
                pass
            # persist local sort preferences if comboboxes exist
            try:
                if hasattr(self, 'local_sort_in_combo') and self.local_sort_in_combo is not None:
                    try:
                        data['local_sort_in'] = self.local_sort_in_combo.currentData()
                    except Exception:
                        data['local_sort_in'] = self.settings.get('local_sort_in') if isinstance(getattr(self, 'settings', None), dict) else None
                else:
                    data['local_sort_in'] = self.settings.get('local_sort_in') if isinstance(getattr(self, 'settings', None), dict) else None
            except Exception:
                pass
            try:
                if hasattr(self, 'local_sort_out_combo') and self.local_sort_out_combo is not None:
                    try:
                        data['local_sort_out'] = self.local_sort_out_combo.currentData()
                    except Exception:
                        data['local_sort_out'] = self.settings.get('local_sort_out') if isinstance(getattr(self, 'settings', None), dict) else None
                else:
                    data['local_sort_out'] = self.settings.get('local_sort_out') if isinstance(getattr(self, 'settings', None), dict) else None
            except Exception:
                pass
            # determine a stable page key for the current page (best-effort)
            try:
                try:
                    cur = self.pages.currentWidget() if hasattr(self, 'pages') else None
                except Exception:
                    cur = None
                pk = None
                try:
                    if cur is not None:
                        if hasattr(self, 'api_page') and cur is self.api_page:
                            pk = 'api'
                        elif hasattr(self, 'local_page') and cur is self.local_page:
                            pk = 'local'
                        elif hasattr(self, 'settings_page') and cur is self.settings_page:
                            pk = 'settings'
                        elif hasattr(self, 'runninghub_page') and cur is self.runninghub_page:
                            pk = 'runninghub'
                        else:
                            # default: treat index 0 as decode page
                            try:
                                if hasattr(self, 'pages') and self.pages.currentIndex() == 0:
                                    pk = 'decode'
                            except Exception:
                                pass
                except Exception:
                    pk = None
                if pk:
                    data['page_key'] = pk
            except Exception:
                pass

            # persist custom expand system prompt if provided in settings
            try:
                if isinstance(getattr(self, 'settings', None), dict):
                    es = self.settings.get('expand_system_prompt') or DEFAULT_EXPAND_SYSTEM_PROMPT
                    data['expand_system_prompt'] = es
                    # ensure in-memory settings also reflect a non-null default
                    self.settings['expand_system_prompt'] = es
                else:
                    data['expand_system_prompt'] = DEFAULT_EXPAND_SYSTEM_PROMPT
            except Exception:
                pass

            # persist image-reverse prompt if provided in settings
            try:
                if isinstance(getattr(self, 'settings', None), dict):
                    ir = self.settings.get('image_reverse_prompt') or DEFAULT_IMAGE_REVERSE_PROMPT
                    data['image_reverse_prompt'] = ir
                    self.settings['image_reverse_prompt'] = ir
                else:
                    data['image_reverse_prompt'] = DEFAULT_IMAGE_REVERSE_PROMPT
            except Exception:
                pass

            with open(self.settings_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


    def _apply_settings(self, settings, apply_window_geometry=True, apply_page_index=True):
        """Apply settings dict to UI widgets if available."""
        if not settings:
            return
        try:
            # input/output already applied earlier in __init__ but keep fields in sync
            self.input_dir = settings.get('input_dir', self.input_dir)
            self.output_dir = settings.get('output_dir', self.output_dir)
            self.local_decode_dir = settings.get('local_decode_dir', self.local_decode_dir)
            try:
                self.rh_local_decode_settings = settings.get('rh_local_decode_settings', {}) if isinstance(settings, dict) else {}
                if not isinstance(self.rh_local_decode_settings, dict):
                    self.rh_local_decode_settings = {}
                if isinstance(getattr(self, 'settings', None), dict):
                    self.settings['rh_local_decode_settings'] = self.rh_local_decode_settings
            except Exception:
                pass
            try:
                os.makedirs(self.local_decode_dir, exist_ok=True)
            except Exception:
                pass
            if settings.get('theme_mode') and settings.get('theme_mode') in getattr(self, '_themes', {}):
                if settings.get('theme_mode') != getattr(self, '_theme_mode', None):
                    self._apply_theme(settings.get('theme_mode'))
            if hasattr(self, 'input_label'):
                self.input_label.setText(self.input_dir)
            if hasattr(self, 'output_label'):
                self.output_label.setText(self.output_dir)
            if hasattr(self, 'local_decode_label'):
                self.local_decode_label.setText(self.local_decode_dir)
            # parameters
            if 'grid_cols' in settings and hasattr(self, 'grid_spin'):
                try:
                    v = int(settings.get('grid_cols') or 32)
                    self.grid_spin.setValue(v)
                except Exception:
                    pass
            if 'decode_mode' in settings and hasattr(self, 'preview_tabs'):
                try:
                    mode_val = settings.get('decode_mode') or 'grc'
                    if mode_val == 'sst':
                        target_idx = getattr(self, '_sst_tab_index', None)
                    else:
                        target_idx = self._grc_tab_index
                    if target_idx is not None and target_idx >= 0:
                        self.preview_tabs.setCurrentIndex(target_idx)
                except Exception:
                    pass
            if 'decode_password' in settings and hasattr(self, 'sst_pwd_edit'):
                try:
                    self.sst_pwd_edit.setText(settings.get('decode_password') or '')
                except Exception:
                    pass
            if 'show_grid' in settings and hasattr(self, 'show_grid_cb'):
                try:
                    self.show_grid_cb.setChecked(bool(settings.get('show_grid')))
                except Exception:
                    pass
            if 'overwrite' in settings and hasattr(self, 'overwrite_cb'):
                try:
                    self.overwrite_cb.setChecked(bool(settings.get('overwrite')))
                except Exception:
                    pass
            if apply_page_index:
                try:
                    target_idx = None
                    # prefer stable page_key when present
                    try:
                        pk = settings.get('page_key') if isinstance(settings, dict) else None
                    except Exception:
                        pk = None
                    try:
                        if pk:
                            if pk == 'api' and hasattr(self, 'api_page') and hasattr(self, 'pages'):
                                i = self.pages.indexOf(self.api_page)
                                if i != -1:
                                    target_idx = i
                            elif pk == 'local' and hasattr(self, 'local_page') and hasattr(self, 'pages'):
                                i = self.pages.indexOf(self.local_page)
                                if i != -1:
                                    target_idx = i
                            elif pk == 'settings' and hasattr(self, 'settings_page') and hasattr(self, 'pages'):
                                i = self.pages.indexOf(self.settings_page)
                                if i != -1:
                                    target_idx = i
                            elif pk == 'runninghub' and hasattr(self, 'runninghub_page') and hasattr(self, 'pages'):
                                i = self.pages.indexOf(self.runninghub_page)
                                if i != -1:
                                    target_idx = i
                            elif pk == 'decode' and hasattr(self, 'pages'):
                                # decode is normally at index 0
                                try:
                                    if self.pages.count() > 0:
                                        target_idx = 0
                                except Exception:
                                    pass
                    except Exception:
                        target_idx = None

                    # fallback to old page_index behavior if page_key didn't resolve
                    if target_idx is None:
                        try:
                            idx = int(settings.get('page_index', 0))
                        except Exception:
                            idx = 0
                        target_idx = idx
                        try:
                            # preserve historical special-case: if old saved idx was 2 (settings moved), remap
                            if idx == 2 and hasattr(self, 'settings_page') and hasattr(self, 'pages'):
                                settings_idx = self.pages.indexOf(self.settings_page)
                                if settings_idx != -1:
                                    target_idx = settings_idx
                        except Exception:
                            pass

                    if hasattr(self, 'pages') and isinstance(target_idx, int) and 0 <= target_idx < self.pages.count():
                        self.pages.setCurrentIndex(target_idx)
                except Exception:
                    pass

            # thumbnail size
            try:
                if 'thumb_size' in settings:
                    ts = int(settings.get('thumb_size') or 200)
                    # apply to spin and slider if present
                    if hasattr(self, 'thumb_size_spin'):
                        try:
                            self.thumb_size_spin.blockSignals(True)
                            self.thumb_size_spin.setValue(ts)
                            self.thumb_size_spin.blockSignals(False)
                        except Exception:
                            pass
                    if hasattr(self, 'thumb_size_slider'):
                        try:
                            self.thumb_size_slider.blockSignals(True)
                            self.thumb_size_slider.setValue(ts)
                            self.thumb_size_slider.blockSignals(False)
                        except Exception:
                            pass
                    try:
                        try:
                            if hasattr(self, 'thumb_size_spin') and self.thumb_size_spin.value() != ts:
                                self.thumb_size_spin.blockSignals(True)
                                self.thumb_size_spin.setValue(ts)
                                self.thumb_size_spin.blockSignals(False)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # apply iconSize to local lists and main file list so UI reflects saved size immediately
                    try:
                        if hasattr(self, 'local_list_in'):
                            self.local_list_in.setIconSize(QtCore.QSize(ts, ts))
                        if hasattr(self, 'local_list_out'):
                            self.local_list_out.setIconSize(QtCore.QSize(ts, ts))
                        # do NOT apply the local thumb size to the main left file list; keep it fixed
                    except Exception:
                        pass
                    # refresh local thumbnails to honor the restored size
                    try:
                        QtCore.QTimer.singleShot(80, lambda: self._refresh_local_list())
                    except Exception:
                        pass
            except Exception:
                pass

            # thumbnail cache cap
            try:
                cap = int(settings.get('thumb_cache_max_mb', getattr(self, 'thumb_cache_max_mb', 300)) or 300)
                self.thumb_cache_max_mb = int(max(50, min(5000, cap)))
                if hasattr(self, 'thumb_cache_spin'):
                    try:
                        if self.thumb_cache_spin.value() != self.thumb_cache_max_mb:
                            self.thumb_cache_spin.blockSignals(True)
                            self.thumb_cache_spin.setValue(self.thumb_cache_max_mb)
                            self.thumb_cache_spin.blockSignals(False)
                    except Exception:
                        pass
            except Exception:
                pass

            # API settings
            try:
                defaults = self._default_api_settings()
                incoming = settings.get('api_settings', {}) if isinstance(settings, dict) else {}
                if not isinstance(incoming, dict):
                    incoming = {}
                merged = defaults.copy()
                for k in merged:
                    try:
                        merged[k].update(incoming.get(k, {}))
                    except Exception:
                        pass
                incoming_profiles = settings.get('api_provider_profiles', {}) if isinstance(settings, dict) else {}
                if isinstance(incoming_profiles, dict):
                    self.api_provider_profiles = incoming_profiles
                else:
                    self.api_provider_profiles = {}
                self._sanitize_api_provider_profiles()
                self._ensure_api_provider_profile_store()
                self.api_settings = merged
                if hasattr(self, 'api_config_fields'):
                    for k, fields in self.api_config_fields.items():
                        cfg = merged.get(k, {}) if isinstance(merged, dict) else {}
                        try:
                            provider_val = str(cfg.get('provider', ''))
                            # detect provider values in form 'custom_<category>' and map to 'custom' for UI
                            custom_provider_key = None
                            try:
                                if isinstance(provider_val, str) and provider_val.startswith('custom_'):
                                    parts = provider_val.split('_', 1)
                                    if len(parts) == 2 and parts[1]:
                                        custom_provider_key = parts[1]
                                        provider_val = 'custom'
                            except Exception:
                                custom_provider_key = None
                            endpoint_val = str(cfg.get('endpoint', ''))
                            model_val = str(cfg.get('model', ''))
                            # provider combo
                            try:
                                matched = False
                                for i in range(fields['provider'].count()):
                                    if fields['provider'].itemData(i) == provider_val:
                                        fields['provider'].setCurrentIndex(i)
                                        matched = True
                                        break
                                if not matched:
                                    if fields['provider'].count() > 0:
                                        fields['provider'].setCurrentIndex(0)
                                        provider_val = fields['provider'].itemData(0)
                                    else:
                                        fields['provider'].addItem('自定义', 'custom')
                                        fields['provider'].setCurrentIndex(0)
                            except Exception:
                                pass

                            custom_entry = {}
                            try:
                                if provider_val == 'custom' and isinstance(getattr(self, 'api_custom_cache', None), dict):
                                    # prefer the explicit custom key if provided (custom_<category>), otherwise fall back to category k
                                    lookup_key = custom_provider_key or k
                                    custom_entry = self.api_custom_cache.get(lookup_key, {}) or {}
                            except Exception:
                                custom_entry = {}
                            if provider_val == 'custom':
                                effective_endpoint = ''
                                effective_model = ''
                                effective_api_key = ''
                                effective_timeout = 30
                                if custom_entry:
                                    effective_endpoint = custom_entry.get('endpoint', '') or ''
                                    effective_model = custom_entry.get('model', '') or ''
                                    effective_api_key = custom_entry.get('api_key', '') or ''
                                    try:
                                        effective_timeout = int(custom_entry.get('timeout', effective_timeout) or effective_timeout)
                                    except Exception:
                                        effective_timeout = effective_timeout
                                elif isinstance(cfg.get('provider'), str) and cfg.get('provider').startswith('custom'):
                                    effective_endpoint = cfg.get('endpoint', '') or ''
                                    effective_model = cfg.get('model', '') or ''
                                    effective_api_key = cfg.get('api_key', '') or ''
                                    try:
                                        effective_timeout = int(cfg.get('timeout', effective_timeout) or effective_timeout)
                                    except Exception:
                                        effective_timeout = effective_timeout
                            else:
                                effective_endpoint = endpoint_val
                                effective_model = model_val
                                effective_api_key = cfg.get('api_key', '')
                                effective_timeout = cfg.get('timeout', 30)

                            # Ollama does not use API keys: ensure api_key is empty for that provider
                            try:
                                if provider_val == 'ollama':
                                    effective_api_key = ''
                            except Exception:
                                pass

                            # update endpoint/model options based on provider
                            entry = self._find_provider_entry(k, provider_val)
                            models = []
                            if provider_val == 'custom':
                                try:
                                    fields['endpoint'].setReadOnly(False)
                                    fields['endpoint'].setText(str(effective_endpoint))
                                except Exception:
                                    pass
                            elif entry is not None:
                                try:
                                    fields['endpoint'].setText(str(effective_endpoint or entry.get('endpoint', endpoint_val)))
                                    # allow editing endpoint for ollama even though it's a known provider
                                    if provider_val != 'ollama':
                                        fields['endpoint'].setReadOnly(True)
                                    else:
                                        fields['endpoint'].setReadOnly(False)
                                except Exception:
                                    pass
                                try:
                                    models = entry.get('models', []) or []
                                except Exception:
                                    models = []
                            else:
                                try:
                                    fields['endpoint'].setReadOnly(False)
                                    fields['endpoint'].setText(endpoint_val)
                                except Exception:
                                    pass
                            if fields.get('model') is not None:
                                try:
                                    fields['model'].blockSignals(True)
                                    fields['model'].clear()
                                    for m in models:
                                        fields['model'].addItem(str(m))
                                    if provider_val == 'custom':
                                        fields['model'].setEditText(str(effective_model or ''))
                                    elif model_val:
                                        fields['model'].setEditText(model_val)
                                    elif models:
                                        fields['model'].setCurrentIndex(0)
                                except Exception:
                                    pass
                                finally:
                                    try:
                                        fields['model'].blockSignals(False)
                                    except Exception:
                                        pass

                            try:
                                # ensure no API key shown for ollama
                                if provider_val == 'ollama':
                                    fields['api_key'].setText('')
                                    try:
                                        fields['api_key'].setReadOnly(True)
                                    except Exception:
                                        pass
                                else:
                                    fields['api_key'].setText(str(effective_api_key))
                            except Exception:
                                pass
                            try:
                                fields['timeout'].setValue(int(effective_timeout or 30))
                            except Exception:
                                pass
                            # ensure provider profile map has at least the active entry populated
                            profile_payload = {
                                'endpoint': str(fields['endpoint'].text()),
                                'api_key': str(fields['api_key'].text()),
                                'model': str(fields['model'].currentText()) if fields.get('model') is not None else '',
                                'timeout': int(fields['timeout'].value()) if fields.get('timeout') else 30,
                            }
                            if provider_val == 'baidu_translate':
                                try:
                                    profile_payload['appid'] = fields['baidu_appid'].text().strip()
                                except Exception:
                                    pass
                                try:
                                    profile_payload['secret'] = fields['baidu_secret'].text().strip()
                                except Exception:
                                    pass
                            existing_profile = self._get_api_provider_profile(k, provider_val)
                            if not existing_profile:
                                self._set_api_provider_profile(k, provider_val, profile_payload)
                        except Exception:
                            pass
            except Exception:
                pass

            # restore local sort combobox selections if present in settings
            try:
                if 'local_sort_in' in settings and hasattr(self, 'local_sort_in_combo') and self.local_sort_in_combo is not None:
                    try:
                        want = settings.get('local_sort_in')
                        for i in range(self.local_sort_in_combo.count()):
                            try:
                                if self.local_sort_in_combo.itemData(i) == want:
                                    self.local_sort_in_combo.setCurrentIndex(i)
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass

            # restore local mode (input/output/both) for local files page
            try:
                if 'local_mode' in settings and hasattr(self, 'local_mode_group') and self.local_mode_group is not None:
                    want_id = int(settings.get('local_mode')) if settings.get('local_mode') is not None else 0
                    btn = self.local_mode_group.button(want_id)
                    if btn is not None:
                        btn.setChecked(True)
                        try:
                            QtCore.QTimer.singleShot(50, lambda: self._refresh_local_list())
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                if 'local_sort_out' in settings and hasattr(self, 'local_sort_out_combo') and self.local_sort_out_combo is not None:
                    try:
                        want = settings.get('local_sort_out')
                        for i in range(self.local_sort_out_combo.count()):
                            try:
                                if self.local_sort_out_combo.itemData(i) == want:
                                    self.local_sort_out_combo.setCurrentIndex(i)
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass

            # window geometry / maximized (optional to avoid jumping when reapplying settings)
            if apply_window_geometry:
                try:
                    win = settings.get('window', {}) or {}
                    if win.get('maximized'):
                        QtCore.QTimer.singleShot(50, lambda: self.showMaximized())
                    else:
                        w = int(win.get('width') or self.width())
                        h = int(win.get('height') or self.height())
                        # If x/y are present in settings use them, otherwise center the window
                        try:
                            has_x = ('x' in win and win.get('x') is not None)
                            has_y = ('y' in win and win.get('y') is not None)
                            if has_x and has_y:
                                x = int(win.get('x'))
                                y = int(win.get('y'))
                                self.resize(w, h)
                                self.move(x, y)
                            else:
                                # center on primary screen's available geometry
                                try:
                                    screen = QtWidgets.QApplication.primaryScreen()
                                    if screen is not None:
                                        geo = screen.availableGeometry()
                                        cx = geo.x() + max(0, (geo.width() - w) // 2)
                                        cy = geo.y() + max(0, (geo.height() - h) // 2)
                                        self.resize(w, h)
                                        self.move(cx, cy)
                                    else:
                                        self.resize(w, h)
                                except Exception:
                                    try:
                                        self.resize(w, h)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                except Exception:
                    pass

            # ensure module grid settings follow UI
            try:
                grc.grid_cols = int(self.grid_spin.value())
                grc.grid_rows = int(grc.grid_cols) + 2
            except Exception:
                pass
        except Exception:
            pass
