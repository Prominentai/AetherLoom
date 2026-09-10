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


def connection(owner, category, identity=None):
    """GUI-thread only. Resolve public settings from a local, known connection."""
    from aetherloom_core import api_manager
    fields = owner.api_config_fields[category]
    active = fields['provider'].currentData() or 'custom'
    if active == 'custom':active = 'custom_' + category
    identity = identity or active
    profile = owner._get_api_provider_profile(category, identity)
    entry = api_manager.find_provider(category, 'custom' if identity.startswith('custom_') else identity)
    if not entry:
        raise ValueError('此模型连接在本机不存在，请在 API 管理中添加后重新选择。')
    if identity == active:
        result = dict(owner._api_probe_controllers[category].snapshot(), provider=identity)
    else:
        result = dict(provider=identity, endpoint=profile.get('endpoint') or entry.get('endpoint', ''),
                      model=profile.get('model', ''), timeout=profile.get('timeout', 120))
        if 'web_search' in profile:result['web_search'] = profile['web_search']
    result['protocol'] = api_manager.effective_protocol(category, identity, profile)
    return public_config(result)


def create(owner, kind):
    category = model.MODEL_KINDS[kind]
    config = connection(owner, category)
    node = model.new_node(kind, model_config=config)
    if kind == 'llm_model':node['params']['system_prompt'] = ''
    if category in ('text2img', 'image_edit'):
        from aetherloom_core.image_prompts import request_options
        options = request_options(owner.settings, category, config['protocol'])
        node['params']['system_prompt'] = options.get('system_prompt', '')
        if 'merge_system_prompt' in options:node['model_config']['merge_system_prompt'] = options['merge_system_prompt']
    return node


def prepare(owner, node):
    category = model.MODEL_KINDS[node['kind']]
    config = public_config(node.get('model_config', {}))
    if not config.get('provider'):
        raise ValueError('请先为模型节点选择 API 管理中的连接。')
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
    timeout = int(config.get('timeout') or 120)
    if not 1 <= timeout <= 3600:raise ValueError('模型请求超时需在 1–3600 秒之间')
    all_results = []
    generation = uuid.uuid4().hex
    for batch in batches:
        _check_stop(stop)
        prompt = (model.input_value(batch['prompt'], {'fieldType': 'STRING'}) if 'prompt' in batch
                  else node.get('params', {}).get('prompt', ''))
        if not str(prompt).strip():raise ValueError('请填写或连接提示词')
        path = batch.get('image', {}).get('path') or ('' if 'image' in batch else node.get('params', {}).get('image', ''))
        if category in ('vision', 'image_edit') and (not path or not os.path.isfile(path)):
            raise ValueError('请连接图像导入节点或选择有效的本地图像')
        system = node.get('params', {}).get('system_prompt', '')
        def operation():
            _check_stop(stop)
            if category in ('llm', 'vision'):
                from api_calls.call_vision import _encode_image_data
                text = complete_text(config['endpoint'], config.get('api_key', ''), config['model'], system, prompt,
                    provider=config['protocol'], timeout=timeout, web_search=config.get('web_search'),
                    image=_encode_image_data(path) if category == 'vision' else None)
                return [('text', str(text).encode('utf-8'))]
            from api_calls.call_images import generate
            return generate(config, prompt, path if category == 'image_edit' else None,
                            system_prompt=system, timeout=timeout, stop=stop)
        lease=temporary.retain([path]) if temporary and path else set()
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
