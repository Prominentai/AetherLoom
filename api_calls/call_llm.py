"""Text model caller; existing positional arguments remain supported."""

from .provider_client import complete_text


def call_llm(
    api_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
    temperature: float = 0.6,
    timeout: int = 30,
    provider=None,
    max_tokens=None,
) -> str:
    return complete_text(api_url, api_key, model, system_prompt, user_text,
                         temperature=temperature, timeout=timeout, provider=provider,
                         max_tokens=max_tokens)
