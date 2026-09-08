"""RunningHub task helpers with explicit response errors and safe diagnostics."""
from __future__ import annotations

import json
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

import requests

DEFAULT_BASE_URL = "https://www.runninghub.cn"
BUSY_SUBMISSION_CODES = frozenset({415, 421})
# These official errors reject creation before a task can be accepted. Existing
# task states (804/805/813), server faults and unknown codes are deliberately not
# included: switching credentials after those could duplicate paid generation.
REJECTED_SUBMISSION_CODES = frozenset({301, 380, 412, 416, 433, 435, 436,
    801, 802, 803, 806, 808, 809, 810, 811, 812, 901, 1001, 1002, 1007, 1008, 1009})


def accepted_task_id(payload):
    """A returned identity outranks even an inconsistent error/status code."""
    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    values = [data.get('taskId') if isinstance(data, dict) else None, payload.get('taskId')]
    for value in values:
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return None


def submission_response_kind(payload):
    """Classify only documented creation outcomes; absence of an ID is not failure."""
    if accepted_task_id(payload):
        return 'accepted'
    try:
        code = int(payload.get('code'))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 'unknown'
    if code in BUSY_SUBMISSION_CODES:
        return 'busy'
    if code in REJECTED_SUBMISSION_CODES:
        return 'rejected'
    return 'unknown'


class RunningHubResponseError(RuntimeError):
    """The HTTP response is not a valid RunningHub response envelope."""


class RunningHubAPIError(RuntimeError):
    """A valid RunningHub response reported a business failure."""

    def __init__(self, operation: str, code: Any, message: str, payload=None):
        self.operation = operation
        self.code = code
        self.message = message
        self.payload = payload
        super().__init__(f"{operation} failed (code={code}): {message}")


def normalize_base_url(base_url: str) -> str:
    """Normalize a host or HTTP(S) base URL, retaining an optional API path."""
    value = (base_url or '').strip()
    if not value:
        raise ValueError("RunningHub base_url must be provided")
    if '://' not in value:
        value = 'https://' + value
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in {'http', 'https'} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or any(ch.isspace() for ch in value)):
            raise ValueError
        parts.port  # Validate malformed or out-of-range port values.
    except ValueError:
        raise ValueError("RunningHub base_url must be an HTTP(S) host or API root without credentials, query, or fragment") from None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


def site_base_url(base_url: str) -> str:
    """Return the website origin of a normalized RunningHub base URL."""
    parts = urlsplit(normalize_base_url(base_url))
    return urlunsplit((parts.scheme, parts.netloc, '', '', ''))


def _api_root(base_url: str) -> str:
    base_url = normalize_base_url(base_url)
    if base_url.endswith('/task/openapi'):
        return base_url
    if '/task/openapi/' in urlsplit(base_url).path:
        raise ValueError("RunningHub base_url must end at the API root, not a task endpoint")
    return base_url + '/task/openapi'


def _safe_message(message: Any, api_key: Optional[str]) -> str:
    text = str(message)
    if api_key:
        for secret in {str(api_key), quote(str(api_key), safe=''), quote_plus(str(api_key))}:
            text = text.replace(secret, '[redacted]')
    return re.sub(r'(?i)(api[_-]?key\s*[=:]\s*)([^&\s,;]+)', r'\1[redacted]', text)


def validate_response(payload: Any, operation: str = 'RunningHub request', *,
                      api_key: Optional[str] = None, require_code: bool = True) -> Dict[str, Any]:
    """Validate the response envelope without changing its data shape.

    Website list/detail endpoints may omit code; pass require_code=False there.
    Task APIs require code=0. Numeric strings are accepted for compatibility.
    """
    if not isinstance(payload, dict):
        raise RunningHubResponseError(f"{operation} returned invalid JSON: expected an object")
    if 'code' not in payload:
        if require_code:
            raise RunningHubResponseError(f"{operation} returned an invalid response: missing code")
        return payload
    code = payload['code']
    if str(code) != '0':
        message = _safe_message(payload.get('msg') or payload.get('message') or 'Unknown API error', api_key)
        safe_code = _safe_message(code, api_key)
        try:
            normalized_code = int(safe_code)
        except (TypeError, ValueError):
            normalized_code = safe_code
        raise RunningHubAPIError(operation, normalized_code, message, payload=payload)
    return payload


