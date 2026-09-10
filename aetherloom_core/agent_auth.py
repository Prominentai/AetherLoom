"""Bounded OAuth flows and independent local subscription sessions.

Protocol attribution and license are recorded in THIRD_PARTY_NOTICES.txt.
Credentials never enter model settings. On Windows the store uses user DPAPI.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import math
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlsplit, parse_qs

from api_calls.provider_client import ProviderAPIError, validated_json
from .agent_catalog import AGENTS, account_slot
from .paths import current_dir

STORE_PATH = Path(current_dir) / 'agent_accounts' / 'sessions.json'
_lock = threading.RLock()
_refresh_locks = {}
AUTH = {
    'codex': ('app_EMoamEEZ73f0CkXaXp7hrann', 'https://auth.openai.com/oauth/authorize',
              'https://auth.openai.com/oauth/token',
              'openid profile email offline_access api.connectors.read api.connectors.invoke'),
    'claude': ('9d1c250a-e61b-44d9-88ed-5944d1962f5e', 'https://claude.ai/oauth/authorize',
               'https://claude.ai/v1/oauth/token',
               'org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload'),
    'grok': ('b1a00492-073a-47ea-816f-4c329264a828', '', '',
             'openid profile email offline_access grok-cli:access api:access'),
}
COPILOT_CLIENT = 'Iv1.b507a08c87ecfe98'


def _error(message, status='auth_error'):
    return ProviderAPIError(message, status=status)


def _protect(data, decrypt=False):
    if os.name != 'nt':
        return data
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    api = ctypes.windll.crypt32
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise _error('无法读取本机 Agent 授权，请重新登录。')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(result.data)


def _read():
    try:
        with STORE_PATH.open('rb') as f:
            raw = f.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get('version') != 1:
            raise ValueError
        if envelope.get('encoding') == 'dpapi':
            if os.name != 'nt':
                raise ValueError
            data = json.loads(_protect(base64.b64decode(envelope['data'], validate=True), True))
        else:
            if os.name == 'nt' or envelope.get('encoding') != 'json':
                raise ValueError
            data = envelope['data']
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValueError
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, KeyError, TypeError, OSError):
        raise _error('Agent 授权文件无法读取，请检查 agent_accounts 中的授权文件及访问权限。') from None


def _write(data):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = (dict(version=1, encoding='dpapi', data=base64.b64encode(
        _protect(json.dumps(data).encode('utf-8'))).decode('ascii')) if os.name == 'nt'
        else dict(version=1, encoding='json', data=data))
    fd, tmp = tempfile.mkstemp(prefix='.sessions-', dir=STORE_PATH.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, STORE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def saved_session(provider, reference):
    slot = account_slot(reference, provider)
    with _lock:
        entry = _read().get(slot, {})
        return copy.deepcopy(entry.get('session')) if entry.get('provider') == provider else None


def validate_session(provider, session):
    if not isinstance(session, dict) or not all(isinstance(session.get(k), str) and
            0 < len(session[k]) < 100000 for k in ('accessToken', 'refreshToken')):
        raise _error('所选授权缺少有效的访问令牌或刷新令牌。')
    if not isinstance(session.get('expiresAt'), (int, float)) or not 0 < session['expiresAt'] < 1e16:
        raise _error('所选授权的有效期格式不正确。')
    if provider == 'agent_codex' and not session.get('accountId'):
        raise _error('Codex 授权缺少 ChatGPT 账户标识。')
    if any('\r' in session[k] or '\n' in session[k] for k in ('accessToken', 'refreshToken')):
        raise _error('授权格式无效。')
    if provider == 'agent_codex' and (not isinstance(session['accountId'], str) or
            len(session['accountId']) > 256 or any(c in session['accountId'] for c in '\r\n')):
        raise _error('Codex 账户标识无效。')
    if provider == 'agent_grok':
        _xai_endpoint(session.get('tokenEndpoint', ''))
    return copy.deepcopy(session)


def save_session(provider, reference, session, expected=None):
    slot = account_slot(reference, provider)
    session = validate_session(provider, session)
    with _lock:
        data = _read()
        if expected is not None and data.get(slot, {}).get('session') != expected:
            raise _error('Agent 账户已更改，请重新发起请求。')
        data[slot] = dict(provider=provider, session=session)
        _write(data)


def logout(provider, reference):
    slot = account_slot(reference, provider)
    with _lock:
        data = _read(); data.pop(slot, None); _write(data)


def _xai_endpoint(value):
    try:
        parts = urlsplit(str(value))
        if parts.scheme != 'https' or parts.hostname != 'auth.x.ai' or parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError
    except ValueError:
        raise _error('xAI 授权地址校验失败。') from None
    return value


def _claims(token):
    try:
        part = token.split('.')[1]
        data = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def _session(route, wire, old=None, token_url=''):
    old = old or {}
    claims = _claims(wire.get('id_token', wire.get('access_token', '')))
    auth = claims.get('https://api.openai.com/auth', {})
    auth = auth if isinstance(auth, dict) else {}
    result = dict(old, accessToken=wire.get('access_token'),
                  refreshToken=wire.get('refresh_token') or old.get('refreshToken'),
                  expiresAt=time.time() * 1000 + float(wire.get('expires_in', 3600)) * 1000)
    if route == 'codex':
        result['accountId'] = auth.get('chatgpt_account_id') or old.get('accountId')
    if route == 'grok':
        result['tokenEndpoint'] = token_url or old.get('tokenEndpoint')
    result['scopes'] = wire.get('scope') or old.get('scopes') or AUTH[route][3]
    result['account'] = claims.get('email') or old.get('account') or ''
    return validate_session('agent_' + route, result)


def copilot_headers():
    return {'user-agent': 'GitHubCopilotChat/0.35.0', 'editor-version': 'vscode/1.107.0',
            'editor-plugin-version': 'copilot-chat/0.35.0', 'copilot-integration-id': 'vscode-chat',
            'openai-intent': 'conversation-edits', 'x-github-api-version': '2026-06-01'}


def _copilot_exchange(token, timeout=30):
    data = validated_json('get', 'https://api.github.com/copilot_internal/v2/token',
                          headers=dict(copilot_headers(), Authorization='Bearer ' + token), allow_redirects=False, timeout=timeout)
    return validate_session('agent_copilot', dict(accessToken=data.get('token'), refreshToken=token,
        expiresAt=float(data.get('expires_at') or time.time() + 1500) * 1000))


def session_for(provider, reference, rejected_token=None, *, timeout=30):
    if not isinstance(timeout,(float,int)) or not math.isfinite(timeout) or timeout<=0:
        raise _error('请求超时必须是正数。','invalid_config')
    deadline=time.monotonic()+timeout
    slot = account_slot(reference, provider)
    with _lock:
        mutex = _refresh_locks.setdefault(slot, threading.Lock())
    if not mutex.acquire(timeout=timeout):
        raise _error('等待 Agent 授权刷新超时，请稍后重试。','timeout')
    try:
        session = saved_session(provider, reference)
        if not session:
            raise _error('请先在模型设置中登录此 Agent 账户。')
        validate_session(provider, session)
        if session['expiresAt'] > time.time() * 1000 + 60000 and session['accessToken'] != rejected_token:
            return session
        route = AGENTS[provider]['route']
        remaining=deadline-time.monotonic()
        if remaining<=0:raise _error('Agent 授权刷新超时，请稍后重试。','timeout')
        if route == 'copilot':
            result = _copilot_exchange(session['refreshToken'],timeout=remaining)
        else:
            client, _, token_url, _ = AUTH[route]
            if route == 'grok':
                token_url = _xai_endpoint(session['tokenEndpoint'])
            body = dict(client_id=client, grant_type='refresh_token', refresh_token=session['refreshToken'])
            if route == 'claude':
                body['scope'] = session.get('scopes', AUTH[route][3])
            result = _session(route, validated_json('post', token_url, allow_redirects=False, timeout=remaining,
                **{'data' if route == 'grok' else 'json': body}), session)
        save_session(provider, reference, result, expected=session)
        return result
    finally:
        mutex.release()


def login(provider, cancel, announce):
    """Run on a worker. announce(url, code) crosses to the GUI thread."""
    if provider == 'agent_copilot':
        return _device_login(cancel, announce)
    route = AGENTS[provider]['route']
    client, authorize, token_url, scope = AUTH[route]
    if route == 'grok':
        config = validated_json('get', 'https://auth.x.ai/.well-known/openid-configuration')
        authorize, token_url = (_xai_endpoint(config.get(k, '')) for k in ('authorization_endpoint', 'token_endpoint'))
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    callback = '/auth/callback' if route == 'codex' else '/callback'
    received = {}
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()
        def log_message(self, *_): pass
        def do_GET(self):
            parts = urlsplit(self.path); params = parse_qs(parts.query)
            valid = parts.path == callback and secrets.compare_digest(params.get('state', [''])[0], state)
            if valid and ('code' in params or 'error' in params):
                received.update(params)
            self.send_response(200 if valid else 400); self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers(); self.wfile.write(('授权已返回，请回到 AetherLoom。' if valid else 'Invalid callback.').encode())
    server = None
    for port in ([1455, 1457] if route == 'codex' else [56121] if route == 'grok' else [0]):
        try:
            server = HTTPServer(('127.0.0.1', port), Handler); break
        except OSError:
            continue
    if server is None:
        raise _error('本机授权回调端口被占用，请结束其他登录流程后重试。')
    server.timeout = .25
    redirect = f'http://{"127.0.0.1" if route == "grok" else "localhost"}:{server.server_port}{callback}'
    params = dict(response_type='code', client_id=client, redirect_uri=redirect, scope=scope,
                  state=state, code_challenge=challenge, code_challenge_method='S256')
    if route == 'codex':
        params.update(id_token_add_organizations='true', codex_cli_simplified_flow='true', originator='codex_cli_rs')
    elif route == 'grok':
        params.update(nonce=secrets.token_urlsafe(32), plan='generic')
    else:
        params['code'] = 'true'
    try:
        announce(authorize + '?' + urlencode(params), '')
        deadline = time.monotonic() + 300
        while not received and not cancel.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if cancel.is_set():
            raise _error('已取消登录。', 'cancelled')
        if not received or 'error' in received:
            raise _error('登录未完成或授权已过期，请重试。')
        body = dict(grant_type='authorization_code', client_id=client, code=received['code'][0],
                    redirect_uri=redirect, code_verifier=verifier)
        if route == 'claude': body['state'] = state
        if route == 'grok': body.update(code_challenge=challenge, code_challenge_method='S256')
        wire = validated_json('post', token_url, allow_redirects=False, **{'json' if route == 'claude' else 'data': body})
        return _session(route, wire, token_url=token_url)
    finally:
        server.server_close()


def _device_login(cancel, announce):
    import requests
    data = validated_json('post', 'https://github.com/login/device/code',
        headers={'Accept': 'application/json'}, data={'client_id': COPILOT_CLIENT, 'scope': 'read:user'})
    if not all(data.get(k) for k in ('device_code', 'user_code')):
        raise _error('GitHub 未返回设备码。')
    announce('https://github.com/login/device', data['user_code'])
    deadline = time.monotonic() + min(float(data.get('expires_in', 900)), 900)
    interval = max(5, float(data.get('interval', 5)))
    while time.monotonic() < deadline and not cancel.wait(interval):
        try:
            with requests.post('https://github.com/login/oauth/access_token', timeout=30,
                headers={'Accept': 'application/json'}, data=dict(client_id=COPILOT_CLIENT,
                    device_code=data['device_code'], grant_type='urn:ietf:params:oauth:grant-type:device_code'), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise _error('GitHub 授权查询失败，请重新登录。')
                wire = response.json()
        except (requests.RequestException, ValueError):
            raise _error('GitHub 授权查询失败，请检查网络后重试。') from None
        if wire.get('access_token'):
            return _copilot_exchange(wire['access_token'])
        if wire.get('error') == 'slow_down': interval += 5
        elif wire.get('error') != 'authorization_pending':
            raise _error('GitHub 授权被拒绝或已过期。')
    raise _error('已取消登录。' if cancel.is_set() else 'GitHub 授权已过期。', 'cancelled')
