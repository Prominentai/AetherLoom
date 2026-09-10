"""Read names or produce renamed intermediate copies without touching sources."""
import tempfile
from pathlib import Path
from . import model
from .save_results import save_results, validate_filename, SaveCanceled


def execute(node, prepared, batches, stop):
    results = []
    temporary = None
    for batch in batches:
        if 'value' not in batch:raise ValueError('请连接需要读取或重命名的文件 / 结果')
        if stop.is_set():raise SaveCanceled('文件处理已取消')
        source = batch['value']
        physical = bool(source.get('path'))
        path = Path(source['path']) if physical else Path(source.get('name') or '文本结果.txt')
        if physical and not path.is_file():raise FileNotFoundError('文件不存在：' + str(path))
        if not physical and 'text' not in source:raise ValueError('结果没有可读取的文件或文本')
        if node['kind'] == 'filename':
            params = node.get('params', {})
            mode = params.get('read_mode', 'full' if params.get('include_extension', False) else 'name')
            text = path.name if mode == 'full' else path.suffix.lstrip('.') if mode == 'extension' else path.stem
            item = {'text': text, 'type': 'text', 'index': len(results),
                    'lineage': model.result_lineage({'value': source}, node['id'], len(results))}
        else:
            def setting(key, fallback):
                value = model.input_value(batch[key], {'fieldType': 'STRING'}) if key in batch else node.get('params', {}).get(key, '')
                text = str(value).strip()
                return text or fallback
            name = setting('name', path.stem)
            extension = setting('extension', path.suffix).lstrip('.') or path.suffix.lstrip('.')
            if '.' in extension or '/' in extension or '\\' in extension:
                raise ValueError('后缀请填写单独的扩展名，例如 png 或 txt')
            filename = name + ('.' + extension if extension else '')
            validate_filename(filename)
            if stop.is_set():raise SaveCanceled('重命名已取消')
            if temporary is None:
                directory = Path(prepared['temporary_dir'])
                directory.mkdir(parents=True, exist_ok=True)
                temporary = Path(tempfile.mkdtemp(prefix='rename-', dir=directory))
            # Preserve exact names for separate items; the save node owns the
            # final overwrite/suffix policy. Never rewrite an upstream file.
            item = save_results([source], str(temporary / str(len(results))), stop, names=[filename])[0]
            item.pop('name', None)
            # Keep actual content type: changing the suffix does not transcode.
            item['index'] = len(results)
        results.append(item)
    return results
