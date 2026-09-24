"""Local input freezing and one-shot execution for the in-memory image queue."""
import copy
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from aetherloom_core import mask_assets
from . import billing, catalog, client, composition, storage, task_records


MAX_INPUT_BYTES = 32 * 1024 * 1024


def check_stop(job):
    if job.stop.is_set():
        raise InterruptedError('任务已取消，尚未提交生成请求。')


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def capture_sources(snapshot, draft):
    """Only small stat calls run on enqueue; image reads/copies happen in stage."""
    action = snapshot.get('action', 'generate')
    image = snapshot.get('image_path')
    uses_image = action in ('img2img', 'infill', 'augment', 'upscale')
    matching = bool(uses_image and image and draft and mask_assets.matches(draft, image))
    bindings = []
    if uses_image and image:
        bindings.append((('snapshot', 'image_path'), image, False))
    if action == 'infill' and not matching and snapshot.get('mask_path'):
        bindings.append((('snapshot', 'mask_path'), snapshot['mask_path'], False))
    if action in ('generate', 'img2img', 'infill'):
        for index, reference in enumerate(snapshot.get('references', [])):
            if not isinstance(reference, dict):
                raise ValueError('参考图参数必须是对象。')
            if reference.get('enabled', True) and reference.get('path'):
                bindings.append((('snapshot', 'references', index, 'path'), reference['path'], False))
    if matching:
        for field, inline in (('path', 'png'), ('paint_path', 'paint_png')):
            if draft.get(field):
                bindings.append((('draft', field), draft[field], bool(draft.get(inline))))
    captured = []
    for binding, source, optional in bindings:
        source = os.path.abspath(os.fspath(source))
        try:
            metadata = os.stat(source)
        except FileNotFoundError:
            if not optional:
                raise ValueError('输入文件不存在：' + source) from None
            captured.append((binding, source, None))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('输入必须是普通文件：' + source)
        if metadata.st_size > MAX_INPUT_BYTES:
            raise ValueError('输入图像超过 32 MB：' + source)
        captured.append((binding, source, _signature(metadata)))
    return {'bindings': captured, 'matching_draft': matching}


class FrozenInputs:
    def __init__(self, temporary, snapshot, draft):
        self.temporary, self.snapshot, self.draft = temporary, snapshot, draft
        self._cleanup_lock = threading.Lock()
        self.input_size = None

    @property
    def directory(self):
        return self.temporary.name

    def close(self):
        with self._cleanup_lock:
            if self.temporary is None:
                return
            target = Path(self.temporary.name).resolve()
            expected = Path(tempfile.gettempdir()).resolve()
            if target.parent != expected or not target.name.startswith('aetherloom-novelai-queue-'):
                raise ValueError('拒绝清理任务临时目录以外的路径。')
            temporary, self.temporary = self.temporary, None
        temporary.cleanup()


def _assign(data, binding, value):
    record = data
    for key in binding[:-1]:
        record = record[key]
    if value is None:
        record.pop(binding[-1], None)
    else:
        record[binding[-1]] = value


def freeze_inputs(context, job):
    """One stage worker copies bytes, without decoding or keeping PIL images."""
    check_stop(job)
    temporary = tempfile.TemporaryDirectory(prefix='aetherloom-novelai-queue-')
    frozen = FrozenInputs(temporary, copy.deepcopy(context['snapshot']), copy.deepcopy(context['draft']))
    data = {'snapshot': frozen.snapshot, 'draft': frozen.draft}
    copied = {}
    try:
        for index, (binding, source, signature) in enumerate(context['sources']['bindings']):
            check_stop(job)
            if signature is None:
                # At enqueue the file was absent and the draft had embedded data.
                _assign(data, binding, None)
                continue
            key = os.path.normcase(source)
            if key not in copied:
                destination = Path(frozen.directory) / f'frozen-{index}{Path(source).suffix}'
                try:
                    with open(source, 'rb') as incoming:
                        if _signature(os.fstat(incoming.fileno())) != signature:
                            raise ValueError('输入文件在入队后发生变化，请重新加入队列：' + source)
                        with destination.open('xb') as outgoing:
                            count = 0
                            while True:
                                check_stop(job)
                                block = incoming.read(1024 * 1024)
                                if not block:
                                    break
                                count += len(block)
                                if count > MAX_INPUT_BYTES:
                                    raise ValueError('输入图像超过 32 MB：' + source)
                                outgoing.write(block)
                        if (_signature(os.fstat(incoming.fileno())) != signature
                                or _signature(os.stat(source)) != signature or count != signature[2]):
                            raise ValueError('固化期间输入文件发生变化，请重新加入队列：' + source)
                except FileNotFoundError:
                    raise ValueError('输入文件在入队后已移除，请重新加入队列：' + source) from None
                copied[key] = (str(destination), signature)
            if copied[key][1] != signature:
                raise ValueError('输入文件在入队期间发生变化，请重新加入队列：' + source)
            _assign(data, binding, copied[key][0])
        if context['sources']['matching_draft']:
            frozen.draft['source'] = frozen.snapshot['image_path']
        check_stop(job)
        frozen.input_size = input_size(frozen.snapshot, frozen.draft)
        context['frozen'] = frozen
        return frozen
    except BaseException:
        frozen.close()
        raise



