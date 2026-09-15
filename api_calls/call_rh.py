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


class RunningHubQueryError(RuntimeError):
    """Both read-only query routes failed; retain the legacy business code."""

    def __init__(self, primary, legacy, api_key):
        self.code = getattr(legacy, 'code', None)
        super().__init__('V2 查询失败：' + query_error_detail(primary, api_key)
                         + '；旧版查询也失败：' + query_error_detail(legacy, api_key))


def query_error_detail(error, api_key=None):
    """UI-safe diagnostics, without response bodies or signed download URLs."""
    if isinstance(error, requests.HTTPError):
        status = getattr(error.response, 'status_code', None)
        text = 'HTTP ' + str(status if status is not None else '错误')
    elif isinstance(error, requests.Timeout):
        text = '请求超时（Timeout）'
    elif isinstance(error, requests.RequestException):
        text = str(error) if str(error).endswith(('ConnectionError', 'SSLError', 'ProxyError')) else type(error).__name__
    elif isinstance(error, (RunningHubAPIError, RunningHubResponseError, RunningHubQueryError)):
        text = str(error)
    else:
        text = type(error).__name__
    text = _safe_message(text, api_key)
    text = re.sub(r'https?://[^\s<>]+', '[URL]', text)
    text = re.sub(r'(?i)bearer\s+[^\s,;]+', 'Bearer [redacted]', text)
    return ' '.join(text.split())[:450]


def _query_unavailable(error):
    if isinstance(error, requests.HTTPError):
        code = getattr(error.response, 'status_code', None)
    elif isinstance(error, RunningHubAPIError):
        code = error.code
    elif isinstance(error, requests.RequestException):
        return True
    elif isinstance(error, RunningHubResponseError):
        return 'invalid JSON' in str(error) or 'missing code' in str(error)
    else:
        return False
    try:
        code = int(code)
    except (TypeError, ValueError):
        return False
    return code in {404, 405, 408, 410, 429} or 500 <= code <= 599


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
    if not api_key:
        raise ValueError('Upload file requires an API key')
    url = site_base_url(base_url) + '/openapi/v2/media/upload/binary'
    headers = {'Authorization': 'Bearer ' + api_key}
    mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
    with open(file_path, 'rb') as source:
        files = {'file': (os.path.basename(file_path), source, mime_type)}
        result = _request_json('POST', url, 'Upload file', headers=headers, files=files, timeout=timeout)
    # Official CN/EN upload examples differ: code 0/200 and fileName/filename.
    # Normalize only this endpoint; task submission still requires code 0.
    if str(result.get('code')) not in {'0', '200'}:
        validate_response(result, 'Upload file', api_key=api_key)
    data = result.get('data')
    token = (data.get('fileName') or data.get('filename')) if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise RunningHubResponseError('Upload file returned an invalid response: missing data.fileName')
    return dict(result, code=0, data=dict(data, fileName=token))


def list_public_models(api_key: str, resource_type: str, *, resource_name: str = '',
                       base_models=None, tags=None, current: int = 1, size: int = 20,
                       base_url: str = DEFAULT_BASE_URL, timeout: int = 15) -> Dict[str, Any]:
    """One bounded page of public resources, including the documented node token.

    nodeModelName is the first/default version's ComfyUI input value. Resource
    IDs and versionResourceName storage paths are not interchangeable with it.
    """
    resource_type = str(resource_type).strip().upper()
    if not api_key:
        raise ValueError('Public model query requires an API key')
    if resource_type not in {'CHECKPOINT', 'LORA', 'UNET', 'GGUF'}:
        raise ValueError('Unsupported public model resource type')
    if type(current) is not int or current < 1 or type(size) is not int or not 1 <= size <= 50:
        raise ValueError('Invalid public model pagination')
    payload = dict(resourceType=resource_type, resourceName=str(resource_name).strip(),
                   current=current, size=size)
    if base_models:
        if not isinstance(base_models, (list, tuple)) or not all(isinstance(v, str) for v in base_models):
            raise ValueError('base_models must be a list of names')
        payload['baseModels'] = [v.strip() for v in base_models if v.strip()]
    if tags:
        if not isinstance(tags, (list, tuple)) or not all(type(v) is int and v >= 0 for v in tags):
            raise ValueError('tags must contain numeric tag IDs')
        payload['tags'] = list(dict.fromkeys(tags))
    result = _request_json('POST', site_base_url(base_url) + '/openapi/v2/resource/list',
        'List public models', headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
        json=payload, timeout=timeout)
    validate_response(result, 'List public models', api_key=api_key)
    data = result.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('records'), list):
        raise RunningHubResponseError('Public model query returned invalid page data')
    if len(data['records']) > size or not all(isinstance(row, dict) for row in data['records']):
        raise RunningHubResponseError('Public model query returned invalid records')
    try:
        total = max(0, int(data.get('total', len(data['records']))))
        page = int(data.get('current', current))
    except (TypeError, ValueError, OverflowError):
        raise RunningHubResponseError('Public model query returned invalid pagination') from None
    if page != current:
        raise RunningHubResponseError('Public model query returned a different page')
    return dict(records=data['records'], current=current, total=total,
                hasNext=data.get('hasNext') is True or current * size < total)


def get_account_status(api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                       timeout: int = 15) -> Dict[str, Any]:
    """Read the account belonging to this exact site/key, without task changes."""
    if not api_key:
        raise ValueError('Account query requires an API key')
    result = _request_json('POST', site_base_url(base_url) + '/uc/openapi/accountStatus',
        'Query account', headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
        json={'apikey': api_key}, timeout=timeout)
    validate_response(result, 'Query account', api_key=api_key)
    data = result.get('data')
    if not isinstance(data, dict):
        raise RunningHubResponseError('Query account returned invalid account data')
    return {name: data.get(name) for name in
            ('remainCoins', 'remainMoney', 'currency', 'currentTaskCounts', 'apiType')}


