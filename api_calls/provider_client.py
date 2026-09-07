"""Protocol-aware text/image requests with strict, credential-safe responses."""

import math
import re
from urllib.parse import urlsplit, urlunsplit

import requests


class ProviderAPIError(RuntimeError):
    def __init__(self, message, *, status='request_error', status_code=None, provider_code=None):
        super().__init__(message)
        self.status = status
        self.status_code = status_code
        self.provider_code = provider_code


def endpoint_parts(endpoint):
    try:
        parts = urlsplit(str(endpoint or '').strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError
        _ = parts.port
        return parts
    except (ValueError, TypeError):
        raise ProviderAPIError('请填写有效的 HTTP API 地址。', status='invalid_config') from None


def _url(parts, path):
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))


def completion_endpoint(endpoint, provider=None):
    parts = endpoint_parts(endpoint)
    path = parts.path.rstrip('/')
    provider = str(provider or '').lower()
    if path.endswith('/chat/completions'):
        return 'openai', _url(parts, path)
    if path.endswith('/responses'):
        return 'responses', _url(parts, path)
    if path.endswith('/messages'):
        return 'claude', _url(parts, path)
    if path.endswith('/api/generate'):
        return 'ollama_generate', _url(parts, path)
    if path.endswith('/api/chat'):
        return 'ollama', _url(parts, path)
    if provider in ('claude', 'anthropic') or parts.hostname == 'api.anthropic.com':
        return 'claude', _url(parts, path + ('/messages' if path.endswith('/v1') else '/v1/messages'))
    if path.endswith('/api') or (provider == 'ollama' and not path.endswith('/v1')):
        return 'ollama', _url(parts, path + ('/chat' if path.endswith('/api') else '/api/chat'))
    if (provider == 'gemini' or parts.hostname == 'generativelanguage.googleapis.com') and path.endswith(('/v1beta', '/v1')):
        path += '/openai'
    if not path:
        path = '/v1'
    return 'openai', _url(parts, path + '/chat/completions')


def request_headers(protocol, api_key):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if protocol == 'claude':
        headers['anthropic-version'] = '2023-06-01'
        if api_key:
            headers['x-api-key'] = str(api_key).strip()
    elif api_key:
        headers['Authorization'] = 'Bearer ' + str(api_key).strip()
    return headers


def _error_info(data, request_options):
    # Gemini's compatibility endpoint can wrap an HTTP error in a single-item
    # array. Unwrap only for diagnostics; successful responses still require a
    # JSON object in validated_json below.
    if (isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict)
            and isinstance(data[0].get('error'), dict)):
        data = data[0]
    if not isinstance(data, dict):
        return '', None
    error = data.get('error')
    error = error if isinstance(error, dict) else {}
    candidates = [error.get('code'), error.get('type'), error.get('status'), data.get('error_code')]
    for detail in error.get('details', []) if isinstance(error.get('details'), list) else []:
        if isinstance(detail, dict):
            candidates.append(detail.get('reason'))
    secrets = []
    for field in ('headers', 'params', 'data'):
        values = request_options.get(field, {})
        if isinstance(values, dict):
            for key, value in values.items():
                if str(key).lower() in ('authorization', 'x-api-key', 'key', 'api_key', 'sign'):
                    secrets.extend((str(value), str(value).removeprefix('Bearer ')))
    codes = []
    for candidate in candidates:
        value = str(candidate) if candidate is not None else ''
        if re.fullmatch(r'[A-Za-z0-9_]{1,48}', value) and not any(secret and secret in value for secret in secrets):
            codes.append(value)
    # Recognise one documented gateway failure without exposing its free-form
    # message (which may otherwise contain credentials or request contents).
    message = error.get('message', data.get('message', ''))
    if isinstance(message, str) and re.search(r'\buser location is not supported\b', message, re.IGNORECASE):
        codes.append('REGION_NOT_SUPPORTED')
    hints = {
        'invalid_api_key': '密钥无效或已过期。', 'api_key_invalid': '密钥无效或已过期。',
        'api_key_expired': '密钥无效或已过期。', 'insufficient_quota': '余额或配额不足。',
        'credit_balance_exhausted': '余额不足。', 'region_not_supported': '此服务不支持当前请求所在地区。',
        'quota_exceeded': '余额或配额不足。', 'service_disabled': '对应 API 服务尚未启用。',
        'model_not_found': '模型不存在或当前账号无权使用。', 'authentication_error': '认证失败，请检查密钥。',
        'unauthenticated': '认证失败，请检查密钥。', 'permission_denied': '账号或模型权限不足。',
        'permission_error': '账号或模型权限不足。', 'rate_limit_exceeded': '请求频率受限，请稍后重试。',
        'resource_exhausted': '额度或请求频率受限。', 'invalid_argument': '请求参数无效。',
        'invalid_request_error': '请求参数无效。', 'not_found': '模型或接口不存在。',
        '52003': '百度应用未授权，请检查 AppID。', '54001': '百度签名错误，请检查 AppID 和密钥。',
        '54003': '百度访问频率受限。', '54004': '百度翻译余额不足。',
        '58000': '百度应用的 IP 限制未通过。', '58001': '翻译语种不受支持。',
    }
    # Specific credential/quota reasons are more informative than a generic type.
    priority = ('invalid_api_key', 'api_key_invalid', 'api_key_expired', 'authentication_error', 'unauthenticated',
                'region_not_supported', 'credit_balance_exhausted', 'insufficient_quota',
                'quota_exceeded', 'service_disabled', 'model_not_found', 'permission_denied', 'permission_error')
    lower = [code.lower() for code in codes]
    chosen = next((code for code in priority if code in lower), next((code for code in lower if code in hints), None))
    suffix = (hints[chosen] + ' ' if chosen else '') + ('错误代码：' + ', '.join(codes[:4]) if codes else '')
    return suffix, ','.join(codes[:4]) or None


