"""Subscription model discovery and bounded inference; no agent tools execute locally."""
import base64
import hashlib
import json
import math
import re
import threading
import time
import uuid
from itertools import chain

import requests
from api_calls.provider_client import ProviderAPIError, response_text
from . import agent_auth as auth
from .agent_catalog import AGENTS, supports, llm_name

_catalogs = {}
_catalog_lock = threading.RLock()
_MAX_RESPONSE = 80 * 1024 * 1024


def _fail(message, status='invalid_response'):
    return ProviderAPIError(message, status=status)


def _headers(provider, session, image=False):
    headers = {'Authorization': 'Bearer ' + session['accessToken'], 'Content-Type': 'application/json',
               'Accept': 'application/json'}
    if provider == 'agent_codex':
        headers.update({'chatgpt-account-id': session['accountId'], 'originator': 'codex_cli_rs',
                        'session-id': str(uuid.uuid4())})
    elif provider == 'agent_claude':
        headers.update({'anthropic-version': '2023-06-01',
            'anthropic-beta': 'claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14',
            'user-agent': 'claude-cli/2.1.234 (external, cli)', 'x-app': 'cli',
            'anthropic-dangerous-direct-browser-access': 'true'})
    elif provider == 'agent_copilot':
        headers.update(auth.copilot_headers())
        if image: headers['copilot-vision-request'] = 'true'
    return headers


def _stream_lines(chunks, deadline):
    """Split bytes without decoding partial UTF-8 characters or rescanning images."""
    fragments = []
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > _MAX_RESPONSE or time.monotonic() > deadline:
            raise _fail('Agent 响应超过大小或时间限制，未自动重复提交。', 'incomplete_response')
        parts = chunk.split(b'\n')
        for part in parts[:-1]:
            fragments.append(part)
            yield b''.join(fragments).rstrip(b'\r')
            fragments = []
        if parts[-1]:fragments.append(parts[-1])
    if fragments:yield b''.join(fragments).rstrip(b'\r')


def _sse(response, deadline, lines=None):
    """Require a terminal event; partial text after disconnection is not success."""
    pending, total = [], 0
    from .agent_search import ClaudeStream
    claude, stop_reason = ClaudeStream(), None
    image_items = {}
    other_items = {}
    for raw in (response.iter_lines(chunk_size=1024) if lines is None else lines):
        total += len(raw)
        if total > _MAX_RESPONSE or time.monotonic() > deadline:
            raise _fail('Agent 响应超过大小或时间限制，未自动重复提交。', 'incomplete_response')
        line = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        if line.startswith('data:'):
            pending.append(line[5:].lstrip())
        elif not line and pending:
            value = '\n'.join(pending); pending = []
            if value == '[DONE]': break
            try:
                event = json.loads(value)
            except (ValueError, TypeError):
                raise _fail('Agent 返回了无效的流式事件。') from None
            if not isinstance(event, dict):
                raise _fail('Agent 返回了无效的流式事件。')
            kind = event.get('type')
            claude.consume(event)
            if kind == 'response.output_item.done':
                item = event.get('item')
                if isinstance(item, dict) and item.get('type') in ('message', 'web_search_call', 'function_call'):
                    index = event.get('output_index')
                    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 2048:
                        raise _fail('Agent 内容顺序无效。')
                    other_items[index] = item
                if isinstance(item, dict) and item.get('type') == 'image_generation_call':
                    index = event.get('output_index')
                    if not isinstance(index, int) or index < 0:
                        raise _fail('Agent 图像结果顺序无效。')
                    if index not in image_items and len(image_items) >= 10:
                        raise _fail('Agent 返回图片数量超过限制。')
                    image_items[index] = item
            if kind == 'response.completed':
                result = event.get('response', {})
                if not isinstance(result, dict): raise _fail('Agent 返回了无效响应。')
                # Some streams carry images only in output_item.done, while
                # response.completed carries IDs/status. Preserve output order.
                output = result.get('output')
                output = list(output) if isinstance(output, list) else []
                for index, item in sorted({**other_items, **image_items}.items()):
                    match = next((i for i, existing in enumerate(output)
                                  if isinstance(existing, dict) and item.get('id') and
                                  existing.get('id') == item['id']), None)
                    if match is not None:
                        output[match] = item
                    elif index < len(output) and output[index] == item:
                        pass
                    elif index < len(output):
                        output.insert(index, item)
                    else:
                        output.append(item)
                if image_items or other_items: result = dict(result, output=output)
                return result
            if kind in ('response.failed', 'response.incomplete', 'error'):
                raise _fail('Agent 未返回完整结果，未自动重复提交。', 'incomplete_response')
            if kind == 'message_delta':
                stop_reason = event.get('delta', {}).get('stop_reason')
            if kind == 'message_stop' and stop_reason:
                return dict(content=claude.content(), stop_reason=stop_reason)
    raise _fail('Agent 连接结束但未确认完成，未自动重复提交。', 'incomplete_response')


