"""GUI-thread handoff to the shared NovelAI queue; no second HTTP executor."""
import copy
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PyQt5 import QtCore

from . import model, novelai_nodes


STATES = {'queued': 'LOCAL_WAIT', 'preparing': 'LOCAL_WAIT', 'ready': 'QUEUED',
          'retry_wait': 'QUEUED', 'running': 'RUNNING', 'saving': 'DOWNLOADING',
          'save_failed': 'DOWNLOAD_FAILED', 'canceling': 'CANCELING',
          'succeeded': 'SUCCESS', 'failed': 'FAILED', 'cancelled': 'CANCELED'}
TERMINAL = {'succeeded', 'failed', 'cancelled'}


class RunError(RuntimeError):
    def __init__(self, message, status='failed'):
        super().__init__(message)
        self.status = status


@dataclass
class Handoff:
    key: tuple
    source: dict
    options: dict
    prepared: dict
    stop: threading.Event
    closing: object = None
    abandoned: threading.Event = field(default_factory=threading.Event)
    task_id: str = ''
    task: dict = field(default_factory=dict)
    error: str = ''
    ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.task), self.error


def create(owner, kind):
    """Copy page settings once; subsequent edits belong solely to this node."""
    from aetherloom_core.novelai import storage
    from aetherloom_core.paths import current_dir
    workspace = getattr(getattr(owner, 'novelai_page', None), 'workspace', None)
    from aetherloom_core.novelai.service import existing_service
    service = existing_service(owner)
    options = (workspace._options() if workspace is not None else
               storage.load_settings(service.data_dir if service is not None else current_dir).get('options', {}))
    params = novelai_nodes.default_params(kind)
    params['options'].update(copy.deepcopy(options))
    params['options']['action'] = novelai_nodes.KIND_ACTIONS[kind]
    params['options']['enhancement'] = kind == 'novelai_enhance'
    if workspace is not None:
        params['options']['image_path'] = str(getattr(workspace, '_input', '') or '')
        # Only durable masks enter the graph. Unsaved drawing bytes remain on
        # their originating page until explicitly saved there.
        params['options']['mask_path'] = str(workspace.controls._raw.get('mask_path', '') or '')
    if kind == 'novelai_upscale':
        params['options']['model'] = novelai_nodes.UPSCALE_MODEL
    return model.new_node(kind, params=params)


def ensure(owner):
    bridge = getattr(owner, '_canvas_novelai_runtime', None)
    if bridge is None:
        bridge = owner._canvas_novelai_runtime = NovelAIRuntime(owner)
    return bridge


