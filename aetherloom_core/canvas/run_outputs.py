"""Durable latest-run copies, without touching the App's original downloads."""
import copy
import json
import os
from pathlib import Path
import shutil
import uuid
import hashlib
from . import model


def capture(files, run_id, node, results):
    directory=files.directory(run_id,node['id'])/'outputs'
    output=[]
    for raw in results:
        result=model.normalize_result(copy.deepcopy(raw))
        source=result.get('path')
        # Image/video/audio inputs always continue to reference their input
        # assets (and the separate durable mask), never a copied library entry.
        if source or 'text' in result or 'value' in result:
            signature=model.result_signature(result)
            digest=signature['content'] if source else hashlib.sha256(
                str(result.get('text',result.get('value',''))).encode('utf-8')).hexdigest()
            name=Path(source).name if source else str(result.get('name') or 'text.txt')
            name=Path(name).name or 'text.txt'
            target=directory/digest/name
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.is_file() or model.file_hash(target)!=digest:
                part=target.with_name('.'+uuid.uuid4().hex+'.part')
                try:
                    if source:shutil.copy2(source,part)
                    else:part.write_text(str(result.get('text',result.get('value',''))),encoding='utf-8')
                    os.replace(part,target)
                finally:part.unlink(missing_ok=True)
            result['archive_path']=str(target)
            if node['kind'] not in model.MEDIA or result.get('source_path'):
                result.setdefault('_file_identity',os.path.normcase(os.path.abspath(source)) if source else 'text:'+digest)
                result['path']=str(target)
        # Auxiliary edit assets belong to the same retained run as the output.
        # Move their references too when reusing a node in the next run.
        for key in ('paint_temp_path','composite_path'):
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
