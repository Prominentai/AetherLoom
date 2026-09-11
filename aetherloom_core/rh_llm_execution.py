"""Bounded RH LLM requests projected into the shared application task model."""
import copy
import json
import os
from pathlib import Path
import threading
import requests

from .rh_model_runtime import request_description, validate_inputs
from .rh_tasks import normalize_api_keys, api_key_id
from .rh_submission_queue import SubmissionCancelled

_slots = threading.BoundedSemaphore(4)


def execute_llm(service, run_id, order, snapshot):
    response = None
    acquired = False
    attempted = False
    received = False
    stop = lambda: service._stopped(run_id)
    def check():
        if stop():raise SubmissionCancelled()
    try:
        nodes = copy.deepcopy(snapshot.get('nodes') or [])
        validate_inputs(snapshot, nodes)
        from .mask_assets import matches, materialize
        for node in nodes:
            mask, source = node.get('_mask'), node.get('fieldValue', '')
            if mask and isinstance(source, str) and matches(mask, source):
                check()
                folder = service.temporary.directory('app-' + run_id)
                with service._condition:
                    service._input_leases.setdefault(run_id, set()).update(service.temporary.retain([folder]))
                assets = {}
                node['fieldValue'], mask_path = materialize(source, mask,
                    snapshot.get('input_dir') or str(service.temporary.project / 'input'), folder, assets=assets)
                for frozen in snapshot['nodes']:
                    if frozen.get('nodeId') == node.get('nodeId') and frozen.get('fieldName') == node.get('fieldName'):
                        frozen['_mask']['path'] = mask_path
                        if assets.get('paint_path'):frozen['_mask']['paint_path'] = assets['paint_path']
        from .rh_execution import public_snapshot
        service._publish(run_id, snapshot=public_snapshot(snapshot))
        service.documents.patch('applications', run_id, {'request': public_snapshot(snapshot)})
        request = request_description(snapshot, nodes)
        request['body']['stream'] = True
        keys = normalize_api_keys(snapshot.get('api_keys')) or normalize_api_keys(snapshot.get('api_key'))
        if not keys:raise ValueError('请配置当前 RH 站点的企业级共享 API Key')
        def submit():
            nonlocal response, acquired, attempted, received
            check()
            if not acquired:
                acquired = _slots.acquire(blocking=False)
                if not acquired:return dict(code=421)
            busy, errors = False, []
            for index, key in enumerate(keys, 1):
                check()
                service._publish(run_id, status='SUBMITTING', message=f'LLM 请求 · Key {index}/{len(keys)}')
                document = copy.deepcopy(request)
                for message in document['body']['messages']:
                    if isinstance(message['content'], list):
                        for part in message['content']:
                            if part.get('type') == 'image_url' and part['image_url']['url'].startswith('data:'):
                                part['image_url']['url'] = '[本地输入图像，路径见 request.nodes]'
                document.update(phase='submitting', credential_ref=dict(site=snapshot['base_url'], key_id=api_key_id(key)))
                service.documents.patch('applications', run_id, {'post': document})
                service.documents.flush('applications', run_id)
                with service._condition:
                    check();service._post_inflight.add(run_id)
                attempted = True
                response = requests.post(request['endpoint'], headers={'Authorization': 'Bearer ' + key},
                    json=request['body'], stream=True, timeout=(15, 90), allow_redirects=False)
                if response.status_code in (400, 401, 403, 404, 422, 429):
                    status = response.status_code;response.close();response = None;attempted = False
                    with service._condition:service._post_inflight.discard(run_id)
                    if status == 429:busy = True
                    errors.append(f'Key {index}: HTTP {status}')
                    service.documents.patch('applications', run_id, {'post': dict(document, phase='rejected', response_code=status)})
                    continue
                response.raise_for_status()
                received = True
                service.documents.patch('applications', run_id, {'post': dict(document, phase='accepted')})
                return dict(code=0)  # Direct response; deliberately no synthetic taskId.
            if busy:
                _slots.release();acquired = False
                return dict(code=421)
            raise ValueError('所有 Key 均拒绝 LLM 请求：' + '；'.join(errors))
        service.queue.submit(submit, dict(webapp_id=snapshot['webapp_id'], run_id=run_id,
            origin=snapshot.get('origin') or {}, _submission_order=order),
            max_retries=max(1, int(snapshot.get('retry_max') or 100)),
            delay=max(1, int(snapshot.get('retry_delay') or 5)), concurrency=4, cancelled=stop,
            on_submit=lambda: service._publish(run_id, status='SUBMITTING', submission_admitted=True, message='提交 LLM 请求'),
            on_wait=lambda attempt, reason: service._publish(run_id, status='LOCAL_WAIT', message=f'等待 LLM 重试 {attempt}'))
        if response is None:raise ValueError('LLM 等候重试次数已用尽，请稍后重新运行')
        check()
        service._publish(run_id, status='RUNNING', progress=None, message='正在接收 LLM 输出')
        # Receiving a stream must not occupy the next submission position.
        with service._condition:service._workers.pop(run_id, None)
        service._dispatch_submissions()
        chunks, size = [], 0
        if 'text/event-stream' in response.headers.get('Content-Type', '').lower():
            finished = False
            for line in response.iter_lines(chunk_size=4096):
                check()
                if not line or not line.startswith(b'data:'):continue
                raw = line[5:].strip()
                if raw == b'[DONE]':finished = True;break
                event = json.loads(raw)
                if event.get('error'):raise ValueError('LLM 返回错误，未自动重新提交')
                for choice in event.get('choices', []):
                    text = choice.get('delta', {}).get('content') or ''
                    if isinstance(text, str):chunks.append(text);size += len(text.encode('utf-8'))
                    if choice.get('finish_reason'):finished = True
                if size > 4 * 1024 * 1024:raise ValueError('LLM 文本输出超过 4 MB 上限')
            if not finished:raise ValueError('LLM 响应中断，未自动重新提交')
        else:
            raw = bytearray()
            for chunk in response.iter_content(16384):
                check();raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:raise ValueError('LLM 响应过大')
            data = json.loads(raw)
            chunks = [c.get('message', {}).get('content') or '' for c in data.get('choices', [])]
        text = ''.join(chunks)
        if not text:raise ValueError('LLM 未返回文本内容')
        check()
        directory = Path(snapshot['output_dir']);directory.mkdir(parents=True, exist_ok=True)
        target = directory / (run_id + '.txt');staging = target.with_suffix('.txt.part')
        try:
            with staging.open('wb') as stream:
                stream.write(text.encode('utf-8'));stream.flush();os.fsync(stream.fileno())
            check();os.replace(staging, target)
        finally:staging.unlink(missing_ok=True)
        result = dict(type='text', kind='text', path=str(target), name=target.name, text=text, index=0)
        service._publish(run_id, status='SUCCESS', progress=100, results=[result], output_files=[str(target)], message='LLM 输出已保存')
    except SubmissionCancelled:
        service._publish(run_id, status='CANCELED', message='已停止本地接收；已发出的 LLM 请求可能仍在云端处理')
    except Exception as error:
        unknown = attempted and not received
        service._publish(run_id, status='UNKNOWN' if unknown else 'FAILED',
            message='LLM 提交结果未知，未自动重新提交，请在 RH 查看调用记录' if unknown else
                    str(error) if isinstance(error, ValueError) else 'LLM 响应失败：' + type(error).__name__)
    finally:
        if response is not None:response.close()
        if acquired:_slots.release()
        service.queue.release_order(order)
        with service._condition:
            service._post_inflight.discard(run_id);service._workers.pop(run_id, None)
            service._snapshots.pop(run_id, None);service._condition.notify_all()
