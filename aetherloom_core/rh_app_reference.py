"""Pure parsing of RH App links and IDs, shared by import and canvas code."""
import html
import re
from urllib.parse import parse_qs, unquote, urlsplit


DEFAULT_SITE = 'https://www.runninghub.cn'
_HOSTS = {'runninghub.cn', 'www.runninghub.cn', 'runninghub.ai', 'www.runninghub.ai'}
_ID = re.compile(r'[0-9]{1,32}')
_LOCALE = r'(?:[a-z]{2}(?:[-_][a-z]{2})?/)?'
_APP_PATH = re.compile(r'/' + _LOCALE + r'(?:ai-detail|webapp)(?:/([0-9]{1,32})(?:/[^?#]*)?)?/?', re.I)


def _url(value):
    value = str(value or '').strip().strip('\ufeff\u200b').strip()
    value = html.unescape(value)
    markdown = re.fullmatch(r'\[[^\]\r\n]*\]\(([^\s]+)\)', value)
    if markdown:value = markdown.group(1)
    value = value.strip(' \t\r\n\"\'<>“”‘’').rstrip('。，；')
    if re.match(r'https?%3a', value, re.I):value = unquote(value)
    if not value or len(value) > 8192 or any(ord(char) < 32 for char in value):
        raise ValueError('请输入有效的 RunningHub 应用链接或 App 编号')
    return value


def _parts(raw):
    try:
        parsed = urlsplit('https:' + raw if raw.startswith('//') else raw if '://' in raw else 'https://' + raw)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname not in _HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
            raise ValueError()
    except ValueError:
        raise ValueError('应用链接必须来自 RunningHub 中文站或国际站') from None
    return parsed


def official_origin(value):
    parsed = _parts(_url(value))
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('默认站点必须是 RunningHub 官方站点地址')
    return 'https://www.runninghub.' + ('ai' if parsed.hostname.endswith('.ai') else 'cn')


def _valid_id(value):
    value = str(value or '').strip()
    if not _ID.fullmatch(value) or not value.strip('0'):
        raise ValueError('App 编号必须是有效的纯数字编号')
    return value


def application_reference(reference, default_base=DEFAULT_SITE):
    """An explicit URL owns its site; a bare ID uses the caller's active site."""
    if isinstance(reference, (str, int)) and not isinstance(reference, bool):reference = {'url': str(reference)}
    if not isinstance(reference, dict):raise ValueError('应用引用格式无效')
    supplied = reference.get('webapp_id') or reference.get('webappId')
    expected = _valid_id(supplied) if supplied not in (None, '') else ''
    raw = _url(reference.get('url')) if reference.get('url') else ''
    if not raw or _ID.fullmatch(raw):
        wid = _valid_id(raw or expected)
        base = official_origin(reference.get('base_url') or default_base)
    else:
        parsed = _parts(raw)
        base = 'https://www.runninghub.' + ('ai' if parsed.hostname.endswith('.ai') else 'cn')
        path, query = unquote(parsed.path), parsed.query
        # Some copied front-end routes put the App path after '#/'.
        if parsed.fragment.startswith('/') and re.fullmatch('/' + _LOCALE, path.rstrip('/') + '/', re.I):
            route = urlsplit(parsed.fragment);path, query = unquote(route.path), route.query
        if any(part in ('.', '..') for part in path.split('/')):
            raise ValueError('应用链接路径无效')
        match = _APP_PATH.fullmatch(path)
        if not match:
            raise ValueError('请填写应用详情链接（ai-detail / webapp）或纯 App 编号')
        # Never use generic id/taskId or a long number from a model/workflow URL.
        params = parse_qs(query, max_num_fields=128)
        ids = [item for key in ('webappId', 'webapp_id', 'appId') for item in params.get(key, [])]
        if match.group(1):ids.insert(0, match.group(1))
        if not ids:raise ValueError('应用链接中没有找到 App 编号')
        ids = [_valid_id(item) for item in ids]
        if len(set(ids)) != 1:raise ValueError('应用链接包含不一致的 App 编号')
        if re.search(r'/(?:ai-detail|webapp)/[0-9]+', path[match.start(1) + len(match.group(1) or ''):] if match.group(1) else '', re.I):
            raise ValueError('应用链接包含多个 App 路径')
        wid = ids[0]
    if expected and expected != wid:raise ValueError('应用链接与 App 编号不一致')
    return dict(webapp_id=wid, url=base + '/webapp/' + wid, base_url=base,
                name=str(reference.get('name') or reference.get('title') or ''))
