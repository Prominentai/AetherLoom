"""List is the execution sequence; a Batch is one atomic item in that sequence.

Inspired by ComfyUI's INPUT_IS_LIST / OUTPUT_IS_LIST and image rebatching.
AetherLoom keeps file references rather than tensors, never resizes media and
retains strict length/range checks instead of repeating or clamping values.
"""
import copy

KINDS = frozenset({'list2batch', 'batch2list', 'merge_list', 'merge_batch',
                   'list_select', 'batch_select', 'rebatch'})
TITLES = {'list2batch': 'List 转 Batch', 'batch2list': 'Batch 转 List',
          'merge_list': '合并 List', 'merge_batch': '合并 Batch',
          'list_select': 'List 取项', 'batch_select': 'Batch 取项', 'rebatch': 'Batch 重组'}
HELP = {
    'list2batch': ('List → Batch', '按类型收集全部列表项，组成 Batch。已有 Batch 的成员也参与收集。', '三张图像 → 一个含三张图像的 Batch'),
    'batch2list': ('Batch → List', '按组顺序拆出每个 Batch 的成员。普通项原样通过。', '一个含三张图像的 Batch → 三个图像列表项'),
    'merge_list': ('List + List → List', '按输入端顺序连接列表；Batch 保持一个完整列表项。结果从同一端口输出，保留每项的原内容类型。', '[Batch A, 图像] + [Batch B] → [Batch A, 图像, Batch B]'),
    'merge_batch': ('多个 Batch → Batch', '展开所有输入的成员，再按类型合并。不会把不同类型放进同一个 Batch。', '两图 Batch + 三图 Batch → 五图 Batch'),
    'list_select': ('List → 所选列表项', '对每个输入端的各类型 List 独立取项；选择 Batch 类型时取出整组，不进入组内。', '[Batch A, Batch B]，索引 1 → 完整的 Batch B'),
    'batch_select': ('Batch → 所选成员的 Batch', '对输入列表里的每个 Batch 独立取成员，输出仍为 Batch；转为逐项执行请接 Batch 转 List。', '三图 Batch，索引 1 → 含第二张图的 Batch'),
    'rebatch': ('Batch → 重新分组的 Batch List', '按类型收集输入成员，再按组大小切分。最后不足一组时保留实际成员，不补尾。', '五张图，组大小 2 → 三个 Batch：2、2、1'),
}


def current(node):
    return node.get('kind') in KINDS and node.get('params', {}).get('collection_version', 1) == 2


def defaults(kind):
    values = {'collection_version': 2}
    if kind in ('list_select', 'batch_select'):
        values.update(type='any', selection_mode='indices', indices=[0], start=0, length=1)
    if kind == 'list2batch':values['batch_size'] = 0
    if kind == 'rebatch':values['batch_size'] = 1
    return values


def upgraded(node):
    values = copy.deepcopy(node.get('params', {}))
    indices = values.get('indices', [1])
    content_type = values.get('type', 'any')
    values.update(defaults(node['kind']))
    if node['kind'] == 'list_select':
        values['type'] = content_type
        values['indices'] = [i - 1 for i in indices] or [0]
        values.pop('keep_batch', None)
    return values


def validate(node):
    from . import model
    params = node.get('params', {})
    version = params.get('collection_version', 1)
    if type(version) is not int or version not in (1, 2):raise ValueError('不支持的 List / Batch 节点规则版本')
    if node['kind'] in ('batch_select', 'rebatch') and version != 2:raise ValueError('此节点需要新版 List / Batch 规则')
    if not current(node):return
    kind = node['kind']
    if kind in ('list_select', 'batch_select'):
        allowed = set(model.ITEM_OUTPUT_TYPES) | {'any'}
        if kind == 'batch_select':allowed.remove('batch')
        if params.get('type', 'any') not in allowed:raise ValueError('取项内容类型无效')
        if params.get('selection_mode', 'indices') not in ('indices', 'range'):raise ValueError('取项方式无效')
        indices = params.get('indices', [0])
        if (not isinstance(indices, list) or not 1 <= len(indices) <= 4096
                or any(type(i) is not int or abs(i) > 1_000_000 for i in indices)):
            raise ValueError('索引必须为整数；0 是第一项，-1 是最后一项，最多指定 4096 项')
        if type(params.get('start', 0)) is not int or abs(params.get('start', 0)) > 1_000_000:
            raise ValueError('起始索引无效')
        if type(params.get('length', 1)) is not int or not 1 <= params.get('length', 1) <= 1_000_000:
            raise ValueError('取项数量必须为正整数')
    if kind in ('list2batch', 'rebatch'):
        size = params.get('batch_size', 0 if kind == 'list2batch' else 1)
        if type(size) is not int or not (0 if kind == 'list2batch' else 1) <= size <= 256:
            raise ValueError('组大小必须为 1–256；List 转 Batch 可设为 0 表示全部')


