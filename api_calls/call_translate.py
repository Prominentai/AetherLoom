"""Translation caller for dedicated translation APIs (Google v2, Baidu).

Usage example (Google Translate v2):
    translate_text(
        api_url="https://translation.googleapis.com/language/translate/v2",
        api_key="AIza...",
        model="nmt",  # or leave blank for base model
        text="你好",
        target_lang="en",
        provider="google_v2",
    )

Usage example (Baidu):
    translate_text(
        api_url="https://api.fanyi.baidu.com/api/trans/vip/translate",
        api_key="<appid>",  # or "<appid>:<secret>"
        model="baidu-text-translate",
        text="你好",
        target_lang="en",
        provider="baidu_translate",
        extra={"secret": "<secret_key>"},  # if not packed into api_key
    )
"""
from typing import Any, Dict, Optional, Tuple

import hashlib
import random
from urllib.parse import urlsplit
from .provider_client import ProviderAPIError, validated_json


def _is_google(api_url: str, provider: Optional[str]) -> bool:
    provider_hint = (provider or "").lower()
    if provider_hint in ("google_v2", "google_translate"):
        return True
    return urlsplit(api_url).hostname == "translation.googleapis.com"


def _is_baidu(api_url: str, provider: Optional[str]) -> bool:
    provider_hint = (provider or "").lower()
    if provider_hint == "baidu_translate":
        return True
    return urlsplit(api_url).hostname in {"fanyi.baidu.com", "api.fanyi.baidu.com", "fanyi-api.baidu.com"}


def _call_google_v2(
    api_url: str,
    api_key: str,
    model: str,
    text: str,
    target_lang: str,
    source_lang: Optional[str],
    timeout: int,
    extra: Optional[Dict[str, Any]],
) -> str:
    params = {"key": api_key}
    body: Dict[str, Any] = {
        "q": text,
        "target": target_lang,
        "format": "text",
    }
    if source_lang:
        body["source"] = source_lang
    if model:
        body["model"] = model  # e.g., "nmt"
    if extra:
        body.update({key: value for key, value in extra.items()
                     if key not in {'key', 'api_key', 'appid', 'app_id', 'secret', 'app_secret', 'secret_key'}})
    data = validated_json('post', api_url, params=params, json=body, timeout=timeout)
    values = data.get('data')
    values = values.get('translations') if isinstance(values, dict) else None
    if not isinstance(values, list) or not values or any(not isinstance(item, dict) or
            not isinstance(item.get('translatedText'), str) or not item['translatedText'].strip() for item in values):
        raise ProviderAPIError('翻译 API 未返回有效译文。', status='empty_response')
    return '\n'.join(item['translatedText'].strip() for item in values)


def _parse_baidu_credentials(api_key: str, extra: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (appid, secret) resolving from api_key/extra.

    Supports formats:
    - api_key="appid:secret"
    - api_key="appid" + extra["secret"]
    - extra["appid"], extra["secret"]
    """
    appid = ""
    secret = ""
    extra = extra or {}

    # support api_key provided as dict (e.g., {'appid':..., 'secret':...})
    if isinstance(api_key, dict):
        appid = api_key.get('appid') or api_key.get('app_id') or api_key.get('api_key') or ''
        secret = api_key.get('secret') or api_key.get('app_secret') or api_key.get('secret_key') or ''
    else:
        # api_key as string: support 'appid:secret' or 'appid'
        if api_key and isinstance(api_key, str) and ":" in api_key:
            appid, secret = api_key.split(":", 1)
        else:
            appid = api_key or ""
            secret = extra.get("secret") or extra.get("app_secret") or extra.get("secret_key") or ""

    # extra can override
    appid = extra.get("appid") or extra.get("app_id") or appid
    secret = extra.get("secret") or extra.get("app_secret") or extra.get("secret_key") or secret

    if not appid or not secret:
        raise ValueError("Baidu translate requires both appid and secret (provide as 'appid:secret' in api_key, api_key={'appid':..., 'secret':...}, or via extra={'appid':..., 'secret':...}).")
    return appid, secret


def _call_baidu(
    api_url: str,
    api_key: str,
    model: str,
    text: str,
    target_lang: str,
    source_lang: Optional[str],
    timeout: int,
    extra: Optional[Dict[str, Any]],
) -> str:
    appid, secret = _parse_baidu_credentials(api_key, extra)
    salt = str(random.randint(100000, 999999))
    sign_str = f"{appid}{text}{salt}{secret}"
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
    params: Dict[str, Any] = {
        "q": text,
        "from": source_lang or "auto",
        "to": target_lang,
        "appid": appid,
        "salt": salt,
        "sign": sign,
    }
    if extra:
        # Allow passing glossary/domain or other Baidu options if needed
        params.update({k: v for k, v in extra.items() if k not in {
            "secret", "app_secret", "secret_key", "appid", "app_id", "q", "salt", "sign", "from", "to"}})
    data = validated_json('post', api_url, data=params, timeout=timeout)
    values = data.get('trans_result')
    if not isinstance(values, list) or not values or any(not isinstance(item, dict) or
            not isinstance(item.get('dst'), str) or not item['dst'].strip() for item in values):
        raise ProviderAPIError('翻译 API 未返回有效译文。', status='empty_response')
    return '\n'.join(item['dst'].strip() for item in values)

def translate_text(
    api_url: str,
    api_key: str,
    model: str,
    text: str,
    target_lang: str,
    source_lang: Optional[str] = None,
    timeout: int = 30,
    provider: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Translate text via dedicated translation APIs (Google v2, Baidu).

    provider hints:
    - "google_v2" (or URL contains translation.googleapis.com)
    - "baidu_translate" (or URL contains fanyi.baidu.com)
    extra: provider-specific fields. For Baidu, secret may be provided via extra['secret'] when api_key is only appid.
    """
    if _is_google(api_url, provider):
        return _call_google_v2(api_url, api_key, model, text, target_lang, source_lang, timeout, extra)
    if _is_baidu(api_url, provider):
        return _call_baidu(api_url, api_key, model, text, target_lang, source_lang, timeout, extra)
    raise ValueError("Unsupported translation provider: supported providers are google_v2 and baidu_translate.")
