"""Safe, Qt-independent API management probes using immutable snapshots."""

import base64
from concurrent.futures import ThreadPoolExecutor
import re
import struct
import time
from urllib.parse import urlunsplit
import zlib

from api_calls.provider_client import (ProviderAPIError, complete_text, completion_endpoint,
                                       endpoint_parts, reasoning_model, request_headers, validated_json)
from api_calls.call_translate import translate_text


def _excerpt(text, snapshot):
    text = str(text)
    for field in ('api_key', 'secret', 'app_secret', 'secret_key'):
        secret = snapshot.get(field)
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'https?://[^\s<>]+', '[链接]', text)
    text = re.sub(r'(?i)((?:api[-_]?key|authorization|token|secret|signature)\s*[:=]\s*)[^\s,;]+', r'\1[已隐藏]', text)
    return text[:300]


def _elapsed(started):
    return max(0, round((time.monotonic() - started) * 1000))


def _failure(error, started):
    if isinstance(error, ProviderAPIError):
        return dict(ok=False, status=error.status, message=str(error), elapsed_ms=_elapsed(started),
                    status_code=error.status_code, provider_code=error.provider_code)
    # Never leak an HTTP library exception, config path, secret or response body.
    return dict(ok=False, status='request_error', message='测试未完成，请检查配置、网络和模型参数。',
                elapsed_ms=_elapsed(started), status_code=None)


def _snapshot(value):
    if not isinstance(value, dict):
        raise ProviderAPIError('API 测试配置无效。', status='invalid_config')
    result = dict(value)
    result['timeout'] = float(result.get('timeout', 30))
    return result