def _pick(values, params, label):
    count = len(values)
    if not count:return []
    if params.get('selection_mode', 'indices') == 'range':
        start = params.get('start', 0)
        start = start + count if start < 0 else start
        end = start + params.get('length', 1)
        if start < 0 or end > count:raise ValueError(f'{label} 只有 {count} 项，所选范围越界；未截断或补项')
        indices = range(start, end)
    else:
        indices = [i + count if i < 0 else i for i in params.get('indices', [0])]
        if any(i < 0 or i >= count for i in indices):
            raise ValueError(f'{label} 只有 {count} 项，有效索引为 0–{count - 1}；未截断或补项')
    return [values[i] for i in indices]


def execute(node, inputs):
    from . import model
    validate(node)
    kind, params, identity = node['kind'], node.get('params', {}), node['id']
    streams = [(index + 1, inputs[port['key']]) for index, port in enumerate(model.input_ports(node)) if port['key'] in inputs]
    values = [value for _, stream in streams for value in stream]
    if not values:raise ValueError('请连接需要处理的 List 或 Batch')
    if kind == 'merge_list':
        output = values  # Never unwrap an atomic Batch here.
    elif kind == 'batch2list':
        return model.unpack_batches(values, identity)
    elif kind in ('list2batch', 'merge_batch', 'rebatch'):
        flat = [member for value in values for member in (model.batch_items(value) if model.result_type(value) == 'batch' else [value])]
        output = []
        size = params.get('batch_size', 1 if kind == 'rebatch' else 0) if kind != 'merge_batch' else 0
        for value_type, members in model.result_groups(flat).items():
            chunk = size or len(members)
            if chunk > 256:raise ValueError(f'{model.ITEM_OUTPUT_TYPES.get(value_type, value_type)} 有 {len(members)} 项；单组上限 256，请设置组大小或使用 Batch 重组')
            for start in range(0, len(members), chunk):output.append(model.pack_batch(members[start:start + chunk], identity))
    elif kind == 'list_select':
        output = []
        for port_index, stream in streams:
            for value_type, members in model.result_groups(stream).items():
                if not model.result_matches(members[0], params.get('type', 'any')):continue
                output.extend(_pick(members, params, f'输入 {port_index} · {model.ITEM_OUTPUT_TYPES.get(value_type, value_type)} List'))
    elif kind == 'batch_select':
        output = []
        for index, batch in enumerate(values):
            if model.result_type(batch) != 'batch':raise ValueError('Batch 取项只接受 Batch；普通 List 请使用 List 取项或先转 Batch')
            members = [v for v in model.batch_items(batch) if model.result_matches(v, params.get('type', 'any'))]
            chosen = _pick(members, params, f'Batch {index + 1}')
            if chosen:
                chosen = copy.deepcopy(chosen)
                for value in chosen:value['lineage'] = dict(model.lineage(batch), **model.lineage(value))
                output.append(model.pack_batch(chosen, identity))
    else:raise ValueError('未知 List / Batch 节点')
    if not output:raise ValueError('没有符合内容类型的结果')
    output = copy.deepcopy(output)
    for index, value in enumerate(output):
        value.update(index=index, lineage=dict(model.lineage(value), **{identity: str(index)}))
        value.pop('_restored_positions', None)
    return output
