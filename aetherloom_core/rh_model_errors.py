"""Conservative submission errors shared by RH Standard and LLM adapters.

Only an explicit refusal permits another paid submission. Unknown responses,
server faults and existing tasks must never be converted into key retries.
"""
import re

from api_calls import call_rh


# Official references: /runninghub-api-doc-cn/doc-8435517 and doc-8287338.
_REJECTED_CODES = {str(code) for code in call_rh.REJECTED_SUBMISSION_CODES} | {
    '401', '403', '1014',
    'invalid_api_key', 'authentication_error', 'authentication_failed',
    'unauthorized', 'permission_denied', 'permission_error', 'access_denied',
    'insufficient_quota', 'insufficient_balance', 'insufficient_funds',
    'apikey_unauthorized', 'corpapikey_invalid', 'corpapikey_insufficient_funds',
}
_BUSY_CODES = {str(code) for code in call_rh.BUSY_SUBMISSION_CODES} | {
    '429', '814', '1003', '1520', 'rate_limit_exceeded', 'rate_limit_error',
    'too_many_requests', 'concurrency_limit_reached', 'task_queue_maxed',
    'task_instance_maxed', 'personal_queue_count_limit',
}
_AMBIGUOUS_CODES = {
    '408', '804', '805', '813', '1000', '1004', '1005', '1006', '1010',
    '1011', '1012', '1013', '1015', '1504', '1516', '1517', '1519',
    'server_error', 'internal_server_error', 'internal_error', 'timeout',
    'request_timeout', 'service_unavailable',
}
_SUCCESS_CODES = {'', '0', '200', 'ok', 'success', 'none', 'null'}
_SUCCESS_MESSAGES = {'', 'ok', 'success', 'successful', '成功', '请求成功'}
_SERVER_FAILURE = re.compile(
    r'\b(?:internal[ _-]*(?:server[ _-]*)?error|server[ _-]*error|'
    r'service[ _-]+unavailable|bad[ _-]+gateway|gateway[ _-]+timeout|'
    r'timed?[ _-]*out|timeout|connection[ _-]+(?:error|reset))\b|'
    r'系统内部错误|服务器(?:内部)?(?:错误|异常)|服务(?:暂时|暂)?不可用|超时', re.I)
_RATE_LIMIT = re.compile(
    r'\b(?:rate[ _-]*limit(?:[ _-]*(?:exceeded|error|reached))?|'
    r'too[ _-]+many[ _-]+requests|concurren(?:cy|t)[ _-]+limit[ _-]+(?:exceeded|reached))\b|'
    r'限流|请求频率(?:超限|过高|超出)|并发(?:数|量)?(?:已)?(?:达到|超出|超过|达)(?:上限|限制)|队列已满', re.I)
_KEY_REFUSAL = re.compile(
    r'\b(?:unauthori[sz]ed|forbidden|access[ _-]+denied|permission[ _-]+denied|'
    r'insufficient[ _-]+(?:permissions?|quota|balance|funds|credits?)|'
    r'authentication[ _-]+(?:failed|error)|invalid[ _-]+(?:api[ _-]*key|credentials?|token)|'
    r'incorrect[ _-]+api[ _-]*key|(?:api[ _-]*key|token)[ _-]+(?:is[ _-]+)?'
    r'(?:invalid|expired|disabled|unauthorized)|quota[ _-]+(?:exceeded|exhausted))\b|'
    r'(?:API\s*KEY|密钥|令牌).{0,16}(?:无效|失效|过期|禁用|不支持|无权限)|'
    r'(?:无效|失效|过期)的?(?:API\s*KEY|密钥|令牌)|权限不足|无权(?:限)?(?:访问|调用)|'
    r'未授权|鉴权失败|认证失败|访问被拒绝|(?:余额|额度|配额)(?:不足|耗尽|已用完)|超出(?:配额|额度)', re.I)
