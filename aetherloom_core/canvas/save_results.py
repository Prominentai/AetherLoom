"""Local result copies for a canvas save node; no downloads or source deletion."""
import copy
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
        source = Path(item['path']) if item.get('path') else None
        if source and not source.is_file():raise FileNotFoundError('待保存的结果文件不存在：' + str(source))
        if overwrite and names is None and source and source.parent.resolve() == folder.resolve():
            saved.append(item)
            continue
        if not source and 'text' not in item:raise ValueError('结果没有可保存的本地文件或文本')
        name = names[index] if names is not None else source.name if source else item.get('name') or '文本结果.txt'
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
