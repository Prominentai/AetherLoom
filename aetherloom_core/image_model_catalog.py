"""Image model presets checked against provider documentation on 2026-09-10.

Presets describe capabilities, not account entitlements or successful inference.
Regional catalogs are deliberately separate; saved custom names remain editable.
"""
from urllib.parse import urlsplit, urlunsplit

IMAGE_CATEGORIES = ('text2img', 'image_edit')


def validate_edit_input_count(config, count, *, batch=False):
    """Enforce only known adapter limits; unknown cardinality stays unrestricted."""
    protocol = config.get('protocol') or config.get('provider') or ''
    name = config.get('model', '')
    limit = (1 if protocol in ('siliconflow_cn', 'siliconflow_com') or name == 'dall-e-2'
             or protocol.startswith('alibaba_') and name.startswith('wanx2.1-') else
             5 if protocol in ('agent_codex', 'grok') else 3 if protocol == 'agent_grok' else None)
    if limit is not None and count > limit:
        raise ValueError(f'当前图像编辑接口最多接收 {limit} 张图像，此次为 {count} 张；请调整 Batch 组大小或转为 List 逐项运行。')


CATALOG_UPDATED = '2026-09-10'
OPENAI_MODELS = ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2']
GEMINI_MODELS = ['gemini-3.1-flash-image', 'gemini-3-pro-image',
                 'gemini-3.1-flash-lite-image', 'gemini-2.5-flash-image']
SEEDREAM_MODELS = ['doubao-seedream-5-0-260128', 'doubao-seedream-4-5-251128',
                   'doubao-seedream-4-0-250828']
# International IDs and region availability are not derived from domestic IDs.
# https://docs.byteplus.com/api/docs/ModelArk/1824121
# https://docs.byteplus.com/api/docs/ModelArk/1099455
BYTEPLUS_AP_IMAGES = ['dola-seedream-5-0-pro-260628', 'seedream-5-0-lite-260128',
                      'seedream-4-5-251128', 'seedream-4-0-250828']
BYTEPLUS_EU_IMAGES = ['seedream-5-0-lite-260128']
QWEN_SHARED = ['qwen-image-3.0-pro', 'qwen-image-3.0', 'qwen-image-2.0-pro', 'qwen-image-2.0']
WAN_SHARED = ['wan2.7-image-pro', 'wan2.7-image', 'wan2.6-image']
ALI_GENERATION = QWEN_SHARED + WAN_SHARED + ['wan2.6-t2i', 'qwen-image-max', 'qwen-image-plus',
    'qwen-image', 'z-image-turbo', 'wan2.5-t2i-preview', 'wan2.2-t2i-plus', 'wan2.2-t2i-flash']
ALI_EDITING = QWEN_SHARED + WAN_SHARED + ['qwen-image-edit-max', 'qwen-image-edit-plus',
                                        'qwen-image-edit', 'wan2.5-i2i-preview']
