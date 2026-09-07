"""Local browsing, filtering, previews, and thumbnail coordination."""
from aetherloom_core.tasks.media import FileInfoJob
from aetherloom_core.resources import IMAGE_EXTS
from PIL import Image
from aetherloom_core.resources import LOW_RES_THUMB
from PyQt5.QtCore import Qt
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.paths import current_dir, SOURCE_ROOT
from aetherloom_core.tasks.media import ThumbnailJob
from aetherloom_core.thumbnail_resources import ThumbnailScheduler
from aetherloom_core.resources import VIDEO_EXTS
from aetherloom_core.platform_utils import _move_to_trash
from aetherloom_core.thumbnail_resources import cancel_list_requests
import cv2
from datetime import datetime, timedelta
from aetherloom_core.thumbnail_resources import ensure_caches
import json
import os
from functools import partial
from aetherloom_core.thumbnail_resources import set_item_icon
import shutil
import subprocess
import sys


class LocalBrowserMixin:
    def _local_browser_folder(self, kind):
        from aetherloom_core.local_browser_ui import current_folder
        return current_folder(self, kind)


    def _navigate_local_folder(self, kind, path):
        from aetherloom_core.local_browser_ui import navigate_folder
        return navigate_folder(self, kind, path)


    def _restore_local_browser_position(self, list_widget):
        from aetherloom_core.local_browser_ui import restore_location
        restore_location(self, list_widget)


    def _refresh_local_list(self):
        """Capture settings on the GUI thread and scan both folders in background."""
        from aetherloom_core.local_media import LocalScanController
        if getattr(self, '_closing', False):
            return
        mode_id = self.local_mode_group.checkedId()
        mode = 'input' if mode_id == 0 else ('output' if mode_id == 1 else 'both')
        request = dict(mode=mode, folders={}, sorts={}, extensions=IMAGE_EXTS + VIDEO_EXTS, include_directories=True)
        for kind, folder, combo in (
            ('input', self._local_browser_folder('input'), self.local_sort_in_combo),
            ('output', self._local_browser_folder('output'), self.local_sort_out_combo),
        ):
            request['folders'][kind] = os.path.abspath(folder)
            request['sorts'][kind] = combo.currentData() or self.settings.get(
                'local_sort_' + ('in' if kind == 'input' else 'out'), 'name_asc')
        controller = getattr(self, '_local_scan_controller', None)
        if controller is None:
            parent = self if isinstance(self, QtCore.QObject) else None
            controller = LocalScanController(self._apply_local_scan_result, parent)
            self._local_scan_controller = controller
        self._local_scan_loading = True
        self._local_scan_generation = controller.submit(request)
        if hasattr(self, '_update_local_browser_state'):
            self._update_local_browser_state()


    def _apply_local_scan_result(self, generation, result):
        """Accept only the current scan; preserve unchanged items and selection."""
        if getattr(self, '_closing', False) or generation != self._local_scan_generation:
            return
        self._local_scan_loading = False
        self._local_scan_errors = result['errors']
        for kind, error in result['errors'].items():
            self.log(f"无法读取{result['request']['folders'].get(kind, '')}: {error}")
        scanned = result['records']
        mode = result['request']['mode']
        for kind, folder, lw in (
            ('input', result['request']['folders']['input'], self.local_list_in),
            ('output', result['request']['folders']['output'], self.local_list_out),
        ):
            lw.setVisible(mode in (kind, 'both'))
            if mode not in (kind, 'both'):
                continue
            records = scanned[kind]
            snapshot = result['snapshots'].get(kind)
            attribute = '_local_snapshot_' + kind
            if snapshot is not None and getattr(self, attribute, None) == snapshot:
                self._filter_local_items(lw)
                self._enqueue_visible_thumbnails(lw)
                continue
            selected = {(item.data(QtCore.Qt.UserRole) or {}).get('path') for item in lw.selectedItems()}
            from aetherloom_core.local_browser_ui import prepare_location
            selected = prepare_location(self, kind, folder, lw, selected)
            key = str(id(lw))
            # Removing the exact old state invalidates already queued timer callbacks.
            self._local_pending_add.pop(key, None)
            cancel_list_requests(self, lw)
            if hasattr(self, '_thumb_residency'):
                self._thumb_residency.release_list(lw)
            selection_blocker = QtCore.QSignalBlocker(lw)
            lw.clear()
            selection_blocker.unblock()
            if not hasattr(self, '_local_item_lookup'):
                self._local_item_lookup = {}
            self._local_item_lookup[id(lw)] = {}
            lw.setProperty('filteredVisibleCount', 0)
            if hasattr(self, '_local_visible_rows'):
                self._local_visible_rows.pop(id(lw), None)
            setattr(self, attribute, snapshot)

            def make_item(record, widget=lw, selected_paths=selected):
                meta = dict(record)
                meta['file_type'] = 'DIR' if meta.get('is_dir') else self._guess_file_type(meta['path'])
                width = widget.iconSize().width()
                revision = (meta['mtime_ns'], meta['size_bytes'])
                if not meta.get('is_dir'):
                    meta['low_thumb_key'] = self._get_thumb_key(meta['path'], LOW_RES_THUMB, revision)
                    meta['thumb_key'] = self._get_thumb_key(meta['path'], width, revision)
                meta['thumb_size'] = width
                item = QtWidgets.QListWidgetItem(meta['name'])
                if meta.get('is_dir'):
                    if not hasattr(self, '_local_folder_icon'):
                        self._local_folder_icon = QtWidgets.QApplication.style().standardIcon(QtWidgets.QStyle.SP_DirIcon)
                    item.setIcon(self._local_folder_icon)
                item.setData(QtCore.Qt.UserRole, meta)
                item.setToolTip(meta['path'])
                item.setSizeHint(QtCore.QSize(width + 12, width + 56))
                self._local_item_lookup[id(widget)][meta['path']] = item
                return item

            if records:
                self._start_chunked_population(lw, records, item_factory=make_item, selected_paths=selected)
            else:
                self._filter_local_items(lw)
                self._restore_local_browser_position(lw)
        self.log(f"本地视图: 输入 {len(scanned['input'])} 项，输出 {len(scanned['output'])} 项")
        if hasattr(self, '_update_local_browser_state'):
            self._update_local_browser_state()


    def _start_image_reverse(self, paths):
        try:
            # collect vision API configuration
            try:
                api_conf = (getattr(self, 'api_settings', None) or {}).get('vision') or (self.settings.get('api_settings') or {}).get('vision') or {}
            except Exception:
                api_conf = {}
            api_url = api_conf.get('endpoint') or api_conf.get('api_url') or api_conf.get('url') or ''
            model = api_conf.get('model') or api_conf.get('models') or ''
            provider = api_conf.get('provider')
            timeout = int(api_conf.get('timeout') or 60)

            # Resolve this provider only; never send another vendor's fallback key.
            from aetherloom_core.api_credentials import get_credentials
            api_key = get_credentials(getattr(self, '_apikeys', {}), provider, 'vision').get('api_key', '')
            if not api_key:
                try:
                    key_path = getattr(self, '_apikeys_file', None) or os.path.join(current_dir, 'apikeys.json')
                    if os.path.isfile(key_path):
                        with open(key_path, 'r', encoding='utf-8') as key_file:
                            api_key = get_credentials(json.load(key_file), provider, 'vision').get('api_key', '')
                except (OSError, ValueError, TypeError):
                    pass

            if not api_url:
                try:
                    self.log('未配置 vision API endpoint，无法调用图像反推')
                except Exception:
                    pass
                return
            if not api_key and provider != 'ollama':
                try:
                    self.log('未找到 vision API key，无法调用图像反推')
                except Exception:
                    pass
                return

            out_dir = getattr(self, 'output_dir', '.') or '.'
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception:
                pass

            class _ImageReverseSignals(QtCore.QObject):
                finished = QtCore.pyqtSignal(object)

            class _ImageReverseWorker(QtCore.QRunnable):
                def __init__(self, paths, api_url, api_key, model, timeout, out_dir, user_text=None, provider=None):
                    super().__init__()
                    self.paths = paths
                    self.api_url = api_url
                    self.provider = provider
                    self.api_key = api_key
                    self.model = model
                    self.timeout = timeout
                    self.user_text = user_text
                    self.out_dir = out_dir
                    self.signals = _ImageReverseSignals()

                def run(self):
                    results = []
                    try:
                        from api_calls.call_vision import call_vision
                    except Exception as e:
                        results.append({'ok': False, 'error': str(e)})
                        try:
                            self.signals.finished.emit(results)
                        except Exception:
                            pass
                        return

                    for p in (self.paths or []):
                        if not p or not os.path.exists(p):
                            results.append({'path': p, 'ok': False, 'error': 'file missing'})
                            continue
                        try:
                            prompt = None
                            try:
                                prompt = self.user_text
                            except Exception:
                                prompt = None
                            # fallback default
                            if not prompt:
                                prompt = '描述该图像'
                            resp_text = call_vision(self.api_url, self.api_key or '', self.model or '', p, prompt, timeout=self.timeout, provider=self.provider)
                        except Exception as e:
                            results.append({'path': p, 'ok': False, 'error': str(e)})
                            continue
                        outfn = os.path.join(self.out_dir, os.path.splitext(os.path.basename(p))[0] + '.txt')
                        try:
                            with open(outfn, 'w', encoding='utf-8') as _f:
                                _f.write(str(resp_text))
                            results.append({'path': p, 'ok': True, 'outfn': outfn})
                        except Exception as e:
                            results.append({'path': p, 'ok': False, 'error': f'save failed: {e}'})

                    try:
                        self.signals.finished.emit(results)
                    except Exception:
                        pass

            try:
                # start one worker per path to allow parallel processing
                total = len(paths or [])
                if total <= 0:
                    try:
                        self.log('没有选中文件用于图像反推')
                    except Exception:
                        pass
                    return
                pending = {'count': total}
                try:
                    self._show_toast(f'开始图像反推: {total} 项', timeout=3000)
                except Exception:
                    pass

                try:
                    pool = getattr(self, '_thumb_pool', None) or QtCore.QThreadPool.globalInstance()
                except Exception:
                    pool = QtCore.QThreadPool.globalInstance()

                def _make_on_done(p):
                    def _on_done_single(res_list):
                        try:
                            # res_list is a list of results for this job (usually single item)
                            for r in (res_list or []):
                                try:
                                    if r.get('ok'):
                                        outfn = r.get('outfn')
                                        try:
                                            self.log(f"图像反推结果保存到: {outfn}")
                                        except Exception:
                                            pass
                                        try:
                                            txt = ''
                                            try:
                                                with open(outfn, 'r', encoding='utf-8') as _f:
                                                    txt = _f.read() or ''
                                            except Exception:
                                                txt = ''
                                            try:
                                                QtWidgets.QApplication.clipboard().setText(str(txt))
                                                self._show_toast(f'图像反推已保存: {os.path.basename(outfn)}；已复制到剪贴板', timeout=4000)
                                            except Exception:
                                                try:
                                                    self._show_toast(f'图像反推已保存: {os.path.basename(outfn)}；已复制到剪贴板', timeout=4000)
                                                except Exception:
                                                    pass
                                        except Exception:
                                            pass
                                    else:
                                        try:
                                            self.log(f"图像反推失败: {r.get('path')} -> {r.get('error')}")
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            pending['count'] -= 1
                            if pending['count'] <= 0:
                                try:
                                    self._show_toast(f'图像反推完成: {os.path.basename(outfn)}；已复制到剪贴板', timeout=3000)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    return _on_done_single

                # determine prompt to use (runtime attr -> persisted settings -> default)
                try:
                    user_text_cfg = None
                    try:
                        user_text_cfg = getattr(self, 'image_reverse_prompt', None)
                    except Exception:
                        user_text_cfg = None
                    if not user_text_cfg and isinstance(getattr(self, 'settings', None), dict):
                        try:
                            user_text_cfg = self.settings.get('image_reverse_prompt')
                        except Exception:
                            user_text_cfg = None
                    if not user_text_cfg:
                        user_text_cfg = '描述该图像'
                except Exception:
                    user_text_cfg = '描述该图像'

                for p in (paths or []):
                    try:
                        job = _ImageReverseWorker([p], api_url, api_key, model, timeout, out_dir, user_text=user_text_cfg, provider=provider)
                        try:
                            job.signals.finished.connect(_make_on_done(p))
                        except Exception:
                            pass
                        try:
                            pool.start(job)
                        except Exception:
                            # fallback synchronous
                            job.run()
                    except Exception as e:
                        try:
                            self.log(f'图像反推提交失败: {e}')
                        except Exception:
                            pass
            except Exception as e:
                try:
                    self.log(f'图像反推提交失败: {e}')
                except Exception:
                    pass
        except Exception as e:
            try:
                self.log(f'图像反推失败: {e}')
            except Exception:
                pass


    def on_local_context_menu(self, pos, list_widget=None):
        try:
            lw = list_widget if list_widget is not None else getattr(self, 'local_list_in', None)
            if lw is None:
                return
            # support multi-selection: if there are selected items use them, otherwise use the item under cursor
            sel_items = lw.selectedItems()
            if sel_items:
                items = sel_items
            else:
                it = lw.itemAt(pos)
                if it is None:
                    return
                items = [it]

            metas = [it.data(QtCore.Qt.UserRole) or {} for it in items]
            directories = [m['path'] for m in metas if m.get('is_dir') and m.get('path')]
            paths = []
            for m in metas:
                p = m.get('path')
                if p and not m.get('is_dir') and os.path.isfile(p):
                    paths.append(p)

            menu = QtWidgets.QMenu(self)
            act_enter = menu.addAction('进入文件夹') if len(directories) == 1 else None
            act_open = menu.addAction('在默认应用中打开')
            act_open_folder = menu.addAction('在本地文件夹中打开')
            act_copy = menu.addAction('复制到剪贴板')
            act_save = menu.addAction('另存为')
            act_compare = menu.addAction('加入比较')
            act_image_reverse = menu.addAction('图像反推')
            act_enqueue = menu.addAction('加入本地解码队列')
            menu.addSeparator()
            # unified delete label
            act_del = menu.addAction('删除')
            for action in (act_open, act_copy, act_save, act_compare, act_image_reverse, act_enqueue, act_del):
                action.setEnabled(bool(paths))
            act = menu.exec_(lw.mapToGlobal(pos))

            if act is None:
                return
            # A refresh can happen while this menu is open; only existing files
            # remain eligible for operations that decode, copy or delete files.
            paths = [path for path in paths if os.path.isfile(path)]
            if act is act_enter:
                kind = 'input' if lw is self.local_list_in else 'output'
                self._navigate_local_folder(kind, directories[0])
            elif act == act_open:
                for path in paths:
                    try:
                        if sys.platform.startswith('win'):
                            os.startfile(path)
                        elif sys.platform == 'darwin':
                            subprocess.Popen(['open', path])
                        else:
                            subprocess.Popen(['xdg-open', path])
                    except Exception as e:
                        self.log(f'打开文件失败: {e}')
            elif act == act_open_folder:
                try:
                    for folder in directories:
                        self._open_folder_path(folder)
                    # open the folder containing each selected file (reveal if possible)
                    for path in paths:
                        try:
                            try:
                                try:
                                    folder = os.path.dirname(path)
                                except Exception:
                                    folder = path
                                self._reveal_in_explorer(folder)
                            except Exception:
                                pass
                        except Exception:
                            pass
                except Exception as e:
                    self.log(f'打开所在目录失败: {e}')
            elif act == act_copy:
                try:
                    md = QtCore.QMimeData()
                    # if single selected and a thumbnail exists, copy image data
                    if len(paths) == 1:
                        try:
                            thumb = self._make_thumbnail(paths[0], size=(self.thumb_size_spin.value() if hasattr(self, 'thumb_size_spin') else 200, self.thumb_size_spin.value() if hasattr(self, 'thumb_size_spin') else 200))
                            if thumb:
                                md.setImageData(thumb.toImage())
                                ba = QtCore.QByteArray()
                                buf = QtCore.QBuffer(ba)
                                buf.open(QtCore.QIODevice.WriteOnly)
                                thumb.save(buf, 'PNG')
                                buf.close()
                                md.setData('image/png', ba)
                        except Exception:
                            pass
                    # always include file URLs so Explorer paste works for multi-selection
                    try:
                        existing = [p for p in paths if os.path.exists(p)]
                        if existing:
                            md.setUrls([QtCore.QUrl.fromLocalFile(os.path.abspath(p)) for p in existing])
                    except Exception:
                        pass
                    QtWidgets.QApplication.clipboard().setMimeData(md)
                    self.log('已将文件复制到剪贴板')
                except Exception as e:
                    self.log(f'复制失败: {e}')
            elif act == act_save:
                try:
                    if len(paths) == 1:
                        src = paths[0]
                        suggested = os.path.join(self.output_dir, os.path.basename(src))
                        dst, _ = QtWidgets.QFileDialog.getSaveFileName(self, '另存为', suggested)
                        if dst:
                            shutil.copy2(src, dst)
                            self.log(f'已另存为: {dst}')
                    else:
                        d = QtWidgets.QFileDialog.getExistingDirectory(self, '选择目标文件夹')
                        if not d:
                            return
                        copied = 0
                        for src in paths:
                            try:
                                if os.path.exists(src):
                                    dst = os.path.join(d, os.path.basename(src))
                                    shutil.copy2(src, dst)
                                    copied += 1
                            except Exception as e:
                                self.log(f'另存 {os.path.basename(src)} 失败: {e}')
                        self.log(f'已另存 {copied} 个文件到 {d}')
                except Exception as e:
                    self.log(f'另存为失败: {e}')
            elif act == act_compare:
                self._add_to_compare(paths)
            elif act == act_image_reverse:
                try:
                    self._start_image_reverse(paths)
                except Exception as e:
                    try:
                        self.log(f'图像反推提交失败: {e}')
                    except Exception:
                        pass
            elif act == act_enqueue:
                try:
                    # ensure decode directory exists
                    decode_dir = getattr(self, 'local_decode_dir', None)
                    if not decode_dir:
                        current_dir = SOURCE_ROOT
                        decode_dir = os.path.join(current_dir, 'decoding')
                    os.makedirs(decode_dir, exist_ok=True)
                    copied = 0
                    errors = []
                    for src in paths:
                        try:
                            if not os.path.exists(src):
                                errors.append((src, '文件不存在'))
                                continue
                            base = os.path.basename(src)
                            dst = os.path.join(decode_dir, base)
                            # avoid overwrite by adding numeric suffix when needed
                            if os.path.exists(dst):
                                name, ext = os.path.splitext(base)
                                i = 1
                                while True:
                                    candidate = os.path.join(decode_dir, f"{name}_{i}{ext}")
                                    if not os.path.exists(candidate):
                                        dst = candidate
                                        break
                                    i += 1
                            shutil.copy2(src, dst)
                            copied += 1
                        except Exception as e:
                            errors.append((src, str(e)))
                    if copied:
                        self.log(f'已加入本地解码队列: {copied} 个文件 -> {decode_dir}')
                    for p, err in errors:
                        self.log(f'加入解码队列失败: {p} 错误: {err}')
                except Exception as e:
                    self.log(f'加入本地解码队列失败: {e}')
            elif act == act_del:
                try:
                    removed, errors = _move_to_trash(paths)
                    if removed:
                        self.log(f'已删除 {removed} 个文件 (已移入回收站)')
                        QtCore.QTimer.singleShot(50, lambda: self._refresh_local_list())
                    for p, err in errors:
                        self.log(f'删除失败: {p} 错误: {err}')
                except Exception as e:
                    self.log(f'删除失败: {e}')
        except Exception as e:
            self.log(f'本地右键菜单失败: {e}')


    def _preview_local_selection(self):
        """Open a non-modal preview confined to the selected local browser list."""
        from aetherloom_core.local_preview import LocalPreviewDialog
        lists = [lw for lw in (getattr(self, 'local_list_in', None),
                               getattr(self, 'local_list_out', None)) if lw is not None and lw.isVisible()]
        focus = QtWidgets.QApplication.focusWidget()
        focused = next((lw for lw in lists if focus is lw or (focus is not None and lw.isAncestorOf(focus))), None)
        ordered = []
        for candidate in (focused, getattr(self, '_local_active_list', None), *lists):
            if candidate in lists and candidate not in ordered:
                ordered.append(candidate)
        selected_list = None
        selected = []
        for candidate in ordered:
            candidates = [item for item in candidate.selectedItems() if not item.isHidden()]
            selected = [item for item in candidates if not (item.data(QtCore.Qt.UserRole) or {}).get('is_dir')]
            if selected:
                selected_list = candidate
                break
            if candidates:
                self._show_toast('双击文件夹进入，选择媒体文件后可快速预览', 2500)
                return
        if selected_list is None:
            self._show_toast('请先选择一个文件进行预览', 2500)
            return
        self._local_active_list = selected_list

        def visible_paths(lw=selected_list):
            if not lw.isVisible():
                return []
            return [(lw.item(row).data(QtCore.Qt.UserRole) or {}).get('path')
                    for row in range(lw.count()) if not lw.item(row).isHidden()
                    and not (lw.item(row).data(QtCore.Qt.UserRole) or {}).get('is_dir')]

        current = selected_list.currentItem()
        if current is None or current not in selected:
            current = selected[0]
        path = (current.data(QtCore.Qt.UserRole) or {}).get('path')

        def open_path(path):
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.UserRole, {'path': path})
            self._open_path_item(item)

        dialog = getattr(self, '_local_preview_window', None)
        if dialog is None:
            dialog = LocalPreviewDialog(self, opener=open_path, pool=getattr(self, '_thumb_pool', None),
                                        mode=getattr(self, '_theme_mode', 'dark'))
            dialog.resize(max(540, min(980, self.width() - 80)), max(400, min(700, self.height() - 80)))
            self._local_preview_window = dialog
        dialog.apply_theme(getattr(self, '_theme_mode', 'dark'))
        dialog.set_items(visible_paths(), path, provider=visible_paths)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()


    def _open_path_item(self, item):
        try:
            meta = item.data(QtCore.Qt.UserRole) or {}
            path = meta.get('path')
            if meta.get('is_dir'):
                widget = item.listWidget()
                if widget in (getattr(self, 'local_list_in', None), getattr(self, 'local_list_out', None)):
                    kind = 'input' if widget is self.local_list_in else 'output'
                    self._navigate_local_folder(kind, path)
                return
            if not path or not os.path.exists(path):
                self.log('文件不存在')
                return
            if sys.platform.startswith('win'):
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            self.log(f'打开失败: {e}')


    def _on_local_thumb_slider_changed(self, val=None):
        """Handler for thumbnail size slider: update icon sizes and item size hints.
        Can be called with a value (from slider) or without (from debounce timer).
        """
        try:
            s = None
            try:
                s = int(val) if val is not None else int(getattr(self, 'thumb_size_slider', None).value())
            except Exception:
                try:
                    s = int(getattr(self, 'thumb_size_slider', None).value())
                except Exception:
                    s = 200

            # sync spinbox
            try:
                if hasattr(self, 'thumb_size_spin') and self.thumb_size_spin.value() != s:
                    self.thumb_size_spin.blockSignals(True)
                    self.thumb_size_spin.setValue(s)
                    self.thumb_size_spin.blockSignals(False)
            except Exception:
                pass

            # Keep existing icons while resizing. Only the visible entries need
            # new cache keys; cell hints are adjusted in cancellable GUI slices.
            import time
            generation = getattr(self, '_local_thumb_resize_generation', 0) + 1
            self._local_thumb_resize_generation = generation
            for lw in (getattr(self, 'local_list_in', None), getattr(self, 'local_list_out', None)):
                try:
                    if lw is None:
                        continue
                    lw.setIconSize(QtCore.QSize(s, s))
                    gap = max(0, lw.spacing())
                    lw.setGridSize(QtCore.QSize(s + 12 + gap, s + 56 + gap))
                    def make_resize_tick(widget, size, version):
                        cursor = [0]
                        def resize_tick():
                            if getattr(self, '_closing', False) or version != self._local_thumb_resize_generation:
                                return
                            deadline = time.perf_counter() + 0.008
                            try:
                                end = min(widget.count(), cursor[0] + 100)
                                widget.setUpdatesEnabled(False)
                                while cursor[0] < end:
                                    widget.item(cursor[0]).setSizeHint(QtCore.QSize(size + 12, size + 56))
                                    cursor[0] += 1
                                    if time.perf_counter() >= deadline:
                                        break
                                widget.setUpdatesEnabled(True)
                                if cursor[0] < widget.count():
                                    QtCore.QTimer.singleShot(10, resize_tick)
                            except RuntimeError:
                                return
                        return resize_tick
                    QtCore.QTimer.singleShot(0, make_resize_tick(lw, s, generation))
                except Exception:
                    pass

            # user finished interacting with slider — end interaction (enqueue high-res)
            try:
                try:
                    self._end_thumb_interaction()
                except Exception:
                    self._in_thumb_interaction = False
            except Exception:
                pass

        except Exception as e:
            try:
                self.log(f'调整缩略图大小失败: {e}')
            except Exception:
                pass

        # persist thumb size change
        try:
            self._save_settings()
        except Exception:
            pass


    def eventFilter(self, obj, event):
        """Catch wheel events on `self.pages` when local page is active and adjust thumbnail slider.
        Ignore events targeted at list widgets to preserve native scrolling.
        """
        try:
            # When viewports paint/resize/show, ensure visible thumbnails are enqueued
            # swallow wheel events on spinboxes to avoid accidental value changes
            try:
                if isinstance(obj, (QtWidgets.QDoubleSpinBox, QtWidgets.QSpinBox)) and event.type() == QtCore.QEvent.Wheel:
                    return True
            except Exception:
                pass

            try:
                if event.type() in (QtCore.QEvent.Paint, QtCore.QEvent.Resize, QtCore.QEvent.Show, QtCore.QEvent.UpdateRequest):
                    # if this event is coming from a QListWidget viewport, schedule enqueue
                    try:
                        w = obj
                        # walk up to find parent QListWidget
                        lw = None
                        p = getattr(w, 'parentWidget', None)
                        if p is not None:
                            parent = w.parentWidget()
                            while parent is not None:
                                if isinstance(parent, QtWidgets.QListWidget):
                                    lw = parent
                                    break
                                parent = parent.parentWidget()
                        if lw is not None:
                            # debounce briefly to allow layout to settle
                            try:
                                from aetherloom_core.thumbnail_resources import schedule_view
                                schedule_view(self, lw)
                            except Exception:
                                try:
                                    self._enqueue_visible_thumbnails(lw)
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass

            if event.type() == QtCore.QEvent.Wheel and getattr(self, 'pages', None) is not None:
                try:
                    if self.pages.currentWidget() is not getattr(self, 'local_page', None):
                        return super().eventFilter(obj, event)
                except Exception:
                    pass

                modifiers = getattr(event, 'modifiers', lambda: QtCore.Qt.NoModifier)()
                try:
                    delta = event.angleDelta().y()
                except Exception:
                    delta = 0
                steps = int(delta / 120) if delta else 0

                if (modifiers & QtCore.Qt.ControlModifier) and steps != 0 and hasattr(self, 'thumb_size_slider'):
                    try:
                        cur = self.thumb_size_slider.value()
                        proportional = max(4, int(cur * 0.15))
                        new = max(self.thumb_size_slider.minimum(), min(self.thumb_size_slider.maximum(), cur + steps * proportional))
                        self.thumb_size_slider.setValue(new)
                        try:
                            if hasattr(self, 'thumb_size_spin') and self.thumb_size_spin.value() != new:
                                self.thumb_size_spin.blockSignals(True)
                                self.thumb_size_spin.setValue(new)
                                self.thumb_size_spin.blockSignals(False)
                        except Exception:
                            pass
                        try:
                            self._thumb_slider_timer.start()
                        except Exception:
                            pass
                        return True
                    except Exception:
                        pass

                lw = None
                if isinstance(obj, QtWidgets.QListWidget):
                    lw = obj
                else:
                    try:
                        parent = obj.parentWidget()
                        while parent is not None:
                            if isinstance(parent, QtWidgets.QListWidget):
                                lw = parent
                                break
                            parent = parent.parentWidget()
                    except Exception:
                        lw = None

                if lw is not None and steps != 0:
                    try:
                        sb = lw.verticalScrollBar()
                        if sb is not None:
                            icon_h = max(1, lw.iconSize().height())
                            spacing = max(2, lw.spacing())
                            row_span = max(1, icon_h + spacing + 32)
                            px = steps * row_span
                            sb.setValue(sb.value() - px)
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
        return super().eventFilter(obj, event)


    def _on_list_scrolled(self, list_widget, *_args, **_kwargs):
        """Called when a list scrolls or its viewport changes; enqueue thumbnails for visible items."""
        try:
            if list_widget is None:
                return
            key = str(id(list_widget))
            t = self._scroll_enqueue_timers.get(key)
            if t is None:
                t = QtCore.QTimer(self)
                t.setSingleShot(True)
                t.setInterval(80)
                t.timeout.connect(lambda lw=list_widget: self._enqueue_visible_thumbnails(lw, margin=2))
                self._scroll_enqueue_timers[key] = t
            # restart debounce timer; enqueue will run after user pauses scrolling briefly
            try:
                # mark scrolling active and restart a global idle timer
                try:
                    self._in_scroll_interaction = True
                    try:
                        self._scroll_idle_timer.start()
                    except Exception:
                        pass
                except Exception:
                    pass
                t.start()
            except Exception:
                try:
                    self._enqueue_visible_thumbnails(list_widget, margin=2)
                except Exception:
                    pass
        except Exception:
            pass


    def _start_thumb_interaction(self):
        """Begin an interaction period: show low-res placeholders for visible items and debounce end."""
        try:
            self._in_thumb_interaction = True
            # restart debounce timer
            try:
                if getattr(self, '_thumb_interaction_timer', None) is not None:
                    self._thumb_interaction_timer.start()
            except Exception:
                pass
            # apply low-res placeholders to visible lists
            try:
                for lw in (getattr(self, 'file_list', None), getattr(self, 'local_list_in', None), getattr(self, 'local_list_out', None)):
                    try:
                        if lw is None:
                            continue
                        self._apply_low_res_to_visible(lw)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass


    def _end_thumb_interaction(self):
        """Called after interaction debounce: allow high-res thumbnails to be enqueued."""
        try:
            self._in_thumb_interaction = False
            # enqueue visible high-res thumbnails now
            try:
                if hasattr(self, 'local_list_in'):
                    self._enqueue_visible_thumbnails(self.local_list_in)
                if hasattr(self, 'local_list_out'):
                    self._enqueue_visible_thumbnails(self.local_list_out)
                if hasattr(self, 'file_list'):
                    self._enqueue_visible_thumbnails(self.file_list)
            except Exception:
                pass
        except Exception:
            pass


    def _apply_low_res_to_visible(self, list_widget, margin=2):
        """Reuse low-resolution pixels without allocating full-size placeholders."""
        from aetherloom_core.thumbnail_resources import visible_rows
        for row in visible_rows(self, list_widget, margin):
            item = list_widget.item(row)
            meta = item.data(QtCore.Qt.UserRole) or {}
            if meta.get('is_dir'):
                continue
            key = meta.get('low_thumb_key')
            icon = self._memcache_get(key)
            if icon is None and key and getattr(self, '_thumb_cache_dir', None):
                path = os.path.join(self._thumb_cache_dir, key + '.png')
                if os.path.exists(path):
                    pixmap = QtGui.QPixmap(path)
                    if not pixmap.isNull():
                        icon = QtGui.QIcon(pixmap)
                        self._memcache_put(key, icon)
            if icon is not None:
                set_item_icon(self, item, icon)
        if list_widget is not None:
            list_widget.viewport().update()


    def _enqueue_visible_thumbnails(self, list_widget, margin=2):
        if list_widget is None or getattr(self, '_closing', False):
            return
        from aetherloom_core.thumbnail_resources import visible_rows
        requests, keys = [], set()
        size = list_widget.iconSize()
        for row in visible_rows(self, list_widget, margin):
            item = list_widget.item(row)
            if item is None or item.isHidden():
                continue
            meta = item.data(QtCore.Qt.UserRole) or {}
            path = meta.get('path')
            if not path or meta.get('is_dir'):
                continue
            if 'mtime_ns' in meta and meta.get('thumb_size') != size.width():
                revision = (meta['mtime_ns'], meta['size_bytes'])
                meta['thumb_key'] = self._get_thumb_key(path, size.width(), revision)
                meta['thumb_size'] = size.width()
                item.setData(QtCore.Qt.UserRole, meta)
            low = meta.get('low_thumb_key') or self._get_thumb_key(path, LOW_RES_THUMB)
            if low:
                requests.append((path, (LOW_RES_THUMB, LOW_RES_THUMB), low))
                keys.add(low)
            if not getattr(self, '_in_thumb_interaction', False):
                high = meta.get('thumb_key') or self._get_thumb_key(path, size.width())
                if high:
                    requests.append((path, (size.width(), size.height()), high))
                    keys.add(high)
        cancel_list_requests(self, list_widget, keys)
        for path, bounds, key in requests:
            self.request_thumbnail(path, bounds, list_widget, key)


    def _apply_local_search(self):
        """Apply filename substring filter to local input/output lists (debounced)."""
        if getattr(self, '_local_search_timer', None) is not None:
            self._local_search_timer.stop()
        self._filter_local_items()
        self._enqueue_visible_thumbnails(self.local_list_in)
        self._enqueue_visible_thumbnails(self.local_list_out)


    def _toggle_local_controls(self):
        try:
            wrapper = getattr(self, 'local_controls_wrapper', None)
            btn = getattr(self, 'local_controls_toggle_btn', None)
            if wrapper is None or btn is None:
                return
            self._local_controls_visible = not bool(getattr(self, '_local_controls_visible', True))
            wrapper.setVisible(self._local_controls_visible)
            btn.setChecked(self._local_controls_visible)
            btn.setText('收起选项' if self._local_controls_visible else '排序与筛选')
        except Exception:
            pass


    def _update_local_browser_state(self):
        from aetherloom_core.local_browser_ui import update_state
        update_state(self)


    def _update_local_selection_summary(self, list_widget):
        from aetherloom_core.local_browser_ui import selection_summary
        selection_summary(self, list_widget)


    def _reset_local_browser_filters(self):
        self.local_search_edit.clear()
        self.local_type_combo.setCurrentIndex(0)
        self._clear_all_filter_rows()
        self._apply_local_search()


    def _add_local_filter_row(self, initial=False):
        try:
            container = getattr(self, 'local_filter_container', None)
            if container is None:
                return
            if not hasattr(self, '_local_filter_rows') or self._local_filter_rows is None:
                self._local_filter_rows = []
            row = {'active': {'kind': 'none', 'target': 'both'}}
            row_widget = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(row_widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            type_combo = QtWidgets.QComboBox()
            filter_options = [
                ('不过滤', 'none'),
                ('文件大小', 'size'),
                ('宽度', 'width'),
                ('高度', 'height'),
                ('像素', 'mp'),
                ('文件类型', 'type'),
                ('文件名', 'name'),
                ('修改时间', 'mtime'),
                ('同时存在于输入输出', 'paired'),
            ]
            for lbl, key in filter_options:
                type_combo.addItem(lbl, key)
            layout.addWidget(type_combo)
            target_combo = QtWidgets.QComboBox()
            target_combo.addItem('全部', 'both')
            target_combo.addItem('仅输入', 'input')
            target_combo.addItem('仅输出', 'output')
            try:
                target_combo.setMinimumWidth(150)
                target_combo.setFixedWidth(160)
            except Exception:
                pass
            layout.addWidget(target_combo)
            name_cmp_combo = QtWidgets.QComboBox()
            name_cmp_combo.addItem('包含', 'contains')
            name_cmp_combo.addItem('不包含', 'not_contains')
            layout.addWidget(name_cmp_combo)
            name_edit = QtWidgets.QLineEdit()
            name_edit.setPlaceholderText('输入关键字')
            name_edit.setFixedWidth(160)
            layout.addWidget(name_edit)
            mtime_preset_combo = QtWidgets.QComboBox()
            mtime_presets = [
                ('今天', 'today'),
                ('三天内', '3days'),
                ('本周', 'week'),
                ('本月', 'month'),
                ('具体时间', 'custom'),
            ]
            for lbl, key in mtime_presets:
                mtime_preset_combo.addItem(lbl, key)
            layout.addWidget(mtime_preset_combo)
            mtime_from_edit = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
            mtime_from_edit.setDisplayFormat('yyyy-MM-dd')
            mtime_from_edit.setCalendarPopup(True)
            mtime_from_edit.setFixedWidth(255)
            layout.addWidget(mtime_from_edit)
            mtime_to_edit = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
            mtime_to_edit.setDisplayFormat('yyyy-MM-dd')
            mtime_to_edit.setCalendarPopup(True)
            mtime_to_edit.setFixedWidth(255)
            layout.addWidget(mtime_to_edit)
            mtime_range_label = QtWidgets.QLabel('')
            mtime_range_label.setStyleSheet('color:#6b7280; padding-left:6px;')
            mtime_range_label.setMinimumWidth(200)
            layout.addWidget(mtime_range_label)
            comp_combo = QtWidgets.QComboBox()
            comp_combo.addItems(['≥', '≤', '>', '<', '='])
            layout.addWidget(comp_combo)
            value_spin = QtWidgets.QDoubleSpinBox()
            value_spin.setRange(0, 100000)
            value_spin.setDecimals(2)
            value_spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
            value_spin.setFixedWidth(110)
            layout.addWidget(value_spin)
            unit_combo = QtWidgets.QComboBox()
            unit_combo.addItems(['MB', 'KB'])
            try:
                unit_combo.setMinimumWidth(110)
                unit_combo.setFixedWidth(120)
            except Exception:
                unit_combo.setFixedWidth(110)
            layout.addWidget(unit_combo)
            unit_label = QtWidgets.QLabel('')
            unit_label.setStyleSheet('color:#6b7280;')
            layout.addWidget(unit_label)
            type_value_combo = QtWidgets.QComboBox()
            type_value_combo.addItem('图像 (IMG)', 'IMG')
            type_value_combo.addItem('视频 (VIDEO)', 'VIDEO')
            type_value_combo.addItem('动图 (GIF)', 'GIF')
            layout.addWidget(type_value_combo)
            pair_combo = QtWidgets.QComboBox()
            pair_combo.addItem('是', 'yes')
            pair_combo.addItem('否', 'no')
            layout.addWidget(pair_combo)
            apply_btn = QtWidgets.QPushButton('应用')
            reset_btn = QtWidgets.QPushButton('重置')
            delete_btn = QtWidgets.QPushButton('删除')
            layout.addWidget(apply_btn)
            layout.addWidget(reset_btn)
            layout.addWidget(delete_btn)
            status_label = QtWidgets.QLabel('未应用')
            status_label.setStyleSheet('color:#9aa0a6; padding-left:6px;')
            layout.addWidget(status_label)
            layout.addStretch(1)
            container.addWidget(row_widget)
            row.update({
                'layout': layout,
                'widget': row_widget,
                'type_combo': type_combo,
                'target_combo': target_combo,
                'name_cmp_combo': name_cmp_combo,
                'name_edit': name_edit,
                'mtime_preset_combo': mtime_preset_combo,
                'mtime_from_edit': mtime_from_edit,
                'mtime_to_edit': mtime_to_edit,
                'mtime_range_label': mtime_range_label,
                'comp_combo': comp_combo,
                'value_spin': value_spin,
                'unit_combo': unit_combo,
                'unit_label': unit_label,
                'type_value_combo': type_value_combo,
                'pair_combo': pair_combo,
                'apply_btn': apply_btn,
                'reset_btn': reset_btn,
                'delete_btn': delete_btn,
                'status_label': status_label,
            })
            self._local_filter_rows.append(row)
            try:
                if not initial:
                    total_rows = len(self._local_filter_rows)
                    if total_rows >= 3:
                        self._filter_dropdown_index = total_rows - 1
            except Exception:
                pass
            comp_combo.setVisible(False)
            value_spin.setVisible(False)
            unit_combo.setVisible(False)
            unit_label.setVisible(False)
            type_value_combo.setVisible(False)
            pair_combo.setVisible(False)
            name_cmp_combo.setVisible(False)
            name_edit.setVisible(False)
            mtime_preset_combo.setVisible(False)
            mtime_from_edit.setVisible(False)
            mtime_to_edit.setVisible(False)
            mtime_range_label.setVisible(False)
            type_combo.currentIndexChanged.connect(lambda _=None, r=row: self._configure_filter_row(r))
            apply_btn.clicked.connect(lambda _=None, r=row: self._on_apply_filter_row(r))
            reset_btn.clicked.connect(lambda _=None, r=row: self._on_clear_filter_row(r))
            delete_btn.clicked.connect(lambda _=None, r=row: self._remove_filter_row(r))
            name_edit.textChanged.connect(lambda _=None, r=row: self._update_filter_row_apply_state(r))
            mtime_preset_combo.currentIndexChanged.connect(lambda _=None, r=row: self._on_mtime_preset_changed(r))
            mtime_from_edit.dateChanged.connect(lambda _=None, r=row: self._on_mtime_date_changed(r))
            mtime_to_edit.dateChanged.connect(lambda _=None, r=row: self._on_mtime_date_changed(r))
            self._configure_filter_row(row)
            self._update_filter_row_status(row)
            self._refresh_filter_dropdown()
            if not initial:
                self._filter_local_items()
        except Exception:
            pass


    def _clear_all_filter_rows(self):
        try:
            rows = getattr(self, '_local_filter_rows', [])
            if not rows:
                return
            for row in list(rows):
                widget = row.get('widget') if isinstance(row, dict) else None
                if widget is not None:
                    widget.setParent(None)
                    try:
                        widget.deleteLater()
                    except Exception:
                        pass
            rows.clear()
            self._filter_dropdown_index = 0
            self._refresh_filter_dropdown()
            self._filter_local_items()
        except Exception:
            pass


    def _remove_filter_row(self, row):
        try:
            rows = getattr(self, '_local_filter_rows', [])
            if row not in rows:
                return
            widget = row.get('widget')
            if widget is not None:
                widget.setParent(None)
                try:
                    widget.deleteLater()
                except Exception:
                    pass
            rows.remove(row)
            if not rows:
                self._filter_dropdown_index = 0
            elif getattr(self, '_filter_dropdown_index', 0) >= len(rows):
                self._filter_dropdown_index = max(0, len(rows) - 1)
            self._refresh_filter_dropdown()
            self._filter_local_items()
        except Exception:
            pass


    def _refresh_filter_dropdown(self):
        try:
            dropdown = getattr(self, 'local_filter_dropdown', None)
            label = getattr(self, 'local_filter_dropdown_label', None)
            container = getattr(self, '_local_filter_rows', []) or []
            if dropdown is None:
                return
            dropdown.blockSignals(True)
            dropdown.clear()
            for idx, row in enumerate(container):
                try:
                    type_combo = row.get('type_combo') if isinstance(row, dict) else None
                except Exception:
                    type_combo = None
                try:
                    target_combo = row.get('target_combo') if isinstance(row, dict) else None
                except Exception:
                    target_combo = None
                type_label = '筛选'
                target_label = '全部'
                try:
                    if type_combo is not None:
                        txt = type_combo.currentText()
                        if txt:
                            type_label = txt
                except Exception:
                    pass
                try:
                    if target_combo is not None:
                        txt = target_combo.currentText()
                        if txt:
                            target_label = txt
                except Exception:
                    pass
                dropdown.addItem(f'{type_label}（{target_label}） #{idx + 1}', idx)
            dropdown.blockSignals(False)
            if len(container) >= 3:
                dropdown.setVisible(True)
                if label is not None:
                    label.setVisible(True)
                sel = getattr(self, '_filter_dropdown_index', 0)
                sel = max(0, min(sel, len(container) - 1))
                self._filter_dropdown_index = sel
                dropdown.blockSignals(True)
                dropdown.setCurrentIndex(sel)
                dropdown.blockSignals(False)
            else:
                dropdown.setVisible(False)
                if label is not None:
                    label.setVisible(False)
                self._filter_dropdown_index = 0
            for idx, row in enumerate(container):
                widget = row.get('widget')
                if widget is None:
                    continue
                if len(container) >= 3:
                    widget.setVisible(idx == getattr(self, '_filter_dropdown_index', 0))
                else:
                    widget.setVisible(True)
            self._update_filter_clear_visibility()
        except Exception:
            pass


    def _on_filter_dropdown_changed(self, idx):
        try:
            self._filter_dropdown_index = max(0, int(idx))
            self._refresh_filter_dropdown()
        except Exception:
            pass


    def _configure_filter_row(self, row):
        try:
            combo = row.get('type_combo')
            if combo is None:
                return
            kind = combo.currentData() or 'none'
            numeric = kind in ('size', 'width', 'height', 'mp')
            type_mode = (kind == 'type')
            paired_mode = (kind == 'paired')
            comp_combo = row.get('comp_combo')
            value_spin = row.get('value_spin')
            unit_combo = row.get('unit_combo')
            unit_label = row.get('unit_label')
            type_value_combo = row.get('type_value_combo')
            pair_combo = row.get('pair_combo')
            apply_btn = row.get('apply_btn')
            name_cmp_combo = row.get('name_cmp_combo')
            name_edit = row.get('name_edit')
            mtime_preset_combo = row.get('mtime_preset_combo')
            mtime_from_edit = row.get('mtime_from_edit')
            mtime_to_edit = row.get('mtime_to_edit')
            mtime_range_label = row.get('mtime_range_label')
            if comp_combo is not None:
                comp_combo.setVisible(numeric)
            if value_spin is not None:
                value_spin.setVisible(numeric)
            if unit_combo is not None:
                unit_combo.setVisible(False)
            if unit_label is not None:
                unit_label.setVisible(False)
            if name_cmp_combo is not None:
                name_cmp_combo.setVisible(kind == 'name')
            if name_edit is not None:
                name_edit.setVisible(kind == 'name')
            show_mtime = (kind == 'mtime')
            if mtime_preset_combo is not None:
                mtime_preset_combo.setVisible(show_mtime)
            preset_key = mtime_preset_combo.currentData() if (show_mtime and mtime_preset_combo is not None) else None
            custom_range = show_mtime and preset_key == 'custom'
            if mtime_from_edit is not None:
                mtime_from_edit.setVisible(custom_range)
            if mtime_to_edit is not None:
                mtime_to_edit.setVisible(custom_range)
            if mtime_range_label is not None:
                mtime_range_label.setVisible(show_mtime)
            if numeric and value_spin is not None:
                if kind == 'size':
                    value_spin.setDecimals(2)
                    value_spin.setSingleStep(0.5)
                    value_spin.setMaximum(1048576)
                    if unit_combo is not None:
                        unit_combo.setVisible(True)
                elif kind in ('width', 'height'):
                    value_spin.setDecimals(0)
                    value_spin.setSingleStep(10)
                    value_spin.setMaximum(100000)
                    if unit_label is not None:
                        unit_label.setText('px')
                        unit_label.setVisible(True)
                else:
                    value_spin.setDecimals(2)
                    value_spin.setSingleStep(0.1)
                    value_spin.setMaximum(1000)
                    if unit_label is not None:
                        unit_label.setText('MP')
                        unit_label.setVisible(True)
            if type_value_combo is not None:
                type_value_combo.setVisible(type_mode)
            if pair_combo is not None:
                pair_combo.setVisible(paired_mode)
            self._update_mtime_range_label(row)
            self._update_filter_row_apply_state(row)
        except Exception:
            pass


    def _on_mtime_preset_changed(self, row):
        try:
            self._configure_filter_row(row)
        except Exception:
            pass


    def _on_mtime_date_changed(self, row):
        try:
            self._update_filter_row_apply_state(row)
            self._update_mtime_range_label(row)
        except Exception:
            pass


    def _update_mtime_range_label(self, row):
        try:
            label = row.get('mtime_range_label') if isinstance(row, dict) else None
            if label is None:
                return
            type_combo = row.get('type_combo')
            kind = type_combo.currentData() if type_combo is not None else 'none'
            if kind != 'mtime':
                label.clear()
                label.setVisible(False)
                return
            preset_combo = row.get('mtime_preset_combo')
            preset = preset_combo.currentData() if preset_combo is not None else None
            if not preset:
                label.setVisible(True)
                label.setText('from ... to ...')
                return
            from_str = None
            to_str = None
            if preset == 'custom':
                from_edit = row.get('mtime_from_edit')
                to_edit = row.get('mtime_to_edit')
                if from_edit is not None and from_edit.date().isValid():
                    from_str = from_edit.date().toString('yyyy-MM-dd')
                if to_edit is not None and to_edit.date().isValid():
                    to_str = to_edit.date().toString('yyyy-MM-dd')
            start, end = self._compute_mtime_range(preset, from_str, to_str)
            if start is None and end is None:
                label.setVisible(True)
                label.setText('from ... to ...')
                return
            start_str = start.date().strftime('%Y-%m-%d') if start else '...'
            if end is not None:
                end_inclusive = end - timedelta(seconds=1)
                end_str = end_inclusive.date().strftime('%Y-%m-%d')
            else:
                end_str = '...'
            label.setVisible(True)
            label.setText(f'from {start_str} to {end_str}')
        except Exception:
            pass


    def _collect_filter_row_state(self, row):
        try:
            type_combo = row.get('type_combo')
            if type_combo is None:
                return {'kind': 'none', 'target': 'both'}
            kind = type_combo.currentData() or 'none'
            target_combo = row.get('target_combo')
            target = target_combo.currentData() if target_combo is not None else 'both'
            cfg = {'kind': kind, 'target': target or 'both'}
            if kind == 'name':
                cmp_combo = row.get('name_cmp_combo')
                txt = row.get('name_edit').text() if row.get('name_edit') is not None else ''
                cfg['cmp'] = cmp_combo.currentData() if cmp_combo is not None else 'contains'
                cfg['value'] = txt or ''
            elif kind in ('size', 'width', 'height', 'mp'):
                comp_combo = row.get('comp_combo')
                value_spin = row.get('value_spin')
                cfg['cmp'] = comp_combo.currentText() if comp_combo is not None else '≥'
                cfg['value'] = float(value_spin.value()) if value_spin is not None else 0.0
                if kind == 'size':
                    unit_combo = row.get('unit_combo')
                    cfg['unit'] = unit_combo.currentText() if unit_combo is not None else 'MB'
                elif kind in ('width', 'height'):
                    cfg['unit'] = 'px'
                else:
                    cfg['unit'] = 'MP'
            elif kind == 'type':
                combo_type = row.get('type_value_combo')
                cfg['value'] = combo_type.currentData() if combo_type is not None else 'IMG'
            elif kind == 'paired':
                combo_pair = row.get('pair_combo')
                cfg['value'] = combo_pair.currentData() if combo_pair is not None else 'yes'
            elif kind == 'mtime':
                preset_combo = row.get('mtime_preset_combo')
                preset = preset_combo.currentData() if preset_combo is not None else 'today'
                cfg['preset'] = preset or 'today'
                if cfg['preset'] == 'custom':
                    from_edit = row.get('mtime_from_edit')
                    to_edit = row.get('mtime_to_edit')
                    if from_edit is not None:
                        cfg['from'] = from_edit.date().toString('yyyy-MM-dd')
                    if to_edit is not None:
                        cfg['to'] = to_edit.date().toString('yyyy-MM-dd')
            return cfg
        except Exception:
            return {'kind': 'none', 'target': 'both'}


    def _on_apply_filter_row(self, row):
        try:
            cfg = self._collect_filter_row_state(row)
            if cfg.get('kind') in (None, 'none'):
                cfg = {'kind': 'none', 'target': cfg.get('target', 'both')}
            row['active'] = cfg
            self._update_filter_row_status(row)
            self._filter_local_items()
        except Exception:
            pass


    def _on_clear_filter_row(self, row):
        try:
            if row.get('type_combo') is not None:
                row['type_combo'].setCurrentIndex(0)
            if row.get('value_spin') is not None:
                row['value_spin'].setValue(0)
            if row.get('name_edit') is not None:
                row['name_edit'].clear()
            if row.get('mtime_preset_combo') is not None:
                row['mtime_preset_combo'].setCurrentIndex(0)
            if row.get('mtime_from_edit') is not None:
                row['mtime_from_edit'].setDate(QtCore.QDate.currentDate())
            if row.get('mtime_to_edit') is not None:
                row['mtime_to_edit'].setDate(QtCore.QDate.currentDate())
            target_combo = row.get('target_combo')
            target = target_combo.currentData() if target_combo is not None else 'both'
            row['active'] = {'kind': 'none', 'target': target or 'both'}
            self._update_filter_row_status(row)
            self._update_filter_row_apply_state(row)
            self._filter_local_items()
        except Exception:
            pass


    def _update_filter_row_status(self, row):
        try:
            lbl = row.get('status_label')
            if lbl is None:
                return
            cfg = row.get('active') or {}
            if cfg.get('kind') in (None, 'none'):
                lbl.setText('未应用')
                lbl.setStyleSheet('color:#9aa0a6; padding-left:6px;')
            else:
                target_map = {'both': '输入+输出', 'input': '仅输入', 'output': '仅输出'}
                lbl.setText(f"已应用（{target_map.get(cfg.get('target', 'both'), '全部')}）")
                lbl.setStyleSheet('color:#10b981; padding-left:6px;')
        except Exception:
            pass


    def _update_filter_row_apply_state(self, row):
        try:
            apply_btn = row.get('apply_btn')
            type_combo = row.get('type_combo')
            if apply_btn is None or type_combo is None:
                return
            kind = type_combo.currentData() or 'none'
            enabled = kind != 'none'
            if kind == 'name':
                text = ''
                try:
                    text = row.get('name_edit').text() if row.get('name_edit') is not None else ''
                except Exception:
                    text = ''
                enabled = bool(text.strip())
            elif kind == 'mtime':
                preset_combo = row.get('mtime_preset_combo')
                preset = preset_combo.currentData() if preset_combo is not None else None
                if not preset:
                    enabled = False
                elif preset == 'custom':
                    from_edit = row.get('mtime_from_edit')
                    to_edit = row.get('mtime_to_edit')
                    if from_edit is None or to_edit is None:
                        enabled = False
                    else:
                        start = from_edit.date()
                        end = to_edit.date()
                        enabled = start.isValid() and end.isValid() and start <= end
                else:
                    enabled = True
            apply_btn.setEnabled(enabled)
        except Exception:
            pass


    def _filter_local_items(self, list_widget=None, start=0):
        try:
            query = ''
            type_combo = getattr(self, 'local_type_combo', None)
            media_type = type_combo.currentData() if type_combo is not None else 'all'
            if hasattr(self, 'local_search_edit') and self.local_search_edit is not None:
                query = (self.local_search_edit.text() or '').strip().lower()
            rows = getattr(self, '_local_filter_rows', []) or []
            active_filters = []
            for row in rows:
                cfg = (row.get('active') or {}) if isinstance(row, dict) else {}
                if cfg.get('kind') not in (None, 'none'):
                    active_filters.append(cfg)
            pairs = [
                (getattr(self, 'local_list_in', None), getattr(self, 'local_count_label_in', None)),
                (getattr(self, 'local_list_out', None), getattr(self, 'local_count_label_out', None)),
            ]
            if not hasattr(self, '_local_visible_rows'):
                self._local_visible_rows = {}
            for lw, label in pairs:
                if lw is None or (list_widget is not None and lw is not list_widget):
                    continue
                total = lw.count()
                first = min(start, total) if list_widget is not None else 0
                visible_rows = self._local_visible_rows.get(id(lw), []) if first else []
                # New batches append; avoid copying all earlier rows every 100 files.
                if first:
                    from bisect import bisect_left
                    del visible_rows[bisect_left(visible_rows, first):]
                visible = len(visible_rows)
                list_kind = 'input' if lw is getattr(self, 'local_list_in', None) else ('output' if lw is getattr(self, 'local_list_out', None) else 'both')
                relevant_filters = [cfg for cfg in active_filters if cfg.get('target', 'both') in ('both', list_kind)]
                selection_blocker = QtCore.QSignalBlocker(lw)
                for i in range(first, total):
                    it = lw.item(i)
                    if it is None:
                        continue
                    meta = it.data(QtCore.Qt.UserRole) or {}
                    if meta.get('is_dir'):
                        it.setHidden(False)
                        visible += 1
                        visible_rows.append(i)
                        continue
                    match = True
                    if query:
                        try:
                            name = (it.text() or '').lower()
                            if query not in name:
                                match = False
                        except Exception:
                            match = False
                    if match and media_type in ('image', 'video'):
                        meta = it.data(QtCore.Qt.UserRole) or {}
                        kind = str(meta.get('file_type') or '').upper()
                        match = kind in ('IMG', 'IMAGE', 'GIF') if media_type == 'image' else kind == 'VIDEO'
                    if match and relevant_filters:
                        meta = it.data(QtCore.Qt.UserRole) or {}
                        for cfg in relevant_filters:
                            if not self._matches_local_filter(meta, cfg):
                                match = False
                                break
                    try:
                        it.setHidden(not match)
                        if not match and it.isSelected():
                            it.setSelected(False)
                    except Exception:
                        pass
                    if match:
                        visible += 1
                        visible_rows.append(i)
                selection_blocker.unblock()
                self._local_visible_rows[id(lw)] = visible_rows
                lw.setProperty('filteredVisibleCount', visible)
                if label is not None:
                    try:
                        prefix = '输入目录' if lw is getattr(self, 'local_list_in', None) else ('输出目录' if lw is getattr(self, 'local_list_out', None) else '目录')
                        suffix = ' (筛选)' if (query or relevant_filters or media_type != 'all') else ''
                        if total:
                            label.setText(f'{prefix}: {visible}/{total} 项{suffix}')
                        else:
                            label.setText(f'{prefix}: 0 项')
                        pending = self._local_pending_add.get(str(id(lw)))
                        if pending:
                            label.setText(label.text() + f' · 正在加载 {pending[2]}/{len(pending[1])}')
                        elif total and not visible:
                            label.setText(label.text() + ' · 没有匹配的文件')
                    except Exception:
                        pass
            if hasattr(self, '_update_local_browser_state'):
                self._update_local_browser_state()
        except Exception:
            pass


    def _compute_mtime_range(self, preset, from_str=None, to_str=None):
        try:
            now = datetime.now()
            start = None
            end = None
            key = preset or 'today'
            if key == 'today':
                start = datetime(now.year, now.month, now.day)
                end = start + timedelta(days=1)
            elif key == '3days':
                start = now - timedelta(days=3)
                end = now + timedelta(seconds=1)
            elif key == 'week':
                week_start = now - timedelta(days=now.weekday())
                start = datetime(week_start.year, week_start.month, week_start.day)
                end = start + timedelta(days=7)
            elif key == 'month':
                start = datetime(now.year, now.month, 1)
                if start.month == 12:
                    end = datetime(start.year + 1, 1, 1)
                else:
                    end = datetime(start.year, start.month + 1, 1)
            elif key == 'custom':
                if from_str:
                    try:
                        start = datetime.strptime(from_str, '%Y-%m-%d')
                    except Exception:
                        start = None
                if to_str:
                    try:
                        end = datetime.strptime(to_str, '%Y-%m-%d') + timedelta(days=1)
                    except Exception:
                        end = None
            return start, end
        except Exception:
            return None, None


    def _matches_local_filter(self, meta, cfg):
        try:
            kind = cfg.get('kind', 'none') if isinstance(cfg, dict) else 'none'
            if kind in (None, 'none'):
                return True
            if kind == 'name':
                pattern = (cfg.get('value') or '').strip().lower()
                if not pattern:
                    return True
                try:
                    fname = os.path.basename(meta.get('path')) if meta.get('path') else meta.get('name', '')
                except Exception:
                    fname = meta.get('name', '')
                name = (fname or '').lower()
                cmp_mode = cfg.get('cmp', 'contains')
                if cmp_mode == 'not_contains':
                    return pattern not in name
                return pattern in name
            if kind == 'mtime':
                stats = self._get_local_filter_stats(meta, require_dims=False)
                file_mtime = stats.get('mtime')
                if file_mtime is None:
                    return False
                file_dt = datetime.fromtimestamp(file_mtime)
                preset = cfg.get('preset', 'today')
                start, end = self._compute_mtime_range(preset, cfg.get('from'), cfg.get('to'))
                if start and file_dt < start:
                    return False
                if end and file_dt >= end:
                    return False
                return True
            if kind == 'type':
                target = (cfg.get('value') or '').upper()
                meta_type = (meta.get('file_type') or '').upper()
                if not meta_type and meta.get('path'):
                    meta_type = self._guess_file_type(meta.get('path'))
                    meta['file_type'] = meta_type
                return meta_type == target
            if kind == 'paired':
                want = cfg.get('value') == 'yes'
                return bool(meta.get('paired')) == want
            if kind == 'size':
                stats = self._get_local_filter_stats(meta, require_dims=False)
                size_bytes = stats.get('size_bytes')
                if size_bytes is None:
                    return False
                unit = cfg.get('unit', 'MB')
                value = size_bytes / (1024.0 * 1024.0) if unit != 'KB' else size_bytes / 1024.0
                return self._compare_numeric(value, cfg.get('value'), cfg.get('cmp'))
            if kind in ('width', 'height', 'mp'):
                stats = self._get_local_filter_stats(meta, require_dims=True)
                if kind == 'width':
                    value = stats.get('width')
                elif kind == 'height':
                    value = stats.get('height')
                else:
                    value = stats.get('mp')
                return self._compare_numeric(value, cfg.get('value'), cfg.get('cmp'))
        except Exception:
            pass
        return True


    def _get_local_filter_stats(self, meta, require_dims=False):
        stats = {}
        if isinstance(meta, dict) and meta.get('is_dir'):
            return stats
        try:
            path = meta.get('path') if isinstance(meta, dict) else None
            if not path:
                return stats
            cache = self._local_meta_cache.get(path)
            if cache is None:
                cache = {}
                self._local_meta_cache[path] = cache
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = None
            if cache.get('mtime') != mtime:
                cache.clear()
                cache['mtime'] = mtime
                cache['size_bytes'] = None
                cache['width'] = None
                cache['height'] = None
                cache['mp'] = None
                cache['file_type'] = meta.get('file_type')
            if cache.get('size_bytes') is None:
                try:
                    cache['size_bytes'] = os.path.getsize(path)
                except Exception:
                    cache['size_bytes'] = None
            if not cache.get('file_type'):
                cache['file_type'] = meta.get('file_type') or self._guess_file_type(path)
            if require_dims and (not cache.get('width') or not cache.get('height')):
                w, h = self._probe_media_dimensions(path, cache.get('file_type'))
                if w:
                    cache['width'] = int(w)
                if h:
                    cache['height'] = int(h)
            if cache.get('width') and cache.get('height') and cache.get('mp') is None:
                try:
                    cache['mp'] = round((cache['width'] * cache['height']) / 1_000_000.0, 3)
                except Exception:
                    cache['mp'] = None
            for key in ('size_bytes', 'width', 'height', 'mp', 'file_type', 'mtime'):
                if cache.get(key) is not None:
                    meta[key] = cache.get(key)
            stats = cache
        except Exception:
            pass
        return stats


    def _probe_media_dimensions(self, path, file_type):
        width = None
        height = None
        try:
            ftype = (file_type or '').upper()
            if ftype == 'VIDEO':
                cap = None
                try:
                    cap = cv2.VideoCapture(path)
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                finally:
                    if cap is not None:
                        cap.release()
            else:
                with Image.open(path) as img:
                    width, height = img.size
        except Exception:
            pass
        return width, height


    def _guess_file_type(self, path):
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == '.gif':
                return 'GIF'
            if ext in IMAGE_EXTS:
                return 'IMG'
            if ext in VIDEO_EXTS:
                return 'VIDEO'
        except Exception:
            pass
        return 'UNKNOWN'


    def _compare_numeric(self, value, target, op):
        try:
            if value is None or target is None:
                return False
            v = float(value)
            t = float(target)
            if op == '≥':
                return v >= t
            if op == '≤':
                return v <= t
            if op == '>':
                return v > t
            if op == '<':
                return v < t
            if op == '=':
                return abs(v - t) < 1e-6
        except Exception:
            return False
        return True


    def _start_chunked_population(self, list_widget, items, batch=100, delay=10,
                                  item_factory=None, selected_paths=None):
        """Create as well as insert items lazily; each batch yields to the event loop."""
        import time
        if list_widget is None or not items:
            return
        key = str(id(list_widget))
        state = [list_widget, items, 0]
        self._local_pending_add[key] = state
        selected_paths = selected_paths or set()
        batch = max(1, int(batch))

        def tick():
            if self._local_pending_add.get(key) is not state:
                return
            lw, records, index = state
            try:
                start_row = lw.count()
            except RuntimeError:
                self._local_pending_add.pop(key, None)
                return
            deadline = time.perf_counter() + 0.008
            end = min(len(records), index + batch)
            lw.setUpdatesEnabled(False)
            selection_blocker = QtCore.QSignalBlocker(lw)
            try:
                while index < end:
                    item = item_factory(records[index]) if item_factory else records[index]
                    lw.addItem(item)
                    if selected_paths and (item.data(QtCore.Qt.UserRole) or {}).get('path') in selected_paths:
                        item.setSelected(True)
                    index += 1
                    if time.perf_counter() >= deadline:
                        break
                state[2] = index
                if index >= len(records):
                    self._local_pending_add.pop(key, None)
                self._filter_local_items(lw, start=start_row)
            except Exception as exc:
                self._local_pending_add.pop(key, None)
                self._local_snapshot_input = None
                self._local_snapshot_output = None
                self.log(f'本地视图加载失败: {exc}')
            finally:
                selection_blocker.unblock()
                lw.setUpdatesEnabled(True)
            self._enqueue_visible_thumbnails(lw, margin=2)
            if self._local_pending_add.get(key) is state:
                QtCore.QTimer.singleShot(max(0, delay), tick)
            elif hasattr(self, '_restore_local_browser_position'):
                self._restore_local_browser_position(lw)

        self._filter_local_items(list_widget)
        QtCore.QTimer.singleShot(0, tick)


    def _on_files_dropped(self, paths):
        """Handle files dropped onto file_list or preview: copy into the local decoding folder and refresh list."""
        copied = 0
        new_names = []
        for p in paths:
            try:
                if os.path.isdir(p):
                    # copy supported files from directory (non-recursive)
                    for name in os.listdir(p):
                        src = os.path.join(p, name)
                        if os.path.isfile(src) and name.lower().endswith(IMAGE_EXTS + VIDEO_EXTS):
                            dst = os.path.join(self.local_decode_dir, os.path.basename(src))
                            # respect overwrite setting: if overwrite checked, always copy (overwrite), else skip existing
                            try_overwrite = getattr(self, 'overwrite_cb', None) and self.overwrite_cb.isChecked()
                            if os.path.abspath(src) != os.path.abspath(dst):
                                if os.path.exists(dst) and not try_overwrite:
                                    # skip
                                    self.log(f'导入时跳过已存在文件: {os.path.basename(dst)}')
                                else:
                                    shutil.copy2(src, dst)
                                    copied += 1
                                    new_names.append(os.path.basename(src))
                else:
                    if p.lower().endswith(IMAGE_EXTS + VIDEO_EXTS):
                        dst = os.path.join(self.local_decode_dir, os.path.basename(p))
                        try_overwrite = getattr(self, 'overwrite_cb', None) and self.overwrite_cb.isChecked()
                        if os.path.abspath(p) != os.path.abspath(dst):
                            if os.path.exists(dst) and not try_overwrite:
                                self.log(f'导入时跳过已存在文件: {os.path.basename(dst)}')
                            else:
                                shutil.copy2(p, dst)
                                copied += 1
                                new_names.append(os.path.basename(p))
            except Exception as e:
                self.log(f'导入文件失败: {p}  错误: {e}')
        if copied > 0:
            self.log(f'已导入 {copied} 个文件到本地解码文件夹')
            # refresh list
            self.load_folder(self.local_decode_dir, selected_names=new_names)


    def select_input_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, '选择输入文件夹', self.input_dir)
        if d:
            self.input_dir = d
            self.input_label.setText(d)
            self._save_settings()
            self.load_folder(self.local_decode_dir)


    def select_output_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, '选择输出文件夹', self.output_dir)
        if d:
            self.output_dir = d
            self.output_label.setText(d)
            self._save_settings()


    def select_local_decode_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, '选择本地解码文件夹', self.local_decode_dir)
        if d:
            self.local_decode_dir = d
            self.local_decode_label.setText(d)
            os.makedirs(self.local_decode_dir, exist_ok=True)
            self._save_settings()
            QtCore.QTimer.singleShot(50, lambda: self.load_folder(self.local_decode_dir))
            QtCore.QTimer.singleShot(50, lambda: self._refresh_local_list())


    def _open_folder_path(self, path, create=False):
        try:
            target = (path or '').strip()
            if not target:
                self.log('路径为空，无法打开。')
                return
            if create:
                try:
                    os.makedirs(target, exist_ok=True)
                except Exception:
                    pass
            if not os.path.exists(target):
                self.log('目标目录不存在，已尝试创建。')
                return
            if sys.platform.startswith('win'):
                os.startfile(target)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', target])
            else:
                subprocess.Popen(['xdg-open', target])
        except Exception as e:
            self.log(f'打开目录失败: {e}')


    def _reveal_in_explorer(self, path):
        """Reveal a file in the OS file manager (select the file when possible).
        Accepts either a file path or a folder; if path is a file, try to select it.
        """
        try:
            if not path:
                return
            p = path
            # if directory passed, open it normally
            if os.path.isdir(p):
                if sys.platform.startswith('win'):
                    os.startfile(p)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', p])
                else:
                    subprocess.Popen(['xdg-open', p])
                return
            # prefer to reveal the file and select it when possible
            if sys.platform.startswith('win'):
                try:
                    import ctypes
                    p_norm = os.path.normpath(os.path.abspath(p))
                    # Use ShellExecuteW for Unicode-safe reveal/select
                    try:
                        # params must be a single string; ensure proper escaping
                        params = f'/select,"{p_norm}"'
                        ctypes.windll.shell32.ShellExecuteW(None, 'open', 'explorer.exe', params, None, 1)
                        return
                    except Exception:
                        # fallback to subprocess with normalized path
                        try:
                            subprocess.Popen(['explorer', f'/select,{p_norm}'])
                            return
                        except Exception:
                            try:
                                os.startfile(os.path.dirname(p_norm))
                                return
                            except Exception:
                                return
                except Exception:
                    try:
                        os.startfile(os.path.dirname(p))
                        return
                    except Exception:
                        return
            elif sys.platform == 'darwin':
                try:
                    subprocess.Popen(['open', '-R', p])
                    return
                except Exception:
                    try:
                        subprocess.Popen(['open', os.path.dirname(p)])
                        return
                    except Exception:
                        return
            else:
                try:
                    subprocess.Popen(['xdg-open', os.path.dirname(p)])
                    return
                except Exception:
                    return
        except Exception:
            return


    def on_input_edit(self):
        d = self.input_label.text().strip()
        if d:
            self.input_dir = d
            os.makedirs(self.input_dir, exist_ok=True)
            self._save_settings()
            self.load_folder(self.local_decode_dir)


    def on_output_edit(self):
        d = self.output_label.text().strip()
        if d:
            self.output_dir = d
            os.makedirs(self.output_dir, exist_ok=True)
            self._save_settings()


    def on_local_decode_edit(self):
        d = self.local_decode_label.text().strip()
        if d:
            self.local_decode_dir = d
            os.makedirs(self.local_decode_dir, exist_ok=True)
            self._save_settings()
            QtCore.QTimer.singleShot(50, lambda: self.load_folder(self.local_decode_dir))
            QtCore.QTimer.singleShot(50, lambda: self._refresh_local_list())


    def load_folder(self, folder, selected_names=None):
        from aetherloom_core.decode_browser import load_directory
        load_directory(self, folder, selected_names)


    def _get_thumb_key(self, path, size, revision=None):
        try:
            import hashlib
            if revision is None:
                stat = os.stat(path)
                revision = (stat.st_mtime_ns, stat.st_size)
            payload = f'{os.path.abspath(path)}|{int(size)}|{revision[0]}|{revision[1]}'
            return hashlib.sha1(payload.encode('utf-8')).hexdigest()
        except OSError:
            return None


    def _memcache_get(self, key):
        try:
            if key is None:
                return None
            v = self._thumb_mem_cache.get(key)
            if v is not None:
                # move to end as most recently used
                try:
                    self._thumb_mem_cache.move_to_end(key)
                except Exception:
                    pass
            return v
        except Exception:
            return None


    def _memcache_put(self, key, value):
        try:
            if key is None or value is None:
                return
            ensure_caches(self)
            self._thumb_mem_cache[key] = value
            try:
                self._thumb_mem_cache.move_to_end(key)
            except Exception:
                pass
            # evict oldest if over capacity
            try:
                while len(self._thumb_mem_cache) > getattr(self, '_thumb_mem_cache_max', 400):
                    self._thumb_mem_cache.popitem(last=False)
            except Exception:
                pass
        except Exception:
            pass


    def _prune_thumb_cache(self, max_size_mb=300, max_files=6000, max_age_days=None, aggressive=False):
        """Keep the on-disk thumbnail cache bounded by size/count/age.

        - max_size_mb / max_files: hard caps; oldest files removed first until under caps.
        - max_age_days: if set, any file older than this is removed eagerly.
        - aggressive: when True, will also remove non-png files and retry twice if initial removal fails.
        Returns a tuple of (removed_count, remaining_bytes) when possible.
        """
        try:
            cache_dir = getattr(self, '_thumb_cache_dir', None)
            if not cache_dir or not os.path.exists(cache_dir):
                return 0, 0
            max_bytes = max_size_mb * 1024 * 1024
            cutoff = None
            if max_age_days is not None:
                try:
                    import time
                    cutoff = time.time() - max(0, max_age_days) * 24 * 3600
                except Exception:
                    cutoff = None

            entries = []
            for name in os.listdir(cache_dir):
                if not name.lower().endswith('.png') and not aggressive:
                    continue
                path = os.path.join(cache_dir, name)
                try:
                    st = os.stat(path)
                    entries.append((st.st_mtime, st.st_size, path))
                except Exception:
                    continue

            if not entries:
                return 0, 0

            entries.sort(key=lambda x: x[0])  # oldest first
            total_bytes = sum(e[1] for e in entries)

            removed = 0
            if cutoff is not None:
                for mtime, size, path in list(entries):
                    if mtime <= cutoff:
                        try:
                            os.remove(path)
                            total_bytes -= size
                            removed += 1
                            entries.remove((mtime, size, path))
                        except Exception:
                            continue

            if total_bytes > max_bytes or len(entries) > max_files:
                for mtime, size, path in entries:
                    if total_bytes <= max_bytes and len(entries) - removed <= max_files:
                        break
                    try:
                        os.remove(path)
                        total_bytes -= size
                        removed += 1
                    except Exception:
                        if not aggressive:
                            continue
                        try:
                            os.remove(path)
                            total_bytes -= size
                            removed += 1
                        except Exception:
                            continue

            try:
                if hasattr(self, 'log'):
                    if removed:
                        self.log(f'缩略图缓存已清理 {removed} 个文件，剩余约 {total_bytes // (1024*1024)} MB (上限 {max_size_mb}MB/{max_files} 个)')
                    elif aggressive:
                        self.log('缩略图缓存未超限，无需清理')
            except Exception:
                pass
            return removed, total_bytes
        except Exception:
            return None


    def _notify_decorations_changed(self, list_widget, indices=None):
        """Notify view that decoration changed. For large lists, use viewport update to avoid expensive model emits."""
        try:
            if list_widget is None:
                return
            vp = list_widget.viewport()
            if vp is None:
                return
            n = list_widget.count()
            # if indices not provided or list large, just update viewport
            if indices is None or n > 300:
                try:
                    vp.update()
                    return
                except Exception:
                    pass
            try:
                model = list_widget.model()
                if model is None:
                    try:
                        vp.update()
                        return
                    except Exception:
                        return
                lo = min(indices)
                hi = max(indices)
                top = model.index(max(0, lo), 0)
                bot = model.index(max(0, hi), 0)
                try:
                    model.dataChanged.emit(top, bot, [QtCore.Qt.DecorationRole])
                    return
                except Exception:
                    try:
                        vp.update()
                    except Exception:
                        pass
            except Exception:
                try:
                    vp.update()
                except Exception:
                    pass
        except Exception:
            pass


    def _enqueue_initial_thumbnails(self, list_widget, max_items=200):
        try:
            n = list_widget.count()
            limit = min(n, max_items)
            # use optimized visible-range enqueue for initial set
            try:
                self._enqueue_visible_thumbnails(list_widget, margin=2)
            except Exception:
                # fallback to naive loop for small counts
                for i in range(limit):
                    it = list_widget.item(i)
                    meta = it.data(QtCore.Qt.UserRole) or {}
                    path = meta.get('path')
                    if path and not meta.get('is_dir'):
                        size = list_widget.iconSize()
                        low_key = meta.get('low_thumb_key') or self._get_thumb_key(path, LOW_RES_THUMB)
                        try:
                            self.request_thumbnail(path, (LOW_RES_THUMB, LOW_RES_THUMB), list_widget, low_key)
                        except Exception:
                            pass
                        try:
                            if not getattr(self, '_in_thumb_interaction', False):
                                key = meta.get('thumb_key') or self._get_thumb_key(path, size.width())
                                self.request_thumbnail(path, (size.width(), size.height()), list_widget, key)
                        except Exception:
                            pass
        except Exception:
            pass


    def request_thumbnail(self, path, size, list_widget, key=None):
        """Apply cached media by path, or share one cancellable generation job."""
        if not path or getattr(self, '_closing', False):
            return
        key = key or self._get_thumb_key(path, size[0])
        if not key:
            return
        from aetherloom_core.thumbnail_resources import retry_allowed
        if not retry_allowed(self, key):
            return
        ico = self._memcache_get(key)
        if ico is not None:
            self._apply_thumbnail_icon(path, key, ico)
            return
        if not getattr(self, '_thumb_cache_dir', None):
            return
        cache_path = os.path.join(self._thumb_cache_dir, key + '.png')
        if os.path.exists(cache_path):
            pix = QtGui.QPixmap(cache_path)
            if not pix.isNull():
                ico = QtGui.QIcon(pix)
                self._memcache_put(key, ico)
                self._apply_thumbnail_icon(path, key, ico)
                return
        existing = self._thumb_jobs_inflight.get(key)
        if existing is not None:
            if list_widget is not None:
                existing.list_ids.add(id(list_widget))
            return
        from types import SimpleNamespace
        token = SimpleNamespace(cancelled=False, list_ids={id(list_widget)} if list_widget is not None else set())
        self._thumb_jobs_inflight[key] = token
        try:
            job = ThumbnailJob(path, size, cache_path, key,
                               list_widget.objectName() if list_widget is not None else '', cancel_token=token)
            job.signals.finished.connect(partial(self._on_thumbnail_ready_for_token, token), QtCore.Qt.QueuedConnection)
            scheduler = getattr(self, '_thumb_scheduler', None)
            if scheduler is None:
                def discard(stale_key, stale_token):
                    if self._thumb_jobs_inflight.get(stale_key) is stale_token:
                        self._thumb_jobs_inflight.pop(stale_key, None)
                scheduler = ThumbnailScheduler(self._thumb_pool, discard,
                    self if isinstance(self, QtCore.QObject) else None)
                self._thumb_scheduler = scheduler
            scheduler.pool = self._thumb_pool
            scheduler.submit(key, job, token)
        except Exception:
            if self._thumb_jobs_inflight.get(key) is token:
                self._thumb_jobs_inflight.pop(key, None)


    def _apply_thumbnail_icon(self, path, key, ico):
        """Local grids have a direct path index; decode lists keep a safe fallback."""
        for lw in (getattr(self, 'file_list', None), getattr(self, 'local_list_in', None), getattr(self, 'local_list_out', None)):
            if lw is None:
                continue
            lookup = getattr(self, '_local_item_lookup', {}).get(id(lw))
            if lookup is not None:
                item = lookup.get(path)
                items = (item,) if item is not None else ()
            else:
                items = (lw.item(i) for i in range(lw.count()))
            changed = False
            for item in items:
                try:
                    if item is None or item.listWidget() is not lw:
                        continue
                    meta = item.data(QtCore.Qt.UserRole) or {}
                    if meta.get('path') != path or key not in (meta.get('thumb_key'), meta.get('low_thumb_key')):
                        continue
                    if meta.pop('preview_error', None):
                        item.setData(QtCore.Qt.UserRole, meta)
                        item.setToolTip(path)
                    if key != meta.get('low_thumb_key') and getattr(self, '_in_thumb_interaction', False):
                        continue
                    current = self._memcache_get(meta.get('thumb_key')) or ico
                    previous = item.data(QtCore.Qt.DecorationRole)
                    if not isinstance(previous, QtGui.QIcon) or previous.cacheKey() != current.cacheKey():
                        set_item_icon(self, item, current)
                        changed = True
                except RuntimeError:
                    # A file may have been removed after indexing.
                    if lookup is not None and lookup.get(path) is item:
                        lookup.pop(path, None)
            if changed:
                self._notify_decorations_changed(lw)


    def _on_thumbnail_ready_for_token(self, token, path, cache_path, key, list_name):
        # A cancelled worker may already have queued a completion signal when a
        # replacement job for this same file is scheduled.
        if not getattr(token, 'cancelled', False) and self._thumb_jobs_inflight.get(key) is token:
            self._on_thumbnail_ready(path, cache_path, key, list_name)


    def _on_thumbnail_ready(self, path, cache_path, key, list_name):
        if not cache_path or getattr(self, '_closing', False):
            if not cache_path and not getattr(self, '_closing', False):
                from aetherloom_core.thumbnail_resources import thumbnail_failed
                token = self._thumb_jobs_inflight.get(key)
                thumbnail_failed(self, path, key, getattr(token, 'error', '') or '无法生成预览，可使用系统打开。')
            self._thumb_jobs_inflight.pop(key, None)
            return
        token = self._thumb_jobs_inflight.get(key)
        data = getattr(token, 'png_bytes', None)
        if data and getattr(self, '_in_scroll_interaction', False):
            ensure_caches(self)
            self._thumb_raw_cache[key] = (data, list_name)
            self._thumb_jobs_inflight.pop(key, None)
            return
        try:
            pix = QtGui.QPixmap()
            if data:
                pix.loadFromData(data)
            else:
                pix.load(cache_path)
            if not pix.isNull():
                ico = QtGui.QIcon(pix)
                self._memcache_put(key, ico)
                self._apply_thumbnail_icon(path, key, ico)
        finally:
            self._thumb_jobs_inflight.pop(key, None)


    def _on_preview_ready(self, path, which, png_bytes):
        """Handle finished preview jobs; ignore stale results if user moved on."""
        if getattr(self, '_closing', False):
            return
        try:
            if not png_bytes:
                return
            pix = QtGui.QPixmap()
            if not pix.loadFromData(png_bytes, 'PNG'):
                return
            # ensure this result is still desired
            if which == 'orig':
                if getattr(self, '_pending_preview_orig', None) != path:
                    return
                self._current_pixmaps['orig'] = pix
                self._current_paths['orig'] = path
                try:
                    mode = self._current_decode_mode()
                    self._orig_pixmaps_by_mode[mode] = pix
                    self._orig_paths_by_mode[mode] = path
                except Exception:
                    pass
                try:
                    if hasattr(self.orig_view, 'set_base_pixmap'):
                        self.orig_view.set_base_pixmap(pix)
                    else:
                        self.orig_view.setPixmap(pix.scaled(self.orig_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                except Exception:
                    try:
                        self.orig_view.setPixmap(pix.scaled(self.orig_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    except Exception:
                        pass
                try:
                    # compute file info in background to avoid blocking on video metadata
                    self._mark_pending_info('orig', path)
                    job = FileInfoJob(path)
                    handler = lambda p, txt, target='orig': self._on_file_info_ready(target, p, txt)
                    try:
                        job.signals.finished.connect(handler, QtCore.Qt.QueuedConnection)
                    except Exception:
                        try:
                            job.signals.finished.connect(handler)
                        except Exception:
                            pass
                    try:
                        if getattr(self, '_thumb_pool', None):
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
                        self._set_file_info(path, target='orig')
                    except Exception:
                        pass
            elif which == 'output':
                if getattr(self, '_pending_preview_output', None) != path:
                    return
                self._current_pixmaps['output'] = pix
                self._current_paths['output'] = path
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
                try:
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
                    if getattr(self, '_thumb_pool', None):
                        self._thumb_pool.start(job)
                    else:
                        job.run()
                except Exception:
                    try:
                        self._set_file_info(path, target='output')
                    except Exception:
                        pass
                try:
                    self._update_output_play_button_visibility(False)
                except Exception:
                    pass
        except Exception:
            pass


    def _mark_pending_info(self, kind, path):
        try:
            if not isinstance(getattr(self, '_pending_info_paths', None), dict):
                self._pending_info_paths = {'orig': None, 'output': None}
            self._pending_info_paths[kind] = path
        except Exception:
            self._pending_info_paths = {kind: path}


    def _on_file_info_ready(self, kind, path, info_text):
        """Handle finished FileInfoJob; ignore if a newer selection is pending."""
        try:
            target = kind or 'output'
            pending = getattr(self, '_pending_info_paths', {}) or {}
            if pending.get(target) != path:
                return
            try:
                # set main file info label
                label = None
                try:
                    label = (getattr(self, '_info_labels', {}) or {}).get(target)
                except Exception:
                    label = self.file_info_label if target == 'output' else None
                if label is not None:
                    label.setText(info_text or '')
            except Exception:
                pass
            try:
                # update floating selection label if visible
                if target == 'output' and self._selection_info_label is not None and self._selection_info_frame is not None and self._selection_info_frame.isVisible():
                    # use same formatting as before but without path
                    parts = [p.strip() for p in (info_text or '').split(' | ') if p.strip()]
                    txt = '\n'.join(parts)
                    try:
                        self._selection_info_label.setText(txt)
                    except Exception:
                        pass
                    try:
                        # resize/move to accommodate
                        try:
                            maxw = max(400, int(self.width() * 0.6))
                            self._selection_info_frame.setFixedWidth(min(maxw, 1000))
                        except Exception:
                            pass
                        self._selection_info_frame.adjustSize()
                        fw = self._selection_info_frame.width()
                        fh = self._selection_info_frame.height()
                        margin = 16
                        x = max(8, self.width() - fw - margin)
                        y = max(8, self.height() - fh - margin)
                        self._selection_info_frame.move(x, y)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass


    def _on_scroll_idle(self):
        """Only convert bytes for current rows; disk cache retains offscreen media."""
        if getattr(self, '_closing', False):
            return
        from aetherloom_core.thumbnail_resources import visible_rows
        self._in_scroll_interaction = False
        for view in (getattr(self, 'file_list', None), getattr(self, 'local_list_in', None),
                     getattr(self, 'local_list_out', None)):
            for row in visible_rows(self, view):
                item = view.item(row)
                meta = item.data(QtCore.Qt.UserRole) or {}
                for key in (meta.get('low_thumb_key'), meta.get('thumb_key')):
                    entry = self._thumb_raw_cache.pop(key, None)
                    if entry:
                        pixmap = QtGui.QPixmap()
                        if pixmap.loadFromData(entry[0], 'PNG'):
                            icon = QtGui.QIcon(pixmap)
                            self._memcache_put(key, icon)
                            self._apply_thumbnail_icon(meta.get('path'), key, icon)
        self._thumb_raw_cache.clear()
