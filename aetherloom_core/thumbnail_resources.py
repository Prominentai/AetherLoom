"""Bounded GUI image residency and backpressure for thumbnail workers."""
from collections import OrderedDict
import time
import weakref

from PyQt5 import QtCore, QtGui, QtWidgets

MIB = 1024 * 1024


def icon_bytes(icon):
    return max(1, sum(size.width() * size.height() * 4 for size in icon.availableSizes()))


class BudgetCache(OrderedDict):
    def __init__(self, max_bytes, max_items, cost):
        super().__init__()
        self.max_bytes, self.max_items, self.cost = max_bytes, max_items, cost
        self.bytes_used = 0
        self._costs = {}

    def __setitem__(self, key, value):
        self.pop(key, None)
        cost = self.cost(value)
        if cost > self.max_bytes:
            return
        super().__setitem__(key, value)
        self._costs[key] = cost
        self.bytes_used += cost
        while len(self) > self.max_items or self.bytes_used > self.max_bytes:
            self.popitem(last=False)

    def pop(self, key, default=None):
        if key not in self:
            return default
        self.bytes_used -= self._costs.pop(key)
        return super().pop(key)

    def popitem(self, last=True):
        key = next(reversed(self)) if last else next(iter(self))
        return key, self.pop(key)

    def clear(self):
        super().clear()
        self._costs.clear()
        self.bytes_used = 0


class IconResidency:
    """LRU eviction must also remove the QListWidgetItem's owning QIcon."""
    def __init__(self, max_bytes=64 * MIB, max_items=400):
        self.max_bytes, self.max_items = max_bytes, max_items
        self.bytes_used = 0
        self.items = OrderedDict()

    def _drop(self, key, clear=True):
        entry = self.items.pop(key, None)
        if entry is None:
            return
        ref, size = entry
        self.bytes_used -= size
        item = ref()
        if clear and item is not None:
            try:
                item.setData(QtCore.Qt.DecorationRole, None)
            except RuntimeError:
                pass

    def set(self, item, icon):
        key = id(item)
        self._drop(key, clear=False)
        size = icon_bytes(icon)
        if size > self.max_bytes:
            item.setData(QtCore.Qt.DecorationRole, None)
            return
        item.setData(QtCore.Qt.DecorationRole, icon)
        self.items[key] = (weakref.ref(item, lambda _ref, key=key: self._drop(key, clear=False)), size)
        self.bytes_used += size
        while len(self.items) > self.max_items or self.bytes_used > self.max_bytes:
            self._drop(next(iter(self.items)))

    def release_list(self, view):
        for key, (ref, _size) in list(self.items.items()):
            item = ref()
            try:
                remove = item is None or item.listWidget() is view
            except RuntimeError:
                remove = True
            if remove:
                self._drop(key)

    def clear(self):
        for key in list(self.items):
            self._drop(key)


def set_item_icon(window, item, icon):
    if not hasattr(window, '_thumb_residency'):
        window._thumb_residency = IconResidency()
    window._thumb_residency.set(item, icon)


def ensure_caches(window):
    if not isinstance(window._thumb_mem_cache, BudgetCache):
        old = window._thumb_mem_cache
        window._thumb_mem_cache = BudgetCache(32 * MIB, 400, icon_bytes)
        for key, value in old.items():
            window._thumb_mem_cache[key] = value
    if not isinstance(window._thumb_raw_cache, BudgetCache):
        old = window._thumb_raw_cache
        window._thumb_raw_cache = BudgetCache(16 * MIB, 128, lambda value: len(value[0]))
        for key, value in old.items():
            window._thumb_raw_cache[key] = value


def cancel_list_requests(window, view, keep=()):
    for key, token in list(getattr(window, '_thumb_jobs_inflight', {}).items()):
        owners = getattr(token, 'list_ids', None)
        if owners is None or id(view) not in owners or key in keep:
            continue
        owners.discard(id(view))
        if not owners:
            token.cancelled = True
            window._thumb_jobs_inflight.pop(key, None)
    scheduler = getattr(window, '_thumb_scheduler', None)
    if scheduler is not None:
        scheduler.prune()


def retry_allowed(window, key):
    failures = getattr(window, '_thumb_failures', {})
    return failures.get(key, 0) <= time.monotonic()


def thumbnail_failed(window, path, key, error):
    if not hasattr(window, '_thumb_failures'):
        window._thumb_failures = OrderedDict()
    failures = window._thumb_failures
    failures[key] = time.monotonic() + 30
    failures.move_to_end(key)
    while len(failures) > 256:
        failures.popitem(last=False)
    for lookup in getattr(window, '_local_item_lookup', {}).values():
        item = lookup.get(path)
        try:
            if item is not None:
                meta = item.data(QtCore.Qt.UserRole) or {}
                if key in (meta.get('thumb_key'), meta.get('low_thumb_key')):
                    meta['preview_error'] = error
                    item.setData(QtCore.Qt.UserRole, meta)
                    item.setToolTip(path + '\n' + error)
        except RuntimeError:
            pass


