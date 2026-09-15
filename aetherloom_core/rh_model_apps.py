"""Local RH model applications: declarative definitions, never executable plugins."""
import copy
import json
import re
import uuid
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

BACKENDS = ('rh_app', 'rh_standard', 'rh_llm')
LABELS = {'rh_app': '应用', 'rh_standard': '标准模型', 'rh_llm': 'LLM'}
GROUPS = {'rh_app': 'app', 'rh_standard': 'standard', 'rh_llm': 'rh_llm'}
CATALOG_SOURCE = 'https://github.com/HM-RunningHub/ComfyUI_RH_OpenAPI'
CATALOG_URL = 'https://raw.githubusercontent.com/HM-RunningHub/ComfyUI_RH_OpenAPI/main/models_registry.json'
CATALOG_MAX_BYTES = 12 * 1024 * 1024


def official_site(value):
    parts = urlsplit(str(value or 'https://www.runninghub.cn'))
    if (parts.scheme != 'https' or parts.hostname not in ('www.runninghub.ai', 'runninghub.ai', 'www.runninghub.cn', 'runninghub.cn')
            or parts.username or parts.password or parts.port not in (None, 443) or parts.query or parts.fragment):
        raise ValueError('RH 模型连接必须使用官方 HTTPS 站点')
    return 'https://www.runninghub.' + ('ai' if parts.hostname.endswith('.ai') else 'cn')


def backend(value):
    name = value.get('backend', 'rh_app')
    if name not in BACKENDS:
        raise ValueError('不支持的 RH 应用类型')
    return name


def metadata(value):
    return {key: copy.deepcopy(value[key]) for key in ('backend', 'model_definition') if key in value}


def supports_local_decode(value):
    return value.get('backend', 'rh_app') == 'rh_app'


def endpoint_path(value):
    value = str(value or '')
    segment = r'[A-Za-z0-9_][A-Za-z0-9_.\[\]-]*'
    if not re.fullmatch(segment + r'(?:/' + segment + r')*', value) or '..' in value:
        raise ValueError('标准模型接口路径无效')
    return '/openapi/v2/' + value


