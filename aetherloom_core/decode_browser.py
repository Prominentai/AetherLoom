"""Asynchronous, pixel-free population of the local decoding directory."""

import os
from functools import partial

from PyQt5 import QtCore, QtWidgets

from aetherloom_core.local_media import LocalScanController, scan_media
from aetherloom_core.thumbnail_resources import cancel_list_requests


SUPPORTED_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp',
                        '.mp4', '.mov', '.avi', '.mkv', '.gif', '.webm')
LOW_RES_THUMB = 64


def scan_decode_directory(request, cancelled):
    try:
        records = scan_media(request['folder'], SUPPORTED_EXTENSIONS, cancelled=cancelled)
        if records is None or cancelled():
            return None
        # The decoding list historically used Python's case-sensitive ordering.
        records.sort(key=lambda record: record['name'])
        return dict(request=request, records={'decode': records}, errors={})
    except OSError as exc:
        return dict(request=request, records={'decode': []}, errors={'decode': str(exc)})


def load_directory(window, folder, selected_names=None):
    """Capture GUI selection, then scan and create only the latest directory view."""
    if getattr(window, '_closing', False):
        return
    folder = os.path.abspath(folder)
    view = window.file_list
    if selected_names is None:
        selected_names = set()
        for item in view.selectedItems():
            path = (item.data(QtCore.Qt.UserRole) or {}).get('path', '')
            if path and os.path.normcase(os.path.dirname(path)) == os.path.normcase(folder):
                selected_names.add(item.text())
    elif isinstance(selected_names, str):
        selected_names = {selected_names}
    else:
        selected_names = set(selected_names)
    if not hasattr(window, '_local_pending_add'):
        window._local_pending_add = {}
    # Invalidate an old population immediately, including callbacks queued before refresh.
    window._local_pending_add.pop(str(id(view)), None)
    cancel_list_requests(window, view)
    controller = getattr(window, '_decode_scan_controller', None)
    if controller is None:
        parent = window if isinstance(window, QtCore.QObject) else None
        controller = LocalScanController(partial(_apply_scan, window), parent,
                                         scanner=scan_decode_directory)
        window._decode_scan_controller = controller
    window._decode_scan_loading = True
    window._decode_scan_generation = controller.submit(dict(folder=folder, selected_names=selected_names))


def _apply_scan(window, generation, result):
    if getattr(window, '_closing', False) or generation != window._decode_scan_generation:
        return
    view = window.file_list
    key = str(id(view))
    window._local_pending_add.pop(key, None)
    cancel_list_requests(window, view)
    residency = getattr(window, '_thumb_residency', None)
    if residency is not None:
        residency.release_list(view)
    blocker = QtCore.QSignalBlocker(view)
    had_selection = bool(view.selectedItems())
    view.clear()
    blocker.unblock()
    if not hasattr(window, '_local_item_lookup'):
        window._local_item_lookup = {}
    window._local_item_lookup[id(view)] = {}
    if hasattr(window, '_local_visible_rows'):
        window._local_visible_rows.pop(id(view), None)
    records = result.get('records', {}).get('decode', [])
    request = result['request']
    selected_paths = {os.path.join(request['folder'], name) for name in request['selected_names']}
    for message in result.get('errors', {}).values():
        window.log(f'无法读取本地解码目录: {message}')

    def make_item(record):
        meta = dict(record)
        width = max(1, view.iconSize().width())
        revision = (meta['mtime_ns'], meta['size_bytes'])
        meta['low_thumb_key'] = window._get_thumb_key(meta['path'], LOW_RES_THUMB, revision)
        meta['thumb_key'] = window._get_thumb_key(meta['path'], width, revision)
        meta['thumb_size'] = width
        meta['file_type'] = window._guess_file_type(meta['path'])
        item = QtWidgets.QListWidgetItem(meta['name'])
        item.setData(QtCore.Qt.UserRole, meta)
        item.setToolTip(meta['path'])
        item.setSizeHint(QtCore.QSize(width + 16, width + 12))
        window._local_item_lookup[id(view)][meta['path']] = item
        return item

    if records:
        window._start_chunked_population(view, records, item_factory=make_item,
                                         selected_paths=selected_paths)

    def finish_when_ready():
        if getattr(window, '_closing', False) or generation != window._decode_scan_generation:
            return
        if key in window._local_pending_add:
            QtCore.QTimer.singleShot(20, finish_when_ready)
            return
        window._decode_scan_loading = False
        window._decode_loaded_folder = request['folder']
        # Batch construction deliberately blocks signals; dispatch once so existing
        # decode selection and preview handlers still react to restored/imported files.
        if had_selection or selected_paths:
            view.itemSelectionChanged.emit()
        window.log(f'已加载 {view.count()} 个可处理文件')

    QtCore.QTimer.singleShot(0, finish_when_ready)