def visible_rows(window, view, margin=2):
    """Return model rows using the same geometry for enqueue, low-res and idle."""
    if view is None or not view.isVisible() or getattr(window, '_closing', False):
        return []
    rows = getattr(window, '_local_visible_rows', {}).get(id(view), range(view.count()))
    if not rows:
        return []
    viewport = view.viewport()
    if view.viewMode() == QtWidgets.QListView.ListMode:
        # ListMode may scroll per item rather than per pixel.
        x = max(1, viewport.width() // 2)
        top = next((index.row() for y in (0, view.spacing() + 1, 24)
                    if (index := view.indexAt(QtCore.QPoint(x, y))).isValid()), rows[0])
        bottom = next((index.row() for y in (viewport.height() - 1,
                       viewport.height() - view.spacing() - 2, viewport.height() - 24)
                       if (index := view.indexAt(QtCore.QPoint(x, max(0, y)))).isValid()),
                      min(view.count() - 1, top + viewport.height() // max(1, view.iconSize().height()) + 2))
        # Local grids use IconMode; the decode list has no hidden-row filtering.
        from bisect import bisect_left, bisect_right
        return rows[bisect_left(rows, max(0, top - margin)):bisect_right(rows, bottom + margin)]
    grid = view.gridSize()
    width = grid.width() if grid.width() > 0 else view.iconSize().width() + max(2, view.spacing()) + 8
    height = grid.height() if grid.height() > 0 else view.iconSize().height() + 56
    columns = max(1, viewport.width() // max(1, width))
    first = max(0, view.verticalScrollBar().value() // max(1, height))
    last = first + viewport.height() // max(1, height) + 1 + margin
    return rows[max(0, first - margin) * columns:min(len(rows), last * columns)]


def schedule_view(window, view):
    if getattr(window, '_closing', False):
        return
    if not hasattr(window, '_thumb_view_timers'):
        window._thumb_view_timers = {}
    timer = window._thumb_view_timers.get(id(view))
    if timer is None:
        timer = QtCore.QTimer(window)
        timer.setSingleShot(True)
        timer.setInterval(30)
        timer.timeout.connect(lambda: window._enqueue_visible_thumbnails(view))
        window._thumb_view_timers[id(view)] = timer
    if not timer.isActive():
        timer.start()


class ThumbnailScheduler(QtCore.QObject):
    """Only hand two jobs to Qt; cancelled waiting jobs never enter its queue."""
    def __init__(self, pool, on_drop, parent=None, max_active=2, max_pending=64):
        super().__init__(parent)
        self.pool, self.on_drop = pool, on_drop
        self.max_active, self.max_pending = max_active, max_pending
        self.active, self.pending = {}, OrderedDict()
        self.closed = False

    def _drop(self, key, pair):
        _job, token = pair
        token.cancelled = True
        self.on_drop(key, token)

    def submit(self, key, job, token):
        if self.closed:
            self._drop(key, (job, token))
            return
        if key in self.pending:
            self._drop(key, self.pending.pop(key))
        self.pending[key] = (job, token)
        self.prune()
        while len(self.pending) > self.max_pending:
            stale_key, pair = self.pending.popitem(last=False)
            self._drop(stale_key, pair)
        self._pump()

    def prune(self):
        for key, pair in list(self.pending.items()):
            if pair[1].cancelled:
                self.pending.pop(key)
                self._drop(key, pair)

    def _pump(self):
        self.prune()
        while not self.closed and self.pending and len(self.active) < self.max_active:
            key, pair = self.pending.popitem(last=False)
            job, token = pair
            self.active[id(token)] = (key, pair)
            job.signals.finished.connect(self._finished, QtCore.Qt.QueuedConnection)
            # One sender identifies its exact generation, including cancelled jobs.
            job.signals.setProperty('thumbnailToken', id(token))
            try:
                self.pool.start(job) if self.pool is not None else job.run()
            except Exception:
                self.active.pop(id(token), None)
                self._drop(key, pair)

    @QtCore.pyqtSlot(str, str, str, str)
    def _finished(self, *_args):
        sender = self.sender()
        token_id = sender.property('thumbnailToken') if sender is not None else None
        self.active.pop(token_id, None)
        self._pump()

    def close(self):
        self.closed = True
        for key, pair in list(self.pending.items()) + list(self.active.values()):
            self._drop(key, pair)
        self.pending.clear()