def normalize_options(snapshot):
    """Apply the same action overrides for estimates and actual execution."""
    options = copy.deepcopy(snapshot)
    if options.get('action') in ('upscale', 'augment'):
        options['references'], options['characters'], options['n_samples'] = [], [], 1
        if options['action'] == 'upscale':
            options['model'] = client.UPSCALE_MODEL
    return options


def input_size(snapshot, draft=None):
    """Read tool dimensions in a worker, without decoding or retaining pixels."""
    if snapshot.get('action') not in ('upscale', 'augment') or not snapshot.get('image_path'):
        return None
    try:
        with Image.open(snapshot['image_path']) as image:
            width, height = image.size
            if image.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
        if draft and mask_assets.matches(draft, snapshot['image_path']):
            orientation = int(draft.get('orientation', 0))
            if not 0 <= orientation <= 7:
                return None
            if orientation in (3, 5, 6, 7):
                width, height = height, width
        return width, height
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        # The normal request validation will report the invalid input. Until then,
        # a missing size must produce an unknown estimate, never a guessed fee.
        return None


def estimate_cost(options, account_evidence=None, size=None, *, now=None):
    """Estimation is advisory and must never cause or repeat a network request."""
    try:
        return billing.estimate(options, account_evidence=account_evidence, input_size=size, now=now)
    except Exception:
        return {'category': 'unknown', 'reason': '暂时无法核实此任务的计费条件。',
                'extras': '', 'parallel_eligible': False}


def _close_focused(focused):
    if focused:
        for image in focused[:2]:
            image.close()


def _prepare(context):
    # Rejected attempts reuse prepared assets; painted masks must not be saved
    # again and random/preprocessing inputs must not change between keys.
    cached = context.get('prepared_inputs')
    if cached is not None:
        focused = None
        if cached['focused'] is not None:
            base_path, mask_path, box = cached['focused']
            with Image.open(base_path) as image:
                base = image.copy()
            try:
                with Image.open(mask_path) as image:
                    mask_image = image.copy()
            except BaseException:
                base.close()
                raise
            focused = (base, mask_image, box)
        return copy.deepcopy(cached['options']), copy.deepcopy(cached['mask']), focused
    frozen = context['frozen']
    prepared = Path(frozen.directory) / 'prepared'
    prepared.mkdir(exist_ok=True)
    options, mask, focused = composition.prepare(frozen.snapshot, frozen.draft,
                                                  context['input_dir'], str(prepared))
    if mask:
        mask['source'] = context['snapshot'].get('image_path', mask.get('source', ''))
    focused_files = None
    try:
        if focused:
            base_path, mask_path = prepared / 'compose-base.png', prepared / 'compose-mask.png'
            focused[0].save(base_path, format='PNG')
            focused[1].save(mask_path, format='PNG')
            focused_files = (str(base_path), str(mask_path), focused[2])
        context['prepared_inputs'] = dict(options=copy.deepcopy(options), mask=copy.deepcopy(mask),
                                          focused=focused_files)
    except BaseException:
        _close_focused(focused)
        raise
    return options, mask, focused


def _history(context, records):
    if not records:
        return ''
    try:
        storage.update_history(context['data_dir'], additions=records)
        return ''
    except Exception as error:
        return '图片已保存，但历史索引保存失败：' + str(error)


def _record_saved(context, records, *, complete=False, message=''):
    record = context.get('record')
    if record is None:
        return
    previous = record.get_task()
    combined = {item.get('id', item.get('path')): item for item in previous.get('results', [])}
    combined.update({item.get('id', item.get('path')): item for item in records})
    results = list(combined.values())
    expected = previous.get('expected_results', 1)
    finished = (complete and isinstance(expected, int) and not isinstance(expected, bool)
                and expected > 0 and len(results) == expected)
    task = {'results': results, 'saved_results': len(results),
            'state': 'succeeded' if finished else 'save_failed', 'result_unknown': False}
    if finished:
        task.update(finished=time.time(), received_results=len(results),
                    message=message or f'已保存 {len(results)} 张图片。')
    elif message:
        task['message'] = message
    record.update(task=task, complete=finished)


