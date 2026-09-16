"""Presentation-only result access; never changes execution references."""
import os
from bisect import bisect_right
from array import array
from . import model

TYPE_NAMES = {'bounding': 'Bounding', 'mask': '遮罩', 'image': '图像', 'video': '视频', 'audio': '音频', 'text': '文本',
              'int': 'INT', 'float': 'FLOAT', 'boolean': '布尔', 'enum': '枚举', 'archive': '压缩文件', 'number': '数值', 'scalar': '数值', 'file': '文件', 'folder': '文件夹', 'batch': 'Batch'}


class ResultIndex:
    """Index the presentation of Batch members once, without copying media data."""
    def __init__(self, results):
        self.source = results
        self.source_length = len(results)
        self.ends, self.groups = [], []
        self.total = self.batch_count = 0
        self._types = None
        for value in results:
            value = value_record(value)
            if kind_of(value) == 'batch':
                self.batch_count += 1
                try:members = model.batch_items(value)
                except ValueError:
                    members = [dict(type='file', _preview_error='Batch 结果格式无效，请重新运行此节点')]
                self.groups.append((members, self.batch_count))
                self.total += len(members)
            else:
                self.groups.append((value, 0));self.total += 1
            self.ends.append(self.total)

    def matches(self, results):
        return self.source is results and self.source_length == len(results)

    def item(self, index):
        if not 0 <= index < self.total:raise IndexError(index)
        group = bisect_right(self.ends, index)
        value, batch = self.groups[group]
        if not batch:return value
        offset = index - (self.ends[group-1] if group else 0)
        return dict(value[offset], _batch_label=f'Batch {batch} · {offset+1}/{len(value)}')

    def type_indices(self):
        if self._types is None:
            self._types = {}
            index = 0
            for value, batch in self.groups:
                for member in value if batch else (value,):
                    self._types.setdefault(kind_of(member), array('I')).append(index)
                    index += 1
        return self._types


def can_open(value):
    path = path_of(value)
    return bool(path and os.path.exists(path) or any(key in value for key in ('text', 'value')))


def value_record(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {'path': value}
    return {'value': value, 'type': 'scalar'}


def path_of(value):
    return str(value.get('path') or value.get('file_path') or '')


def text_of(value):
    for key in ('text', 'value'):
        if key in value:
            return str(value[key])
    return ''


def has_inputs(node):
    return node['kind'] in model.MEDIA | {'text'}


def has_batches(node):
    results = node.get('results') or []
    if (not node.get('bypass') and not node.get('bypassed')
            and node['kind'] in model.MEDIA | set(model.MODEL_KINDS) | {'app', 'text', 'text_file', 'batch2list', 'filename', 'rename'}):
        return False  # These executors produce ordinary items; avoid scanning large lists on paint.
    return any(isinstance(result, dict) and model.result_type(result) == 'batch' for result in results)


def count(node, source='results'):
    if source == 'input':
        return 1 if node['kind'] == 'text' else len(node.get('params', {}).get('files') or [])
    results = node.get('results') or []
    if has_batches(node):return sum(len(model.batch_items(r)) if model.result_type(r) == 'batch' else 1 for r in results)
    return len(results)


def item_at(node, index, source='results'):
    if source != 'input':
        if has_batches(node):
            for group, record in enumerate(node['results']):
                members = model.batch_items(record) if model.result_type(record) == 'batch' else [record]
                if index < len(members):
                    return (dict(members[index], _batch_label=f'B{group+1} · {index+1}')
                            if model.result_type(record) == 'batch' else value_record(record))
                index -= len(members)
            raise IndexError(index)
        return value_record(node['results'][index])
    if node['kind'] == 'text':
        return {'text': node.get('params', {}).get('text', ''), 'type': 'text'}
    path = node['params']['files'][index]
    value = {'path': path, 'type': node['kind']}
    if os.path.isdir(path):
        return dict(value, type='folder', text='运行时读取文件夹内符合格式的文件')
    if node['kind'] == 'image' and not (node.get('stale') or node.get('_ui_stale')):
        generated = next((r for r in node.get('results', []) if isinstance(r, dict)
                          and r.get('source_path') == path and r.get('composite_path')), None)
        if generated and os.path.isfile(generated['composite_path']):
            value = dict(value, path=generated['composite_path'], source_path=path)
    return value


def kind_of(value):
    return 'folder' if value.get('type') == 'folder' else model.result_type(value)


def title_of(value):
    path = value.get('source_path') or path_of(value)
    if path:
        return os.path.basename(path) or path
    text = text_of(value)[:100].replace('\n', ' ').strip()
    return text[:100] or ('空文本' if kind_of(value) == 'text' else str(value.get('url') or '未命名结果'))
