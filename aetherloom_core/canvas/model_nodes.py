"""Model-node configuration snapshots and bounded, cancellable local dispatch."""
import copy
import os
import queue
import threading
import uuid
from pathlib import Path

from api_calls.provider_client import ProviderAPIError, complete_text
from . import model

_slots = threading.BoundedSemaphore(4)
PUBLIC_FIELDS = ('provider', 'protocol', 'endpoint', 'model', 'timeout', 'web_search', 'merge_system_prompt')


def public_config(value):
    return {key: copy.deepcopy(value[key]) for key in PUBLIC_FIELDS if key in value}


def connection(owner, category, identity=None, *, allow_unconfigured=False):
    """GUI-thread only. Resolve public settings from a local, known connection."""
    from aetherloom_core import api_manager
    fields = owner.api_config_fields[category]
    active = fields['provider'].currentData() or 'custom'
    if active == 'custom':active = 'custom_' + category
    identity = identity or active
    entry = api_manager.find_provider(category, 'custom' if identity.startswith('custom_') else identity)
    if not entry:
        if allow_unconfigured:
            return {}
        raise ValueError('此模型连接在本机不存在，请在 API 管理中添加后重新选择。')
    profile = owner._get_api_provider_profile(category, identity)
    if identity == active:
        result = dict(owner._api_probe_controllers[category].snapshot(), provider=identity)
    else:
        result = dict(provider=identity, endpoint=profile.get('endpoint') or entry.get('endpoint', ''),
                      model=profile.get('model', ''), timeout=profile.get('timeout', 90))
        if 'web_search' in profile:result['web_search'] = profile['web_search']
    result['protocol'] = api_manager.effective_protocol(category, identity, profile)
    return public_config(result)


def create(owner, kind):
    category = model.MODEL_KINDS[kind]
    # Creating/editing a graph must not require a configured provider. Only
    # execution resolves credentials and enforces a usable local connection.
    config = connection(owner, category, allow_unconfigured=True)
    node = model.new_node(kind, model_config=config)
    if kind == 'llm_model':
        from .text_processing import initialize
        node['params'] = initialize(node['params'], getattr(owner, 'settings', {}))
    if category in ('text2img', 'image_edit'):
        from aetherloom_core.image_prompts import request_options
        options = request_options(owner.settings, category, config.get('protocol', ''))
        node['params']['system_prompt'] = options.get('system_prompt', '')
        if 'merge_system_prompt' in options:node['model_config']['merge_system_prompt'] = options['merge_system_prompt']
    return node


def prepare(owner, node):
    category = model.MODEL_KINDS[node['kind']]
    config = public_config(node.get('model_config', {}))
    if not config.get('provider'):
        raise ValueError('请先在节点设置的“模型设置”页选择连接和模型；尚无连接时，请到 API 管理中添加。')
    local = connection(owner, category, config['provider'])
    # Imported JSON must not redirect a saved key to a different host/path.
    # Model-dependent official image endpoints may legitimately differ.
    endpoint = local.get('endpoint', '')
    if category in ('text2img', 'image_edit'):
        from aetherloom_core.image_model_catalog import follow_model_endpoint
        endpoint = follow_model_endpoint(local['protocol'], category, config.get('model', ''), endpoint)
    if config.get('endpoint') != endpoint or config.get('protocol') != local['protocol']:
        raise ValueError('模型连接地址或协议与本机配置不一致，请重新选择该连接。')
    if not config.get('model'):
        raise ValueError('请在节点设置中选择或填写模型名称。')
    from aetherloom_core.api_credentials import get_credentials
    config.update(get_credentials(getattr(owner, '_apikeys', {}), config['provider'], category))
    return dict(model_config=config, output_dir=str(Path(owner.output_dir) / 'models'), model_node=True)


def _check_stop(stop):
    if stop.is_set():
        raise ProviderAPIError('已停止本地等待；已送达供应商的请求可能仍在处理，不会自动重发。', status='canceled')


def _bounded_call(operation, stop, release=None):
    """At most four in-flight HTTP operations, even after repeated cancellations."""
    acquired=False
    try:
        while not _slots.acquire(timeout=.1):_check_stop(stop)
        acquired=True
        _check_stop(stop)
    except BaseException:
        if acquired:_slots.release()
        if release:release()
        raise
    result = queue.Queue(maxsize=1)
    def worker():
        try:result.put((True, operation()))
        except Exception as error:result.put((False, error))
        finally:
            if release:release()
            _slots.release()
    try:threading.Thread(target=worker, name='canvas-model-http', daemon=True).start()
    except Exception:
        if release:release()
        _slots.release();raise
    while True:
        _check_stop(stop)
        try:ok, value = result.get(timeout=.1)
        except queue.Empty:continue
        _check_stop(stop)
        if not ok:raise value
        return value