def _save(context, directory, snapshot, results, mask):
    try:
        records = storage.save_results(directory, snapshot, results)
        valid = [record for record in records if Path(record.get('path', '')).is_file()
                 and Path(record['path']).stat().st_size > 0]
        if len(valid) != len(results):
            raise storage.SaveError('生成结果尚未全部成功保存到本地。', [], results)
    except storage.SaveError as error:
        error.snapshot, error.mask = snapshot, mask
        error.needs_composition = False
        _record_saved(context, error.saved, message=str(error))
        _history(context, error.saved)
        raise
    except Exception as error:
        # output_directory() itself can fail before save_results wraps failures.
        failure = storage.SaveError('图片已生成，但本地保存失败：' + str(error), [], results)
        failure.snapshot, failure.mask, failure.needs_composition = snapshot, mask, False
        _record_saved(context, [], message=str(failure))
        raise failure from None
    warning = _history(context, records)
    _record_saved(context, records, complete=True, message=warning)
    return {'records': records, 'mask': mask, 'warning': warning}


def _request_progress(context, job, value):
    # Set safety/audit latches synchronously, even if Qt stops delivering events.
    phase = value.get('phase') if isinstance(value, dict) else ''
    if phase == 'accepted':
        context['accepted'] = True
    record = context.get('record')
    if record is not None and phase in ('submitted', 'accepted', 'encoding'):
        update = {'submitted': True}
        if phase == 'accepted':
            update['accepted'] = True
        if context.get('_shutdown') is not None and context['_shutdown'].is_set():
            update['result_unknown'] = True
        record.update(task=update)
    job.emit_safe('progress', value)


def _record_request(context, job, metadata, mask):
    record = context.get('record')
    metadata['input_files'] = {field: context['snapshot'].get(field, '') for field in ('image_path', 'mask_path')}
    metadata['references'] = copy.deepcopy(context['snapshot'].get('references', []))
    if record is not None:
        record.update(request=metadata,
                      copy_value=task_records.copy_from_request(context['snapshot'], mask or context.get('draft'), metadata),
                      wait=True)
    check_stop(job)
    job.emit_safe('progress', {'phase': 'request', 'request': metadata})


def execute(context, job):
    """Run generation once. Editor settings are owned by the page, not workers."""
    check_stop(job)
    if context.get('record') is not None:
        context['record'].update(wait=True)
    check_stop(job)
    storage.verify_output_directory(context['output_dir'])
    focused = None
    try:
        options, mask, focused = _prepare(context)
        options = catalog.validate_options(normalize_options(options))
        durable = storage.public(copy.deepcopy(context['snapshot']))
        if options['action'] in ('upscale', 'augment'):
            durable.update(model=options['model'], n_samples=1)
        if mask:
            durable['mask_reference'] = mask
        check_stop(job)
        results = client.run_image(options, context['token'], stop=job.stop,
            on_progress=lambda value: _request_progress(context, job, value),
            on_preview=lambda value: job.emit_safe('preview', value),
            cache_dir=Path(context['data_dir']) / 'novelai' / 'vibe_cache',
            request_state=context.setdefault('request_state', {}),
            task_cache_dir=Path(context['frozen'].directory) / 'vibe-results',
            on_request=lambda value: _record_request(context, job, value, mask))
        received = len(results)
        if not received:
            raise RuntimeError('NovelAI 未返回任何最终图片；未自动重新提交。')
        counts = {'received_results': received}
        if options['action'] == 'augment':
            # Director tools may return multiple images. Fix the count once
            # the complete response arrives, before saving or retrying a save.
            context['expected_results'] = received
            counts['expected_results'] = received
        if context.get('record') is not None:
            context['record'].update(task=dict(state='saving', result_unknown=False, **counts))
        job.emit_safe('progress', dict(phase='saving', message='已收到图片，正在本地处理并保存…', **counts))
        # Once complete results arrive, cancellation must not discard their bytes.
        try:
            composed = composition.compose_focused(results, focused)
        except Exception as error:
            failure = storage.SaveError('图片已生成，但局部合成失败，请重新保存：' + str(error), [], results)
            failure.snapshot, failure.mask, failure.needs_composition = durable, mask, True
            _record_saved(context, [], message=str(failure))
            raise failure from None
        return _save(context, context['output_dir'], durable, composed, mask)
    finally:
        _close_focused(focused)


def resave(context, directory, job):
    """Only compose/write previously received bytes; never call the image API."""
    snapshot, pending = context['recovery_snapshot'], context['pending']
    mask, focused = context.get('mask'), None
    try:
        if context.get('needs_composition'):
            try:
                unused, unused_mask, focused = _prepare(context)
                pending = composition.compose_focused(pending, focused)
            except Exception as error:
                failure = storage.SaveError('局部合成仍未完成：' + str(error), [], context['pending'])
                failure.snapshot, failure.mask, failure.needs_composition = snapshot, mask, True
                _record_saved(context, [], message=str(failure))
                raise failure from None
        return _save(context, directory, snapshot, pending, mask)
    finally:
        _close_focused(focused)
