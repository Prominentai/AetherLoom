"""Immutable translation requests shared by App text tools and API probes."""
import copy
from .api_credentials import get_credentials

DEFAULT_PROMPT = ('Translate the user text into {target_lang}. Preserve meaning, prompt tags, '
                  'weights and formatting. Return only the translated text, without explanations.')


def snapshot(settings, keys):
    config = copy.deepcopy((settings or {}).get('translator') or {})
    selected = config.get('provider') or 'free_translate'
    prompt = config.get('translation_prompt') or DEFAULT_PROMPT
    if selected == 'llm_translate':
        config = copy.deepcopy((settings or {}).get('llm') or {})
        config['translation_mode'] = 'llm'
    else:
        config['translation_mode'] = 'free' if selected == 'free_translate' else 'api'
    config.update(get_credentials(keys, config.get('provider'), 'llm' if selected == 'llm_translate' else 'translator'))
    config['translation_prompt'] = prompt
    return config


def translate(config, text, language):
    timeout = int(config.get('timeout') or 30)
    mode = config.get('translation_mode', 'api')
    if mode == 'free':
        from api_calls.translators import translate_auto
        return translate_auto(text, language, verbose=False, timeout=timeout)
    if mode == 'llm':
        from api_calls.call_llm import call_llm
        if not config.get('endpoint') or not config.get('model'):
            raise ValueError('请先在 API 管理中配置大语言模型')
        prompt = str(config.get('translation_prompt') or DEFAULT_PROMPT).replace('{target_lang}', str(language))
        return call_llm(config['endpoint'], config.get('api_key', ''), config['model'], prompt, text,
                        timeout=timeout, temperature=.2, provider=config.get('protocol') or config.get('provider'),
                        web_search=config.get('web_search'))
    from api_calls.call_translate import translate_text
    if not config.get('endpoint') or not any(config.get(k) for k in ('api_key', 'secret')):
        # Preserve the old unconfigured automatic translation behavior.
        from api_calls.translators import translate_auto
        return translate_auto(text, language, verbose=False, timeout=timeout)
    return translate_text(config['endpoint'], config.get('api_key', ''), config.get('model', ''), text, language,
                          timeout=timeout, provider=config.get('protocol') or config.get('provider'),
                          extra={k:config[k] for k in ('appid','secret') if config.get(k)})
