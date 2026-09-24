"""Independent LLM text-preset drafts; only actual prompt fields reach requests."""

import copy

from aetherloom_core.resources import DEFAULT_EXPAND_SYSTEM_PROMPT, DEFAULT_POLISH_SYSTEM_PROMPT
from aetherloom_core.translation import DEFAULT_PROMPT as DEFAULT_TRANSLATION_PROMPT


PRESETS = (
    ('custom', '自定义'),
    ('translate_zh', '翻译成中文'),
    ('translate_en', '翻译成英文'),
    ('polish', '润色'),
    ('expand', '扩写'),
)
MODES = frozenset(mode for mode, _ in PRESETS)
METADATA_FIELDS = ('text_preset', 'text_presets', 'text_drafts')
MAX_PRESET_CHARS = 1_000_000


def operation(params):
    """Legacy LLM nodes are custom; malformed explicit modes are not guessed."""
    if not isinstance(params, dict):
        raise ValueError('文本处理参数必须是字典')
    mode = params.get('text_preset', 'custom')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('不支持的文本处理模式')
    return mode


def validate(params):
    """Accept legacy dictionaries and bound each new preset metadata field."""
    operation(params)
    for key in ('prompt', 'system_prompt'):
        if key in params and not isinstance(params[key], str):
            raise ValueError('文本处理提示词必须是文本：' + key)
    for key in ('text_presets', 'text_drafts'):
        if key not in params:
            continue
        values = params[key]
        if not isinstance(values, dict) or len(values) > len(PRESETS):
            raise ValueError('文本处理预设或草稿必须是模式到文本的字典：' + key)
        for mode, value in values.items():
            if not isinstance(mode, str) or mode not in MODES:
                raise ValueError('文本处理预设或草稿包含未知模式：' + key)
            if not isinstance(value, str) or len(value) > MAX_PRESET_CHARS:
                raise ValueError('文本处理预设或草稿必须是不超过 100 万字符的文本：' + key)


def _configured(value, fallback):
    return value if isinstance(value, str) and value.strip() else fallback


def _baselines(settings):
    settings = settings if isinstance(settings, dict) else {}
    nested = settings.get('api_settings')
    nested = nested if isinstance(nested, dict) else {}
    translation = None
    for source in (settings, nested):
        translator = source.get('translator')
        if isinstance(translator, dict):
            translation = _configured(translator.get('translation_prompt'), None)
            if translation is not None:
                break
    translation = translation if translation is not None else DEFAULT_TRANSLATION_PROMPT
    return {
        'custom': '',
        'translate_zh': translation.replace('{target_lang}', '中文'),
        'translate_en': translation.replace('{target_lang}', '英文'),
        'polish': _configured(settings.get('polish_system_prompt'), DEFAULT_POLISH_SYSTEM_PROMPT),
        'expand': _configured(settings.get('expand_system_prompt'), DEFAULT_EXPAND_SYSTEM_PROMPT),
    }


def initialize(params, settings=None):
    """Copy defaults once without changing an existing node's actual prompt.

    Saved baselines and drafts belong to the node. Later application-setting
    changes only fill missing modes; they never replace an existing baseline.
    """
    validate(params)
    result = copy.deepcopy(params)
    mode = operation(result)
    baselines = _baselines(settings)
    baselines.update(result.get('text_presets', {}))
    baselines['custom'] = ''
    drafts = result.get('text_drafts', {})
    result.update(text_preset=mode, text_presets=baselines, text_drafts=drafts)
    result.setdefault('prompt', '')
    # Empty/whitespace system prompts are intentional edits, not missing values.
    result.setdefault('system_prompt', drafts.get(mode, baselines[mode]))
    validate(result)
    return result


def select_preset(params, mode, settings=None):
    """Save the current edit and restore the selected mode's draft or baseline."""
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('不支持的文本处理模式')
    result = initialize(params, settings)
    current = operation(result)
    result['text_drafts'][current] = result['system_prompt']
    result['text_preset'] = mode
    result['system_prompt'] = result['text_drafts'].get(mode, result['text_presets'][mode])
    validate(result)
    return result


def reset_preset(params, settings=None):
    """Reset only the current draft; custom always resets to an empty prompt."""
    result = initialize(params, settings)
    mode = operation(result)
    result['text_drafts'].pop(mode, None)
    result['system_prompt'] = result['text_presets'][mode]
    return result


def fingerprint_params(params):
    """Preset labels, baselines and inactive drafts cannot change a request hash."""
    if not isinstance(params, dict):
        raise ValueError('文本处理参数必须是字典')
    result = copy.deepcopy(params)
    for key in METADATA_FIELDS:
        result.pop(key, None)
    return result