def query_task(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
               timeout: int = 15) -> Dict[str, Any]:
    """Query V2 once; task failure is data, request failure is an exception."""
    operation = 'Query task V2'
    result = _request_json('POST', site_base_url(base_url) + '/openapi/v2/query', operation,
                           headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
                           json={'taskId': str(task_id)}, timeout=timeout)
    if 'code' in result:
        validate_response(result, operation, api_key=api_key)
    status = str(result.get('status') or '').strip().upper()
    if status == 'CANCELLED':
        status = 'CANCELED'
    code = result.get('errorCode')
    request_errors = {'401', '403', '429', '802', '806', '811', '1002', '1003', '1004', '1014'} | {str(c) for c in range(500,600)}
    if str(code or '') not in {'', '0'} and (status not in {'FAILED', 'CANCELED'} or str(code) in request_errors):
        raise RunningHubAPIError(operation, str(code), _safe_message(result.get('errorMessage') or 'Query rejected', api_key))
    if status not in {'QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELED'}:
        raise RunningHubResponseError('Query task V2 returned an unknown task status')
    identity = result.get('taskId')
    if identity is not None and str(identity) != str(task_id):
        raise RunningHubResponseError('Query task V2 returned a mismatched taskId')
    return dict(taskId=str(task_id), status=status, results=result.get('results'),
                errorCode=_safe_message(code or '', api_key),
                errorMessage=_safe_message(result.get('errorMessage') or '', api_key),
                netWssUrl=progress_connection_url(result))


def progress_connection_url(result) -> Optional[str]:
    """Use only a supplied secure socket URL; never synthesize one from clientId."""
    data = result.get('data') if isinstance(result, dict) else None
    url = (result.get('netWssUrl') or (data.get('netWssUrl') if isinstance(data, dict) else None)) if isinstance(result, dict) else None
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'wss' or not parts.hostname or parts.username or parts.password
                or parts.fragment or len(url) > 16384):
            return None
    except ValueError:
        return None  # Optional progress must never invalidate a task response.
    return url


def _output_records(result):
    values = result.get('results')
    if not isinstance(values, list):
        raise RunningHubResponseError('Query task V2 returned no output list')
    records = []
    for value in values:
        if not isinstance(value, dict):
            raise RunningHubResponseError('Query task V2 returned an invalid output record')
        record = {key: value[key] for key in ('nodeId', 'fileSize', 'sha256', 'text') if key in value}
        record.update(fileUrl=value.get('url'), fileType=value.get('outputType'))
        records.append(record)
    return records


def get_status(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
               timeout: int = 15) -> Dict[str, Any]:
    try:
        result = query_task(api_key, task_id, base_url=base_url, timeout=timeout)
        return dict(code=0, data=result['status'], query=result)
    except Exception as error:
        if not _query_unavailable(error):
            raise
        return _legacy_query('status', api_key, task_id, base_url, timeout, error)


def get_outputs(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
                timeout: int = 30) -> Dict[str, Any]:
    try:
        result = query_task(api_key, task_id, base_url=base_url, timeout=timeout)
        return outputs_from_query(result)
    except Exception as error:
        if not _query_unavailable(error):
            raise
        return _legacy_query('outputs', api_key, task_id, base_url, timeout, error)


def _legacy_query(endpoint, api_key, task_id, base_url, timeout, primary_error):
    """One fallback on the original origin/key; never resubmit generation."""
    operation = 'Query task legacy ' + endpoint
    origin = site_base_url(base_url)
    try:
        result = _request_json('POST', origin + '/task/openapi/' + endpoint, operation,
            headers={'Host': urlsplit(origin).netloc, 'Authorization': 'Bearer ' + api_key,
                     'Content-Type': 'application/json'},
            json={'apiKey': api_key, 'taskId': str(task_id)}, timeout=timeout)
        validate_response(result, operation, api_key=api_key)
        identity = accepted_task_id(result)
        if identity is not None and identity != str(task_id):
            raise RunningHubResponseError(operation + ' returned a mismatched taskId')
        if endpoint == 'status':
            value = result.get('data')
            status = value.strip().upper() if isinstance(value, str) else ''
            if status == 'CANCELLED':status = 'CANCELED'
            if status not in {'QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELED'}:
                raise RunningHubResponseError(operation + ' returned an unknown task status')
            result = dict(code=0, data=status,
                          query=dict(taskId=str(task_id), status=status, netWssUrl=progress_connection_url(result)))
        elif not isinstance(result.get('data'), list) or not all(isinstance(v, dict) for v in result['data']):
            raise RunningHubResponseError(operation + ' returned an invalid output list')
        result['query_warning'] = 'V2 查询不可用（' + query_error_detail(primary_error, api_key) + '），已使用同站点旧版接口'
        return result
    except Exception as legacy_error:
        raise RunningHubQueryError(primary_error, legacy_error, api_key) from None


def outputs_from_query(result):
    if result.get('status') != 'SUCCESS':
        raise RunningHubResponseError('Task output is not ready')
    return dict(code=0, data=_output_records(result))


def get_progress_connection(api_key: str, task_id: str, *, base_url: str = DEFAULT_BASE_URL,
                            timeout: int = 8) -> Optional[str]:
    # Compatibility entry point for callers outside the shared lifecycle.
    return query_task(api_key, task_id, base_url=base_url, timeout=timeout).get('netWssUrl')


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
