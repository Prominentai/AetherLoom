"""Conservative menu filtering for server model discovery.

These are suggestions, not a validator for user-entered deployment names. An
unknown vision model needs capability metadata before it is offered as one.
Unknown LLM deployment names are candidates and still require response testing.
"""
import re

from aetherloom_core.api_manager import get_models_for_provider


_NON_CHAT = re.compile(
    r'(?:embed|rerank|moderation|transcri|whisper|\btts\b|speech|audio|realtime|'
    r'dall-e|sora|flux|cogview|cogvideo|stable-diffusion|qwen[-_/]image|'
    r'(?:^|[/_-])(?:imagen|veo|lyria)(?:[-_/\d]|$)|'
    r'gemini[^/]*[-_]image(?:[-_]|$)|grok[-_.\d]*[-_](?:image|video)(?:[-_]|$)|'
    r'gpt[-_/]image|(?:^|[/_-])wan(?:\d|[-_/])|image[-_/]gen|video[-_/]gen|'
    r'imagine|deep[-_/]research|multi[-_/]agent|codex|search[-_/]preview|'
    r'(?:^|[-_/])(?:babbage|davinci)(?:[-_/]|$))', re.I
)
_NON_CHAT_TASKS = {
    'embedding', 'embeddings', 'text-embedding', 'feature-extraction', 'rerank',
    'reranking', 'image-generation', 'text-to-image', 'image-to-image',
    'text-to-video', 'image-to-video', 'text-to-speech', 'speech-to-text',
    'automatic-speech-recognition', 'moderation',
}
_CHAT_TASKS = {'chat', 'completion', 'text-generation', 'text2text-generation', 'image-text-to-text'}
_RETIRED = {
    'gpt-4.1-nano', 'o4-mini', 'gemini-2.0-flash', 'gemini-2.0-flash-lite',
    'gemini-3-pro-preview', 'grok-3', 'grok-3-mini', 'grok-4', 'grok-4-0709',
    'grok-4-fast', 'grok-4.1-fast',
}


def _values(value):
    if isinstance(value, str):
        return {value.lower().replace('_', '-')}
    if isinstance(value, (list, tuple, set)):
        return {item.lower().replace('_', '-') for item in value if isinstance(item, str)}
    return set()


def _modalities(record, direction):
    value = record.get(direction + '_modalities')
    for container in ('architecture', 'modalities'):
        nested = record.get(container)
        if value is None and isinstance(nested, dict):
            value = nested.get(direction + '_modalities', nested.get(direction))
    return _values(value)


def _metadata(record, category, provider):
    """Return True/False for explicit evidence, None when no useful evidence."""
    tasks = set()
    for field in ('task', 'task_type', 'pipeline_tag', 'type'):
        tasks.update(_values(record.get(field)))
    if tasks & _NON_CHAT_TASKS:
        return False

    raw_caps = record.get('capabilities')
    caps = _values(raw_caps)
    if isinstance(raw_caps, dict):
        caps = {key.lower().replace('_', '-') for key, value in raw_caps.items()
                if isinstance(key, str) and value is True}
        if category == 'vision' and raw_caps.get('vision') is False:
            return False
    if caps & _NON_CHAT_TASKS:
        return False

    endpoints = _values(record.get('supported_endpoints'))
    if endpoints and not any(
        'chat' in endpoint or 'generatecontent' in endpoint or
        (provider == 'claude' and 'messages' in endpoint)
        for endpoint in endpoints
    ):
        return False
    methods = _values(record.get('supportedGenerationMethods', record.get('supported_generation_methods')))
    if methods and not any('generatecontent' in method for method in methods):
        return False

    output_modes = _modalities(record, 'output')
    if output_modes and 'text' not in output_modes:
        return False
    input_modes = _modalities(record, 'input')
    if input_modes:
        return ('image' in input_modes or 'vision' in input_modes) if category == 'vision' else 'text' in input_modes

    if category == 'vision':
        if 'vision' in caps or 'image-input' in caps:
            return True
        if raw_caps is not None:  # e.g. Ollama show: completion but no vision.
            return False
    elif caps & {'chat', 'completion', 'text-generation', 'vision'}:
        return True
    if category == 'llm' and (tasks & _CHAT_TASKS or endpoints or methods):
        return True
    return None