def _request_json(method: str, url: str, operation: str, *, preserve_task_id=False, **kwargs) -> Dict[str, Any]:
    """Do not copy request URLs, credentials, or response bodies into exceptions."""
    try:
        request = requests.get if method == 'GET' else requests.post
        response = request(url, **kwargs)
        if preserve_task_id:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and (accepted_task_id(payload) or 'code' in payload):
                return payload
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = getattr(exc.response, 'status_code', None)
        # Preserve the response for diagnosis; HTTP codes are not RH business codes.
        raise requests.HTTPError(f"{operation} failed: HTTP {status if status is not None else 'error'}", response=exc.response) from None
    except requests.Timeout:
        raise requests.Timeout(f"{operation} timed out") from None
    except requests.RequestException as exc:
        raise requests.RequestException(f"{operation} failed: {type(exc).__name__}") from None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise RunningHubResponseError(f"{operation} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise RunningHubResponseError(f"{operation} returned invalid JSON: expected an object")
    return payload


def _post_task(endpoint: str, api_key: str, payload: Dict[str, Any], *,
               base_url: str, timeout: int, operation: str, validate: bool = True) -> Dict[str, Any]:
    url = f"{_api_root(base_url)}/{endpoint}"
    headers = {'Host': urlsplit(url).netloc, 'Content-Type': 'application/json'}
    result = _request_json('POST', url, operation, preserve_task_id=endpoint == 'ai-app/run',
                           headers=headers, json=payload, timeout=timeout)
    return validate_response(result, operation, api_key=api_key) if validate else result


def run_task(webapp_id: int, api_key: str, node_info_list: List[Dict[str, Any]], *,
             base_url: str = DEFAULT_BASE_URL, timeout: int = 30) -> Dict[str, Any]:
    """Submit a task; leave business codes (including 415/421) to caller retries."""
    return _post_task('ai-app/run', api_key,
                      {'webappId': webapp_id, 'apiKey': api_key, 'nodeInfoList': node_info_list},
                      base_url=base_url, timeout=timeout, operation='Submit task', validate=False)


def upload_file(file_path: str, api_key: Optional[str] = None, *,
                base_url: str = DEFAULT_BASE_URL, timeout: int = 60) -> Dict[str, Any]:
    """Upload a local resource; return only a successful response with a token."""
    url = f"{_api_root(base_url)}/upload"
    headers = {'Host': urlsplit(url).netloc}
    payload = {'fileType': 'input'}
    if api_key:
        payload['apiKey'] = api_key
    mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
    with open(file_path, 'rb') as source:
        files = {'file': (os.path.basename(file_path), source, mime_type)}
        result = _request_json('POST', url, 'Upload file', headers=headers, data=payload, files=files, timeout=timeout)
    validate_response(result, 'Upload file', api_key=api_key)
    data = result.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('fileName'), str) or not data['fileName'].strip():
        raise RunningHubResponseError('Upload file returned an invalid response: missing data.fileName')
    return result


def get_status(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
               timeout: int = 15) -> Dict[str, Any]:
    return _post_task('status', api_key, {'apiKey': api_key, 'taskId': task_id},
                      base_url=base_url, timeout=timeout, operation='Query task status')


def get_outputs(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
                timeout: int = 30) -> Dict[str, Any]:
    return _post_task('outputs', api_key, {'apiKey': api_key, 'taskId': task_id},
                      base_url=base_url, timeout=timeout, operation='Get task outputs')


