"""Bounded local text imports, frozen once before cache lookup or execution."""
import codecs
import copy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import uuid


TEXT_SUFFIXES = frozenset({'.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.yaml', '.yml', '.log'})
ENCODINGS = (('auto', '自动（UTF-8 / UTF-16 / GB18030）'),
             ('utf-8-sig', 'UTF-8'), ('utf-16', 'UTF-16（含 BOM）'),
             ('gb18030', 'GB18030 / GBK'))
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 512
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def _check_stop(stop):
    if stop is not None and stop.is_set():
        from .save_results import SaveCanceled
        raise SaveCanceled('读取文本文件已取消')


def encoding_name(value):
    value = 'utf-8-sig' if value == 'utf-8' else value
    if value not in dict(ENCODINGS):
        raise ValueError('不支持的文本编码，请选择自动、UTF-8、UTF-16 或 GB18030')
    return value


def validate_params(params):
    paths = params.get('files', [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError('文本输入路径必须是文件或文件夹路径列表')
    if len(paths) > MAX_FILES:
        raise ValueError('文本文件导入每次最多支持 512 个输入路径')
    encoding_name(params.get('encoding', 'auto'))


def _path(value):
    if not isinstance(value, str):raise ValueError('文本文件路径必须是文本')
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not value or any(character in value for character in ('\0', '\r', '\n')):
        raise ValueError('每项文本输入需要一个有效文件或文件夹路径；多个路径请使用 List')
    return Path(os.path.abspath(os.path.expanduser(value)))


def _resolve_entries(paths, origins=None, stop=None):
    if len(paths) > MAX_FILES:raise ValueError('文本文件导入每次最多支持 512 个输入路径')
    output, seen = [], set()
    def add(path, origin):
        identity = os.path.normcase(os.path.realpath(path))
        if identity in seen:return
        if len(output) >= MAX_FILES:raise ValueError('文本文件导入每次最多读取 512 个文件')
        seen.add(identity);output.append((str(path), origin))
    for index, raw in enumerate(paths):
        _check_stop(stop)
        path = _path(raw)
        origin = origins[index] if origins is not None else {}
        if path.is_dir():
            matching = []
            with os.scandir(path) as entries:
                for entry in entries:
                    _check_stop(stop)
                    if Path(entry.name).suffix.lower() in TEXT_SUFFIXES and entry.is_file():
                        matching.append(entry.path)
                        if len(matching) > MAX_FILES:
                            raise ValueError('文本文件夹超过 512 个匹配文件，请缩小输入范围')
            for child in sorted(matching, key=lambda item: (Path(item).name.casefold(), Path(item).name)):
                add(child, origin)
        elif path.is_file():
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise ValueError('不支持的文本文件格式：' + path.name)
            add(path, origin)
        else:
            raise FileNotFoundError('文本输入路径不存在：' + str(path))
    return output


def resolve_files(paths, stop=None):
    """Expand only the current folder level, preserving List and filename order."""
    return [path for path, _ in _resolve_entries(paths, stop=stop)]


def _read(path, stop=None, limit=MAX_FILE_BYTES):
    _check_stop(stop)
    path = _path(str(path))
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError('不支持的文本文件格式：' + path.name)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError('文本文件超过 4 MiB：' + path.name)
        raw = stream.read(min(MAX_FILE_BYTES, limit) + 1)
        after = os.fstat(stream.fileno())
    _check_stop(stop)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('文本文件在读取时发生变化，请重试：' + path.name)
    if len(raw) > MAX_FILE_BYTES:raise ValueError('文本文件超过 4 MiB：' + path.name)
    truncated = len(raw) > limit
    return raw[:limit], truncated


def _decode(raw, encoding, *, final=True):
    requested = encoding_name(encoding)
    if not raw:return '', 'utf-8-sig' if requested == 'auto' else requested
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError('不支持 UTF-32 文本，请先另存为 UTF-8 或 UTF-16')
    utf16 = raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    if requested == 'auto':
        choices = ('utf-16',) if utf16 else ('utf-8-sig',) if raw.startswith(codecs.BOM_UTF8) else ('utf-8-sig', 'gb18030')
    else:choices = (requested,)
    if choices == ('utf-16',) and not utf16:
        raise ValueError('UTF-16 文本需要包含 BOM；请确认编码或另存为 UTF-8')
    for candidate in choices:
        try:
            text = codecs.getincrementaldecoder(candidate)(errors='strict').decode(raw, final=final)
        except UnicodeError:
            continue
        if any(ord(character) < 32 and character not in '\t\r\n' or ord(character) == 127 for character in text):
            raise ValueError('文件含 NUL 或二进制控制字符，不能作为文本读取')
        return text, candidate
    raise ValueError('无法按所选编码读取文本，请确认编码（支持 UTF-8、含 BOM 的 UTF-16、GB18030）')


def read_preview(path, encoding='auto', max_bytes=65536, max_chars=8000):
    """Read only a bounded prefix; callers must use a worker, never the GUI thread."""
    if type(max_bytes) is not int or max_bytes < 4 or type(max_chars) is not int or max_chars < 1:
        raise ValueError('文本预览范围无效')
    raw, truncated = _read(path, limit=min(max_bytes, MAX_FILE_BYTES))
    text, _ = _decode(raw, encoding, final=not truncated)
    truncated = truncated or len(text) > max_chars
    return text[:max_chars] + ('\n\n…（仅预览文件开头，运行时读取完整文本）' if truncated else '')


@dataclass
class PreparedTextFiles:
    results: list
    signatures: list
    fingerprint_inputs: dict

    def discard(self):
        """Delete only this preparation's own temporary copies."""
        for result in self.results:
            try:Path(result['path']).unlink(missing_ok=True)
            except OSError:pass  # The per-run cleanup can retry a sharing lock.


def prepare(node, inputs, directory, stop=None):
    """Freeze bytes, decoded UTF-8 output and cache identity in one worker read."""
    from . import model
    validate_params(node.get('params', {}))
    encoding = node.get('params', {}).get('encoding', 'auto')
    fingerprint_inputs = {}
    paths, origins = [], []
    if 'path' in inputs:
        values = inputs['path']
        if len(values) > MAX_FILES:raise ValueError('文本文件导入每次最多支持 512 个输入路径')
        fingerprint_inputs['path'] = []
        for result in values:
            _check_stop(stop)
            if model.result_type(result) != 'text':
                raise ValueError('文件路径需要普通文本 List；Batch 请先转换为 List')
            value = model.input_value(result, {'fieldType': 'STRING'})
            paths.append(value);origins.append(result)
            # A path-producing node may itself be backed by a text file. Freeze
            # that text too, rather than hash a second read of its mutable path.
            frozen = {key: copy.deepcopy(result[key]) for key in ('lineage', 'generation', 'task_id', 'index') if key in result}
            fingerprint_inputs['path'].append(dict(frozen, type='text', text=value))
    else:
        paths = node.get('params', {}).get('files', [])
    entries = _resolve_entries(paths, origins if 'path' in inputs else None, stop)
    if not entries:raise ValueError('输入路径中没有支持的文本文件')
    directory = Path(directory) / 'text_inputs'
    directory.mkdir(parents=True, exist_ok=True)
    prepared = PreparedTextFiles([], [], fingerprint_inputs)
    total = total_output = 0
    try:
        for index, (path, origin) in enumerate(entries):
            raw, _ = _read(path, stop)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:raise ValueError('本节点文本输入总量超过 32 MiB，请分批处理')
            try:text, used_encoding = _decode(raw, encoding)
            except ValueError as error:raise ValueError(Path(path).name + '：' + str(error)) from error
            content = text.encode('utf-8')
            total_output += len(content)
            if total_output > MAX_OUTPUT_BYTES:raise ValueError('本节点解码后的文本总量超过 64 MiB，请分批处理')
            _check_stop(stop)
            target = directory / (uuid.uuid4().hex + '.txt')
            result = {'type': 'text', 'path': str(target), 'source_path': path,
                      'name': Path(path).name, '_archive_name': Path(path).name,
                      '_file_identity': os.path.normcase(os.path.abspath(path)),
                      'source_encoding': used_encoding, 'index': index,
                      'lineage': model.result_lineage({'path': origin} if origin else {}, node['id'], index)}
            prepared.results.append(result)
            # The file is private to this running node; no other task can see
            # it until the complete prepared result is returned.
            with target.open('xb') as output:output.write(content)
            prepared.signatures.append({'path': result['_file_identity'],
                                        'source_sha256': hashlib.sha256(raw).hexdigest(),
                                        'text_sha256': hashlib.sha256(content).hexdigest()})
        return prepared
    except BaseException:
        prepared.discard()
        raise