PRESETS = {
    'text2img': {
        'openai': OPENAI_MODELS,
        'gemini': GEMINI_MODELS,
        'grok': ['grok-imagine-image-2.0'],
        'volcengine': SEEDREAM_MODELS,
        'byteplus_ap': BYTEPLUS_AP_IMAGES,
        'byteplus_eu': BYTEPLUS_EU_IMAGES,
        'glm': ['glm-image', 'cogview-4-250304', 'cogview-3-flash'],
        'siliconflow_cn': ['Qwen/Qwen-Image', 'Kwai-Kolors/Kolors'],
        'siliconflow_com': ['Qwen/Qwen-Image', 'Tongyi-MAI/Z-Image-Turbo',
            'black-forest-labs/FLUX.2-pro', 'black-forest-labs/FLUX.2-flex',
            'black-forest-labs/FLUX-1.1-pro', 'black-forest-labs/FLUX-1.1-pro-Ultra',
            'black-forest-labs/FLUX.1-schnell', 'black-forest-labs/FLUX.1-dev'],
        'alibaba_bj': ALI_GENERATION,
        'alibaba_sg': ALI_GENERATION,
    },
    'image_edit': {
        'openai': OPENAI_MODELS,
        'gemini': GEMINI_MODELS,
        'grok': ['grok-imagine-image-2.0'],
        'volcengine': SEEDREAM_MODELS,
        'byteplus_ap': BYTEPLUS_AP_IMAGES,
        'byteplus_eu': BYTEPLUS_EU_IMAGES,
        'siliconflow_cn': ['Qwen/Qwen-Image-Edit-2509', 'Qwen/Qwen-Image-Edit'],
        'siliconflow_com': ['Qwen/Qwen-Image-Edit', 'black-forest-labs/FLUX.1-Kontext-pro',
            'black-forest-labs/FLUX.1-Kontext-max', 'black-forest-labs/FLUX.1-Kontext-dev'],
        'alibaba_bj': ALI_EDITING + ['wanx2.1-imageedit'],
        'alibaba_sg': ALI_EDITING,
    },
}
DOCS = {
    'openai': 'https://developers.openai.com/api/docs/guides/image-generation',
    'gemini': 'https://ai.google.dev/gemini-api/docs/image-generation',
    'grok': 'https://docs.x.ai/developers/model-capabilities/images/',
    'volcengine': 'https://www.volcengine.com/docs/82379/1541523',
    'byteplus_ap': 'https://docs.byteplus.com/api/docs/ModelArk/1824121',
    'byteplus_eu': 'https://docs.byteplus.com/api/docs/ModelArk/1824121',
    'glm': 'https://docs.bigmodel.cn/api-reference/模型-api/图像生成',
    'siliconflow_cn': 'https://docs.siliconflow.cn/docs/api/images-generations-post',
    'siliconflow_com': 'https://docs.siliconflow.com/en/api-reference/images/images-generations',
    'alibaba_bj': 'https://help.aliyun.com/zh/model-studio/image-model',
    'alibaba_sg': 'https://help.aliyun.com/en/model-studio/image-model',
}


def entries(category):
    return [dict(key=key, models=list(models)) for key, models in PRESETS[category].items()]


def endpoint(provider, category, model=''):
    if category not in IMAGE_CATEGORIES:
        return ''
    route = 'generations' if category == 'text2img' else 'edits'
    if provider in ('agent_grok', 'agent_codex'):
        from .agent_catalog import AGENTS
        return AGENTS[provider]['endpoint']
    if provider in ('openai', 'grok'):
        base = {'openai': 'https://api.openai.com/v1', 'grok': 'https://api.x.ai/v1'}[provider]
        return base + '/images/' + route
    if provider == 'gemini':
        return 'https://generativelanguage.googleapis.com/v1beta/interactions'
    if provider == 'volcengine':
        # Ark accepts reference images in the same JSON generation request.
        return 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
    if provider in ('byteplus_ap', 'byteplus_eu'):
        region = 'ap-southeast' if provider == 'byteplus_ap' else 'eu-west'
        return f'https://ark.{region}.bytepluses.com/api/v3/images/generations'
    if provider in ('siliconflow_cn', 'siliconflow_com'):
        return 'https://api.siliconflow.' + ('cn' if provider.endswith('_cn') else 'com') + '/v1/images/generations'
    if provider == 'glm':
        return 'https://open.bigmodel.cn/api/paas/v4/images/generations' if category == 'text2img' else ''
    if provider in ('alibaba_bj', 'alibaba_sg'):
        # Existing regional domains remain functional (official migration guide).
        base = 'https://dashscope' + ('-intl' if provider.endswith('_sg') else '') + '.aliyuncs.com/api/v1/services/aigc/'
        if model in ('wan2.5-i2i-preview', 'wanx2.1-imageedit'):
            return base + 'image2image/image-synthesis'
        if model in ('wan2.5-t2i-preview', 'wan2.2-t2i-plus', 'wan2.2-t2i-flash'):
            return base + 'text2image/image-synthesis'
        return base + 'multimodal-generation/generation'
    return ''


def follow_model_endpoint(provider, category, model, current):
    """Follow model changes only for known provider-owned defaults, never a proxy."""
    target = endpoint(provider, category, model)
    if not target:
        return current
    known = {endpoint(provider, category, m) for m in PRESETS.get(category, {}).get(provider, [])}
    known.add(endpoint(provider, category))
    if provider == 'gemini':
        known.add('https://generativelanguage.googleapis.com/v1beta/openai/images/generations')
    if current.rstrip('/') in known or not current:
        return target
    return current


def docs_url(provider, category):
    url = DOCS.get(provider, '')
    return url + ('generation' if category == 'text2img' else 'editing') if provider == 'grok' else url