def validate_catalog(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 5000:
        raise ValueError('模型目录为空或结构不受支持')
    supported = {'AUDIO', 'BOOLEAN', 'FLOAT', 'IMAGE', 'INT', 'LIST', 'SIZE', 'STRING', 'VIDEO'}
    for entry in entries:
        if not isinstance(entry, dict):raise ValueError('模型定义格式无效')
        endpoint_path(entry.get('endpoint'))
        if not isinstance(entry.get('display_name'), str) or not entry['display_name'].strip():
            raise ValueError('模型名称缺失')
        if entry.get('output_type') not in {'3d', 'audio', 'image', 'string', 'video'}:
            raise ValueError('目录包含当前版本不支持的输出类型，请更新客户端')
        params = entry.get('params')
        if not isinstance(params, list) or len(params) > 256:raise ValueError('模型参数结构无效')
        keys = set()
        for param in params:
            if not isinstance(param, dict):raise ValueError('模型参数格式无效')
            key = param.get('fieldKey')
            if not isinstance(key, str) or not key or len(key) > 256 or key in keys:
                raise ValueError('模型参数名称缺失或重复')
            keys.add(key)
            if param.get('type') not in supported:
                raise ValueError('目录包含当前版本不支持的参数类型，请更新客户端')
            if 'options' in param and (not isinstance(param['options'], list) or len(param['options']) > 5000):
                raise ValueError('模型枚举选项无效')
            if param.get('multipleInputs') and not 1 <= int(param.get('maxInputNum') or 1) <= 256:
                raise ValueError('模型多输入数量无效')
        fields_for(entry)
    return entries


def catalog_cache_path(root):
    return Path(root).parent / '.rh_model_cache' / 'standard_catalog.json'


def catalog_document(root=None):
    if root is not None:
        try:
            path = catalog_cache_path(root)
            if path.stat().st_size <= CATALOG_MAX_BYTES:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('version') == 1 and data.get('source') == CATALOG_SOURCE:
                    validate_catalog(data['models'])
                    return data
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            pass
    return json.loads(Path(__file__).with_name('rh_standard_catalog.json').read_text(encoding='utf-8'))


def catalog(root=None):
    return catalog_document(root)['models']


def fetch_catalog():
    """Download only declarative JSON from the fixed upstream; never import code."""
    import requests
    deadline = time.monotonic() + 45
    with requests.get(CATALOG_URL, timeout=(10, 20), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:raise ValueError(f'GitHub 返回 HTTP {response.status_code}')
        if int(response.headers.get('Content-Length') or 0) > CATALOG_MAX_BYTES:
            raise ValueError('模型目录文件过大')
        raw = bytearray()
        for chunk in response.iter_content(65536):
            raw.extend(chunk)
            if len(raw) > CATALOG_MAX_BYTES:raise ValueError('模型目录文件过大')
            if time.monotonic() > deadline:raise TimeoutError('模型目录下载超时')
    def invalid_constant(value):raise ValueError('模型目录包含无效数值')
    entries = validate_catalog(json.loads(raw.decode('utf-8-sig'), parse_constant=invalid_constant))
    return {'version': 1, 'source': CATALOG_SOURCE, 'license': 'Apache-2.0',
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'models': entries}


def save_catalog(root, data):
    from .rh_app_install import _atomic_write
    validate_catalog(data['models'])
    _atomic_write(catalog_cache_path(root), data)


def fields_for(definition):
    fields = []
    for param in definition['params']:
        key, kind = param['fieldKey'], param['type']
        multiple = bool(param.get('multipleInputs'))
        array = multiple and kind in ('IMAGE', 'VIDEO', 'AUDIO')
        count = 1 if array else max(1, min(256, int(param.get('maxInputNum') or 1))) if multiple else 1
        for index in range(count):
            name = key if not multiple else key + '__' + str(index + 1)
            label = str(param.get('label') or key)
            if label == key:
                label = {'prompt': '提示词', 'model': '模型名称', 'system': '系统提示词', 'temperature': '采样温度',
                         'max_tokens': '最大输出 Token', 'aspectRatio': '画面比例', 'resolution': '分辨率',
                         'duration': '时长', 'imageUrls': '参考图像', 'imageUrl': '输入图像',
                         'image': '输入图像', 'seed': '种子'}.get(key, key)
            value = copy.deepcopy(param.get('defaultValue', [])) if array else param.get('defaultValue', '') if not multiple else ''
            if isinstance(value, bool):value = str(value).lower()
            field_kind = 'LIST' if kind == 'BOOLEAN' else 'STRING' if kind == 'SIZE' else kind
            details = (['true', 'false'] if kind == 'BOOLEAN' else param.get('options', []) if kind == 'LIST'
                       else {k: param[k] for k in ('min', 'max', 'step') if k in param})
            fields.append(dict(nodeId='model', fieldName=name, fieldType=field_kind, fieldValue=value,
                fieldData=details, description=label + (f' {index+1}' if multiple and not array else ''),
                _model_field=key, _model_index=index, _model_multiple=multiple,
                _model_batch=array, _model_array=array, _model_constraints=copy.deepcopy(param) if array else {},
                _model_help=str(param.get('description') or '')))
    return fields


def standard_application(definition, name='', base_url='https://www.runninghub.cn'):
    base_url = official_site(base_url)
    endpoint_path(definition['endpoint'])
    definition = copy.deepcopy(definition)
    return dict(schema_version=1, backend='rh_standard', webappId='standard_' + uuid.uuid4().hex,
        title=name.strip() or definition.get('name_cn') or definition.get('display_name') or definition['endpoint'],
        base_url=base_url, model_definition=definition, nodeInfoList=fields_for(definition),
        description='RH 标准模型 · 企业级共享 API Key；参数在此应用内独立保存。')


def llm_application(model_name, name='', base_url='https://www.runninghub.cn'):
    base_url = official_site(base_url)
    if not model_name.strip():raise ValueError('请选择或填写 LLM 模型名称')
    definition = dict(model=model_name.strip(), output_type='string', params=[
        dict(fieldKey='model', type='STRING', required=True, defaultValue=model_name.strip()),
        dict(fieldKey='system', type='STRING', required=False, defaultValue=''),
        dict(fieldKey='prompt', type='STRING', required=True, defaultValue=''),
        dict(fieldKey='image', type='IMAGE', required=False, description='可选；仅用于支持图像理解的模型'),
        dict(fieldKey='temperature', type='FLOAT', required=False, defaultValue=1, min=0, max=2),
        dict(fieldKey='max_tokens', type='INT', required=False, defaultValue=4096, min=1, max=131072)])
    return dict(schema_version=1, backend='rh_llm', webappId='llm_' + uuid.uuid4().hex,
        title=name.strip() or model_name.strip(), base_url=base_url, model_definition=definition,
        nodeInfoList=fields_for(definition), description='RH LLM · 每次运行独立对话；图像输入仅适用于支持视觉的模型。')


def application_from_canvas(reference):
    """Restore a local model card from the saved definition, without HTTP or keys."""
    kind = backend(reference)
    if kind not in ('rh_standard', 'rh_llm'):raise ValueError('不是 RH 模型节点')
    identity = str(reference.get('webapp_id') or '')
    prefix = 'standard' if kind == 'rh_standard' else 'llm'
    if not re.fullmatch(prefix + r'_[a-f0-9]{32}', identity):raise ValueError('模型类型与应用标识不一致')
    definition = copy.deepcopy(reference.get('model_definition'))
    if not isinstance(definition, dict):raise ValueError('画布未保存模型定义，请重新添加对应模型节点')
    if kind == 'rh_standard':
        validate_catalog([definition])
    else:
        if not isinstance(definition.get('model'), str) or not definition['model'].strip():
            raise ValueError('画布未保存 LLM 模型名称，请重新添加对应节点')
        validate_catalog([dict(definition, endpoint='llm', display_name=definition['model'], output_type='string')])
    nodes = copy.deepcopy(reference.get('nodes'))
    if not nodes:
        nodes = fields_for(definition)
    if not isinstance(nodes, list) or not all(isinstance(n, dict) and n.get('nodeId') is not None
            and isinstance(n.get('fieldName'), str) and n['fieldName'] for n in nodes):
        raise ValueError('画布保存的模型输入定义无效')
    return dict(schema_version=1, webappId=identity, title=str(reference.get('name') or identity),
                backend=kind, base_url=official_site(reference.get('base_url')),
                model_definition=definition, nodeInfoList=nodes)


def install(root, application):
    from .rh_app_install import _atomic_write
    identity = str(application['webappId'])
    if not re.fullmatch(r'(?:standard|llm)_[a-f0-9]{32}', identity):raise ValueError('本地应用标识无效')
    folder = Path(root) / identity
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (identity + '.json')
    _atomic_write(path, application)
    return path
