"""Deterministic local prompt tools with libraries stored in the canvas document."""
import copy
import csv
import io
import json
import random
import re


CACHE_VERSION = 1
MAX_TEXT = 4 * 1024 * 1024
MAX_ITEMS = 10000
MAX_STYLES = 1000
SCHEMAS = {
    'text_random_line': ('随机文本行', 'text_tools', [
        ('text', '候选文本（每行一项）', 'text', '', None),
        ('seed', '随机种子', 'int', 0, (0, 2**64 - 1)),
        ('count', '抽取数量', 'int', 1, (1, MAX_ITEMS)),
        ('unique', '去重且不重复抽取', 'bool', True, None),
    ]),
    'text_wildcards': ('提示词通配符', 'text_tools', [
        ('text', '提示词', 'text', '', None),
        ('wildcards', '词库（JSON）', 'wildcards', '{}', None),
        ('seed', '随机种子', 'int', 0, (0, 2**64 - 1)),
        ('max_depth', '最大展开层数', 'int', 16, (1, 32)),
    ]),
    'prompt_styles': ('提示词样式', 'text_tools', [
        ('positive', '正向提示词', 'text', '', None),
        ('negative', '反向提示词', 'text', '', None),
    ]),
}
KINDS = frozenset(SCHEMAS)
HINTS = {
    'text_random_line': '跳过空行，输出文本 List。开启去重时按文本内容去重并不放回抽取；数量不能超过有效行数。普通 List 逐项计算，相同内容与种子得到相同结果。',
    'text_wildcards': '使用 __词库名__ 和 <选项一|选项二>，支持嵌套；缺少词库或循环引用会报错。词库保存在节点内，也可连接 JSON 文本。不会读取外部词库目录；相同输入和种子得到相同结果。',
    'prompt_styles': '按所选顺序依次组合样式。{prompt} 代入当前提示词；没有占位符时用逗号追加。上方文本输出为正向，下方为反向；未选样式时原样输出。样式库独立保存在本节点。',
}
DESCRIPTIONS = HINTS


def defaults(kind):
    params = {key: copy.deepcopy(default) for key, _, _, default, _ in SCHEMAS[kind][2]}
    if kind == 'prompt_styles':params.update(styles=[], selected_styles=[])
    return params


def inputs(node):
    ports = []
    for key, label, editor, _, _ in SCHEMAS[node['kind']][2]:
        if editor in ('text', 'wildcards', 'int'):
            ports.append(dict(key=key, label=label, type='int' if editor == 'int' else 'text'))
    return ports


def output_type(node):
    return 'text'


