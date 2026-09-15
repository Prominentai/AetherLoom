"""Local result copies for a canvas save node; no downloads or source deletion."""
import copy
import hashlib
import os
import tempfile
import re
from pathlib import Path


class SaveCanceled(Exception):
    status = 'canceled'


def default_directory(output_root, document):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(document.get('name') or '画布')).strip(' .')[:64] or '画布'
    identity = re.sub(r'[^a-zA-Z0-9_-]', '_', document['id'])[:48]
    return str(Path(output_root) / 'canvases' / (name + '_' + identity))


def validate_filename(name):
    if (not isinstance(name, str) or not name or name in ('.', '..') or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((' ', '.')) or len(name)>240 or
            re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name)):
        raise ValueError('文件名包含无效字符、保留名称或长度超限')


def save_results(results, directory, stop=None, overwrite=False, *, names=None):
    """Atomic file writer; names is used internally by transformation nodes."""
    def check():
        if stop is not None and stop.is_set():raise SaveCanceled('保存已取消')
    check()
    folder = Path(directory).expanduser()
    if not folder.is_absolute():raise ValueError('保存目录请使用绝对路径')
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, result in enumerate(results):
        check()
        item = copy.deepcopy(result)
        from . import model
        if model.result_type(item) == 'batch':
            if names is not None:raise ValueError('文件重命名不接受 Batch，请在列表转 Batch 前重命名')
            item['items'] = save_results(model.batch_items(item), directory, stop, overwrite)
            saved.append(item)
            continue
        source = Path(item['path']) if item.get('path') else None
        if source and not source.is_file():raise FileNotFoundError('待保存的结果文件不存在：' + str(source))
        if overwrite and names is None and source and source.parent.resolve() == folder.resolve():
            saved.append(item)
            continue
        if not source and 'text' not in item:raise ValueError('结果没有可保存的本地文件或文本')
        name = names[index] if names is not None else item.get('_archive_name') or (source.name if source else item.get('name') or '文本结果.txt')
        item.pop('_archive_name', None)
        validate_filename(name)
        temporary = target = None
        committed = False
        try:
            with tempfile.NamedTemporaryFile(dir=folder, prefix='.aetherloom-', suffix='.part', delete=False) as output:
                temporary = Path(output.name)
                if source:
                    with source.open('rb') as input_file:
                        before = os.fstat(input_file.fileno())
                        while True:
                            check()
                            chunk = input_file.read(1024 * 1024)
                            if not chunk:break
                            output.write(chunk)
                        after = os.fstat(input_file.fileno())
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError('保存期间源文件发生变化，请重新运行节点')
                else:
                    output.write(str(item['text']).encode('utf-8'))
                output.flush();os.fsync(output.fileno())
            check()
            suffix = 0
            while True:
                check()
                candidate = folder / (name if not suffix else f'{Path(name).stem}({suffix}){Path(name).suffix}')
                if overwrite:
                    target = candidate
                    break
                try:
                    with candidate.open('xb'):pass
                except FileExistsError:
                    suffix += 1
                    continue
                target = candidate
                break
            check()
            os.replace(temporary, target)
            committed = True
            item['path'] = str(target)
            saved.append(item)
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
            if target is not None and not committed and not overwrite:target.unlink(missing_ok=True)
    check()
    return saved


def save_node_results(results, directory, stop=None, overwrite=False, *, previous=None, cached=False):
    """Keep a save node's export destinations separate from its durable cache.

    Only this node's previous results may authorize reusing an existing export.
    Other callers retain save_results' ordinary copy/overwrite behavior.
    """
    from . import model
    folder = Path(directory).expanduser()
    if not folder.is_absolute():raise ValueError('保存目录请使用绝对路径')
    folder = folder.resolve()
    output = []
    previous = previous or []
    for index, raw in enumerate(results):
        if stop is not None and stop.is_set():raise SaveCanceled('保存已取消')
        item = copy.deepcopy(raw)
        before = previous[index] if index < len(previous) and isinstance(previous[index], dict) else {}
        if model.result_type(item) == 'batch':
            old = model.batch_items(before) if model.result_type(before) == 'batch' else []
            item['items'] = save_node_results(model.batch_items(item), str(folder), stop, overwrite, previous=old, cached=cached)
            output.append(item)
            continue
        # Ignore export metadata inherited from an upstream save node.
        for key in ('saved_path', 'saved_signature', '_saved_targets', '_save_name'):
            item.pop(key, None)
        source = Path(item['path']) if item.get('path') else None
        signature = model.result_signature(item)
        if source:
            content = signature['content']
        elif 'text' in item:
            content = hashlib.sha256(str(item['text']).encode('utf-8')).hexdigest()
        else:
            raise ValueError('结果没有可保存的本地文件或文本')
        name = item.get('_archive_name') or (source.name if source else item.get('name') or '文本结果.txt')
        if cached:name = before.get('_save_name') or name
        same = False
        if isinstance(before.get('saved_signature'), str):
            same = (content == before['saved_signature'] and model.result_type(item) == model.result_type(before)
                    and all(signature.get(key, default) == before.get(key, default)
                            for key, default in (('generation', ''), ('task_id', ''), ('index', 0))))
        elif before:
            try:same = signature == model.result_signature(before)
            except (OSError, ValueError, TypeError):pass
        if before.get('_save_name') and name != before['_save_name']:same = False
        targets = copy.deepcopy(before.get('_saved_targets') or []) if same else []
        targets = [entry for entry in targets if isinstance(entry, dict)
                   and isinstance(entry.get('path'), str) and entry.get('content') == content]
        validate_filename(name)
        target = None
        for entry in reversed(targets):
            candidate = Path(entry['path'])
            if (candidate.is_absolute() and candidate.parent.resolve() == folder
                    and entry.get('overwrite', overwrite) == overwrite):
                target = candidate
                break
        # Old snapshots have no export metadata. Their archived logical name
        # still lets us recover the original destination without making copies.
        if target is None and same:target = folder / name
        reusable = False
        if target is not None and target.is_file():
            try:reusable = model.file_hash(target) == content
            except (OSError, ValueError):pass
        if reusable:
            item['path'] = str(target)
            item.pop('_archive_name', None)
        else:
            item = save_results([item], str(folder), stop, overwrite,
                                names=[target.name if target is not None else name])[0]
            target = Path(item['path'])
        entry = {'path': str(target), 'content': content, 'overwrite': overwrite}
        targets = [value for value in targets if Path(value['path']).parent.resolve() != folder]
        item.update(saved_path=str(target), saved_signature=content,
                    _saved_targets=(targets + [entry])[-16:], _save_name=name)
        output.append(item)
    return output
