"""Canvas data and dependency rules, independent of widgets and network calls."""

import copy
import hashlib
import json
import mimetypes
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit


VERSION = 1
MODEL_KINDS = {'llm_model': 'llm', 'vision_model': 'vision', 'image_model': 'text2img', 'edit_model': 'image_edit'}
NODE_CATEGORIES = {'input': '素材输入', 'app': 'RH App', 'api': 'API 节点', 'output': '处理与输出'}
LIBRARY_KINDS = ('text', 'image', 'video', 'audio', *MODEL_KINDS, 'select', 'filename', 'rename', 'preview')


def node_category(kind):
    if kind in MODEL_KINDS:return 'api'
    if kind == 'app':return 'app'
    return 'output' if kind in ('select', 'preview', 'filename', 'rename') else 'input'


KINDS = frozenset({'app', 'image', 'video', 'audio', 'text', 'select', 'preview', 'filename', 'rename'} | set(MODEL_KINDS))
MEDIA = frozenset({'image', 'video', 'audio'})
MEDIA_SUFFIXES = {
    'image': {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.avif', '.heic'},
    'video': {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'},
    'audio': {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus'},
}
TITLES = {'app': 'App', 'image': '图像导入', 'video': '视频导入',
          'audio': '音频导入', 'text': '文本', 'select': '内容过滤', 'preview': '预览 / 保存'}
TITLES.update(llm_model='大语言模型', vision_model='视觉模型', image_model='图像生成', edit_model='图像编辑')
TITLES.update(filename='读取文件名', rename='文件重命名')
RUNTIME_FIELDS = frozenset({'results', 'result_signatures', 'fingerprint', 'status', 'progress', 'node_progress',
                            'message', 'error', 'generation', 'cached', 'stale', 'activated', 'bypassed',
                            '_restored_missing_results', '_restored_positions_ambiguous'})


def normalize_batch_count(value=1):
    """Keep omitted legacy counts equivalent to an explicit single batch."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99:
        raise ValueError('画布批次数必须是 1 至 99 的整数')
    return value


def app_reference(app):
    """Resolve old App references without mutating a workflow or its hash."""
    error = 'App 链接无效，请填写与此 App ID 对应的 RunningHub 官方 HTTPS 链接。'
    wid = str(app.get('webapp_id') or app.get('webappId') or '').strip()
    raw = str(app.get('url') or '').strip()
    if not raw:
        if app.get('url_error'):
            raise ValueError(error)
        raw = str(app.get('base_url') or 'https://www.runninghub.cn').rstrip('/') + '/webapp/' + wid
    try:
        parsed = urlsplit(raw if '://' in raw else 'https://' + raw)
        host = (parsed.hostname or '').lower()
        match = re.fullmatch(r'/(?:webapp|ai-detail)/(\d+)/?', parsed.path)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or host not in ('runninghub.cn', 'www.runninghub.cn', 'runninghub.ai', 'www.runninghub.ai')
                or not match or (wid and wid != match.group(1))):
            raise ValueError(error)
    except (ValueError, TypeError):
        raise ValueError(error) from None
    wid = match.group(1)
    base = 'https://' + (host if host.startswith('www.') else 'www.' + host)
    return {'webapp_id': wid, 'url': base + '/webapp/' + wid, 'base_url': base,
            'name': str(app.get('name') or app.get('title') or wid)}


def normalize_app_urls(document):
    """Call only for newly created nodes or an explicit paired save/export.

    Loading never adds URL metadata: old workflow/snapshot hashes must remain
    comparable. Invalid references retain a safe marker, never credentials or
    an external destination that could receive local keys.
    """
    for node in document.get('nodes', []):
        if node.get('kind') != 'app':
            continue
        app = node.setdefault('app', {})
        try:
            reference = app_reference(app)
            for key in ('webapp_id', 'url', 'base_url'):
                app[key] = reference[key]
            app.pop('url_error', None)
        except ValueError:
            app['url'] = ''
            app['base_url'] = ''
            app['url_error'] = 'App 链接无效，请修正后添加。'
    return document


def migrate_save_name_inputs(document):
    """Move the former save-name wire to an explicit rename node on load.

    IDs are deterministic so repeated workflow reads compare identically.
    Old runtime graphs are deliberately not migrated: their configuration
    mismatch discards the obsolete snapshot through the normal recovery path.
    """
    if not isinstance(document, dict):return document
    nodes, edges = document.get('nodes'), document.get('edges')
    if not isinstance(nodes, list) or not isinstance(edges, list):return document
    for target in list(nodes):
        if not isinstance(target, dict) or target.get('kind') != 'preview':continue
        name_edges = [e for e in edges if isinstance(e, dict) and e.get('target') == target.get('id') and e.get('input') == 'name']
        if not name_edges:continue
        identity = uuid.uuid5(uuid.NAMESPACE_URL, str(document.get('id')) + '/save-name/' + target['id']).hex
        if any(n.get('id') == identity for n in nodes if isinstance(n, dict)):
            raise ValueError('旧版保存节点迁移时节点标识冲突')
        rename = new_node('rename')
        rename['id'] = identity
        rename['x'], rename['y'] = target.get('x', 0) - 300, target.get('y', 0)
        for key in RUNTIME_FIELDS:rename.pop(key, None)
        nodes.append(rename)
        for edge in edges:
            if isinstance(edge, dict) and edge.get('target') == target['id'] and edge.get('input') in ('name', 'value'):
                edge['target'] = identity
        edges.append({'id': uuid.uuid5(uuid.NAMESPACE_URL, identity + '/output').hex,
                      'source': identity, 'target': target['id'], 'input': 'value', 'mode': 'all', 'indices': []})
    return document


def workflow_document(document):
    """A portable workflow contains configuration, never imported media or runs."""
    result = {key: copy.deepcopy(document.get(key)) for key in ('version', 'id', 'name', 'nodes', 'edges', 'view')}
    result['batch_count'] = normalize_batch_count(document.get('batch_count', 1))
    result['view'] = result.get('view') or {}
    for node in result['nodes']:
        # Obsolete node repetition settings must not survive the next save/export.
        node.pop('run_count', None)
        node.pop('bypass_input', None)
        for key in RUNTIME_FIELDS:
            node.pop(key, None)
        if node.get('kind') in MEDIA:
            node.setdefault('params', {}).pop('files', None)
    return result


def initialize_runtime(document):
    document['batch_count'] = normalize_batch_count(document.get('batch_count', 1))
    document.setdefault('run', {})
    for node in document['nodes']:
        node.pop('run_count', None)
        node.pop('bypass_input', None)
        node.setdefault('results', [])
        node.setdefault('status', 'IDLE')
        node.setdefault('fingerprint', '')
        if node.get('kind') in MEDIA:
            node.setdefault('params', {}).setdefault('files', [])
    return document


def new_document(name='未命名画布'):
    return {'version': VERSION, 'id': uuid.uuid4().hex, 'name': str(name),
            'nodes': [], 'edges': [], 'view': {}, 'batch_count': 1, 'run': {}}


def new_node(kind, title=None, **values):
    if kind not in KINDS:
        raise ValueError('不支持的节点类型')
    node = {'id': uuid.uuid4().hex, 'kind': kind, 'title': title or TITLES[kind],
            'x': 0, 'y': 0, 'params': {}, 'filter_repeats': False,
            'decode_settings': {}, 'results': [], 'fingerprint': '', 'status': 'IDLE'}
    node.update(copy.deepcopy(values))
    node.pop('run_count', None)
    node.pop('bypass_input', None)
    if kind in MEDIA:
        node['params'].setdefault('files', [])
    elif kind == 'text':
        node['params'].setdefault('text', '')
    elif kind in MODEL_KINDS:
        node['params'].setdefault('prompt', '')
        node['params'].setdefault('system_prompt', '')
        node.setdefault('model_config', {})
    elif kind == 'select':
        node['params'].setdefault('type', 'any')
        node['params'].setdefault('indices', [])
    elif kind == 'preview':
        node['params'].setdefault('save_enabled', False)
        node['params'].setdefault('overwrite', False)
    elif kind == 'filename':
        node['params'].setdefault('read_mode', 'full' if node['params'].get('include_extension', False) else 'name')
    elif kind == 'rename':
        node['params'].setdefault('name', '')
        node['params'].setdefault('extension', '')
    return node


def parameter_key(field):
    return '{}::{}'.format(field.get('nodeId', ''), field.get('fieldName', ''))


def field_type(field):
    kind = str(field.get('fieldType') or '').lower()
    if kind in MEDIA:
        return kind
    if kind in ('upload', 'file'):
        return 'file'
    details = field.get('fieldData') or {}
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (ValueError, TypeError):
            details = {}
    if isinstance(details, dict):
        for media in MEDIA:
            if details.get(media + '_upload'):
                return media
        if details.get('upload') or details.get('zip'):
            return 'file'
    if field.get('_rh_upload'):
        return 'file'
    if kind in ('int', 'integer', 'float', 'number', 'double'):
        return 'number'
    if kind in ('boolean', 'bool', 'combo', 'enum', 'select', 'list'):
        return 'scalar'
    return 'text'


def app_fields(node):
    app = node.get('app') or {}
    return app.get('nodes') or app.get('nodeInfoList') or []


def node_title(node):
    title = str(node.get('title') or TITLES.get(node.get('kind'), '节点'))
    # Display old default titles consistently without changing JSON/snapshots.
    return TITLES['select'] if node.get('kind') == 'select' and title == '结果选择' else title


def input_ports(node):
    if node.get('kind') in ('filename', 'rename'):
        ports = [{'key': 'value', 'label': '文件 / 结果', 'type': 'any'}]
        if node['kind'] == 'rename':
            ports += [{'key': 'name', 'label': '文件名（不含后缀）', 'type': 'text'},
                      {'key': 'extension', 'label': '后缀名', 'type': 'text'}]
        return ports
    if node.get('kind') in MODEL_KINDS:
        ports = [{'key': 'prompt', 'label': '提示词', 'type': 'text'}]
        if node['kind'] in ('vision_model', 'edit_model'):
            ports.append({'key': 'image', 'label': '图像', 'type': 'image'})
        return ports
    if node.get('kind') == 'app':
        return [{'key': parameter_key(field),
                 'label': str(field.get('description') or field.get('fieldName') or '输入'),
                 'type': field_type(field)} for field in app_fields(node)]
    if node.get('kind') in ('select', 'preview'):
        ports = [{'key': 'value', 'label': '内容' if node['kind'] == 'select' else '结果', 'type': 'any'}]
        return ports
    return []


def output_types(node):
    kind = node.get('kind')
    if kind in MODEL_KINDS:
        return {'text'} if kind in ('llm_model', 'vision_model') else {'image'}
    if kind == 'filename':return {'text'}
    if kind in MEDIA or kind == 'text':
        return {kind}
    if kind == 'select' and node.get('params', {}).get('type', 'any') != 'any':
        return {node['params']['type']}
    # App outputs are determined by actual RH result metadata, not input types.
    return {'any'}


def types_compatible(produced, accepted):
    return (produced == 'any' or accepted == 'any' or produced == accepted
            or accepted == 'file' and produced in MEDIA | {'file'}
            or produced == 'text' and accepted in {'number', 'scalar'})


def validate_document(document):
    """Validate structure, typed connections and DAG; return stable topological IDs."""
    if not isinstance(document, dict) or document.get('version') != VERSION:
        raise ValueError('不支持的画布文件版本')
    normalize_batch_count(document.get('batch_count', 1))
    if not isinstance(document.get('nodes'), list) or not isinstance(document.get('edges'), list):
        raise ValueError('画布节点或连线格式错误')
    nodes = {}
    for node in document['nodes']:
        validate_node(node)
        if node['id'] in nodes:
            raise ValueError('画布节点标识重复')
        if not isinstance(node.get('params', {}), dict):
            raise ValueError('节点参数格式错误')
        nodes[node['id']] = node
    occupied, edge_ids = set(), set()
    indegree = dict.fromkeys(nodes, 0)
    outgoing = {node_id: [] for node_id in nodes}
    for edge in document['edges']:
        if not isinstance(edge, dict) or not edge.get('id') or edge['id'] in edge_ids:
            raise ValueError('连线标识无效或重复')
        edge_ids.add(edge['id'])
        source, target = edge.get('source'), edge.get('target')
        if source not in nodes or target not in nodes or source == target:
            raise ValueError('连线必须连接两个有效的不同节点')
        port = next((p for p in input_ports(nodes[target]) if p['key'] == edge.get('input')), None)
        if port is None:
            raise ValueError('连线输入参数已不存在，请重新绑定')
        identity = (target, edge['input'])
        if identity in occupied:
            raise ValueError('同一输入端口只能连接一条线')
        occupied.add(identity)
        if not any(types_compatible(t, port['type']) for t in output_types(nodes[source])):
            raise ValueError('连线两端的数据类型不兼容')
        if edge.get('mode', 'first') not in ('first', 'index', 'all'):
            raise ValueError('连线结果选择模式无效')
        validate_indices(edge.get('indices') or [])
        indegree[target] += 1
        outgoing[source].append(target)
    ready = [node_id for node_id, degree in indegree.items() if not degree]
    order = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for target in outgoing[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(order) != len(nodes):
        raise ValueError('画布不支持循环连接')
    return order


def validate_node(node):
    if (not isinstance(node, dict) or node.get('kind') not in KINDS
            or not isinstance(node.get('id'), str) or not node['id']):
        raise ValueError('画布包含无效节点')
    if not isinstance(node.get('params', {}), dict) or not isinstance(node.get('decode_settings', {}), dict):
        raise ValueError('节点参数格式错误')
    if type(node.get('bypass',False)) is not bool:
        raise ValueError('忽略节点设置格式错误')
    if node['kind'] in MODEL_KINDS:
        if not isinstance(node.get('model_config', {}), dict):
            raise ValueError('模型节点配置格式错误')
        config = node.get('model_config', {})
        if any(not isinstance(config.get(k, ''), str) for k in ('provider', 'protocol', 'endpoint', 'model')):
            raise ValueError('模型节点连接字段格式错误')
        if 'timeout' in config and (type(config['timeout']) is not int or not 1 <= config['timeout'] <= 3600):
            raise ValueError('模型节点超时必须是 1–3600 秒的整数')
        if any(k in config and type(config[k]) is not bool for k in ('web_search', 'merge_system_prompt')):
            raise ValueError('模型节点开关格式错误')
        if any(not isinstance(node.get('params', {}).get(k, ''), str) for k in ('prompt', 'system_prompt', 'image')):
            raise ValueError('模型节点文本或图像路径格式错误')
    if node['kind'] == 'app':
        if not isinstance(node.get('app', {}), dict):
            raise ValueError('App 定义格式错误')
        fields = app_fields(node)
        if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
            raise ValueError('App 参数定义格式错误')
        keys = [parameter_key(field) for field in fields]
        if len(keys) != len(set(keys)):
            raise ValueError('App 参数标识重复')
    if node['kind'] in MEDIA:
        files = node.get('params', {}).get('files', [])
        if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
            raise ValueError('媒体文件列表格式错误')
    if node['kind'] == 'select':
        validate_indices(node.get('params', {}).get('indices') or [])
    if node['kind'] == 'preview' and not isinstance(node.get('params', {}).get('save_directory', ''), str):
        raise ValueError('保存目录格式错误')
    if node['kind'] == 'preview' and type(node.get('params', {}).get('save_enabled', False)) is not bool:
        raise ValueError('保存开关格式错误')
    if node['kind'] == 'preview' and type(node.get('params', {}).get('overwrite', False)) is not bool:
        raise ValueError('重名覆盖开关格式错误')
    if node['kind'] == 'filename' and type(node.get('params', {}).get('include_extension', False)) is not bool:
        raise ValueError('文件名后缀开关格式错误')
    if node['kind'] == 'filename' and node.get('params', {}).get('read_mode', 'name') not in ('name', 'extension', 'full'):
        raise ValueError('文件名读取模式无效')
    if node['kind'] == 'rename' and any(not isinstance(node.get('params', {}).get(k, ''), str) for k in ('name', 'extension')):
        raise ValueError('重命名参数格式错误')
    if not isinstance(node.get('results', []), list) or any(not isinstance(r, dict) for r in node.get('results', [])):
        raise ValueError('节点结果格式错误')


def connect(document, source, target, input, mode=None, indices=None):
    if mode is None:
        target_node = next((node for node in document['nodes'] if node['id'] == target), {})
        mode = 'all' if target_node.get('kind') in ('select', 'filename', 'rename', 'preview') else 'first'
    edge = {'id': uuid.uuid4().hex, 'source': source, 'target': target,
            'input': input, 'mode': mode, 'indices': list(indices or [])}
    candidate = dict(document, edges=list(document['edges']) + [edge])
    validate_document(candidate)
    document['edges'].append(edge)
    return edge


def incoming(document, node_id):
    return [edge for edge in document['edges'] if edge['target'] == node_id]


def execution_edges(document):
    allowed={}
    for node in document['nodes']:
        if not node.get('bypass'):continue
        if node['kind'] in ('rename','filename','select','preview'):keys={'value'}
        else:keys={port['key'] for port in input_ports(node) if any(types_compatible(port['type'],kind) for kind in output_types(node))}
        allowed[node['id']]=keys
    return [edge for edge in document['edges'] if edge['target'] not in allowed or edge['input'] in allowed[edge['target']]]


def execution_incoming(document,node_id):
    return [edge for edge in execution_edges(document) if edge['target']==node_id]


def ancestors(document, target):
    nodes = {node['id'] for node in document['nodes']}
    targets=target if isinstance(target,(list,tuple)) else [target]
    if not targets or any(not isinstance(value,str) or value not in nodes for value in targets):
        raise ValueError('所选节点不存在')
    found, pending = set(), list(targets)
    reverse = {}
    for edge in execution_edges(document):
        reverse.setdefault(edge['target'], []).append(edge['source'])
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(reverse.get(current, []))
    return found


def validate_indices(indices):
    if not isinstance(indices, list) or any(isinstance(i, bool) or not isinstance(i, int)
                                          or i < 1 for i in indices):
        raise ValueError('结果序号必须是从 1 开始的整数')


def result_type(result):
    kind = str(result.get('type') or result.get('kind') or result.get('fileType') or '').lower()
    if '/' in kind:
        kind = kind.split('/', 1)[0]
    if kind in MEDIA | {'text', 'number', 'scalar', 'file'}:
        return kind
    if 'text' in result:
        return 'text'
    path = result.get('path') or result.get('file_path') or result.get('url') or ''
    suffix = Path(str(path).split('?', 1)[0]).suffix.lower()
    for media, suffixes in MEDIA_SUFFIXES.items():
        if suffix in suffixes:
            return media
    mime = mimetypes.guess_type(str(path).split('?', 1)[0])[0] or ''
    prefix = mime.split('/', 1)[0]
    return prefix if prefix in MEDIA | {'text'} else 'file'


def normalize_result(result):
    if isinstance(result, str):
        result = {'path': result}
    result = copy.deepcopy(result)
    if 'path' not in result and result.get('file_path'):
        result['path'] = result['file_path']
    result['type'] = result['kind'] = result_type(result)
    return result


def available_results(results, signatures=None):
    """Restore usable references individually without reading/decoding media.

    The returned signatures stay paired with their original result. Missing
    historical files are a local restoration omission, never an exception.
    """
    available, kept_signatures = [], [] if isinstance(signatures, list) and len(signatures) == len(results) else None
    missing, readable, positions = False, {}, []
    accepted_types = ('any', 'image', 'video', 'audio', 'text', 'file', 'number', 'scalar')
    counters = dict.fromkeys(accepted_types, 0)
    for index, value in enumerate(results):
        try:
            result = normalize_result(value)
            original_positions = result.get('_restored_positions') or {}
            current_positions = {}
            for accepted in accepted_types:
                if types_compatible(result_type(result), accepted):
                    counters[accepted] += 1
                    position = original_positions.get(accepted, counters[accepted])
                    if isinstance(position, int) and not isinstance(position, bool) and position > 0:
                        current_positions[accepted] = position
            path = result.get('path')
            if path:
                key = os.path.normcase(os.fspath(path))
                if key not in readable:
                    readable[key] = False
                    if os.path.isfile(path):
                        # Opening alone checks access; no image/text/video bytes
                        # are loaded on the restoration path.
                        with open(path, 'rb'):
                            readable[key] = True
                if not readable[key]:
                    missing = True
                    continue
            elif result_type(result) in MEDIA | {'file'} or not any(key in result for key in ('text', 'value')):
                missing = True
                continue
            available.append(result)
            positions.append(current_positions)
            if kept_signatures is not None:
                kept_signatures.append(copy.deepcopy(signatures[index]))
        except (OSError, TypeError, ValueError, AttributeError):
            missing = True
    if missing:
        for result, position in zip(available, positions):
            result['_restored_positions'] = position
    return available, kept_signatures, missing


def snapshot_result_references(document):
    """Keep file results as references, including text files, not inline copies."""
    result = copy.deepcopy(document)
    containers = list(result.get('nodes') or [])
    run = result.get('run') or {}
    containers.extend((run.get('snapshot') or {}).get('nodes') or [])
    for section in ('nodes', 'cache'):
        for state in (run.get(section) or {}).values():
            containers.append(state)
            containers.extend(state.get('items') or [])
    payload_keys = {'text', 'value', 'content', 'data', 'bytes', 'blob', 'base64',
                    'thumbnail', 'preview', 'image', 'image_data', 'file_data'}
    for container in containers:
        for item in container.get('results') or []:
            if isinstance(item, dict) and (item.get('path') or item.get('file_path')
                                          or result_type(item) in MEDIA | {'file'}):
                for key in payload_keys:
                    item.pop(key, None)
    return result


def select_results(results, edge=None, accepted='any'):
    edge = edge or {}
    matches = [normalize_result(result) for result in results
               if types_compatible(result_type(result), accepted)]
    if not matches:
        raise ValueError('上游没有符合输入类型的结果')
    mode = edge.get('mode', 'first')
    if mode == 'all':
        return matches
    if mode == 'first':
        return matches[:1]
    indices = edge.get('indices') or [1]
    validate_indices(indices)
    if any('_restored_positions' in result for result in matches):
        selected = []
        for index in indices:
            candidates = [result for result in matches
                          if result.get('_restored_positions', {}).get(accepted) == index]
            if len(candidates) != 1:
                raise ValueError('所选历史结果已不可用，或其原始序号无法确定')
            selected.append(candidates[0])
        return selected
    if max(indices) > len(matches):
        raise ValueError('所选结果序号超出范围（共 {} 项）'.format(len(matches)))
    return [matches[index - 1] for index in indices]


def pair_inputs(inputs):
    """Zip multi-item ports and broadcast singletons; never silently truncate."""
    if not inputs:
        return [{}]
    if any(not values for values in inputs.values()):
        raise ValueError('输入结果为空')
    driver = max(inputs, key=lambda key:(len(inputs[key]), key == 'value'))
    primary = inputs[driver]
    def axes(values):
        return set.intersection(*(set(lineage(value)) for value in values)) if values else set()
    primary_axes = axes(primary)
    aligned = {driver: primary}
    for key, values in inputs.items():
        if key == driver:continue
        shared = sorted(primary_axes & axes(values))
        if shared:
            def identity_of(value):
                origins = lineage(value)
                return tuple(origins[axis] for axis in shared)
            lookup = {}
            for value in values:
                identity = identity_of(value)
                if identity in lookup:
                    raise ValueError('同一来源对应多个输入，无法唯一配对，请先按序号筛选')
                lookup[identity] = value
            try:
                aligned[key] = [lookup[identity_of(value)] for value in primary]
            except KeyError:
                raise ValueError('来源对应的输入缺失，请检查连线是否选择全部匹配结果') from None
        elif len(values) == 1:
            aligned[key] = values * len(primary)
        elif len(values) == len(primary):
            aligned[key] = values
        else:
            raise ValueError('多个输入列表长度不一致且缺少可关联的来源，请调整选择结果')
    paired = [{key: values[index] for key, values in aligned.items()} for index in range(len(primary))]
    for batch in paired:result_lineage(batch, '', 0)  # Validate before any network submission.
    return paired


def lineage(result):
    value = result.get('lineage', {})
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)} if isinstance(value, dict) else {}


def result_lineage(batch, node_id, index):
    result = {}
    for value in batch.values():
        for key, item in lineage(value).items():
            if key in result and result[key] != item:raise ValueError('输入来源冲突，无法确定结果对应关系')
            result[key] = item
    result[node_id] = str(index)
    return result


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        before = os.fstat(source.fileno())
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
        after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('输入文件在读取时发生变化，请重试')
    return digest.hexdigest()


def result_signature(result):
    result = normalize_result(result)
    path = result.get('path')
    if path:
        if not os.path.isfile(path):
            raise ValueError('结果文件不存在：' + str(path))
        content = file_hash(path)
    else:
        if result['type'] in MEDIA | {'file'} or not any(key in result for key in ('text','value')):
            raise ValueError('结果没有可读取的文件或内容')
        content = result.get('text', result.get('value', ''))
    signature = {'type': result['type'], 'content': content,
            'generation': result.get('generation', ''),
            'task_id': result.get('task_id', ''), 'index': result.get('index', 0)}
    if not path and result.get('name'):signature['name'] = result['name']
    return signature


def results_valid(results, signatures=None):
    try:
        if any(isinstance(result,dict) and result.get(key) and not os.path.isfile(result[key]) for result in results
               for key in ('paint_temp_path','composite_path')):return False
        actual = [result_signature(result) for result in results]
        return bool(actual) and (signatures is None or actual == signatures)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def bypass_results(node, inputs):
    """Bypass one output to the first connected input of each compatible type."""
    keys=[port['key'] for port in input_ports(node) if port['key'] in inputs]
    if node['kind'] in ('rename','select','preview','filename'):keys=['value'] if 'value' in inputs else []
    result=[];claimed=set();produced=output_types(node)
    for key in keys:
        values=inputs[key]
        kinds={result_type(value) for value in values}
        eligible={kind for kind in kinds if kind not in claimed and any(types_compatible(kind,p) for p in produced)}
        result.extend(copy.deepcopy(value) for value in values if result_type(value) in eligible)
        claimed.update(eligible)
    return result


def canonical_fields(node):
    fields = copy.deepcopy(app_fields(node))
    for field in fields:
        key = parameter_key(field)
        if key in node.get('params', {}):
            field['fieldValue'] = copy.deepcopy(node['params'][key])
        mask = node.get('params', {}).get('_masks', {}).get(key)
        if mask:field['_mask'] = copy.deepcopy(mask)
        if field_type(field) in MEDIA | {'file'}:
            field['_rh_upload'] = True
    return fields


def input_value(result, field):
    accepted = field_type(field)
    if accepted in MEDIA | {'file'}:
        path = result.get('path')
        if not path or not os.path.isfile(path):
            raise ValueError('输入媒体文件不存在')
        return path
    value = result.get('text', result.get('value'))
    if result.get('path') and result_type(result) == 'text':
        with open(result['path'], 'r', encoding='utf-8-sig') as source:
            value = source.read(4 * 1024 * 1024 + 1)
        if len(value) > 4 * 1024 * 1024:
            raise ValueError('文本输入文件过大')
    if value is None:
        raise ValueError('上游没有可用文本值')
    value = str(value)
    if accepted == 'number':
        from decimal import Decimal, InvalidOperation
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise ValueError('上游文本不是有效数值') from error
        if not number.is_finite():
            raise ValueError('数值输入必须有限')
        if str(field.get('fieldType', '')).lower() in ('int', 'integer') and number != number.to_integral_value():
            raise ValueError('上游文本不是整数')
        details = field.get('fieldData') or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (ValueError, TypeError):
                details = {}
        if isinstance(details, list):
            details = next((part for part in reversed(details) if isinstance(part, dict)), {})
        if isinstance(details, dict):
            for key in ('min', 'max'):
                try:
                    bound = Decimal(str(details[key]))
                except (KeyError, InvalidOperation, ValueError):
                    continue
                if bound.is_finite() and (number < bound if key == 'min' else number > bound):
                    raise ValueError('上游数值超出此参数允许的范围')
    elif accepted == 'scalar':
        kind = str(field.get('fieldType', '')).lower()
        if kind in ('bool', 'boolean'):
            value = value.strip().lower()
            if value not in ('true', 'false'):
                raise ValueError('布尔输入必须是 true 或 false')
        else:
            details = field.get('fieldData') or []
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except (ValueError, TypeError):
                    details = []
            if isinstance(details, dict):
                details = details.get('options', details.get('values', details.get('enum', [])))
            if isinstance(details, list) and details and isinstance(details[0], list):
                details = details[0]
            if isinstance(details, list):
                # This pure metadata helper is also used by the App's dropdown;
                # display labels never replace the scalar API value.
                from aetherloom_core.rh_parameters import _list_option
                options = [entry[1] for option in details if (entry := _list_option(option)) is not None]
                if options and value not in options + [str(field.get('fieldValue', ''))]:
                    raise ValueError('上游文本不属于此参数的枚举选项')
    return value


def fingerprint(node, inputs, edges=()):
    """Content-addressed execution identity. Titles and canvas layout are irrelevant."""
    app = node.get('app') or {}
    fields = canonical_fields(node)
    for field in fields:
        if parameter_key(field) in inputs:
            field['fieldValue'] = {'connected': True}
            field.pop('_mask',None)
        value = field.get('fieldValue')
        if field_type(field) in MEDIA | {'file'} and isinstance(value, str) and os.path.isfile(value):
            field['fieldValue'] = {'sha256': file_hash(value)}
        # Cosmetic descriptions do not change generation semantics.
        field.pop('description', None)
    params = copy.deepcopy(node.get('params', {}))
    if isinstance(params.get('_masks'),dict):
        params['_masks']={key:value for key,value in params['_masks'].items() if key not in inputs}
        if not params['_masks']:params.pop('_masks')
    if node['kind'] in MODEL_KINDS:
        for key in inputs:params.pop(key, None)
        if params.get('image'):params['image'] = file_hash(params['image'])
    if 'files' in params:
        params['files'] = [file_hash(path) for path in params['files']]
    for key in [parameter_key(field) for field in fields]:
        params.pop(key, None)
    payload = {'kind': node['kind'], 'params': params, 'nodes': fields,
               'app': {key: app.get(key) for key in ('webapp_id', 'base_url')},
               'decode_settings': node.get('decode_settings', {}),
               # Preserve cache identity for legacy single-run nodes only.
               'run_count': 1,
               'inputs': {key: [result_signature(r) for r in values] for key, values in inputs.items()},
               'edges': [{key: edge.get(key) for key in ('source', 'input', 'mode', 'indices')}
                         for edge in edges]}
    if node['kind'] == 'rename':payload['intermediate_copy_version'] = 1
    if any(r.get('path') for values in inputs.values() for r in values):
        # Pass-through nodes must not return an old path just because a renamed
        # or replaced file has identical bytes. Consumers receive the new file.
        payload['input_file_references'] = {key: [r.get('_file_identity') or (os.path.normcase(os.path.abspath(r['path'])) if r.get('path') else '')
                                                 for r in values] for key, values in inputs.items()}
    if node['kind'] in MEDIA:
        payload['source_paths'] = [os.path.normcase(os.path.abspath(path)) for path in node.get('params', {}).get('files', [])]
    if node['kind'] in MODEL_KINDS:
        payload['model_config'] = node.get('model_config', {})
    if node['kind'] in ('filename', 'rename', 'preview'):
        payload['input_filenames'] = [Path(r.get('path') or r.get('name') or '文本结果.txt').name for r in inputs.get('value', [])]
    if any(lineage(r) for values in inputs.values() for r in values):
        payload['input_lineage'] = {key: [lineage(r) for r in values] for key, values in inputs.items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode('utf-8')).hexdigest()