_ENTERPRISE_REFUSAL = re.compile(
    r'(?:only[ _-]+(?:supports?[ _-]+)?|restricted[ _-]+to[ _-]+|requires?[ _-]+)'
    r'(?:an?[ _-]+)?enterprise[ _-]+(?:shared[ _-]+)?(?:api[ _-]*)?keys?|'
    r'enterprise[ _-]+shared[ _-]+(?:api[ _-]*)?keys?[ _-]+(?:only|required)|'
    r'(?:仅限|仅支持|只支持|只允许|需要|必须使用).{0,20}企业(?:级)?.{0,12}(?:共享|密钥|API\s*KEY)|'
    r'非企业(?:级)?(?:共享)?(?:API\s*KEY|密钥)', re.I)


def _envelopes(data):
    """Walk response wrappers, never generated choices/content or user inputs."""
    pending, seen = [data], set()
    while pending:
        value = pending.pop()
        if not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        yield value
        for name in ('response', 'data', 'error'):
            child = value.get(name)
            if isinstance(child, dict):
                pending.append(child)


def accepted_model_task_id(data):
    """An accepted identity takes precedence over inconsistent error wrappers."""
    for envelope in _envelopes(data):
        task_id = call_rh.accepted_task_id(envelope)
        if task_id:
            return task_id
    return None


def _safe_detail(value, api_key, limit=450):
    text = call_rh._safe_message(value, api_key)
    text = re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>]+', '[URL]', text)
    text = re.sub(r'(?i)\bbearer\s+[^\s,;]+', 'Bearer [redacted]', text)
    text = re.sub(r'''(?i)(["']?(?:api[_ -]?key|authorization|token)["']?\s*[=:]\s*)["']?[^\s,;"'}]+''',
                  r'\1[redacted]', text)
    return ' '.join(text.split())[:limit]


def classify_model_error(data, http_status=200, api_key=''):
    """Return None or a safe {kind, code, message} submission error.

    HTTP 200 can carry an error envelope. Authentication/entitlement refusals
    and documented rejection codes may try another key; explicit rate limits
    can queue. Server errors and all other failures remain ambiguous.
    """
    if accepted_model_task_id(data):
        return None
    try:
        status = int(http_status)
    except (TypeError, ValueError, OverflowError):
        status = 200
    codes, messages = [], []
    explicit_error = not isinstance(data, dict)
    for envelope in _envelopes(data):
        error = envelope.get('error')
        if error:
            explicit_error = True
            if isinstance(error, str):
                messages.append(error)
        for name in ('errorCode', 'error_code', 'code'):
            value = envelope.get(name)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                code = str(value).strip()
                if code.lower() not in _SUCCESS_CODES:
                    codes.append(code)
        for name in ('errorMessage', 'error_message', 'msg', 'message', 'detail'):
            value = envelope.get(name)
            if isinstance(value, str) and value.strip().lower() not in _SUCCESS_MESSAGES:
                messages.append(value.strip())
        value = envelope.get('type')
        if isinstance(value, str) and (value.lower().endswith('_error') or value.lower() in _REJECTED_CODES | _BUSY_CODES):
            codes.append(value)
        if envelope.get('success') is False or str(envelope.get('status', '')).lower() in ('failed', 'error'):
            explicit_error = True
    if not (explicit_error or codes or messages or status >= 300):
        return None

    lowered = {code.lower() for code in codes}
    detail = '；'.join(dict.fromkeys(messages))
    semantics = ' '.join(codes + messages)
    if (status >= 500 or status == 408 or lowered & _AMBIGUOUS_CODES
            or any(re.fullmatch(r'5[0-9]{2}', code) for code in lowered)
            or _SERVER_FAILURE.search(semantics)):
        kind = 'unknown'
    elif status == 429 or lowered & _BUSY_CODES or _RATE_LIMIT.search(semantics):
        kind = 'busy'
    elif (status in (400, 401, 403, 404, 422) or lowered & _REJECTED_CODES
            or _KEY_REFUSAL.search(semantics) or _ENTERPRISE_REFUSAL.search(semantics)):
        kind = 'rejected'
    else:
        kind = 'unknown'
    code = codes[0] if codes else str(status) if status >= 300 else ''
    if not detail:
        detail = {'rejected': '模型拒绝请求', 'busy': '模型请求达到频率或并发限制',
                  'unknown': '模型响应异常，不能确认是否已受理'}[kind]
    return dict(kind=kind, code=_safe_detail(code, api_key, 80),
                message=_safe_detail(detail, api_key))
