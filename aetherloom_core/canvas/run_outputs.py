"""Durable latest-run copies, without touching the App's original downloads."""
import copy
import json
import os
from pathlib import Path
import shutil
import uuid
import hashlib
from . import model


def _target(directory, digest, name):
    target = directory / digest / name
    def units(path):
        return len(str(path).encode('utf-16-le', errors='surrogatepass')) // 2
    if os.name == 'nt' and max(units(target), units(target.parent / ('.' + '0' * 32 + '.part'))) > 259:
        # Keep cache paths short even when RH's downloaded name contains both
        # task and resource IDs. The logical filename is retained separately.
        identity = hashlib.sha256((digest + '\0' + name).encode('utf-8')).hexdigest()[:32]
        target = directory / (identity + Path(name).suffix)
        if max(units(target), units(directory / ('.' + '0' * 32 + '.part'))) > 259:
            raise ValueError('画布缓存目录路径过长，请将项目移至较短的目录后重试')
    return target


def capture(files, run_id, node, results):
    directory=files.directory(run_id,node['id'])/'outputs'
    output=[]
    for raw in results:
        result=model.normalize_result(copy.deepcopy(raw))
        if model.result_type(result) == 'batch':
            result['items'] = capture(files, run_id, node, model.batch_items(result))
            output.append(result)
            continue
        source=result.get('path')
        # Image/video/audio inputs always continue to reference their input
        # assets (and the separate durable mask), never a copied library entry.
        if source or 'text' in result or 'value' in result:
            content = None
            if not source:
                if model.result_type(result) == 'bounding':
                    from .bounding_nodes import validate_bounding
                    content = json.dumps(validate_bounding(result.get('value')), ensure_ascii=False,
                                         separators=(',', ':')).encode('utf-8')
                else:
                    content = str(result.get('text', result.get('value', ''))).encode('utf-8')
            signature=model.result_signature(result)
            digest=signature['content'] if source else hashlib.sha256(content).hexdigest()
            fallback_name = 'bounding.json' if model.result_type(result) == 'bounding' else 'text.txt'
            name=str(result.get('_archive_name') or (Path(source).name if source else result.get('name') or fallback_name))
            name=Path(name).name or 'text.txt'
            target=_target(directory,digest,name)
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.is_file() or model.file_hash(target)!=digest:
                part=target.with_name('.'+uuid.uuid4().hex+'.part')
                try:
                    if source:shutil.copy2(source,part)
                    else:part.write_bytes(content)
                    os.replace(part,target)
                finally:part.unlink(missing_ok=True)
            result['archive_path']=str(target)
            # saved_path / _saved_targets describe independent user exports;
            # moving the cache must never replace those destinations.
            if node['kind'] not in model.MEDIA or result.get('source_path'):
                result.setdefault('_file_identity',os.path.normcase(os.path.abspath(source)) if source else 'text:'+digest)
                result['path']=str(target)
                if target.name != name or '_archive_name' in result:
                    result['_archive_name']=name
        # Auxiliary edit assets belong to the same retained run as the output.
        # Move their references too when reusing a node in the next run.
        for key in ('paint_temp_path','composite_path') + (('mask_path',) if result.get('_processed_mask') else ()):
            original=result.get(key)
            if not original:continue
            digest=model.file_hash(original)
            target=directory/'layers'/(key+'_'+digest[:24]+'.png')
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.is_file() or model.file_hash(target)!=digest:
                part=target.with_name('.'+uuid.uuid4().hex+'.part')
                try:shutil.copy2(original,part);os.replace(part,target)
                finally:part.unlink(missing_ok=True)
            result[key]=str(target)
        output.append(result)
    directory.mkdir(parents=True,exist_ok=True)
    manifest=directory/'results.json';part=directory/('.'+uuid.uuid4().hex+'.part')
    try:
        references=model.snapshot_result_references({'nodes':[{'results':output}]})['nodes'][0]['results']
        part.write_text(json.dumps(references,ensure_ascii=False),encoding='utf-8')
        os.replace(part,manifest)
    finally:part.unlink(missing_ok=True)
    return output