def execute(node, prepared, batches, stop, temporary=None):
    if not prepared or prepared.get('_preparation_error'):
        raise ValueError((prepared or {}).get('_preparation_error') or '模型连接未准备完成')
    config = copy.deepcopy(prepared['model_config'])
    category = model.MODEL_KINDS[node['kind']]
    if node['kind'] == 'llm_model':
        from .text_processing import validate
        validate(node.get('params', {}))
    timeout = int(config.get('timeout') or 90)
    if not 1 <= timeout <= 3600:raise ValueError('模型请求超时需在 1–3600 秒之间')
    all_results = []
    generation = uuid.uuid4().hex
    # Check every group before any provider request; an oversized later Batch
    # must not leave an earlier group already billed.
    prompts = []
    for batch in batches:
        image = batch.get('image', {})
        if model.result_type(image) == 'batch':
            members = model.batch_items(image)
            if any(model.result_type(value) != 'image' for value in members):raise ValueError('图像 Batch 只能包含图像。')
            if category == 'image_edit':
                from aetherloom_core.image_model_catalog import validate_edit_input_count
                validate_edit_input_count(config, len(members))
        prompt = (model.input_value(batch['prompt'], {'fieldType': 'STRING', '_canvas_text_batch': True}) if 'prompt' in batch
                  else node.get('params', {}).get('prompt', ''))
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('请填写或连接文本' if category == 'llm' else '请填写或连接提示词')
        prompts.append(prompt)
    for batch, prompt in zip(batches, prompts):
        _check_stop(stop)
        image = batch.get('image', {})
        grouped = model.result_type(image) == 'batch'
        images = model.batch_items(image) if grouped else [image] if 'image' in batch else []
        if any(model.result_type(value) != 'image' for value in images):raise ValueError('图像编辑的 Batch 必须全部为图像')
        paths = [value.get('path') or value.get('file_path') or '' for value in images]
        if not images and node.get('params', {}).get('image'):paths = [node['params']['image']]
        if category in ('vision', 'image_edit') and (not paths or any(not path or not os.path.isfile(path) for path in paths)):
            raise ValueError('请连接图像导入节点或选择有效的本地图像')
        path = paths[0] if paths else ''
        system = node.get('params', {}).get('system_prompt', '')
        def operation():
            _check_stop(stop)
            if category in ('llm', 'vision'):
                from api_calls.call_vision import _encode_image_data
                text = complete_text(config['endpoint'], config.get('api_key', ''), config['model'], system, prompt,
                    provider=config['protocol'], timeout=timeout, web_search=config.get('web_search'),
                    image=([_encode_image_data(value) for value in paths] if grouped else _encode_image_data(path)) if category == 'vision' else None)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('模型未返回有效文本，请检查模型响应。')
                return [('text', text.encode('utf-8'))]
            from api_calls.call_images import generate
            return generate(config, prompt, (paths if grouped else path) if category == 'image_edit' else None,
                            system_prompt=system, timeout=timeout, stop=stop)
        lease=temporary.retain(paths) if temporary and paths else set()
        outputs = _bounded_call(operation, stop, (lambda:temporary.release(lease)) if lease else None)
        _check_stop(stop)
        directory = Path(prepared['output_dir']) / category
        directory.mkdir(parents=True, exist_ok=True)
        for mime, raw in outputs:
            _check_stop(stop)
            suffix = {'text': '.txt', 'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif'}[mime]
            target = directory / (uuid.uuid4().hex + suffix)
            staging = target.with_suffix(suffix + '.part')
            try:
                with staging.open('xb') as stream:stream.write(raw)
                _check_stop(stop)
                os.replace(staging, target)
            finally:
                staging.unlink(missing_ok=True)
            item = dict(path=str(target), type='text' if mime == 'text' else 'image',
                        index=len(all_results), generation=generation,
                        lineage=model.result_lineage(batch, node['id'], len(all_results)))
            if mime == 'text':item['text'] = raw.decode('utf-8')
            all_results.append(item)
    return all_results