def _request(provider, reference, url, *, payload=None, stream=False, image=False, timeout=30):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise _fail('请求超时必须是正数。', 'invalid_config')
    # All URLs are module-owned; endpoint fields cannot redirect OAuth credentials.
    from urllib.parse import urlsplit
    expected = urlsplit(AGENTS[provider]['endpoint']).hostname
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != expected:
        raise _fail('Agent 请求地址不匹配。', 'invalid_config')
    deadline = time.monotonic() + timeout
    rejected = None
    for attempt in range(2):
        session = auth.session_for(provider, reference, rejected, timeout=max(.001, deadline-time.monotonic()))
        headers = _headers(provider, session, image)
        if stream: headers['Accept'] = 'text/event-stream'
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise requests.Timeout()
            response = requests.request('GET' if payload is None else 'POST', url, headers=headers,
                **({} if payload is None else {'json': payload}), timeout=(min(15, remaining), remaining),
                stream=True, allow_redirects=False)
            if response.status_code == 401 and attempt == 0:
                rejected = session['accessToken']; continue
            if not 200 <= response.status_code < 300:
                code = response.status_code
                message = ('Agent 登录已失效，请重新登录。' if code == 401 else
                           'Agent 账户无权访问此模型或工具，请检查订阅权益。' if code == 403 else
                           'Agent 额度或请求频率受限，请稍后再试。' if code == 429 else
                           f'Agent 请求失败（HTTP {code}），未自动重复提交。')
                raise ProviderAPIError(message, status='auth_error' if code in (401, 403) else 'http_error', status_code=code)
            if stream and 'text/event-stream' in response.headers.get('Content-Type', '').lower():
                return _sse(response, deadline)
            raw = bytearray()
            chunks = iter(response.iter_content(1024 if stream else 65536))
            for chunk in chunks:
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE or time.monotonic() > deadline:
                    raise _fail('Agent 响应超过大小或时间限制。', 'incomplete_response')
                # Some subscription gateways omit or mislabel Content-Type.
                # Inspect the wire format instead of decoding SSE as JSON.
                if stream:
                    prefix = bytes(raw[:128]).lstrip(b'\xef\xbb\xbf \t\r\n')
                    if prefix.startswith((b'event:', b'data:', b':')):
                        return _sse(response, deadline, _stream_lines(chain((bytes(raw).lstrip(b'\xef\xbb\xbf'),), chunks), deadline))
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('error'):
                raise _fail('Agent 返回错误或无效响应。')
            return data
        except requests.Timeout:
            raise _fail('Agent 请求超时，未自动重复提交；可检查网络或增加超时设置。', 'timeout') from None
        except requests.RequestException:
            raise _fail('Agent 网络请求未确认完成，未自动重复提交。', 'network_error') from None
        except (ValueError, UnicodeError):
            raise _fail('Agent 返回了无效响应。') from None
        finally:
            if response is not None: response.close()


