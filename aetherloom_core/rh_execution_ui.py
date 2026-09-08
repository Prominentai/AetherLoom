"""GUI adapters for the shared executor, independent of individual App pages."""
import copy
import os
import time
import weakref

from PyQt5 import QtCore, QtWidgets, sip

from aetherloom_core.rh_storage import app_output_directories


def ensure_execution_service(owner):
    from aetherloom_core.rh_execution import RhExecutionService
    service = getattr(owner, '_rh_execution_service', None)
    if service is None:
        service = RhExecutionService(owner)
        owner._rh_execution_service = service
    if not hasattr(owner, '_rh_execution_bridge'):
        owner._rh_execution_bridge = AppResultBridge(owner, service)
    return service


def app_snapshot(owner, captured):
    """Normalize an existing App page snapshot without reading it on a worker."""
    parsed = captured.get('parsed') or {}
    wid = str(parsed.get('webappId') or parsed.get('webapp_id') or parsed.get('id')
              or captured.get('webapp_id') or '')
    if not wid:
        raise ValueError('应用缺少 webappId，请重新添加应用')
    title = str(parsed.get('title') or parsed.get('name') or parsed.get('webappName') or wid)
    output_dir, _ = app_output_directories(captured['output_dir'], title, wid)
    return dict(webapp_id=wid, app_name=title, nodes=copy.deepcopy(captured['nodes']),
                base_url=captured['host'], api_key=captured['api_key'],
                api_keys=copy.deepcopy(captured.get('api_keys') or [captured['api_key']]), output_dir=output_dir,
                input_dir=captured.get('input_dir'), decode_settings=copy.deepcopy(captured.get('decode_settings', {})),
                retry_max=captured.get('retry_max', 100), retry_delay=captured.get('retry_delay', 5),
                retry_concurrency=captured.get('retry_concurrency', 25), origin=copy.deepcopy(captured.get('origin') or {}))


def prepare_canvas_app(owner, node, nodes):
    """Capture all credentials/preferences on the GUI thread before graph execution."""
    app = node.get('app') or {}
    wid = str(app.get('webapp_id') or '')
    if not wid:
        raise ValueError('请为节点选择已添加的应用')
    installed = getattr(owner, '_rh_app_paths', {})
    if wid not in installed or not os.path.isfile(installed[wid]):
        raise ValueError('应用未添加或已移除，请先在 RH 应用页添加：' + wid)
    connection = getattr(owner, '_rh_connection_settings', None)
    if connection is not None:
        connection.flush_pending()
    owner._refresh_rh_task_credentials()
    lifecycle = owner._rh_task_lifecycle
    from aetherloom_core.rh_tasks import normalize_base_url
    host = normalize_base_url(app.get('base_url') or owner.rh_host_combo.currentText())
    from aetherloom_core.canvas.model import app_reference
    # Validate saved references even during recovery, which can bypass the
    # editor's normal preflight. Missing legacy URLs use the captured site.
    host = app_reference(dict(app, base_url=host))['base_url']
    with lifecycle.lock:
        key = lifecycle.site_keys.get(host, '')
        keys = list(getattr(lifecycle, 'site_keyrings', {}).get(host) or ([key] if key else []))
    title = str(app.get('name') or node.get('title') or wid)
    directory, _ = app_output_directories(owner.output_dir, title, wid)
    return dict(webapp_id=wid, app_name=title, base_url=host, api_key=key, api_keys=keys,
                nodes=copy.deepcopy(nodes), decode_settings=copy.deepcopy(node.get('decode_settings', {})),
                input_dir=owner.input_dir, output_dir=directory,
                retry_max=getattr(owner, 'rh_retry_max', 100),
                retry_delay=getattr(owner, 'rh_retry_delay', 5),
                retry_concurrency=getattr(owner, 'rh_retry_concurrency', 25))


