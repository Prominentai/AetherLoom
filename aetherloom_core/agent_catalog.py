"""Subscription routes, separate from API-key providers (see THIRD_PARTY_NOTICES)."""

AGENTS = {
    'agent_codex': dict(name='Codex Agent · ChatGPT 订阅', route='codex',
                        endpoint='https://chatgpt.com/backend-api/codex/responses', image_tool=True),
    'agent_claude': dict(name='Claude Agent · Claude 订阅', route='claude',
                         endpoint='https://api.anthropic.com/v1/messages?beta=true'),
    'agent_grok': dict(name='xAI Agent · Grok 订阅', route='grok',
                       endpoint='https://api.x.ai/v1/responses', image_tool=True),
    'agent_copilot': dict(name='Copilot Agent · GitHub 订阅', route='copilot',
                          endpoint='https://api.githubcopilot.com/chat/completions'),
}


def supports(provider, category):
    return provider in AGENTS and (category in ('llm', 'vision') or
                                   category in ('text2img', 'image_edit') and AGENTS[provider].get('image_tool', False))


def llm_name(provider, model):
    """Discard legacy image-only IDs; never guess a subscription LLM ID."""
    import re
    name = str(model or '').strip()
    if provider in AGENTS and re.search(r'(?:gpt[-_]image|grok-imagine|image|video|embed|audio|tts|speech)', name, re.I):
        return ''
    return name


def credential_ref(identity):
    # An opaque local account selector, never a bearer token or user endpoint.
    return 'agent-account:' + str(identity)


def account_slot(reference, provider):
    from api_calls.provider_client import ProviderAPIError
    if provider not in AGENTS:
        raise ProviderAPIError('不支持此 Agent。', status='unsupported')
    value = str(reference or '')
    slot = value[len('agent-account:'):] if value.startswith('agent-account:') else provider
    import re
    if not re.fullmatch(r'(?:agent_(?:codex|claude|grok|copilot)|user_[a-f0-9]{32})', slot):
        raise ProviderAPIError('Agent 账户配置无效。', status='invalid_config')
    return slot
