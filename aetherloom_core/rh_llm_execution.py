"""Bounded RH LLM requests projected into the shared application task model."""
import copy
from itertools import chain
import json
import os
from pathlib import Path
import threading
import time
import requests

from .rh_model_errors import classify_model_error, accepted_model_task_id
from .rh_model_runtime import request_description, validate_inputs
from .rh_tasks import normalize_api_keys, api_key_id
from .rh_submission_queue import SubmissionCancelled

_slots = threading.BoundedSemaphore(4)


class _ResponseError(ValueError):
    """A bounded, credential-free response diagnostic suitable for the task UI."""


class _InvalidPayload(_ResponseError):
    """An explicit HTTP refusal may have an HTML/text body instead of JSON."""


def _error_detail(error):
    code = (' (code=' + error['code'] + ')') if error['code'] else ''
    return error['message'] + code


def _payloads(response, check):
    """Read each response once, retaining the stream after its first real event."""
    streaming = 'text/event-stream' in response.headers.get('Content-Type', '').lower()
    if not streaming:
        raw = bytearray()
        for chunk in response.iter_content(16384):
            check()
            raw.extend(chunk)
            if len(raw) > 8 * 1024 * 1024:
                raise _ResponseError('LLM 响应超过 8 MB 上限')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _InvalidPayload('LLM 返回无效 JSON') from None
        if not isinstance(payload, dict):
            raise _InvalidPayload('LLM 返回无效结果结构')
        yield payload
        return

    lines, size, event_name = [], 0, ''

    def decode():
        raw = b'\n'.join(lines).strip()
        if raw == b'[DONE]':
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _InvalidPayload('LLM 流返回无效 JSON') from None
        if not isinstance(payload, dict):
            raise _InvalidPayload('LLM 流返回无效结果结构')
        if event_name == 'error' and not payload.get('error'):
            payload = {'error': payload}
        return payload

    # Small chunks allow an initial refusal/first token to release the queue
    # promptly, rather than waiting for the rest of a long generated response.
    for line in response.iter_lines(chunk_size=1):
        check()
        size += len(line)
        if size > 8 * 1024 * 1024:
            raise _ResponseError('LLM 流事件超过 8 MB 上限')
        if not line:
            if lines:
                yield decode()
            lines, size, event_name = [], 0, ''
        elif line.startswith(b'data:'):
            lines.append(line[5:].lstrip(b' '))
        elif line.startswith(b'event:'):
            event_name = line[6:].strip().decode('ascii', errors='replace').lower()
    if lines:
        yield decode()


def _generation_started(payload):
    """Never switch credentials after generated text, reasoning, tools or usage."""
    if accepted_model_task_id(payload):
        return True
    pending, seen = [payload], set()
    while pending:
        value = pending.pop()
        if not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        if value.get('usage'):
            return True
        for choice in value.get('choices') or []:
            if not isinstance(choice, dict):
                continue
            if choice.get('finish_reason'):
                return True
            for name in ('delta', 'message'):
                part = choice.get(name)
                if isinstance(part, dict) and any(item for key, item in part.items() if key != 'role'):
                    return True
            if choice.get('text'):
                return True
        pending.extend(value.get(name) for name in ('error', 'data', 'response'))
    return False


