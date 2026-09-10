"""Image API adapters. Omit dimensions to use provider defaults; never re-POST."""
import base64
import io
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from PIL import Image
from .provider_client import ProviderAPIError, endpoint_parts

MAX_BYTES = 32 * 1024 * 1024


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise ProviderAPIError('图像任务已停止本地等待，未自动重发请求。', status='canceled')


def image_bytes(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ProviderAPIError('图像为空或超过 32 MB。', status='invalid_image')
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width * picture.height > 40_000_000:raise ValueError()
            mime = Image.MIME.get(picture.format)
            if mime not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):raise ValueError()
            picture.verify()
        return mime, raw
    except Exception:
        raise ProviderAPIError('图像损坏、尺寸过大或格式不支持。', status='invalid_image') from None


def load_image(path):
    if Path(path).stat().st_size > MAX_BYTES:
        raise ProviderAPIError('输入图像超过 32 MB。', status='invalid_image')
    return image_bytes(Path(path).read_bytes())


def _http(method, url, deadline, stop, *, binary=False, **kwargs):
    check_stop(stop); endpoint_parts(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:raise ProviderAPIError('图像请求超时，未自动重发。', status='timeout')
    response = None
    try:
        response = requests.request(method, url, timeout=(min(15, remaining), remaining),
                                    stream=True, allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            raise ProviderAPIError(f'图像接口请求失败（HTTP {response.status_code}），请检查模型和账户权限；未自动重发。',
                                   status='http_error', status_code=response.status_code)
        raw = bytearray()
        limit = MAX_BYTES if binary else 160 * 1024 * 1024
        for chunk in response.iter_content(65536):
            check_stop(stop)
            raw.extend(chunk)
            if len(raw) > limit or time.monotonic() > deadline:
                raise ProviderAPIError('图像响应过大或超时。', status='incomplete_response')
        check_stop(stop)
        if binary:return bytes(raw)
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('error') or
                data.get('code') not in (None, 0, '0', 200, '200')):
            raise ProviderAPIError('图像接口返回业务错误，请检查模型和参数。', status='api_error')
        return data
    except requests.RequestException:
        raise ProviderAPIError('图像请求未确认完成；未自动重新生成，请先核对供应商记录。', status='network_error') from None
    except (ValueError, UnicodeError):
        raise ProviderAPIError('图像接口响应格式无效。', status='invalid_response') from None
    finally:
        if response is not None:response.close()


def _outputs(data, protocol):
    if protocol == 'gemini':
        if data.get('status') not in (None, 'completed'):
            raise ProviderAPIError('Gemini 图像尚未完成。', status='incomplete_response')
        steps = data.get('steps') or []
        blocks = [block for step in steps if step.get('type') == 'model_output' for block in step.get('content', [])]
        if not blocks:blocks = data.get('outputs') or []
        return [{'b64_json': b['data']} for b in blocks if b.get('type') == 'image' and b.get('data')]
    if protocol.startswith('alibaba_'):
        output = data.get('output') or {}
        status = output.get('task_status')
        if status not in (None, 'SUCCEEDED'):
            raise ProviderAPIError('图像任务未成功完成。', status='incomplete_response')
        if output.get('results'):return output['results']
        return [{'url': block['image']} for choice in output.get('choices', [])
                for block in choice.get('message', {}).get('content', []) if block.get('image')]
    return data.get('data') or data.get('images') or []


def generate(config, prompt, image_path=None, *, system_prompt='', timeout=120, stop=None):
    protocol = config.get('protocol') or config.get('provider')
    check_stop(stop)
    source = load_image(image_path) if image_path else None
    if protocol in ('agent_codex', 'agent_grok'):
        from aetherloom_core.agent_client import edit_images, generate_images
        args = (protocol, config.get('api_key', ''), config['model'], prompt)
        options = dict(timeout=timeout, system_prompt=system_prompt or None,
                       merge_system_prompt=config.get('merge_system_prompt', False))
        if source:
            return edit_images(*args, [(source[0], base64.b64encode(source[1]).decode('ascii'))], **options)
        return generate_images(*args, **options)
    if str(protocol).startswith('agent_'):
        raise ProviderAPIError('此 Agent 不支持图像生成或编辑。', status='unsupported')
    url = config['endpoint']; endpoint_parts(url)
    deadline = time.monotonic() + timeout
    headers = {'Authorization': 'Bearer ' + config.get('api_key', '')}
    payload = dict(model=config['model'], prompt=prompt)
    data_url = ('data:' + source[0] + ';base64,' + base64.b64encode(source[1]).decode('ascii')) if source else None
    files = None
    if protocol == 'gemini':
        headers = {'x-goog-api-key': config.get('api_key', '')}
        content = [dict(type='text', text=prompt)]
        if source:content.append(dict(type='image', mime_type=source[0], data=data_url.split(',', 1)[1]))
        payload = dict(model=config['model'], input=content, store=False)
    elif protocol == 'grok':
        payload['response_format'] = 'b64_json'
        if source:payload['image'] = dict(url=data_url, type='image_url')
    elif protocol in ('volcengine', 'byteplus_ap', 'byteplus_eu'):
        payload.update(response_format='b64_json', stream=False, sequential_image_generation='disabled')
        if source:payload['image'] = data_url
    elif protocol in ('siliconflow_cn', 'siliconflow_com'):
        if source:payload['image'] = data_url
    elif str(protocol).startswith('alibaba_'):
        content = ([dict(image=data_url)] if source else []) + [dict(text=prompt)]
        if 'image-synthesis' in url:
            payload = dict(model=config['model'], input=dict(prompt=prompt), parameters=dict(n=1))
            if source:
                if config['model'].startswith('wanx2.1-'):
                    payload['input'].update(base_image_url=data_url, function='description_edit')
                else:
                    payload['input']['images'] = [data_url]
            headers['X-DashScope-Async'] = 'enable'
        else:
            payload = dict(model=config['model'], input=dict(messages=[dict(role='user', content=content)]), parameters=dict(n=1))
            if '/image-generation/generation' in url:
                headers['X-DashScope-Async'] = 'enable'
    elif protocol == 'glm':
        if source:raise ProviderAPIError('此供应商未接入图像编辑。', status='unsupported')
    elif protocol == 'openai' or str(protocol).startswith('custom'):
        # The custom image route follows the Images API, not chat completion.
        if source:files = [('image', ('input' + ('.jpg' if source[0] == 'image/jpeg' else '.' + source[0].split('/')[1]), source[1], source[0]))]
    else:
        raise ProviderAPIError('此图像供应商的调用协议尚未接入。', status='unsupported')
    data = _http('POST', url, deadline, stop, headers=headers,
                 **(dict(data=payload, files=files) if files else dict(json=payload)))
    task_id = (data.get('output') or {}).get('task_id') if str(protocol).startswith('alibaba_') else None
    if task_id:
        import re
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            raise ProviderAPIError('图像任务标识无效。', status='invalid_response')
        parts = urlsplit(url)
        query_url = urlunsplit((parts.scheme, parts.netloc, '/api/v1/tasks/' + task_id, '', ''))
        while (data.get('output') or {}).get('task_status') in ('PENDING', 'RUNNING', None):
            if stop is not None:stop.wait(min(2, max(0, deadline - time.monotonic())))
            else:time.sleep(min(2, max(0, deadline - time.monotonic())))
            data = _http('GET', query_url, deadline, stop, headers={'Authorization': headers['Authorization']})
    records = _outputs(data, str(protocol))
    if not isinstance(records, list) or not 1 <= len(records) <= 10:
        raise ProviderAPIError('接口没有返回有效图像，或结果数量超过上限。', status='empty_response')
    results = []
    for record in records:
        check_stop(stop)
        if not isinstance(record, dict) or record.get('error'):
            raise ProviderAPIError('图像结果失败或格式无效。', status='invalid_response')
        encoded, remote = record.get('b64_json'), record.get('url')
        if encoded:
            try:raw = base64.b64decode(encoded, validate=True)
            except (TypeError, ValueError):raise ProviderAPIError('图像编码无效。', status='invalid_image') from None
        elif remote:
            parts = urlsplit(remote)
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
                raise ProviderAPIError('图像下载地址无效。', status='invalid_response')
            # Signed result URLs never receive the model API's auth headers.
            raw = _http('GET', remote, deadline, stop, binary=True)
        else:raise ProviderAPIError('图像结果缺少下载地址或数据。', status='empty_response')
        results.append(image_bytes(raw))
    return results