def get_progress_connection(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
                            timeout: int = 8) -> Optional[str]:
    """Official outputs API returns code=804 + data.netWssUrl while running.

    code=813 (queued) and code=0 (finished) are normal races with status polling.
    Never expose the signed WebSocket URL in diagnostics or persist it to disk.
    """
    result = _post_task('outputs', api_key, {'apiKey': api_key, 'taskId': task_id},
                        base_url=base_url, timeout=timeout, operation='Query task progress', validate=False)
    if str(result.get('code')) not in {'0', '804', '813'}:
        validate_response(result, 'Query task progress', api_key=api_key)
    data = result.get('data')
    url = data.get('netWssUrl') if isinstance(data, dict) else None
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    if (parts.scheme != 'wss' or not parts.hostname or parts.username or parts.password
            or parts.fragment or len(url) > 16384):
        raise RunningHubResponseError('Query task progress returned an invalid WebSocket URL')
    return url


def get_app_progress_nodes(webapp_id: str, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                           timeout: int = 4) -> Dict[str, str]:
    """Optional full workflow map. App parameter fields are never a denominator.

    Only follow an explicit workflowId returned by the official App metadata API;
    App IDs and workflow IDs are different identities. Private Apps may omit it.
    """
    origin = site_base_url(base_url)
    result = _request_json('GET', origin + '/api/webapp/apiCallDemo', 'Get application progress metadata',
                           headers={'Host': urlsplit(origin).netloc},
                           params={'apiKey': api_key, 'webappId': webapp_id}, timeout=timeout)
    validate_response(result, 'Get application progress metadata', api_key=api_key)
    data = result.get('data')
    workflow_id = data.get('workflowId') if isinstance(data, dict) else None
    if not isinstance(workflow_id, (str, int)) or isinstance(workflow_id, bool) or not str(workflow_id).isdigit():
        return {}
    result = _request_json('POST', origin + '/api/openapi/getJsonApiFormat', 'Get workflow progress nodes',
                           headers={'Host': urlsplit(origin).netloc},
                           json={'apiKey': api_key, 'workflowId': str(workflow_id)}, timeout=timeout)
    validate_response(result, 'Get workflow progress nodes', api_key=api_key)
    data = result.get('data')
    prompt = data.get('prompt') if isinstance(data, dict) else None
    if isinstance(prompt, str) and len(prompt) <= 8 * 1024 * 1024:
        try:
            prompt = json.loads(prompt)
        except ValueError:
            return {}
    if (not isinstance(prompt, dict) or not 0 < len(prompt) <= 10000
            or not all(isinstance(value, dict) and isinstance(value.get('class_type'), str)
                       and isinstance(value.get('inputs'), dict) for value in prompt.values())):
        return {}
    return {str(key): value['class_type'][:120] for key, value in prompt.items()}


def get_nodeinfo(webapp_id: str, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: int = 15) -> bytes:
    """Return UTF-8 node-list JSON bytes, distinguishing errors from a valid []."""
    origin = site_base_url(base_url)
    result = _request_json('GET', origin + '/api/webapp/apiCallDemo', 'Get application nodes',
                           headers={'Host': urlsplit(origin).netloc},
                           params={'apiKey': api_key, 'webappId': webapp_id}, timeout=timeout)
    validate_response(result, 'Get application nodes', api_key=api_key)
    data = result.get('data')
    if not isinstance(data, dict):
        raise RunningHubResponseError('Get application nodes returned an invalid response: data must be an object')
    nodes = data.get('nodeInfoList')
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise RunningHubResponseError('Get application nodes returned an invalid response: data.nodeInfoList must be a list of objects')
    return json.dumps(nodes, indent=2, ensure_ascii=False).encode('utf-8')


def cancel_task(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
                timeout: int = 15) -> Dict[str, Any]:
    return _post_task('cancel', api_key, {'apiKey': api_key, 'taskId': task_id},
                      base_url=base_url, timeout=timeout, operation='Cancel task')


def account_status(api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                   timeout: int = 15) -> Dict[str, Any]:
    origin = site_base_url(base_url)
    result = _request_json('POST', origin + '/uc/openapi/accountStatus', 'Query account',
                           headers={'Host': urlsplit(origin).netloc, 'Content-Type': 'application/json'},
                           json={'apikey': api_key}, timeout=timeout)
    return validate_response(result, 'Query account', api_key=api_key)