def _probe_image():
    """A synthetic 64x64 red image; never reads or uploads a user file."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    raw = (b'\x00' + b'\xff\x00\x00' * 64) * 64
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 64, 64, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    return 'image/png', base64.b64encode(png).decode('ascii')


def test_response(snapshot):
    started = time.monotonic()
    try:
        config = _snapshot(snapshot)
        category = str(config.get('category') or '')
        provider = str(config.get('provider') or '')
        endpoint = str(config.get('endpoint') or '')
        key, model = config.get('api_key', ''), str(config.get('model') or '')
        if category == 'translator':
            text = translate_text(endpoint, key, model, 'Hello', 'zh', source_lang='en',
                                  timeout=config['timeout'], provider=provider,
                                  extra={name: config[name] for name in ('appid', 'secret') if config.get(name)})
        elif category in ('llm', 'vision'):
            protocol, _ = completion_endpoint(endpoint, provider)
            modern = reasoning_model(model)
            deepseek_v4 = model.lower().startswith('deepseek-v4')
            # Small visible replies can still need a reasoning budget. Native
            # DeepSeek supports disabling thinking for this connectivity probe.
            budget = config.get('max_tokens') or (256 if deepseek_v4 else 4096)
            text = complete_text(endpoint, key, model, '',
                                 'Describe the dominant color of this image in one short word.' if category == 'vision' else 'Reply with OK.',
                                 provider=provider, timeout=config['timeout'], max_tokens=int(budget),
                                 image=_probe_image() if category == 'vision' else None,
                                 reasoning_effort='low' if modern and not deepseek_v4 else None,
                                 thinking=False if deepseek_v4 else None)
        else:
            raise ProviderAPIError('此类别暂不支持文本/图片响应测试。', status='unsupported')
        excerpt = _excerpt(text, config)
        return dict(ok=True, status='ok', message='响应测试通过，已收到有效文本。', elapsed_ms=_elapsed(started),
                    status_code=200, text=excerpt, response_excerpt=excerpt)
    except Exception as error:
        return _failure(error, started)


def _models_endpoint(endpoint, provider):
    protocol, completion = completion_endpoint(endpoint, provider)
    parts = endpoint_parts(completion)
    path = parts.path
    if provider == 'ollama' or protocol.startswith('ollama'):
        if '/api/' in path:
            path = path.rsplit('/api/', 1)[0] + '/api/tags'
        elif '/v1/' in path:
            path = path.rsplit('/v1/', 1)[0] + '/api/tags'
        else:
            path = '/api/tags'
        protocol = 'ollama'
    elif protocol == 'claude':
        path = path[:-len('/messages')] + '/models'
    elif protocol == 'responses':
        path = path[:-len('/responses')] + '/models'
    else:
        path = path[:-len('/chat/completions')] + '/models'
    return protocol, urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))


def _model_ids(data, protocol):
    records = data.get('models' if protocol == 'ollama' else 'data')
    if not isinstance(records, list):
        raise ProviderAPIError('模型目录响应格式无效。', status='invalid_response')
    result = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = (record.get('model') or record.get('name')) if protocol == 'ollama' else record.get('id')
        if isinstance(name, str) and name.strip() and len(name) <= 256 and not re.search(r'\s|[\x00-\x1f]', name) and '://' not in name:
            result.append(name.strip())
    return list(dict.fromkeys(result))


def _ollama_capabilities(names, category, url, headers, deadline):
    if category not in ('llm', 'vision'):
        raise ProviderAPIError('Ollama 模型目录目前支持文本和视觉类别。', status='unsupported')
    if len(names) > 256:
        raise ProviderAPIError('本地模型目录过大，请手动输入模型或减少待检查模型。', status='invalid_response')
    parts = endpoint_parts(url)
    show_url = urlunsplit((parts.scheme, parts.netloc, parts.path[:-len('/tags')] + '/show', parts.query, ''))

    def inspect(name):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderAPIError('模型能力检查超时，请增加超时设置。', status='timeout')
        data = validated_json('post', show_url, timeout=remaining, headers=headers, json={'model': name})
        capabilities = data.get('capabilities')
        if not isinstance(capabilities, list):
            raise ProviderAPIError('Ollama 未提供模型能力信息，请检查服务版本。', status='invalid_response')
        expected = 'vision' if category == 'vision' else 'completion'
        return name if expected in capabilities else None

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='api-model-capability') as executor:
        return list(filter(None, executor.map(inspect, names)))


def fetch_models(snapshot):
    started = time.monotonic()
    try:
        config = _snapshot(snapshot)
        provider = str(config.get('provider') or '').lower()
        if config.get('category') == 'translator' or provider in ('baidu_translate', 'google_translate', 'google_v2'):
            raise ProviderAPIError('此翻译接口不提供动态模型目录；请使用“测试响应”验证配置。', status='unsupported')
        if provider.startswith('runninghub'):
            raise ProviderAPIError('RunningHub 使用应用目录，不支持此模型列表接口。', status='unsupported')
        protocol, url = _models_endpoint(config.get('endpoint'), provider)
        headers = request_headers(protocol, config.get('api_key', ''))
        names, cursors, records = [], set(), []
        params = None
        for _ in range(10):
            data = validated_json('get', url, timeout=config['timeout'], headers=headers,
                                  **({'params': params} if params else {}))
            names.extend(_model_ids(data, protocol))
            page_records = data.get('models' if protocol == 'ollama' else 'data')
            if isinstance(page_records, list):
                records.extend(page_records)
            if protocol != 'claude' or not data.get('has_more'):
                break
            cursor = data.get('last_id')
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ProviderAPIError('模型目录分页无效，未更新列表。', status='invalid_response')
            cursors.add(cursor)
            params = {'after_id': cursor, 'limit': 100}
        else:
            raise ProviderAPIError('模型目录分页超过限制，未更新列表。', status='invalid_response')
        names = list(dict.fromkeys(names))
        category = str(config.get('category') or '')
        if protocol == 'ollama':
            names = _ollama_capabilities(names, category, url, headers, started + config['timeout'])
        else:
            from aetherloom_core.api_model_capabilities import filter_models
            allowed = set(filter_models(category, provider, records))
            names = [name for name in names if name in allowed]
        if not names:
            raise ProviderAPIError('接口未返回符合当前类别的模型；可保留自定义模型并测试响应。', status='empty_response')
        message = f'已从接口读取并筛选 {len(names)} 个候选模型；请用“测试响应”确认可用性。'
        return dict(ok=True, status='ok', message=message, elapsed_ms=_elapsed(started),
                    status_code=200, source='remote', models=names)
    except Exception as error:
        result = _failure(error, started)
        result['models'] = []
        return result


# Pytest may import the public API under its original name in a test module.
test_response.__test__ = False