def validated_json(method, url, *, timeout=30, **kwargs):
    endpoint_parts(url)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ProviderAPIError('请求超时必须是正数。', status='invalid_config')
    response = None
    try:
        response = getattr(requests, method.lower())(url, timeout=timeout, **kwargs)
        status = response.status_code
        try:
            data = response.json()
        except (ValueError, TypeError):
            data = None
        detail, provider_code = _error_info(data, kwargs)
        if not isinstance(status, int) or not 200 <= status < 300:
            if status in (401, 403):
                message, kind = (f'认证失败（HTTP {status}），请检查密钥。' if status == 401 else
                                 '权限不足（HTTP 403），请检查模型访问权限。'), 'auth_error'
            elif status == 429:
                message, kind = '请求受限，请检查额度或稍后重试。', 'rate_limited'
            else:
                message, kind = f'API 请求失败（HTTP {status if isinstance(status, int) else "未知"}）。', 'http_error'
            raise ProviderAPIError(message + (' ' + detail if detail else ''), status=kind,
                                   status_code=status if isinstance(status, int) else None, provider_code=provider_code)
        if not isinstance(data, dict):
            raise ProviderAPIError('API 未返回有效的 JSON 对象。', status='invalid_response', status_code=status)
        if (('error' in data and data['error'] not in (None, False, ''))
                or data.get('type') == 'error'
                or data.get('error_code') not in (None, 0, '0', 52000, '52000')
                or data.get('code') not in (None, 0, '0', 200, '200')):
            raise ProviderAPIError('API 返回业务错误。' + (detail or '请检查模型、参数、权限或额度。'),
                                   status='api_error', status_code=status, provider_code=provider_code)
        return data
    except ProviderAPIError:
        raise
    except requests.Timeout:
        raise ProviderAPIError('请求超时，请检查网络或适当增加超时。', status='timeout') from None
    except requests.RequestException:
        raise ProviderAPIError('网络请求失败，请检查地址、网络和代理设置。', status='network_error') from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def openai_reasoning_model(model):
    return bool(re.match(r'^(?:gpt-[56](?:[.\-]|$)|o[134](?:[.\-]|$))', str(model).lower()))


def reasoning_model(model):
    return openai_reasoning_model(model) or bool(re.match(
        r'^(?:deepseek-(?:v4|reasoner)(?:$|\-)|gemini-(?:2\.5|3)(?:[.\-]|$))', str(model).lower()))


def _text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return '\n'.join(item['text'].strip() for item in value if isinstance(item, dict)
                         and item.get('type') in ('text', 'output_text') and isinstance(item.get('text'), str)
                         and item['text'].strip())
    return ''