class NovelAIRuntime(QtCore.QObject):
    preview = QtCore.pyqtSignal(object, object)
    _submit = QtCore.pyqtSignal(object)
    _cancel = QtCore.pyqtSignal(str)
    _release = QtCore.pyqtSignal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.queue = None
        self._by_key, self._by_id = {}, {}
        self._submit.connect(self._enqueue, QtCore.Qt.QueuedConnection)
        self._cancel.connect(self._cancel_task, QtCore.Qt.QueuedConnection)
        self._release.connect(self._detach, QtCore.Qt.QueuedConnection)

    def prepare(self, node):
        from aetherloom_core.novelai.credentials import tokens_for
        from aetherloom_core.novelai.service import get_service
        if self.queue is None:
            self.queue = get_service(self.owner)
            self.queue.changed.connect(self._sync)
            self.queue.preview.connect(self._preview)
        keys = tokens_for(self.owner)
        if not keys:
            raise ValueError('请先在 NovelAI 页的连接设置中添加 API Key。')
        return dict(novelai_node=True, api_keys=keys, data_dir=self.queue.data_dir,
                    output_dir=str(self.owner.output_dir), input_dir=str(self.owner.input_dir))

    @QtCore.pyqtSlot(object)
    def _enqueue(self, entry):
        # The stop event is shared with the scheduler. A stop which wins this
        # race must prevent enqueue, including before a local task ID exists.
        if entry.stop.is_set() or entry.abandoned.is_set() or entry.closing is not None and entry.closing.is_set():
            entry.error = '已停止，未提交 NovelAI 任务。'
            entry.ready.set()
            return
        existing = self._by_key.get(entry.key)
        if existing is not None:
            entry.error = '此执行项已提交，禁止重复生成。'
            entry.ready.set()
            return
        try:
            if self.queue is None or self.queue._closed:
                raise RuntimeError('NovelAI 队列未准备完成或客户端正在关闭。')
            self._by_key[entry.key] = entry
            p = entry.prepared
            identity = self.queue.enqueue(entry.options, None, p['api_keys'], p['output_dir'],
                                      p['input_dir'], p['data_dir'], source=entry.source,
                                      batch={'id': ':'.join(map(str, entry.key[:-1])),
                                             'index': entry.key[-1], 'count': entry.source['item_count']})
            entry.task_id = identity
            self._by_id[entry.task_id] = entry
            if entry.stop.is_set() or entry.abandoned.is_set() or entry.closing is not None and entry.closing.is_set():
                self.queue.cancel(entry.task_id)
            self._sync()
        except Exception as error:
            entry.error = str(error)
            self._by_key.pop(entry.key, None)
        finally:
            # Credentials and potentially large reference arrays need not live
            # twice once the queue has taken its immutable snapshot.
            entry.prepared = {}
            entry.options = {}
            entry.ready.set()

    @QtCore.pyqtSlot()
    def _sync(self):
        if self.queue is None:return
        for identity, entry in tuple(self._by_id.items()):
            task = self.queue.get_task(identity)
            with entry.lock:
                if task is None:
                    if entry.task.get('state') not in TERMINAL:
                        entry.error = 'NovelAI 任务已被移除，未重新提交。'
                    continue
                entry.task = {key: copy.deepcopy(task.get(key)) for key in
                    ('id', 'state', 'message', 'progress', 'accepted', 'submitted',
                     'result_unknown', 'attempts', 'key_index', 'key_count')}
                # Result settings already live in the queue's task JSON. Canvas
                # updates carry references only, not another prompt/reference
                # snapshot per output image on every progress notification.
                entry.task['results'] = [{key: record[key] for key in
                    ('id','path','index','seed','width','height') if key in record}
                    for record in task.get('results') or []]

    @QtCore.pyqtSlot(str, object)
    def _preview(self, identity, value):
        entry = self._by_id.get(identity)
        if entry is not None and not entry.stop.is_set():
            self.preview.emit(entry.source, value)

    @QtCore.pyqtSlot(str)
    def _cancel_task(self, identity):
        if self.queue is not None:self.queue.cancel(identity)

    def cancel(self, identity):
        self._cancel.emit(str(identity))

    @QtCore.pyqtSlot(object)
    def _detach(self, entries):
        for entry in entries:
            if self._by_key.get(entry.key) is entry:self._by_key.pop(entry.key, None)
            if self._by_id.get(entry.task_id) is entry:self._by_id.pop(entry.task_id, None)

    def execute(self, node, prepared, batches, source, stop, update, closing=None):
        """Worker-only wait. All QObject mutations stay on the GUI thread."""
        if not prepared or prepared.get('_preparation_error'):
            raise ValueError((prepared or {}).get('_preparation_error') or 'NovelAI 连接未准备完成')
        requests = novelai_nodes.build_requests(node, batches)
        entries, last_revision = [], None

        def check():
            nonlocal last_revision
            if stop.is_set() or closing is not None and closing.is_set():
                raise RunError('已停止本地等待；已送达 NovelAI 的请求可能仍在处理。', 'canceled')
            snapshots = [entry.snapshot() for entry in entries]
            items = []
            for index, (task, error) in enumerate(snapshots):
                state = 'UNKNOWN' if task.get('result_unknown') else STATES.get(task.get('state'), 'SUBMITTING')
                item = dict(run_id=entries[index].task_id, backend='novelai', task_id='',
                            status=state, batch_index=index, message=task.get('message') or error or '',
                            accepted=bool(task.get('accepted')), results=task.get('results') or [])
                items.append(item)
            revision = [(i['run_id'], i['status'], i['message'], len(i['results']),
                         (snapshots[n][0].get('progress') or {}).get('progress')) for n,i in enumerate(items)]
            if revision != last_revision:
                last_revision = revision
                active = [i for i in items if i['status'] != 'SUCCESS']
                current = next((i for i in active if i['status'] in {'RUNNING','DOWNLOADING','DOWNLOAD_FAILED'}),
                               active[0] if active else (items[-1] if items else {}))
                index = items.index(current) if current else 0
                progress = ((snapshots[index][0].get('progress') or {}).get('progress') if snapshots else None)
                phase = current.get('status','QUEUED')
                if phase == 'SUCCESS':phase = 'DOWNLOADING'
                update(items=items, status=phase, activated=True,
                       progress=progress * 100 if isinstance(progress,(float,int)) else None,
                       node_progress=progress * 100 if isinstance(progress,(float,int)) else None,
                       message=current.get('message') or '等待 NovelAI 队列')
            for task, error in snapshots:
                if error:raise RunError(error)
                if task.get('state') in {'failed','cancelled'}:
                    raise RunError(task.get('message') or 'NovelAI 任务未成功',
                                   'unknown' if task.get('result_unknown') else
                                   'canceled' if task['state']=='cancelled' else 'failed')
            return snapshots

        try:
            for index, options in enumerate(requests):
                check()
                origin = dict(source, type='canvas', node_id=node['id'], node_title=model.node_title(node),
                              item_index=index, item_count=len(requests))
                key = (source['canvas_id'], source['round_id'], source.get('canvas_batch_index',0), node['id'],index)
                entry = Handoff(key, origin, options, prepared, stop, closing=closing)
                entries.append(entry)
                self._submit.emit(entry)
                while not entry.ready.wait(.1):check()
                check()
                # Preserve enqueue ordering while still overlapping accepted
                # generation with the following item when the provider allows.
                while True:
                    snapshots = check()
                    task = snapshots[-1][0]
                    if task.get('accepted') or task.get('state') == 'succeeded':break
                    stop.wait(.1)
            while True:
                snapshots = check()
                if all(task.get('state')=='succeeded' for task, _ in snapshots):break
                stop.wait(.1)
            results = []
            for index,(task,_) in enumerate(snapshots):
                for position,record in enumerate(task.get('results') or []):
                    path = str(record.get('path') or '')
                    if not path or not Path(path).is_file():
                        raise RunError('NovelAI 结果文件已丢失，未重新提交生成。')
                    results.append(dict(path=path,type='image',name=Path(path).name,
                        index=len(results),generation=task['id'],batch_index=index,
                        output_index=position,lineage=model.result_lineage(batches[index],node['id'],f'{index}:{position}')))
            return results
        except BaseException:
            for entry in entries:
                entry.abandoned.set()
                if entry.task_id:self.cancel(entry.task_id)
            raise
        finally:
            self._release.emit(entries)