def catalog(provider, reference, *, timeout=30, refresh=False):
    deadline = time.monotonic() + timeout
    session = auth.session_for(provider, reference, timeout=timeout)
    fingerprint = hashlib.sha256(session['accessToken'].encode()).hexdigest()
    key = (provider, reference)
    with _catalog_lock:
        cached = _catalogs.get(key)
        if not refresh and cached and cached[0] == fingerprint and cached[1] > time.monotonic():
            return [dict(v) for v in cached[2]]
    urls = {
        'agent_codex': 'https://chatgpt.com/backend-api/codex/models?client_version=0.153.4',
        'agent_claude': 'https://api.anthropic.com/v1/models?beta=true&limit=100',
        'agent_grok': 'https://api.x.ai/v1/models',
        'agent_copilot': 'https://api.githubcopilot.com/models',
    }
    data = _request(provider, reference, urls[provider], timeout=max(.001, deadline-time.monotonic()))
    records = data.get('models' if provider == 'agent_codex' else 'data')
    if not isinstance(records, list) or len(records) > 2000:
        raise _fail('Agent 模型目录格式无效。')
    result, seen = [], set()
    for record in records:
        if not isinstance(record, dict): continue
        name = record.get('slug' if provider == 'agent_codex' else 'id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', name) or name in seen: continue
        if not llm_name(provider, name): continue
        vision, wire = True, 'responses'
        if provider == 'agent_codex' and record.get('visibility') in ('hide', 'none'): continue
        if provider == 'agent_claude': wire = 'claude'
        if provider == 'agent_grok':
            if re.search(r'image|imagine|video|embed|audio|tts|speech', name, re.I): continue
            vision = not bool(re.search('code', name, re.I))
        if provider == 'agent_copilot':
            if record.get('model_picker_enabled') is not True or record.get('policy', {}).get('state') == 'disabled': continue
            endpoints = record.get('supported_endpoints', ['/chat/completions'])
            if '/chat/completions' in endpoints: wire = 'openai'
            elif '/responses' not in endpoints: continue
            vision = record.get('capabilities', {}).get('supports', {}).get('vision') is True
        result.append(dict(id=name, vision=vision, wire=wire)); seen.add(name)
    if not result: raise _fail('Agent 未返回可用模型。', 'empty_response')
    with _catalog_lock:
        if len(_catalogs) >= 64: _catalogs.pop(next(iter(_catalogs)))
        _catalogs[key] = (fingerprint, time.monotonic() + 300, result)
    return [dict(v) for v in result]


def model_names(provider, reference, category, timeout=30):
    if not supports(provider, category):
        raise _fail('此 Agent 不支持当前模型类别。', 'unsupported')
    return [r['id'] for r in catalog(provider, reference, timeout=timeout, refresh=True)
            if category not in ('vision', 'image_edit') or r['vision']]