class AppResultBridge(QtCore.QObject):
    """Project the one task model into lazily created/recreated App output cards."""

    PAGE_SIZE = 60
    FRAME_CARDS = 4

    def __init__(self, owner, service):
        super().__init__(owner)
        self.owner, self.service = owner, service
        self.pages = weakref.WeakValueDictionary()
        self._dirty = set()
        self._select_dirty = set()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        service.changed.connect(self.on_changed)

    @staticmethod
    def _record_group(record):
        status = str(record.get('status') or '').upper()
        if record.get('task_id') or status in {
            'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN', 'PAUSED', 'SKIPPED', 'INTERRUPTED',
            'CANCELING', 'CANCEL_FAILED',
        }:
            return 'active'
        if 'submission_admitted' in record:
            return 'active' if record['submission_admitted'] else 'waiting'
        # Legacy task metadata predates admission tracking. A cloud task or a
        # status beyond local submission is already in the running/results set.
        return 'waiting' if status in {'PENDING', 'PREPARING', 'LOCAL_WAIT', 'SUBMITTING', 'WAITING_FOR_KEY'} else 'active'

    def bind(self, wid, page, add_card, update_card, remove_card=None, output_layout=None):
        wid = str(wid)
        page._rh_shared_add_card = add_card
        page._rh_shared_update_card = update_card
        page._rh_shared_remove_card = remove_card
        page._rh_shared_cards = {}
        page._rh_shared_removed = set()
        page._rh_shared_known = set()
        page._rh_shared_meta = {}
        page._rh_shared_pending = set()
        page._rh_shared_refresh_index = True
        page._rh_shared_selected = []
        page._rh_shared_total = 0
        page._rh_shared_groups = {
            key: {'offset': 0, 'total': 0, 'selected': []}
            for key in ('active', 'waiting')
        }
        page._rh_shared_wid = wid
        page._rh_shared_reflow = lambda: self._place_cards(page)
        page.installEventFilter(self)
        groups = getattr(page, '_rh_output_groups', None)
        if groups is None and output_layout is not None:
            from aetherloom_core.rh_output_groups import RhOutputGroups
            groups = RhOutputGroups(page)
            output_layout.addWidget(groups, 1)
            page._rh_output_groups = groups
        if groups is not None:
            groups.page_requested.connect(lambda key, direction: self._turn_page(wid, direction, key))
            groups.group_toggled.connect(lambda key, expanded: self._group_toggled(wid, key, expanded))
        self.pages[wid] = page
        self.replay(wid)

    def replay(self, wid):
        wid = str(wid)
        page = self.pages.get(wid)
        if page is None or sip.isdeleted(page):
            return
        page._rh_shared_refresh_index = True
        page._rh_shared_pending.update(page._rh_shared_selected)
        self._select_dirty.add(wid)
        self._dirty.add(wid)
        if page.isVisible():
            self._schedule()

    def eventFilter(self, page, event):
        if event.type() == QtCore.QEvent.Show and hasattr(page, '_rh_shared_wid'):
            self.replay(page._rh_shared_wid)
        return False

    def _schedule(self):
        if not self._timer.isActive() and not getattr(self.owner, '_closing', False):
            self._timer.start(16)

    def _turn_page(self, wid, direction, key='active'):
        page = self.pages.get(wid)
        if page is None or sip.isdeleted(page):
            return
        group = page._rh_shared_groups[key]
        group['offset'] = max(0, group['offset'] + direction * self.PAGE_SIZE)
        self._select_dirty.add(wid)
        self._dirty.add(wid)
        groups = getattr(page, '_rh_output_groups', None)
        if groups is not None:
            groups.groups[key].scroll.verticalScrollBar().setValue(0)
        self._schedule()

    def _group_toggled(self, wid, key, expanded):
        page = self.pages.get(wid)
        if page is None or sip.isdeleted(page) or not expanded:
            return
        page._rh_shared_pending.update(page._rh_shared_groups[key]['selected'])
        self._dirty.add(wid)
        self._schedule()

    @staticmethod
    def _expanded(page, key):
        groups = getattr(page, '_rh_output_groups', None)
        return groups is None or groups.is_expanded(key)

    def _update_meta(self, page, run_id, record):
        group = self._record_group(record)
        previous = page._rh_shared_meta.get(run_id)
        if previous and previous[0] == 'active':
            group = 'active'  # Admission is permanent, including late queued signals.
        try:
            created = float(record.get('created_at') or 0)
        except (ValueError, TypeError):
            created = 0.0
        meta = (group, created)
        page._rh_shared_known.add(run_id)
        page._rh_shared_meta[run_id] = meta
        return meta != previous

    def _select(self, wid, page):
        if page._rh_shared_refresh_index:
            for record in self.service.record_headers(wid):
                self._update_meta(page, record['run_id'], record)
            page._rh_shared_refresh_index = False
        for run_id, card in list(page._rh_shared_cards.items()):
            if sip.isdeleted(card):
                page._rh_shared_removed.add(run_id)
        ordered = sorted(page._rh_shared_meta, key=lambda run_id: (page._rh_shared_meta[run_id][1], run_id), reverse=True)
        identities = {'active': [], 'waiting': []}
        for run_id in ordered:
            if run_id not in page._rh_shared_removed:
                identities[page._rh_shared_meta[run_id][0]].append(run_id)
        selected = []
        groups = getattr(page, '_rh_output_groups', None)
        for key, group in page._rh_shared_groups.items():
            total = len(identities[key])
            offset = min(group['offset'], max(0, (total - 1) // self.PAGE_SIZE * self.PAGE_SIZE))
            group.update(offset=offset, total=total, selected=identities[key][offset:offset + self.PAGE_SIZE])
            selected.extend(group['selected'])
            if groups is not None:
                groups.set_page(key, offset, total, self.PAGE_SIZE)
        keep = set(selected)
        previous_selected = set(page._rh_shared_selected)
        page._rh_shared_layout_suspended = True
        for run_id in list(page._rh_shared_cards):
            if run_id not in keep:
                card = page._rh_shared_cards.pop(run_id)
                self.service.bind_card(run_id, None)
                if not sip.isdeleted(card):
                    remove = page._rh_shared_remove_card
                    if remove is not None:
                        remove(card)
                    else:
                        card.hide()
                        card.deleteLater()
        page._rh_shared_layout_suspended = False
        page._rh_shared_selected = selected
        page._rh_shared_pending.intersection_update(keep)
        page._rh_shared_pending.update(keep - previous_selected)
        page._rh_shared_total = sum(group['total'] for group in page._rh_shared_groups.values())
        label = getattr(page, '_rh_result_count_label', None)
        if label is not None:
            label.setText(f'{page._rh_shared_total} 项')
        self._place_cards(page)

    @staticmethod
    def _place_cards(page):
        if getattr(page, '_rh_shared_layout_suspended', False):
            return
        groups = getattr(page, '_rh_output_groups', None)
        if groups is None:
            return
        cards = page._rh_shared_cards
        for key, group in page._rh_shared_groups.items():
            groups.set_cards(key, [cards[run_id] for run_id in group['selected'] if run_id in cards])

    def _card_destroyed(self, wid, run_id, reference):
        if getattr(self.owner, '_closing', False):
            return
        page = self.pages.get(wid)
        if (page is None or sip.isdeleted(page)
                or run_id not in page._rh_shared_cards
                or page._rh_shared_cards.get(run_id) is not reference()):
            return
        page._rh_shared_cards.pop(run_id, None)
        page._rh_shared_removed.add(run_id)
        self.service.bind_card(run_id, None)
        self._select_dirty.add(wid)
        self._dirty.add(wid)
        if page.isVisible():
            self._schedule()

    def _flush(self):
        if getattr(self.owner, '_closing', False):
            return
        remaining = self.FRAME_CARDS
        for wid in list(self._dirty):
            page = self.pages.get(wid)
            if page is None or sip.isdeleted(page) or not page.isVisible():
                self._dirty.discard(wid)
                continue
            if wid in self._select_dirty:
                self._select(wid, page)
                self._select_dirty.discard(wid)
            incomplete = False
            for key, group in page._rh_shared_groups.items():
                if not self._expanded(page, key):
                    continue
                for run_id in group['selected']:
                    if run_id not in page._rh_shared_cards:
                        if not remaining:
                            incomplete = True
                            continue
                        remaining -= 1
                    elif run_id not in page._rh_shared_pending:
                        continue
                    record = self.service.get(run_id)
                    if record is not None:
                        self._render(run_id, record, latest=True)
                    page._rh_shared_pending.discard(run_id)
            self._place_cards(page)
            if not incomplete:
                self._dirty.discard(wid)
        if self._dirty:
            self._schedule()

    @QtCore.pyqtSlot(str, dict)
    def on_changed(self, run_id, record):
        if getattr(self.owner, '_closing', False):
            return
        wid = str(record.get('webapp_id') or '')
        page = self.pages.get(wid)
        if page is None or sip.isdeleted(page):
            return
        if self._update_meta(page, run_id, record):
            self._select_dirty.add(wid)
        if run_id in page._rh_shared_selected:
            page._rh_shared_pending.add(run_id)
        self._dirty.add(wid)
        if not page.isVisible():
            return
        group = page._rh_shared_meta[run_id][0]
        if (self._expanded(page, group) and record.get('status') in {'CANCELING', 'CANCEL_FAILED', 'CANCELED'}
                and run_id in page._rh_shared_cards):
            self._render(run_id, record)
        self._schedule()

    def show_task_details(self, run_id):
        from aetherloom_core.rh_task_details import show_task_details
        return show_task_details(self.owner, self.service, run_id)

    @staticmethod
    def _decode_summary(record):
        decode = (record.get('snapshot') or {}).get('decode_settings')
        if not isinstance(decode, dict):
            return '本次解码：配置未记录', '本次任务未保存完整解码配置。'
        if decode.get('settings_missing'):
            return '本次解码：待补配置', '只补齐此任务缺失的配置，不会读取当前 App 设置。'
        if not decode.get('enabled'):
            return '本次解码：关闭', '本次任务保留原始输出；后续修改 App 设置不会改变本次任务。'
        mode = 'SST' if decode.get('mode') == 'sst' else 'GRC'
        summary = f'本次解码：{mode}'
        if mode == 'GRC':
            summary += f' · {decode.get("grid_cols", 32)} 列'
        original = '删除原图' if decode.get('delete_original', True) else '保留原图'
        return summary, f'解码完成后{original}。配置固定于本次任务发起时。'

    def _render(self, run_id, record, latest=False):
        # Queued worker notifications can arrive after a direct GUI cancel.
        # Always project the current shared state, never replay stale RUNNING.
        if not latest:
            record = self.service.get(run_id) or record
        wid = str(record.get('webapp_id') or '')
        page = self.pages.get(wid)
        if page is None or sip.isdeleted(page):
            return
        cards = page._rh_shared_cards
        card = cards.get(run_id)
        if card is not None and sip.isdeleted(card):
            return  # Removing a card does not cause it to reappear on every progress tick.
        if card is not None and getattr(card, '_rh_shared_revision', None) == record.get('updated_at'):
            return
        if card is None:
            card = page._rh_shared_add_card(None, '准备运行…')
            if card is None:
                return
            cards[run_id] = card
            card.destroyed.connect(lambda _obj=None, w=wid, r=run_id, ref=weakref.ref(card): self._card_destroyed(w, r, ref))
            self.service.bind_card(run_id, card)
            card._rh_run_id = run_id
            card._webapp_id = wid
            card._rh_show_task_details = lambda identity=run_id: self.show_task_details(identity)
            card._rh_shared_paths = set()
            card._rh_shared_started = False
            if not hasattr(self.owner, '_rh_local_cards'):
                self.owner._rh_local_cards = weakref.WeakSet()
            self.owner._rh_local_cards.add(card)
            page._rh_run_enabled = True
            origin = record.get('origin') or {}
            if origin.get('canvas_id'):
                label = QtWidgets.QLabel('画布 · ' + str(origin.get('canvas_name') or origin['canvas_id'])
                                        + ' / ' + str(origin.get('node_title') or origin.get('node_id', '')))
                label.setObjectName('rhMuted')
                label.setWordWrap(True)
                label.setToolTip(str(origin))
                card.layout().insertWidget(1, label)
            decode_label = QtWidgets.QLabel()
            decode_label.setObjectName('rhMuted')
            decode_label.setWordWrap(True)
            card.layout().insertWidget(1, decode_label)
            card._rh_decode_summary_label = decode_label
        summary, summary_hint = self._decode_summary(record)
        decode_label = card._rh_decode_summary_label
        if decode_label.text() != summary:
            decode_label.setText(summary)
            decode_label.setToolTip(summary_hint)
        card._rh_task_document = record.get('task_document')
        task_id = record.get('task_id')
        if task_id:
            card._task_id = str(task_id)
            self.service.bind_card(run_id, card)
        status = record.get('status', 'SUBMITTING')
        card._rh_cancel_requested = bool(record.get('cancel_requested'))
        card._rh_cancel_pending = (status == 'CANCELING' or
                                   (status == 'WAITING_FOR_KEY' and card._rh_cancel_requested))
        card._rh_cancelled = status == 'CANCELED'
        card._rh_run_inflight = status not in {'SUCCESS', 'FAILED', 'CANCELED', 'PAUSED', 'UNKNOWN', 'INTERRUPTED'}
        card._rh_output_files = tuple(record.get('output_files') or ())
        card._rh_input_files = tuple(record.get('input_files') or ())
        directory = record.get('output_dir') or (record.get('snapshot') or {}).get('output_dir')
        if directory:
            card._rh_output_roots = (directory,)
        from aetherloom_core.rh_progress import update_card_progress
        progress = record.get('node_progress') or record.get('progress')
        if progress is not None and not isinstance(progress, dict):
            progress = {'percent': progress, 'value': progress, 'maximum': 100}
        update_card_progress(self.owner, card, status, progress)
        message = record.get('message')
        widget = getattr(card, '_rh_progress_widget', None)
        if widget is not None and message:
            widget.set_message(str(message))
        update = page._rh_shared_update_card
        if status == 'RUNNING' and not card._rh_shared_started:
            card._rh_shared_started = True
            update(card, None, {'timer_start': time.time()})
        if status == 'SUCCESS':
            card._rh_results_ready = True
            for result in record.get('results') or ():
                path = result.get('path')
                if path and path not in card._rh_shared_paths:
                    card._rh_shared_paths.add(path)
                    update(card, path, os.path.basename(path))
            if message and card._rh_shared_paths:
                card.setToolTip(str(message))
        if status in {'SUCCESS', 'FAILED', 'CANCELED', 'UNKNOWN', 'PAUSED', 'INTERRUPTED'}:
            update(card, None, {'timer_stop': True})
        card._rh_shared_revision = record.get('updated_at')


def install_canvas_page(owner):
    """Called once after the regular pages and RH lifecycle are ready."""
    from aetherloom_core.canvas.page import CanvasPage
    service = ensure_execution_service(owner)
    owner._canvas_prepare_app = lambda node, nodes: prepare_canvas_app(owner, node, nodes)
    owner.canvas_page = CanvasPage(owner, service, prepare_app=owner._canvas_prepare_app)
    owner.pages.addWidget(owner.canvas_page)

    def selected():
        for button in owner._sidebar_buttons:
            button.setChecked(button is owner.canvas_btn)
        owner.pages.setCurrentWidget(owner.canvas_page)

    owner.canvas_btn.clicked.connect(selected)
    owner.pages.currentChanged.connect(
        lambda _: owner.canvas_btn.setChecked(owner.pages.currentWidget() is owner.canvas_page))

    def restore_selected_canvas():
        if getattr(owner, '_closing', False):
            return
        page = owner.canvas_page
        try:
            errors = page.workflow_queue.recover()
            page.enable_selected_recovery()
            if errors:
                page._message('部分工作流队列暂未恢复，可打开工作流队列查看状态。')
        except (OSError, ValueError, RuntimeError) as error:
            page._message('工作流队列恢复暂未完成：' + str(error))
        finally:
            start_recovery = getattr(owner, '_rh_start_recovery_worker', None)
            if callable(start_recovery):
                owner._rh_start_recovery_worker = None
                start_recovery()
        page._sync_actions()

    # Run only once the window, installed App definitions and task observers are
    # ready. Credentials are captured on this GUI thread; workers use snapshots.
    QtCore.QTimer.singleShot(0, restore_selected_canvas)
