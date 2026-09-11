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
from . import collections


VERSION = 1
MODEL_KINDS = {'llm_model': 'llm', 'vision_model': 'vision', 'image_model': 'text2img', 'edit_model': 'image_edit'}
NODE_CATEGORIES = {'input': '素材输入', 'app': 'RH 应用', 'standard': 'RH 标准模型', 'rh_llm': 'RH LLM', 'api': 'API 节点', 'collection': 'List / Batch', 'output': '处理与输出'}
MERGE_KINDS = frozenset({'merge_batch', 'merge_list'})
DYNAMIC_INPUT_KINDS = MERGE_KINDS | {'list_select'}
LIBRARY_KINDS = ('int', 'float', 'text', 'image', 'video', 'audio', *MODEL_KINDS, 'merge_list', 'list_select', 'list2batch', 'merge_batch', 'batch_select', 'rebatch', 'batch2list', 'select', 'filename', 'rename', 'preview')


def node_category(kind):
    if kind in collections.KINDS:return 'collection'
    if kind in MODEL_KINDS:return 'api'
    if kind == 'app':return 'app'
    return 'output' if kind in DYNAMIC_INPUT_KINDS or kind in ('list2batch', 'batch2list', 'select', 'preview', 'filename', 'rename') else 'input'


KINDS = frozenset({'app', 'image', 'video', 'audio', 'text', 'int', 'float', 'list2batch', 'batch2list', 'select', 'preview', 'filename', 'rename'} | set(MODEL_KINDS) | DYNAMIC_INPUT_KINDS | collections.KINDS)
NUMERIC_TYPES = frozenset({'int', 'float', 'number'})
VALUE_TYPES = NUMERIC_TYPES | {'boolean', 'enum', 'scalar'}
MEDIA = frozenset({'image', 'video', 'audio'})
MEDIA_SUFFIXES = {
    'image': {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.avif', '.heic'},
    'video': {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'},
    'audio': {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus'},
}
TITLES = {'app': 'App', 'image': '图像导入', 'video': '视频导入',
          'audio': '音频导入', 'text': '文本', 'select': '内容过滤', 'preview': '预览 / 保存'}
TITLES.update(llm_model='大语言模型', vision_model='视觉模型', image_model='图像生成', edit_model='图像编辑')
TITLES.update(int='整数 INT', float='浮点数 FLOAT')
TITLES.update(filename='读取文件名', rename='文件重命名')
TITLES['list2batch'] = '列表转 Batch'
TITLES['batch2list'] = 'Batch 转列表'
TITLES.update(merge_batch='合成 Batch', merge_list='合成列表')
TITLES['list_select'] = '列表取项'
TITLES.update(collections.TITLES)
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
    if app.get('backend') in ('rh_standard', 'rh_llm'):
        from aetherloom_core.rh_model_apps import official_site
        if not re.fullmatch(r'(?:standard|llm)_[a-f0-9]{32}', wid):raise ValueError('模型应用标识无效')
        return dict(webapp_id=wid, url='', base_url=official_site(app.get('base_url') or 'https://www.runninghub.cn'),
                    name=str(app.get('name') or app.get('title') or wid))
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


def supports_local_decode(node):
    from aetherloom_core.rh_model_apps import supports_local_decode as supported
    return node.get('kind') == 'app' and supported(node.get('app') or {})


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
        if not supports_local_decode(node):node['decode_settings'] = {}
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
    sync_dynamic_inputs(result)
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
    sync_dynamic_inputs(document)
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
    if kind == 'app' and not supports_local_decode(node):node['decode_settings'] = {}
    node.pop('run_count', None)
    node.pop('bypass_input', None)
    if kind in collections.KINDS:
        for key, value in collections.defaults(kind).items():node['params'].setdefault(key, value)
    if kind in DYNAMIC_INPUT_KINDS:
        node.setdefault('input_keys', ['input_1'])
    if kind == 'list_select':
        node['params'].setdefault('type', 'any')
        node['params'].setdefault('indices', [1])
        if not collections.current(node):node['params'].setdefault('keep_batch', False)
    if kind in MEDIA:
        node['params'].setdefault('files', [])
    elif kind in ('int', 'float'):
        node['params'].setdefault('value', 0 if kind == 'int' else 0.0)
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
    if field.get('_canvas_text_batch'):return 'text_input'
    if (field.get('_model_batch') or field.get('_model_multiple') and field.get('_model_index', 0) == 0) and str(field.get('fieldType', '')).lower() in MEDIA:
        return str(field['fieldType']).lower() + '_input'
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
    if isinstance(details, list):
        details = next((part for part in reversed(details) if isinstance(part, dict)), {})
    if isinstance(details, dict):
        for media in MEDIA:
            if details.get(media + '_upload'):
                return media
        if details.get('zip'):return 'archive'
        if details.get('upload'):return 'file'
    if field.get('_rh_upload'):
        return 'file'
    if kind in ('int', 'integer'):return 'int'
    if kind in ('float', 'double'):return 'float'
    if kind == 'number':return 'float'
    if kind in ('boolean', 'bool'):return 'boolean'
    if kind in ('combo', 'enum', 'select', 'list'):return 'enum'
    if kind in ('zip', 'archive'):return 'archive'
    return 'text'


def app_fields(node):
    app = node.get('app') or {}
    return app.get('nodes') or app.get('nodeInfoList') or []


def node_title(node):
    title = str(node.get('title') or TITLES.get(node.get('kind'), '节点'))
    # Display old default titles consistently without changing JSON/snapshots.
    return TITLES['select'] if node.get('kind') == 'select' and title == '结果选择' else title


def sync_dynamic_inputs(document):
    """Derive stable sockets from edges, retaining one trailing empty socket.

    Replace changed node dictionaries instead of mutating them: connection
    previews validate shallow graph copies which share the editor's nodes.
    """
    connected = {n['id']: set() for n in document['nodes']
                 if isinstance(n, dict) and n.get('kind') in DYNAMIC_INPUT_KINDS and n.get('id')}
    for edge in document['edges']:
        if not isinstance(edge, dict) or edge.get('target') not in connected:
            continue
        key = edge.get('input')
        if not isinstance(key, str) or not re.fullmatch(r'input_[1-9][0-9]{0,8}', key):
            raise ValueError('动态输入端口标识无效')
        connected[edge['target']].add(key)
    nodes = []
    for node in document['nodes']:
        if isinstance(node, dict) and node.get('id') in connected:
            keys = sorted(connected[node['id']], key=lambda key: int(key[6:]))
            spare = 'input_' + str(int(keys[-1][6:]) + 1 if keys else 1)
            keys.append(spare)
            if node.get('input_keys') != keys:
                node = dict(node, input_keys=keys)
        nodes.append(node)
    document['nodes'] = nodes
    return document


def app_field_label(field, definition=None):
    """One short display name for the socket and its parameter editor."""
    definition = definition or {}
    def clean(value):
        return ' '.join(str(value or '').strip().splitlines()[0].split()) if str(value or '').strip() else ''
    label = clean(definition.get('label')) or clean(field.get('description'))
    if not label or len(label) > 28:
        label = clean(field.get('nodeName')) or clean(field.get('fieldName')) or label or '参数'
    return label if len(label) <= 28 else label[:26] + '…'


def app_input_labels(node):
    definitions = {p['fieldKey']: p for p in (node.get('app', {}).get('model_definition') or {}).get('params', []) if 'fieldKey' in p}
    fields = app_fields(node)
    labels = [app_field_label(field, definitions.get(field.get('_model_field') or field.get('fieldName'))) for field in fields]
    counts = {}
    for label in labels:counts[label] = counts.get(label, 0) + 1
    return {parameter_key(field): label if counts[label] == 1 else label + ' · ' + parameter_key(field).replace('::', '.')
            for field, label in zip(fields, labels)}


def input_ports(node):
    if node.get('kind') in ('batch_select', 'rebatch'):
        return [{'key': 'value', 'label': '内容',
                 'type': 'batch' if node['kind'] == 'batch_select' else 'any'}]
    if node.get('kind') in DYNAMIC_INPUT_KINDS:
        return [{'key': key, 'label': '输入 ' + key.rsplit('_', 1)[-1], 'type': 'any'}
                for key in node.get('input_keys') or ['input_1']]
    if node.get('kind') in ('filename', 'rename'):
        ports = [{'key': 'value', 'label': '文件', 'type': 'any'}]
        if node['kind'] == 'rename':
            ports += [{'key': 'name', 'label': '文件名（不含后缀）', 'type': 'text'},
                      {'key': 'extension', 'label': '扩展名', 'type': 'text'}]
        return ports
    if node.get('kind') in MODEL_KINDS:
        ports = [{'key': 'prompt', 'label': '提示词', 'type': 'text_input'}]
        if node['kind'] in ('vision_model', 'edit_model'):
            ports.append({'key': 'image', 'label': '图像', 'type': 'image_input'})
        return ports
    if node.get('kind') == 'app':
        labels = app_input_labels(node)
        return [{'key': parameter_key(field),
                 'label': labels[parameter_key(field)],
                 'type': 'text_input' if node.get('app', {}).get('backend') == 'rh_llm' and field_type(field) == 'text' else field_type(field)} for field in app_fields(node)]
    if node.get('kind') in ('list2batch', 'batch2list', 'select', 'preview'):
        ports = [{'key': 'value', 'label': '内容', 'type': 'any'}]
        return ports
    return []


RESULT_TYPES = {'image': '图像', 'video': '视频', 'audio': '音频', 'text': '文本', 'file': '其他内容'}
ITEM_OUTPUT_TYPES = dict(RESULT_TYPES, int='INT', float='FLOAT', boolean='布尔', enum='枚举', archive='压缩文件', number='数值（旧版）', scalar='其他值（旧版）', batch='Batch')
PORT_TYPE_NAMES = {'image': '图像', 'video': '视频', 'audio': '音频', 'text': '文本',
                   'any': '任意', 'archive': '压缩文件', 'int': 'INT', 'float': 'FLOAT',
                   'number': '数值', 'boolean': '布尔', 'enum': '枚举'}


def port_type_name(kind):
    """Socket captions describe content; List / Batch are execution containers."""
    return PORT_TYPE_NAMES.get(kind.removesuffix('_input'), '任意')


def filter_type_options(current='any', *, batch=False):
    options = [('any', '全部类型')] + [(key, label) for key, label in PORT_TYPE_NAMES.items() if key not in ('any', 'number')]
    if batch:options.append(('batch', 'Batch'))
    if current not in {key for key, _ in options} and current in ITEM_OUTPUT_TYPES:
        options.append((current, ITEM_OUTPUT_TYPES[current]))
    return options


def result_matches(result, accepted, *, connections=False):
    kind = result_type(result)
    if accepted == 'archive':return is_archive_result(result)
    if connections:return types_compatible(kind, accepted)
    return (accepted == 'any' or kind == accepted
            or accepted == 'number' and kind in NUMERIC_TYPES
            or accepted == 'scalar' and kind in VALUE_TYPES
            or accepted == 'file' and kind in {'file', 'archive'})


def primitive_value(node):
    from decimal import Decimal, InvalidOperation
    import math
    value = node.get('params', {}).get('value', 0)
    try:
        if isinstance(value, bool):raise ValueError('数值不能是布尔值')
        number = Decimal(str(value))
        if not number.is_finite():raise ValueError('数值必须有限')
        if node['kind'] == 'int':
            if number != number.to_integral_value():raise ValueError('INT 输入必须是整数，不能截断小数')
            if not -(2**63) <= number <= 2**63-1:raise ValueError('INT 输入超出 64 位整数范围')
            return int(number)
        result = float(number)
        if not math.isfinite(result):raise ValueError('FLOAT 输入超出有限浮点数范围')
        return result
    except (InvalidOperation, TypeError, OverflowError) as error:
        raise ValueError('请输入有效的 ' + node['kind'].upper() + ' 数值') from error


def visible_output_ports(node, edges):
    ports = output_ports(node)
    existing = {port['key'] for port in ports}
    used = {edge.get('output', 'output') for edge in edges if edge['source'] == node['id']}
    ports.extend(port for port in output_ports(node, legacy=True)
                 if port['key'] in used and port['key'] not in existing)
    return ports


def result_groups(results):
    """Views of ordinary result lists, grouped by exact media type in order.

    Keep the canonical result records flat for task cards/snapshots. A typed
    output socket selects one such list; it never constructs a Batch.
    """
    groups = {}
    for result in results:
        groups.setdefault(result_type(result), []).append(result)
    return groups


def output_ports(node, legacy=False):
    types = output_types(node)
    kind = next(iter(types)) if len(types) == 1 else 'any'
    ports = [{'key': 'output', 'type': kind, 'label': port_type_name(kind)}]
    if legacy and node.get('kind') == 'app':
        ports.extend({'key': key, 'type': key, 'label': port_type_name(key)} for key in RESULT_TYPES)
    if legacy and (node.get('kind') == 'list_select' or collections.current(node) and node['kind'] in ('merge_list','batch2list')):
        ports.extend({'key': key, 'type': key, 'label': port_type_name(key)} for key in ITEM_OUTPUT_TYPES)
    return ports


def connection_output_types(node, key='output'):
    if key == 'output':return output_types(node)  # Legacy connections retain their selection.
    port = next((port for port in output_ports(node, legacy=True) if port['key'] == key), None)
    if port is None:raise ValueError('连线输出类型已不存在，请重新连接')
    return {port['type']}


def port_colors(document):
    """Resolve content colors in O(nodes + edges); cardinality has no color."""
    from collections import defaultdict, deque
    nodes = {node['id']: node for node in document.get('nodes', [])}
    incoming_edges, outgoing, degree = defaultdict(list), defaultdict(list), dict.fromkeys(nodes, 0)
    for edge in document.get('edges', []):
        if edge['source'] not in nodes or edge['target'] not in nodes:continue
        incoming_edges[edge['target']].append(edge);outgoing[edge['source']].append(edge['target']);degree[edge['target']] += 1
    ready = deque(key for key in nodes if not degree[key]);outputs, inputs = {}, {}
    def base(kind):return kind[:-6] if kind.endswith('_input') else kind if kind != 'batch' else 'any'
    def combine(kinds):
        kinds = set(kinds)
        return next(iter(kinds)) if len(kinds) == 1 else 'any'
    while ready:
        identity = ready.popleft();node = nodes[identity]
        upstream = {edge['input']:outputs.get((edge['source'],edge.get('output','output')), 'any') for edge in incoming_edges[identity]}
        for port in input_ports(node):
            kind = base(port['type'])
            inputs[(identity,port['key'])] = upstream.get(port['key'], 'any') if kind == 'any' else kind
        inherited = combine(upstream.values())
        if node['kind'] in ('rename', 'select', 'preview'):
            inherited = upstream.get('value', 'any')
        if node['kind'] in ('select', 'list_select', 'batch_select'):
            selected = node.get('params', {}).get('type', 'any')
            if selected != 'any':inherited = selected
        for port in output_ports(node, legacy=True):
            kind = base(port['type'])
            if kind == 'any' and node['kind'] != 'app':
                kind = inherited if node['kind'] in collections.KINDS | {'rename','select','preview'} else 'any'
            outputs[(identity,port['key'])] = kind if kind in PORT_TYPE_NAMES else 'any'
        for target in outgoing[identity]:
            degree[target] -= 1
            if not degree[target]:ready.append(target)
    return inputs, outputs


def output_types(node):
    kind = node.get('kind')
    if kind == 'app':
        declared = str(node.get('app', {}).get('model_definition', {}).get('output_type') or '').lower()
        declared = {'string': 'text', 'zip': 'archive', 'integer': 'int', 'double': 'float', 'bool': 'boolean'}.get(declared, declared)
        return {declared} if declared in PORT_TYPE_NAMES else {'any'}
    if kind in ('list2batch', 'merge_batch', 'batch_select', 'rebatch'):return {'batch'}
    if kind in MODEL_KINDS:
        return {'text'} if kind in ('llm_model', 'vision_model') else {'image'}
    if kind == 'filename':return {'text'}
    if kind in MEDIA | {'text', 'int', 'float'}:
        return {kind}
    if (kind == 'select' or kind == 'list_select' and collections.current(node)) and node.get('params', {}).get('type', 'any') != 'any':
        return {node['params']['type']}
    # App outputs are determined by actual RH result metadata, not input types.
    return {'any'}


def types_compatible(produced, accepted):
    return (produced == 'any' or accepted == 'any' or produced == accepted
            or accepted in ('image_input', 'video_input', 'audio_input', 'text_input') and produced in (accepted[:-6], 'batch')
            or accepted == 'number' and produced in NUMERIC_TYPES
            or produced == 'number' and accepted in NUMERIC_TYPES
            or accepted == 'scalar' and produced in VALUE_TYPES
            or produced == 'scalar' and accepted in VALUE_TYPES
            or accepted == 'file' and produced in MEDIA | {'file', 'archive'}
            or produced == 'text' and accepted in VALUE_TYPES)


def validate_document(document):
    """Validate structure, typed connections and DAG; return stable topological IDs."""
    if not isinstance(document, dict) or document.get('version') != VERSION:
        raise ValueError('不支持的画布文件版本')
    normalize_batch_count(document.get('batch_count', 1))
    if not isinstance(document.get('nodes'), list) or not isinstance(document.get('edges'), list):
        raise ValueError('画布节点或连线格式错误')
    document = sync_dynamic_inputs(dict(document))
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
        if not any(types_compatible(t, port['type']) for t in connection_output_types(nodes[source], edge.get('output', 'output'))):
            raise ValueError('连线两端的数据类型不兼容')
        if edge.get('mode', 'all') not in ('first', 'index', 'all'):
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
    if node['kind'] in ('int', 'float'):primitive_value(node)
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
    if node['kind'] in collections.KINDS:collections.validate(node)
    if node['kind'] == 'list_select' and not collections.current(node):
        params = node.get('params', {})
        validate_indices(params.get('indices', [1]))
        if not params.get('indices', [1]):raise ValueError('列表取项至少需要一个序号')
        if params.get('type', 'any') not in {'any'} | set(ITEM_OUTPUT_TYPES) - {'batch'}:
            raise ValueError('列表取项的内容类型无效')
        if type(params.get('keep_batch', False)) is not bool:
            raise ValueError('保留 Batch 设置格式错误')
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


def connect(document, source, target, input, mode=None, indices=None, output=None):
    source_node = next((node for node in document['nodes'] if node['id'] == source), {})
    target_node = next((node for node in document['nodes'] if node['id'] == target), {})
    port = next((port for port in input_ports(target_node) if port['key'] == input), {})
    accepted = port.get('type', '')
    if accepted.endswith('_input') and connection_output_types(source_node, output or 'output') == {'batch'}:
        unused, output_colors = port_colors(document)
        content = output_colors.get((source, output or 'output'), 'any')
        if content != 'any' and not types_compatible(content, accepted):
            raise ValueError('连线两端的内容类型不兼容；分组不会改变文本或图像等内容类型。')
    if mode is None:mode = 'all'
    edge = {'id': uuid.uuid4().hex, 'source': source, 'target': target,
            'input': input, 'mode': mode, 'indices': list(indices or [])}
    if output and output != 'output':edge['output'] = output
    candidate = dict(document, edges=list(document['edges']) + [edge])
    sync_dynamic_inputs(candidate)
    validate_document(candidate)
    document['nodes'] = candidate['nodes']
    document['edges'] = candidate['edges']
    return edge


def incoming(document, node_id):
    return [edge for edge in document['edges'] if edge['target'] == node_id]


def execution_edges(document):
    allowed={}
    for node in document['nodes']:
        if not node.get('bypass'):continue
        if node['kind'] in DYNAMIC_INPUT_KINDS:keys={port['key'] for port in input_ports(node)}
        elif node['kind'] in ('list2batch','batch2list','batch_select','rebatch','rename','filename','select','preview'):keys={'value'}
        elif node['kind'] == 'edit_model':keys={'image'}
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
    kind = {'integer': 'int', 'double': 'float', 'bool': 'boolean'}.get(kind, kind)
    if '/' in kind:
        kind = kind.split('/', 1)[0]
    if kind in ('number', 'scalar') or not kind:
        value = result.get('value')
        if type(value) is bool:return 'boolean'
        if type(value) is int:return 'int'
        if type(value) is float:return 'float'
        if kind == 'number' and isinstance(value, str):
            from decimal import Decimal, InvalidOperation
            try:
                if Decimal(value).is_finite():return 'int' if re.fullmatch(r'[+-]?\d+', value.strip()) else 'float'
            except InvalidOperation:pass
        if kind == 'scalar' and isinstance(value, str):return 'enum'
    if kind == 'file' and is_archive_result(result):return 'archive'
    if kind in MEDIA | VALUE_TYPES | {'text', 'file', 'batch', 'archive'}:
        return kind
    if 'text' in result:
        return 'text'
    path = result.get('path') or result.get('file_path') or result.get('url') or ''
    suffix = Path(str(path).split('?', 1)[0]).suffix.lower()
    for media, suffixes in MEDIA_SUFFIXES.items():
        if suffix in suffixes:
            return media
    if is_archive_result(result):return 'archive'
    mime = mimetypes.guess_type(str(path).split('?', 1)[0])[0] or ''
    prefix = mime.split('/', 1)[0]
    return prefix if prefix in MEDIA | {'text'} else 'file'


def is_archive_result(result):
    """Recognize archives without treating every opaque downloaded file as one."""
    kind = str(result.get('type') or result.get('kind') or result.get('fileType') or '').lower()
    path = result.get('path') or result.get('file_path') or result.get('url') or ''
    suffix = Path(str(path).split('?', 1)[0]).suffix.lower()
    return kind in {'archive', 'zip', 'application/zip', 'application/x-7z-compressed',
                    'application/x-rar-compressed', 'application/vnd.rar', 'application/x-tar',
                    'application/gzip'} or suffix in {'.zip', '.7z', '.rar', '.tar', '.gz', '.tgz', '.bz2', '.xz'}


def normalize_result(result):
    if isinstance(result, str):
        result = {'path': result}
    result = copy.deepcopy(result)
    if 'path' not in result and result.get('file_path'):
        result['path'] = result['file_path']
    result['type'] = result['kind'] = result_type(result)
    if result['type'] in ('int', 'float') and 'value' in result:
        result['value'] = primitive_value({'kind': result['type'], 'params': {'value': result['value']}})
    if result['type'] == 'batch':
        batch_items(result)  # Validate without silently flattening the execution unit.
    return result


def batch_items(result):
    items = result.get('items')
    if (not isinstance(items, list) or not 1 <= len(items) <= 256 or
            any(not isinstance(item, dict) or result_type(item) == 'batch' for item in items)):
        raise ValueError('Batch 需要包含 1–256 项普通结果，不支持嵌套 Batch')
    return items


def pack_batch(results, node_id):
    values = []
    for result in results:
        values.extend(batch_items(result) if result_type(result) == 'batch' else [result])
        if len(values) > 256:raise ValueError('单个 Batch 最多包含 256 项，请先过滤输入列表')
    item = {'type': 'batch', 'items': copy.deepcopy(values), 'index': 0}
    batch_items(item)
    # A grouped input has only common ancestry; conflicting per-file axes stay
    # on its members and must not make a singleton batch impossible to broadcast.
    common = lineage(values[0])
    for value in values[1:]:common = {k:v for k,v in common.items() if lineage(value).get(k) == v}
    item['lineage'] = dict(common, **{node_id: '0'})
    return item


def unpack_batches(results, node_id):
    """Expand groups in order and preserve each member's file/provenance data."""
    if not results:raise ValueError('请连接需要展开的 Batch 或列表')
    output = []
    for raw in results:
        result = normalize_result(raw)
        members = batch_items(result) if result_type(result) == 'batch' else [result]
        for member in members:
            value = normalize_result(member)
            origins = dict(lineage(result), **lineage(value))
            origins[node_id] = str(len(output))
            value.update(index=len(output), lineage=origins)
            value.pop('_restored_positions', None)
            output.append(value)
    return output


def available_results(results, signatures=None):
    """Restore usable references individually without reading/decoding media.

    The returned signatures stay paired with their original result. Missing
    historical files are a local restoration omission, never an exception.
    """
    available, kept_signatures = [], [] if isinstance(signatures, list) and len(signatures) == len(results) else None
    missing, readable, positions = False, {}, []
    accepted_types = ('any', 'image', 'image_input', 'video_input', 'audio_input', 'text_input', 'batch', 'video', 'audio', 'text', 'file', 'number', 'scalar', 'int', 'float', 'boolean', 'enum', 'archive')
    counters = dict.fromkeys(accepted_types, 0)
    for index, value in enumerate(results):
        try:
            result = normalize_result(value)
            original_positions = result.get('_restored_positions') or {}
            current_positions = {}
            for accepted in accepted_types:
                if result_matches(result, accepted):
                    counters[accepted] += 1
                    position = original_positions.get(accepted, counters[accepted])
                    if isinstance(position, int) and not isinstance(position, bool) and position > 0:
                        current_positions[accepted] = position
            path = result.get('path')
            if result_type(result) == 'batch':
                unused, unused_signatures, member_missing = available_results(batch_items(result))
                if member_missing:
                    missing = True
                    continue  # Never restore a smaller, semantically different edit batch.
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
            elif result_type(result) != 'batch' and (result_type(result) in MEDIA | {'file', 'archive'} or not any(key in result for key in ('text', 'value'))):
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
        records = container.get('results') or []
        for item in [child for item in records for child in ([item] + (batch_items(item) if isinstance(item, dict) and result_type(item) == 'batch' else []))]:
            if isinstance(item, dict) and (item.get('path') or item.get('file_path')
                                          or result_type(item) in MEDIA | {'file', 'archive'}):
                for key in payload_keys:
                    if key != 'value' or result_type(item) not in VALUE_TYPES:item.pop(key, None)
    return result


def select_results(results, edge=None, accepted='any', *, strict=False):
    edge = edge or {}
    output = edge.get('output', 'output')
    if output != 'output' and output not in ITEM_OUTPUT_TYPES:
        raise ValueError('无效的输出列表类型')
    matches = [normalize_result(result) for result in results
               if (output == 'output' or result_matches(result, output))
               and result_matches(result, accepted, connections=not strict)]
    if not matches:
        if accepted not in ('any', 'batch', 'image_input', 'video_input', 'audio_input', 'text_input') and any(result_type(r) == 'batch' for r in results):
            raise ValueError('此输入不接收 Batch；请先连接“Batch 转 List”，再逐项运行。')
        raise ValueError('上游没有符合输入类型的结果')
    mode = edge.get('mode', 'all')
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
                          if result.get('_restored_positions', {}).get(output if output != 'output' else accepted) == index]
            if len(candidates) != 1:
                raise ValueError('所选历史结果已不可用，或其原始序号无法确定')
            selected.append(candidates[0])
        return selected
    if max(indices) > len(matches):
        raise ValueError('所选结果序号超出范围（共 {} 项）'.format(len(matches)))
    return [matches[index - 1] for index in indices]


def select_list_items(node, inputs):
    """Select inside each typed input list / Batch, without pairing streams."""
    validate_node(node)
    params = node.get('params', {})
    selected_type = params.get('type', 'any')
    indices = params.get('indices', [1])
    output = []
    for port_index, port in enumerate(input_ports(node), 1):
        groups, ordinary = [], {}
        for value in inputs.get(port['key'], []):
            kind = result_type(value)
            if kind == 'batch':
                groups.append((value, batch_items(value)))
            else:
                if kind not in ordinary:
                    ordinary[kind] = []
                    groups.append((None, ordinary[kind]))
                ordinary[kind].append(value)
        for group_index, (batch, members) in enumerate(groups, 1):
            members = [v for v in members if result_matches(v, selected_type)]
            if not members:continue  # Explicit type filtering removes this group.
            label = 'Batch' if batch else ITEM_OUTPUT_TYPES.get(result_type(members[0]), '内容') + ' List'
            try:
                chosen = select_results(members, {'mode': 'index', 'indices': indices},
                                        selected_type if selected_type != 'any' else 'any' if batch else result_type(members[0]))
            except ValueError as error:
                raise ValueError(f'输入 {port_index} · 第 {group_index} 组 {label}（{len(members)} 项）：{error}') from error
            if batch:
                for value in chosen:value['lineage'] = dict(lineage(batch), **lineage(value))
            for value in chosen:value.pop('_restored_positions', None)
            if batch and params.get('keep_batch', False):
                result = pack_batch(chosen, node['id'])
                result['index'] = len(output)
                result['lineage'][node['id']] = str(len(output))
                output.append(result)
            else:
                for value in chosen:
                    value['index'] = len(output)
                    value['lineage'] = dict(lineage(value), **{node['id']: str(len(output))})
                    value.pop('_restored_positions', None)
                    output.append(value)
    if not output:raise ValueError('没有可选的列表项，请检查输入连接和内容类型')
    return output


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
    if result['type'] == 'batch':
        return {'type': 'batch', 'content': [dict(signature=result_signature(item),
                    reference=item.get('_file_identity') or item.get('path') or item.get('file_path') or '',
                    name=Path(item.get('path') or item.get('file_path') or item.get('name') or '').name)
                    for item in batch_items(result)]}
    path = result.get('path')
    if path:
        if not os.path.isfile(path):
            raise ValueError('结果文件不存在：' + str(path))
        content = file_hash(path)
    else:
        if result['type'] in MEDIA | {'file', 'archive'} or not any(key in result for key in ('text','value')):
            raise ValueError('结果没有可读取的文件或内容')
        content = result.get('text', result.get('value', ''))
    signature = {'type': result['type'], 'content': content,
            'generation': result.get('generation', ''),
            'task_id': result.get('task_id', ''), 'index': result.get('index', 0)}
    if not path and result.get('name'):signature['name'] = result['name']
    return signature


def results_valid(results, signatures=None):
    try:
        if any(isinstance(r, dict) and result_type(r) == 'batch' and not results_valid(batch_items(r)) for r in results):return False
        if any(isinstance(result,dict) and result.get(key) and not os.path.isfile(result[key]) for result in results
               for key in ('paint_temp_path','composite_path')):return False
        actual = [result_signature(result) for result in results]
        return bool(actual) and (signatures is None or actual == signatures)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def bypass_results(node, inputs):
    """Bypass one output to the first connected input of each compatible type."""
    keys=[port['key'] for port in input_ports(node) if port['key'] in inputs]
    if node['kind'] in DYNAMIC_INPUT_KINDS:
        return copy.deepcopy([value for key in keys for value in inputs[key]])
    if node['kind'] in ('list2batch','batch2list','batch_select','rebatch','rename','select','preview','filename'):keys=['value'] if 'value' in inputs else []
    result=[];claimed=set();produced=output_types(node)
    for key in keys:
        values=inputs[key]
        kinds={result_type(value) for value in values}
        eligible={kind for kind in kinds if kind not in claimed and (node['kind'] in collections.KINDS or node['kind'] == 'edit_model' and kind == 'batch' or any(types_compatible(kind,p) for p in produced))}
        result.extend(copy.deepcopy(value) for value in values if result_type(value) in eligible)
        claimed.update(eligible)
    return result


def canonical_fields(node):
    fields = copy.deepcopy(app_fields(node))
    for field in fields:
        if node.get('app', {}).get('backend') == 'rh_llm' and field_type(field) == 'text':field['_canvas_text_batch'] = True
        key = parameter_key(field)
        if key in node.get('params', {}):
            field['fieldValue'] = copy.deepcopy(node['params'][key])
        mask = node.get('params', {}).get('_masks', {}).get(key)
        if mask:field['_mask'] = copy.deepcopy(mask)
        if field_type(field) in MEDIA | {'file', 'archive', 'image_input', 'video_input', 'audio_input'}:
            field['_rh_upload'] = True
    return fields


def input_value(result, field):
    accepted = field_type(field)
    if accepted == 'text_input':
        members = batch_items(result) if result_type(result) == 'batch' else [result]
        if any(result_type(value) != 'text' for value in members):raise ValueError('提示词输入需要文本内容；文本 Batch 内不能混入其他类型。')
        return '\n\n'.join(input_value(value, {'fieldType': 'STRING'}) for value in members)
    if accepted in ('image_input', 'video_input', 'audio_input'):
        values = batch_items(result) if result_type(result) == 'batch' else [result]
        if any(result_type(value) != accepted[:-6] or not os.path.isfile(value.get('path', '')) for value in values):
            raise ValueError('多文件输入需要同一类型的有效媒体文件：' + accepted[:-6])
        return [value['path'] for value in values]
    if accepted in MEDIA | {'file', 'archive'}:
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
    if accepted in NUMERIC_TYPES:
        from decimal import Decimal, InvalidOperation
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise ValueError('上游输入不是有效数值') from error
        if not number.is_finite():
            raise ValueError('数值输入必须有限')
        if str(field.get('fieldType', '')).lower() in ('int', 'integer') and number != number.to_integral_value():
            raise ValueError('INT 输入必须是整数，不能截断小数')
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
    elif accepted in ('scalar', 'boolean', 'enum'):
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
        if field_type(field) in MEDIA | {'file', 'archive', 'image_input', 'video_input', 'audio_input'} and parameter_key(field) not in inputs:
            from aetherloom_core.rh_multi_inputs import values as media_values
            def signature(path):
                return {'sha256': file_hash(path)} if isinstance(path, str) and os.path.isfile(path) else path
            field['fieldValue'] = ([signature(path) for path in media_values(value)]
                                   if field.get('_model_multiple') or isinstance(value, list) else signature(value))
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
               'decode_settings': node.get('decode_settings', {}) if supports_local_decode(node) else {},
               # Preserve cache identity for legacy single-run nodes only.
               'run_count': 1,
               'inputs': {key: [result_signature(r) for r in values] for key, values in inputs.items()},
               'edges': [{key: edge.get(key) for key in ('source', 'input', 'mode', 'indices') + (('output',) if edge.get('output') not in (None, 'output') else ())}
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
    if app.get('backend') in ('rh_standard', 'rh_llm'):
        payload['backend'] = app['backend']
        payload['model_definition'] = app.get('model_definition')
    if any(lineage(r) for values in inputs.values() for r in values):
        payload['input_lineage'] = {key: [lineage(r) for r in values] for key, values in inputs.items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode('utf-8')).hexdigest()