def complete(provider, reference, model, system, text, *, image=None, timeout=90, max_tokens=None, web_search=None):
    from api_calls.provider_client import image_sources
    images = image_sources(image)
    deadline = time.monotonic() + timeout
    from . import agent_search as search
    use_search = search.enabled(provider) if web_search is None else web_search is True
    search_tool = search.tool(provider) if use_search else None
    if not str(model or '').strip(): raise _fail('请刷新并选择 Agent 模型。', 'invalid_config')
    wire = 'claude' if provider == 'agent_claude' else 'responses'
    if provider == 'agent_copilot':
        entry = next((r for r in catalog(provider, reference, timeout=timeout) if r['id'] == model), None)
        if not entry or image and not entry['vision']:
            raise _fail('所选 Copilot 模型不可用或不支持图片，请刷新模型。', 'unsupported')
        wire = entry['wire']
    elif provider == 'agent_grok' and image and re.search(r'code|embed', model, re.I):
        raise _fail('此 xAI 模型不支持视觉输入。', 'unsupported')
    content = [{'type': 'input_text', 'text': str(text)}]
    content.extend({'type': 'input_image', 'image_url': f'data:{mime};base64,{encoded}'} for mime, encoded in images)
    payload = dict(model=model, input=[dict(role='user', content=content)], instructions=str(system or ''),
                   stream=True, store=False)
    url = AGENTS[provider]['endpoint']
    if wire == 'claude':
        content = [dict(type='text', text=str(text))]
        content = [dict(type='image', source=dict(type='base64', media_type=mime, data=encoded)) for mime, encoded in images] + content
        payload = dict(model=model, max_tokens=max_tokens or 4096, stream=True,
            system=[dict(type='text', text="You are Claude Code, Anthropic's official CLI for Claude.")] +
                   ([dict(type='text', text=str(system))] if system else []), messages=[dict(role='user', content=content)])
    elif wire == 'openai':
        content = str(text) if not images else [dict(type='text', text=str(text))] + [
                    dict(type='image_url', image_url=dict(url=f'data:{mime};base64,{encoded}')) for mime, encoded in images]
        payload = dict(model=model, messages=([dict(role='system', content=str(system))] if system else []) +
                       [dict(role='user', content=content)], stream=False)
    elif provider == 'agent_copilot':
        url = 'https://api.githubcopilot.com/responses'
    if search_tool:
        payload['tools'] = [search_tool]
    search_calls = 0
    history = []
    for continuation in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _fail('Agent 搜索处理超时，未自动重新提交。', 'incomplete_response')
        try:
            data = _request(provider, reference, url, payload=payload, stream=payload['stream'], image=bool(image), timeout=remaining)
        except ProviderAPIError as error:
            if use_search and error.status_code in (400, 403, 422):
                raise ProviderAPIError('Agent 拒绝含联网搜索工具的请求；请检查模型、账户搜索权限或关闭联网搜索后手动重试。',
                                       status='search_unavailable', status_code=error.status_code) from None
            raise
        search_calls += search.check_tools(data, wire)
        if wire != 'claude':
            return search.result(data, wire, search_calls)
        history.extend(data.get('content', []))
        if data.get('stop_reason') != 'pause_turn':
            if data.get('stop_reason') not in ('end_turn', 'stop_sequence'):
                raise _fail('Agent 尚未完成回答，未自动重新提交。', 'incomplete_response')
            return search.result(dict(data, content=history), wire, search_calls)
        if not use_search or continuation == 3 or search_calls >= 5:
            raise _fail('Agent 搜索达到续接上限，尚未完成回答。', 'incomplete_response')
        # Continue the same assistant turn, retaining tool state and citations.
        # This is not a retry of the original user request.
        payload['messages'] = [payload['messages'][0], dict(role='assistant', content=list(history))]