def execute_llm(service, run_id, order, snapshot):
    response = None
    acquired = False
    attempted = False
    received = False
    events, first_event = None, None
    active_key, document, probe_deadline = '', {}, None
    stop = lambda: service._stopped(run_id)
    def check():
        if stop():raise SubmissionCancelled()
    def check_response():
        check()
        if not received and probe_deadline is not None and time.monotonic() > probe_deadline:
            raise requests.Timeout('LLM initial response timed out')
    try:
        nodes = copy.deepcopy(snapshot.get('nodes') or [])
        validate_inputs(snapshot, nodes)
        from .mask_assets import matches, materialize, asset_reference
        for node in nodes:
            mask, source = node.get('_mask'), node.get('fieldValue', '')
            attached = node.pop('_canvas_masks', {})
            sources = source if isinstance(source, list) else [source]
            processed = []
            for source in sources:
                selected = attached.get(source) if isinstance(source, str) else None
                selected = selected or mask
                if not selected or not isinstance(source, str) or not matches(selected, source):
                    processed.append(source)
                    continue
                check()
                folder = service.temporary.directory('app-' + run_id)
                with service._condition:
                    service._input_leases.setdefault(run_id, set()).update(service.temporary.retain([folder]))
                assets = {}
                path, mask_path = materialize(source, selected,
                    snapshot.get('input_dir') or str(service.temporary.project / 'input'), folder, assets=assets)
                processed.append(path)
                for frozen in snapshot['nodes']:
                    if frozen.get('nodeId') == node.get('nodeId') and frozen.get('fieldName') == node.get('fieldName'):
                        saved = asset_reference(selected, mask_path)
                        if assets.get('paint_path'):saved['paint_path'] = assets['paint_path']
                        if source in frozen.get('_canvas_masks', {}):frozen['_canvas_masks'][source] = saved
                        else:frozen['_mask'] = saved
            node['fieldValue'] = processed if isinstance(node.get('fieldValue'), list) else processed[0]
        from .rh_execution import public_snapshot
        service._publish(run_id, snapshot=public_snapshot(snapshot))
        service.documents.patch('applications', run_id, {'request': public_snapshot(snapshot)})
        request = request_description(snapshot, nodes)
        request['body']['stream'] = True
        keys = normalize_api_keys(snapshot.get('api_keys')) or normalize_api_keys(snapshot.get('api_key'))
        if not keys:raise ValueError('请配置当前 RH 站点的企业级共享 API Key')
        def submit():
            nonlocal response, acquired, attempted, received, events, first_event, active_key, document, probe_deadline
            check()
            if not acquired:
                acquired = _slots.acquire(blocking=False)
                if not acquired:return dict(code=421)
            busy, errors = False, []
            for index, key in enumerate(keys, 1):
                check()
                active_key = key
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
                http_error = classify_model_error({}, response.status_code, key)
                if http_error is not None and http_error['kind'] == 'unknown':
                    raise _ResponseError('LLM 返回错误：' + _error_detail(http_error) + '；未自动重新提交')
                if http_error is None:
                    response.raise_for_status()
                # Inspect even 4xx bodies: a taskId or generated output takes
                # precedence over inconsistent HTTP error/status information.
                events = _payloads(response, check_response)
                probe_deadline = time.monotonic() + 90
                refusal = None
                try:
                    for probe_index, event in enumerate(events):
                        if event is None:
                            raise _ResponseError('LLM 未返回有效内容，未自动重新提交')
                        started = _generation_started(event)
                        refusal = classify_model_error(event, response.status_code, key)
                        if started:
                            received = True
                        if refusal is not None:
                            if received or refusal['kind'] == 'unknown':
                                raise _ResponseError('LLM 返回错误：' + _error_detail(refusal) + '；未自动重新提交')
                            break
                        if started:
                            first_event = event
                            received = True
                            service.documents.patch('applications', run_id, {'post': dict(document, phase='accepted')})
                            return dict(code=0)  # Direct response; never create a synthetic taskId.
                        if probe_index >= 255:
                            raise _ResponseError('LLM 未确认开始生成，未自动重新提交')
                except _InvalidPayload:
                    if http_error is None or received:
                        raise
                    refusal = http_error
                if refusal is None:
                    refusal = http_error
                if refusal is None:
                    raise _ResponseError('LLM 响应在确认生成前中断，未自动重新提交')
                if refusal['kind'] in ('rejected', 'busy'):
                    response.close();response = None;attempted = False
                    with service._condition:service._post_inflight.discard(run_id)
                    if refusal['kind'] == 'busy':busy = True
                    errors.append(f'Key {index}: ' + _error_detail(refusal))
                    service.documents.patch('applications', run_id, {'post': dict(document, phase='rejected',
                        response_code=refusal['code'], error_message=refusal['message'])})
                    continue
                raise _ResponseError('LLM 返回错误：' + _error_detail(refusal) + '；未自动重新提交')
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
        streaming = 'text/event-stream' in response.headers.get('Content-Type', '').lower()
        finished = not streaming
        for event in chain((first_event,), events):
            check()
            if event is None:
                finished = True
                break
            error = classify_model_error(event, response.status_code, active_key)
            if error:
                raise _ResponseError('LLM 返回错误：' + _error_detail(error) + '；未自动重新提交')
            for choice in event.get('choices') or []:
                part = choice.get('delta' if streaming else 'message') or {}
                text = part.get('content') or ''
                if isinstance(text, str):chunks.append(text);size += len(text.encode('utf-8'))
                if choice.get('finish_reason'):finished = True
            if size > 4 * 1024 * 1024:raise _ResponseError('LLM 文本输出超过 4 MB 上限')
        if not finished:raise _ResponseError('LLM 响应中断，未自动重新提交')
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
        if attempted:
            service.documents.patch('applications', run_id, {'post': dict(document, phase='unknown' if unknown else 'accepted')})
        detail = str(error) if isinstance(error, _ResponseError) else type(error).__name__
        service._publish(run_id, status='UNKNOWN' if unknown else 'FAILED',
            message=('LLM 提交结果未知，未自动重新提交，请在 RH 查看调用记录：' + detail) if unknown else
                    str(error) if isinstance(error, ValueError) else 'LLM 响应失败：' + type(error).__name__)
    finally:
        if response is not None:response.close()
        if acquired:_slots.release()
        service.queue.release_order(order)
        with service._condition:
            service._post_inflight.discard(run_id);service._workers.pop(run_id, None)
            service._snapshots.pop(run_id, None);service._condition.notify_all()