def _known_incompatible(name, provider):
    leaf = name.lower().rsplit('/', 1)[-1]
    if leaf in _RETIRED or any(leaf.startswith(retired + '-') for retired in _RETIRED):
        return True
    # Only known OpenAI model families: a custom deployment named sales-pro
    # must not be confused with the Responses-only OpenAI Pro variants.
    return provider == 'openai' and bool(re.match(r'(?:gpt-[456].*|o[134])-pro(?:-|$)', leaf))


def _family_supports(name, category, provider):
    leaf = name.lower().rsplit('/', 1)[-1]
    if provider == 'openai':
        return bool(re.match(r'gpt-(?:4o(?:-mini)?|4\.1(?:-mini)?|5(?:[.-]|$)|6-astra(?:-|$))', leaf))
    if provider == 'gemini':
        return bool(re.match(r'gemini-(?:2\.5|3(?:\.\d+)?)-(?:flash|pro)', leaf))
    if provider == 'claude':
        return bool(re.match(r'claude-(?:fable|opus|sonnet|haiku)-', leaf))
    if provider == 'grok':
        return bool(re.match(r'grok-4\.(?:[3-9]|[1-9]\d)(?:-|$)', leaf))
    if provider in ('glm', 'deepseek', 'siliconflow_cn', 'siliconflow_com', 'alibaba_bj', 'alibaba_sg'):
        if category == 'llm':
            return bool(re.match(r'(?:deepseek-(?:v[34]|r1)|glm-[45]|qwen(?:\d|[-_])|kimi-k[23]|minimax-m[23])', leaf))
        # Only documented multimodal families; a generic GLM/DeepSeek/Qwen name
        # is insufficient to advertise image support.
        return bool(
            re.match(r'deepseek-v4-flash-vision-exp(?:-|$)', leaf)
            or re.match(r'glm-(?:5\.3-flash|5v|4(?:\.\d+)?v)(?:-|$)', leaf)
            or re.match(r'qwen(?:2(?:\.5)?|3)[-_]vl(?:-|$)', leaf)
            or re.match(r'qwen3\.(?:[568](?:-|$)|7-(?:plus|flash)(?:-|$))', leaf)
            or re.match(r'kimi-k(?:2\.[567]|3)(?:-|$)', leaf)
        )
    return False


def filter_models(category, provider, records):
    """Return unique, ordered candidate IDs from strings or provider records.

    Explicit contradictory capability metadata wins over a static suggestion.
    Ollama requires its /api/show result; installed names are not capabilities.
    Other unknown LLM deployment names are retained for response testing; an
    absence of metadata does not establish that a custom deployment is invalid.
    """
    if category not in ('llm', 'vision') or not isinstance(records, (list, tuple)):
        return []
    provider = str(provider or '').lower()
    known = set(get_models_for_provider(category, provider))
    result, seen = [], set()
    for record in records:
        data = record if isinstance(record, dict) else {}
        name = (data.get('id') or data.get('model') or data.get('name')) if data else record
        if not isinstance(name, str) or not name or len(name) > 256 or re.search(r'\s|[\x00-\x1f]', name) or '://' in name:
            continue
        if provider == 'gemini' and name.startswith('models/'):
            name = name[len('models/'):]
        if name in seen or _NON_CHAT.search(name) or _known_incompatible(name, provider):
            continue
        evidence = _metadata(data, category, provider)
        if evidence is False:
            continue
        if evidence is not True:
            if provider == 'ollama':
                continue
            if category == 'vision' and name not in known and not _family_supports(name, category, provider):
                continue
        seen.add(name)
        result.append(name)
    return result