def wildcard_text(value):
    """Use the same editable JSON for stored mappings and shared text refreshes."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def _text(value, label='文本'):
    if not isinstance(value, str):raise ValueError(label + '必须是文本')
    if len(value) > MAX_TEXT:raise ValueError(label + '超过 4 MiB 字符上限')
    return value


def _integer(value, label, bounds):
    try:
        number = int(value)
        if isinstance(value, bool) or str(number) != str(value) or not bounds[0] <= number <= bounds[1]:raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError(label + '超出允许的整数范围') from None
    return number


def parse_wildcards(value):
    """Accept a JSON object or a stored mapping; never interpret a value as a path."""
    if isinstance(value, str):
        _text(value, '词库')
        try:value = json.loads(value or '{}')
        except (ValueError, RecursionError):raise ValueError('词库需要 JSON 对象，例如 {"天气": ["晴天", "雨天"]}') from None
    if not isinstance(value, dict) or len(value) > MAX_ITEMS:
        raise ValueError('词库需要不超过 10000 项的 JSON 对象')
    result = {};total = 0;count = 0
    for name, entries in value.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(c in name for c in '\r\n') or '__' in name:
            raise ValueError('词库名称不能为空、超过 128 字符或包含换行和双下划线')
        if name != name.strip():raise ValueError('词库名称首尾不能有空格：' + name)
        if isinstance(entries, str):entries = entries.splitlines()
        if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
            raise ValueError('词库 ' + name + ' 需要文本数组或按行分隔的文本')
        entries = [item.strip() for item in entries if item.strip()]
        if not entries:raise ValueError('词库没有有效候选项：' + name)
        count += len(entries);total += len(name) + sum(map(len, entries))
        if count > MAX_ITEMS or total > MAX_TEXT:raise ValueError('词库最多 10000 个候选项、4 MiB 字符')
        result[name] = entries
    return result


def normalize_styles(value):
    """Normalize A1111-style rows, named mappings and the saved node format."""
    if isinstance(value, str):
        _text(value, '样式库')
        try:value = json.loads(value)
        except (ValueError, RecursionError):raise ValueError('样式库 JSON 格式无效') from None
    if isinstance(value, dict):
        if isinstance(value.get('styles'), list):value = value['styles']
        elif 'name' in value:value = [value]
        else:
            rows = []
            for name, style in value.items():
                if isinstance(style, str):style = {'positive': style}
                if not isinstance(style, dict):raise ValueError('每个样式需要正向和反向提示词')
                rows.append(dict(style, name=name))
            value = rows
    if not isinstance(value, list) or len(value) > MAX_STYLES:
        raise ValueError('样式库需要不超过 1000 项的样式列表')
    result = [];names = set();total = 0
    for row in value:
        if not isinstance(row, dict):raise ValueError('样式记录格式无效')
        name = row.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ValueError('样式名称不能为空或超过 256 字符')
        name = name.strip()
        if name in names:raise ValueError('样式名称重复：' + name)
        positive = _text(row.get('positive', row.get('prompt', '')), '样式正向提示词')
        negative = _text(row.get('negative', row.get('negative_prompt', '')), '样式反向提示词')
        total += len(name) + len(positive) + len(negative)
        if total > MAX_TEXT:raise ValueError('样式库超过 4 MiB 字符上限')
        names.add(name);result.append(dict(name=name, positive=positive, negative=negative))
    return result


def import_styles(text, file_format):
    """Parse explicitly chosen UTF-8 CSV/JSON content; this function has no I/O."""
    text = _text(text.lstrip('\ufeff'), '样式文件')
    if file_format.lower().lstrip('.') == 'json':return normalize_styles(text)
    if file_format.lower().lstrip('.') != 'csv':raise ValueError('仅支持 CSV 或 JSON 样式文件')
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''))
        if not reader.fieldnames or 'name' not in reader.fieldnames or not ({'prompt', 'positive'} & set(reader.fieldnames)):
            raise ValueError('CSV 需含 name、prompt、negative_prompt 列（后者可省略）')
        rows = []
        for row in reader:
            if None in row:raise ValueError('CSV 列数不一致，请检查引号与逗号')
            rows.append(row)
            if len(rows) > MAX_STYLES:raise ValueError('样式库最多 1000 项')
        return normalize_styles(rows)
    except csv.Error as error:raise ValueError('CSV 样式文件格式无效：' + str(error)) from error


def selected_styles(params, library=None):
    selected = params.get('selected_styles', [])
    if isinstance(selected, str):selected = [selected] if selected else []
    if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
        raise ValueError('所选样式需要名称列表')
    if len(selected) != len(set(selected)):raise ValueError('不能重复选择同一样式')
    names = {style['name'] for style in (library if library is not None else normalize_styles(params.get('styles', [])))}
    for name in selected:
        if name not in names:raise ValueError('所选样式已不存在：' + name)
    return list(selected)


def validate(node):
    params = dict(defaults(node['kind']), **node.get('params', {}))
    for key, label, editor, _, bounds in SCHEMAS[node['kind']][2]:
        value = params[key]
        if editor == 'int':_integer(value, label, bounds)
        elif editor == 'text':_text(value, label)
        elif editor == 'bool' and type(value) is not bool:raise ValueError(label + '必须为开关值')
        elif editor == 'wildcards':parse_wildcards(value)
    if node['kind'] == 'prompt_styles':selected_styles(params, normalize_styles(params['styles']))


def _check_stop(stop):
    if stop.is_set():
        from .save_results import SaveCanceled
        raise SaveCanceled('提示词处理已取消')


_WILDCARD = re.compile(r'__([^\r\n]{1,128}?)__')


def expand_wildcards(text, library, seed=0, max_depth=16, stop=None):
    """Expand balanced choices and wildcard references with bounded recursion."""
    text = _text(text);rng = random.Random(seed);budget = [MAX_ITEMS]

    def check():
        if stop is not None:_check_stop(stop)

    def spend():
        check();budget[0] -= 1
        if budget[0] < 0:raise ValueError('通配符展开超过 10000 次，请简化提示词或词库')

    def brackets(value):
        pairs = {};stack = [];i = 0
        while i < len(value):
            if i % 1024 == 0:check()
            if value[i] == '\\' and i + 1 < len(value):i += 2;continue
            if value[i] == '<':stack.append(i)
            elif value[i] == '>' and stack:pairs[stack.pop()] = i
            i += 1
        return pairs

    def alternatives(value):
        result = [];start = 0;depth = 0;i = 0
        while i < len(value):
            if i % 1024 == 0:check()
            if value[i] == '\\' and i + 1 < len(value):i += 2;continue
            if value[i] == '<':depth += 1
            elif value[i] == '>':depth = max(0, depth - 1)
            elif value[i] == '|' and depth == 0:result.append(value[start:i]);start = i + 1
            i += 1
        result.append(value[start:]);return result

    def expand(value, depth, chain):
        if depth > max_depth:raise ValueError('通配符超过最大展开层数：' + str(max_depth))
        pairs = brackets(value);parts = [];size = 0;i = 0
        while i < len(value):
            if i % 1024 == 0:check()
            token = value[i];advance = 1
            if token == '\\' and i + 1 < len(value) and value[i + 1] in '<>|_\\':
                token = value[i + 1];advance = 2
            elif value.startswith('__', i):
                match = _WILDCARD.match(value, i)
                if match:
                    spend();name = match.group(1)
                    if name not in library:raise ValueError('词库不存在：' + name)
                    if name in chain:raise ValueError('词库循环引用：' + ' → '.join((*chain, name)))
                    token = expand(rng.choice(library[name]), depth + 1, (*chain, name));advance = match.end() - i
            elif token == '<' and i in pairs:
                end = pairs[i];content = value[i + 1:end];choices = alternatives(content)
                if len(choices) > 1:
                    spend();token = expand(rng.choice(choices), depth + 1, chain)
                else:
                    token = '<' + expand(content, depth + 1, chain) + '>'
                advance = end + 1 - i
            size += len(token)
            if size > MAX_TEXT:raise ValueError('展开后的提示词超过 4 MiB 字符上限')
            parts.append(token);i += advance
        return ''.join(parts)

    return expand(text, 0, ())


def _apply_style(prompt, template):
    if '{prompt}' in template:return template.replace('{prompt}', prompt)
    return ', '.join(part for part in (prompt, template) if part)


def execute(node, directory, batches, stop):
    """Process ordinary List items individually; return tagged, typed text results."""
    from . import model
    output = [];kind = node['kind'];base = dict(defaults(kind), **node.get('params', {}))
    for batch_index, batch in enumerate(batches):
        _check_stop(stop);params = copy.deepcopy(base)
        for key, label, editor, _, bounds in SCHEMAS[kind][2]:
            if key in batch:
                expected = 'int' if editor == 'int' else 'text'
                if model.result_type(batch[key]) != expected:
                    raise ValueError(label + '需要 ' + ('INT' if expected == 'int' else '文本') + '；Batch 请先转为 List')
                params[key] = model.input_value(batch[key], {'fieldType': 'INT' if expected == 'int' else 'STRING'})
            if editor == 'int':params[key] = _integer(params[key], label, bounds)
            elif editor == 'text':_text(params[key], label)
            elif editor == 'bool' and type(params[key]) is not bool:raise ValueError(label + '必须为开关值')
        if kind == 'text_random_line':
            rows = [line.strip() for line in params['text'].splitlines() if line.strip()]
            if params['unique']:rows = list(dict.fromkeys(rows))
            if not rows:raise ValueError('没有可抽取的有效文本行')
            if len(rows) > MAX_ITEMS:raise ValueError('候选文本最多 10000 行')
            rng = random.Random(params['seed']);count = params['count']
            if params['unique'] and count > len(rows):raise ValueError('抽取数量超过去重后的有效行数')
            chosen = rng.sample(rows, count) if params['unique'] else [rng.choice(rows) for _ in range(count)]
            results = [dict(type='text', text=text) for text in chosen]
        elif kind == 'text_wildcards':
            text = expand_wildcards(params['text'], parse_wildcards(params['wildcards']), params['seed'], params['max_depth'], stop)
            results = [dict(type='text', text=text)]
        elif kind == 'prompt_styles':
            library = normalize_styles(params['styles']);lookup = {style['name']: style for style in library}
            positive, negative = params['positive'], params['negative']
            for name in selected_styles(params, library):
                _check_stop(stop);style = lookup[name]
                positive = _text(_apply_style(positive, style['positive']), '组合后的正向提示词')
                negative = _text(_apply_style(negative, style['negative']), '组合后的反向提示词')
            results = [dict(type='text', text=positive, _prompt_port='positive'),
                       dict(type='text', text=negative, _prompt_port='negative')]
        else:raise ValueError('不支持的提示词节点')
        if len(output) + len(results) > MAX_ITEMS:raise ValueError('单个提示词节点最多输出 10000 条结果')
        for result in results:
            _check_stop(stop)
            origin = batch_index if kind == 'prompt_styles' else len(output)
            result.update(index=len(output), lineage=model.result_lineage(batch, node['id'], origin))
            if kind == 'prompt_styles':result['lineage']['__prompt_group__:' + node['id']] = str(batch_index)
            output.append(result)
    _check_stop(stop)
    return output