def response_text(data, protocol):
    result = ''
    if protocol == 'claude':
        if data.get('stop_reason') in ('max_tokens', 'refusal'):
            raise ProviderAPIError('模型输出被截断或拒绝，请调整参数后重试。', status='incomplete_response')
        result = _text(data.get('content'))
    elif protocol == 'ollama_generate':
        if data.get('done') is False or data.get('done_reason') == 'length':
            raise ProviderAPIError('本地模型未返回完整文本。', status='incomplete_response')
        result = _text(data.get('response'))
    elif protocol == 'ollama':
        if data.get('done') is False or data.get('done_reason') == 'length':
            raise ProviderAPIError('本地模型未返回完整文本。', status='incomplete_response')
        message = data.get('message')
        result = _text(message.get('content')) if isinstance(message, dict) else ''
    elif protocol == 'responses':
        if data.get('status') in ('failed', 'incomplete', 'cancelled', 'queued', 'in_progress'):
            raise ProviderAPIError('模型尚未返回完整文本。', status='incomplete_response')
        result = _text(data.get('output_text'))
        if not result and isinstance(data.get('output'), list):
            result = '\n'.join(filter(None, (_text(item.get('content')) for item in data['output']
                                              if isinstance(item, dict) and item.get('type') == 'message')))
    else:
        choices = data.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
            if first.get('finish_reason') in ('length', 'content_filter', 'tool_calls', 'function_call'):
                raise ProviderAPIError('模型未返回完整文本（输出上限、内容限制或工具调用）。', status='incomplete_response')
            message = first.get('message')
            result = _text(message.get('content')) if isinstance(message, dict) else ''
    if not result:
        raise ProviderAPIError('API 返回了空文本或不兼容的响应，测试未通过。', status='empty_response')
    return result


def complete_text(endpoint, api_key, model, system_prompt, user_text, *, provider=None,
                  temperature=None, timeout=30, max_tokens=None, image=None, reasoning_effort=None,
                  thinking=None):
    protocol, url = completion_endpoint(endpoint, provider)
    if not isinstance(model, str) or not model.strip():
        raise ProviderAPIError('请先填写或选择模型。', status='invalid_config')
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0):
        raise ProviderAPIError('输出 token 上限必须是正整数。', status='invalid_config')
    messages = []
    if system_prompt:
        messages.append({'role': 'system', 'content': str(system_prompt)})
    content = str(user_text)
    if image:
        mime, encoded = image
        content = [{'type': 'text', 'text': str(user_text)},
                   {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}}]
    messages.append({'role': 'user', 'content': content})
    payload = {'model': model.strip(), 'messages': messages, 'stream': False}
    if protocol == 'claude':
        payload['messages'] = [item for item in messages if item['role'] != 'system']
        payload['max_tokens'] = max_tokens or 4096
        if system_prompt:
            payload['system'] = str(system_prompt)
        if image:
            payload['messages'][-1]['content'] = [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': encoded}},
                {'type': 'text', 'text': str(user_text)}]
        # Native reasoning models reject sampling controls; omit them rather
        # than overriding each model's own supported defaults.
    elif protocol.startswith('ollama'):
        if protocol == 'ollama_generate':
            payload = {'model': model.strip(), 'prompt': str(user_text), 'stream': False}
            if system_prompt:
                payload['system'] = str(system_prompt)
            if image:
                payload['images'] = [encoded]
        else:
            payload['messages'][-1]['content'] = str(user_text)
            if image:
                payload['messages'][-1]['images'] = [encoded]
        options = {}
        if temperature is not None:
            options['temperature'] = temperature
        if max_tokens is not None:
            options['num_predict'] = max_tokens
        if options:
            payload['options'] = options
    elif protocol == 'responses':
        input_content = [{'type': 'input_text', 'text': str(user_text)}]
        if image:
            input_content.append({'type': 'input_image', 'image_url': f'data:{mime};base64,{encoded}'})
        payload = {'model': model.strip(), 'input': [{'role': 'user', 'content': input_content}], 'stream': False}
        if system_prompt:
            payload['instructions'] = str(system_prompt)
        if max_tokens is not None:
            payload['max_output_tokens'] = max_tokens
        if temperature is not None and not reasoning_model(model):
            payload['temperature'] = temperature
    else:
        if max_tokens is not None:
            payload['max_completion_tokens' if openai_reasoning_model(model) else 'max_tokens'] = max_tokens
        if temperature is not None and not reasoning_model(model):
            payload['temperature'] = temperature
    if thinking is not None and str(model).lower().startswith('deepseek-v4'):
        if protocol == 'openai':
            payload['thinking'] = {'type': 'enabled' if thinking else 'disabled'}
        elif protocol == 'responses' and not thinking:
            payload['reasoning'] = {'effort': 'none'}
    if reasoning_effort and reasoning_model(model) and thinking is not False:
        if protocol == 'openai':
            payload['reasoning_effort'] = reasoning_effort
        elif protocol == 'responses':
            payload['reasoning'] = {'effort': reasoning_effort}
    data = validated_json('post', url, headers=request_headers(protocol, api_key), json=payload, timeout=timeout)
    return response_text(data, protocol)
