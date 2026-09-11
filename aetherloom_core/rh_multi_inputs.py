"""Ordered standard-model media arrays, including legacy numbered fields."""
import ast
import copy
import json
import os

MEDIA = {'IMAGE', 'VIDEO', 'AUDIO'}


def values(value):
    if isinstance(value, str) and value.lstrip().startswith('[') and len(value) < 1024 * 1024:
        try:
            decoded = ast.literal_eval(value)
            if isinstance(decoded, list) and all(isinstance(v, str) for v in decoded):value = decoded
        except (ValueError, SyntaxError, MemoryError, RecursionError):pass
    return [v for v in (value if isinstance(value, (list, tuple)) else [value]) if v not in (None, '', 'empty')]


def groups(fields, definition=None):
    params = {p['fieldKey']: p for p in (definition or {}).get('params', [])}
    grouped = {}
    for index, field in enumerate(fields):
        if field.get('_model_multiple') and str(field.get('fieldType')).upper() in MEDIA:
            grouped.setdefault(field.get('_model_field') or field['fieldName'], []).append(index)
    result = {}
    for key, indices in grouped.items():
        first = fields[indices[0]]
        param = copy.deepcopy(params.get(key) or first.get('_model_constraints') or {})
        param.setdefault('type', first['fieldType'])
        param.setdefault('fieldKey', key)
        param.setdefault('maxInputNum', max(1, len(indices)))
        result[indices[0]] = dict(indices=indices, param=param)
    return result


def group_values(fields, indices):
    return [value for index in indices for value in values(fields[index].get('fieldValue'))]


def distribute(fields, indices, paths):
    """Keep legacy port IDs and scalar positions; last slot can hold overflow."""
    paths = list(paths)
    for offset, index in enumerate(indices):
        field = fields[index]
        if field.get('_model_array') or offset == len(indices)-1 and len(paths) > len(indices):
            value = paths[offset:]
        else:value = paths[offset] if offset < len(paths) else ''
        field['fieldValue'] = copy.deepcopy(value)


def file_suffixes(param):
    accepted = param.get('accept', [])
    if isinstance(accepted, str):
        try:accepted = json.loads(accepted)
        except ValueError:accepted = []
    from .canvas.model import MEDIA_SUFFIXES
    accepted = accepted or MEDIA_SUFFIXES.get(str(param.get('type', '')).lower(), ())
    suffixes = {'.' + str(s).lower().lstrip('.') for s in accepted}
    for aliases in ({'.jpeg', '.jpg'}, {'.tif', '.tiff'}):
        if suffixes & aliases:suffixes.update(aliases)
    return suffixes


def validate_file(path, param):
    if not isinstance(path, str):raise ValueError('媒体输入需要文件路径或 HTTPS 地址')
    if path.startswith('https://'):return
    if not os.path.isfile(path):raise ValueError('输入文件不存在：' + os.path.basename(path))
    if os.path.getsize(path) > float(param.get('maxSize') or 100) * 1024 * 1024:
        raise ValueError('输入文件超过模型大小限制：' + os.path.basename(path))
    accepted = file_suffixes(param)
    suffix = os.path.splitext(path)[1].lower()
    if accepted and suffix not in accepted:
        raise ValueError('模型不支持此文件格式：' + suffix)