def model_note(provider, category, model):
    if provider.startswith('agent_'):
        return '订阅 Agent 由所选 LLM 调用图像工具；普通 API 模型目录不代表订阅账户的权限。'
    if provider.startswith('custom'):
        return '画布图像节点按 OpenAI Images 格式调用自定义接口：生成使用 JSON，编辑使用 multipart；请填写对应的完整地址。'
    names = PRESETS.get(category, {}).get(provider, [])
    parts = ['文档预设 · 2026-09-10 · 具体可用性以账户权限为准']
    if provider not in PRESETS.get(category, {}) and not provider.startswith('custom'):
        parts.append('此供应商已不在当前类别的预设中，请重新选择供应商；原配置保留供核对。')
    if model and model not in names:
        parts.append('当前为保留或手填模型，请确认它支持当前图像能力。')
    if provider == 'gemini':
        parts.append('使用 Gemini 原生图像接口。')
    if provider == 'volcengine':
        parts.append('火山方舟（北京）API Key；可手填已开通的模型 ID 或接入点 ID（ep-…）。')
        parts.append('Seedream 5.0 Lite / 4.5 / 4.0 均支持生成和编辑；编辑通过同一接口的 image 字段传入参考图。')
    if provider in ('byteplus_ap', 'byteplus_eu'):
        parts.append('使用所选区域的 BytePlus ModelArk API Key；国内方舟与国际站、国际站不同区域的密钥分别保存。')
        parts.append('国际模型 ID 单独维护；生成和编辑共用 images/generations 接口，编辑通过 image 字段传入参考图。')
        if provider == 'byteplus_eu':
            parts.append('欧洲区仅列入已确认支持的 Seedream 5.0 Lite；可手填本区域已开通的模型或接入点 ID。')
    if provider.startswith('alibaba_'):
        parts.append('北京与新加坡的密钥、模型权限独立；可填写本地域业务空间专属地址。')
        if 'image-synthesis' in endpoint(provider, category, model):
            parts.append('此模型需要异步提交并查询任务结果。')
    if provider.startswith('siliconflow_'):
        parts.append('中文站与国际站的预设分别维护。')
    return '\n'.join(parts)


def fetch_models(config):
    """Read-only catalog query; return only models with known image capabilities."""
    from api_calls.provider_client import ProviderAPIError, endpoint_parts, validated_json
    provider, category = config['provider'], config['category']
    if provider not in ('openai', 'grok', 'gemini', 'siliconflow_cn', 'siliconflow_com'):
        raise ProviderAPIError('此供应商请使用文档预设或手动填写模型。', status='unsupported')
    parts = endpoint_parts(config['endpoint'])
    key = config.get('api_key', '')
    headers = {'x-goog-api-key': key} if provider == 'gemini' else {'Authorization': 'Bearer ' + key}
    if provider == 'gemini':
        path = parts.path.rsplit('/interactions', 1)[0].rstrip('/') + '/models'
    else:
        path = parts.path.rsplit('/images/', 1)[0].rstrip('/') + '/models'
    url = urlunsplit((parts.scheme, parts.netloc, path, '', ''))
    records, cursors = [], set()
    params = {'pageSize': 100} if provider == 'gemini' else {}
    import time
    deadline = time.monotonic() + config['timeout']
    for _ in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:raise ProviderAPIError('图片模型目录查询超时。', status='timeout')
        data = validated_json('get', url, headers=headers, timeout=remaining, allow_redirects=False, params=params)
        page = data.get('models' if provider == 'gemini' else 'data')
        if not isinstance(page, list):
            raise ProviderAPIError('图片模型目录格式无效。', status='invalid_response')
        records.extend(page)
        cursor = data.get('nextPageToken') if provider == 'gemini' else None
        if not cursor:break
        if not isinstance(cursor, str) or cursor in cursors:
            raise ProviderAPIError('图片模型目录分页无效。', status='invalid_response')
        cursors.add(cursor);params['pageToken'] = cursor
    else:
        raise ProviderAPIError('图片模型目录分页超限。', status='invalid_response')
    present = {(r.get('name', '').removeprefix('models/') if provider == 'gemini' else r.get('id'))
               for r in records if isinstance(r, dict)}
    return [name for name in PRESETS[category].get(provider, []) if name in present]
