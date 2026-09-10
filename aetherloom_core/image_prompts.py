"""Agent-only image prompt preferences and system-prompt fallback."""

DEFAULTS = {
    'text2img': ('image_generation_system_prompt',
        '根据用户要求生成图像。准确保留主体、数量、动作、构图、风格及指定文字；'
        '未指定的细节应服务于画面表达，不擅自添加水印、标志或无关文字。'),
    'image_edit': ('image_edit_system_prompt',
        '根据用户要求编辑输入图像。只修改明确要求变更的内容，尽量保持其他区域、'
        '主体身份、构图、光照和风格一致；保留未要求修改的文字与细节。'),
}


def options(settings, category):
    key, default = DEFAULTS[category]
    settings = settings if isinstance(settings, dict) else {}
    value = settings.get(key)
    return {
        'system_prompt': value if isinstance(value, str) and value.strip() else default,
        'merge_system_prompt': settings.get(key + '_merge_user_prompt') is True,
    }


def prepare(prompt, system_prompt, *, supports_system_prompt):
    """Choose the representation before submission; never retry a paid POST.

    Agent compatibility mode composes the user prompt without system-role text.
    Merging preserves the text but does not give it system-role priority.
    """
    prompt = str(prompt or '')
    system = str(system_prompt or '').strip()
    if supports_system_prompt:
        return system, prompt
    return '', ('通用要求：\n' + system + '\n\n本次请求：\n' + prompt) if system else prompt


def request_options(settings, category, provider):
    # Caller resolves named accounts to their provider template first. Ordinary
    # image APIs must receive neither these instructions nor a merged prompt.
    if category not in DEFAULTS or provider not in ('agent_codex', 'agent_grok'):
        return {}
    return options(settings, category)