def _image_data(mime, encoded):
    if mime not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif') or len(encoded) > 28 * 1024 * 1024:
        raise _fail('图片格式不支持或图片超过 20 MB。', 'invalid_config')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise _fail('图片数据无效；未发送编辑请求。', 'invalid_config') from None
    actual = ('image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else
              'image/gif' if raw.startswith((b'GIF87a', b'GIF89a')) else
              'image/webp' if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' else '')
    if mime != actual or len(raw) > 20 * 1024 * 1024:
        raise _fail('图片内容与格式不符；未发送编辑请求。', 'invalid_config')
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width * picture.height > 40_000_000:
                raise ValueError
            picture.verify()
    except Exception:
        raise _fail('图片损坏或尺寸超过限制；未发送编辑请求。', 'invalid_config') from None
    return raw


def edit_images(provider, reference, model, prompt, images, *, timeout=90,
                system_prompt=None, merge_system_prompt=False):
    """Return verified (mime, bytes) results. Never turn a failed edit into generation."""
    if not supports(provider, 'image_edit'):
        raise _fail('图像编辑 Agent 仅支持 Codex 和 xAI。', 'unsupported')
    _validate_image_llm(provider, model, prompt)
    limit = 3 if provider == 'agent_grok' else 5
    if not isinstance(images, (list, tuple)) or not 1 <= len(images) <= limit:
        raise _fail(f'此 Agent 的图像编辑需要 1–{limit} 张有效的输入图片。', 'invalid_config')
    total, urls = 0, []
    for mime, encoded in images:
        total += len(_image_data(mime, encoded))
        if total > 32 * 1024 * 1024: raise _fail('输入图片合计超过 32 MB。', 'invalid_config')
        urls.append(f'data:{mime};base64,{encoded}')
    return _image_tool(provider, reference, model, prompt, urls, 'edit', timeout,
                       system_prompt, merge_system_prompt)


def generate_images(provider, reference, model, prompt, *, timeout=90,
                    system_prompt=None, merge_system_prompt=False):
    if not supports(provider, 'text2img'):
        raise _fail('图像生成 Agent 仅支持 Codex 和 xAI。', 'unsupported')
    _validate_image_llm(provider, model, prompt)
    return _image_tool(provider, reference, model, prompt, [], 'generate', timeout,
                       system_prompt, merge_system_prompt)


def _validate_image_llm(provider, model, prompt):
    if not llm_name(provider, model):
        raise _fail('请刷新并选择 Agent 的 LLM；图像模型 ID 不能用于 Agent 调用。', 'invalid_config')
    if not str(prompt or '').strip():
        raise _fail('请填写图像提示词。', 'invalid_config')


def _image_tool(provider, reference, model, prompt, urls, action, timeout,
                system_prompt, merge_system_prompt):
    """The selected LLM orchestrates one hosted image-tool request.

    No separate image-model ID, client-side tool execution, or direct Images API
    fallback. Subscription access remains subject to the server's entitlement.
    """
    from .image_prompts import options, prepare
    if system_prompt is None:
        system_prompt = options({}, 'image_edit' if action == 'edit' else 'text2img')['system_prompt']
    instruction = ('Edit the supplied images as requested using the image_generation tool. '
                   'Do not generate an unrelated replacement.' if action == 'edit' else
                   'Generate the requested image using the image_generation tool.')
    instruction, prompt = prepare(str(prompt).strip(),
        instruction + ('\n\n' + str(system_prompt).strip() if str(system_prompt or '').strip() else ''),
        supports_system_prompt=not merge_system_prompt)
    content = [dict(type='input_text', text=prompt)]
    content.extend(dict(type='input_image', image_url=url) for url in urls)
    payload = dict(model=str(model).strip(), input=[dict(role='user', content=content)],
                   tools=[dict(type='image_generation', action=action)],
                   stream=True, store=False)
    # Codex requires the instructions key, even when no system text is sent.
    if instruction or provider == 'agent_codex':payload['instructions'] = instruction
    if provider == 'agent_codex': payload['tool_choice'] = dict(type='image_generation')
    data = _request(provider, reference, AGENTS[provider]['endpoint'], payload=payload,
                    stream=True, image=bool(urls), timeout=timeout)
    return _image_results(data)


def _image_results(data):
    if data.get('status') != 'completed':
        raise _fail('Agent 图像任务未确认完成，未自动重复提交。', 'incomplete_response')
    output = data.get('output')
    if not isinstance(output, list): raise _fail('Agent 图像响应格式无效。')
    records = [item for item in output if isinstance(item, dict) and item.get('type') == 'image_generation_call']
    if not records:
        raise _fail('Agent 未返回图像工具结果；请核对所选 LLM 和账户的工具权限。未自动重复提交。', 'empty_response')
    if len(records) > 10:
        raise _fail('Agent 返回图片数量超过限制。')
    results = []
    for record in records:
        if record.get('status') != 'completed':
            raise _fail('Agent 图像工具未确认完成，未自动重复提交。', 'incomplete_response')
        encoded = record.get('result', '')
        if not isinstance(encoded, str) or not encoded or len(encoded) > 28 * 1024 * 1024:
            raise _fail('Agent 图像工具结果无效或过大。')
        # Request base64 only; do not fetch arbitrary URLs from a response.
        try: raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError): raise _fail('Agent 返回了无效图片。') from None
        mime = ('image/png' if raw.startswith(b'\x89PNG') else 'image/jpeg' if raw.startswith(b'\xff\xd8') else
                'image/webp' if raw[:4] == b'RIFF' else 'image/gif')
        try:
            results.append((mime, _image_data(mime, encoded)))
        except ProviderAPIError:
            raise _fail('Agent 返回的图片损坏或超过大小限制；未自动重复提交。') from None
    return results
