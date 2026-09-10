"""Hosted Agent search configuration and source metadata; no local tool execution."""
from urllib.parse import quote, urlsplit

from api_calls.provider_client import ProviderAPIError, response_text

PROVIDERS = frozenset(('agent_codex', 'agent_grok', 'agent_claude'))


def enabled(provider, config=None):
    return provider in PROVIDERS and (config or {}).get('web_search', True) is True


def tool(provider):
    if provider == 'agent_claude':
        return dict(type='web_search_20250305', name='web_search', max_uses=5)
    if provider == 'agent_codex':
        return dict(type='web_search', external_web_access=True)
    if provider == 'agent_grok':
        return dict(type='web_search')
    raise ProviderAPIError('此 Agent 的当前接口尚未接入联网搜索。', status='unsupported')


def safe_source(value):
    if not isinstance(value, dict):
        return None
    url = value.get('url')
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
            return None
    except ValueError:
        return None
    title = ' '.join(str(value.get('title') or parts.hostname).split())[:240]
    return dict(title=title, url=url)


def check_tools(data, wire):
    """HTTP 200 can contain a failed search. Do not present it as grounded success."""
    calls = 0
    for item in data.get('content' if wire == 'claude' else 'output', []):
        if not isinstance(item, dict):
            continue
        kind = item.get('type')
        if kind == 'web_search_tool_result':
            content = item.get('content')
            if isinstance(content, dict) and content.get('type') == 'web_search_tool_result_error':
                code = content.get('error_code')
                label = {'too_many_requests': '搜索频率受限', 'max_uses_exceeded': '已达到搜索次数上限',
                         'unavailable': '搜索服务暂不可用', 'invalid_tool_input': '搜索参数无效',
                         'query_too_long': '搜索词过长', 'request_too_large': '搜索请求过大'}.get(code, '搜索工具执行失败')
                raise ProviderAPIError(label + '；未自动降级或重复提交。', status='search_error')
            if isinstance(content, list):
                calls += 1
        if kind == 'web_search_call':
            if item.get('status') != 'completed' or item.get('error'):
                raise ProviderAPIError('联网搜索未确认完成；未自动降级或重复提交。', status='search_error')
            calls += 1
        if kind in ('function_call', 'tool_use'):
            raise ProviderAPIError('Agent 请求了未接入的客户端工具，无法作为完整结果返回。', status='unsupported')
    return calls


class SearchText(str):
    """Keep the string API while retaining verified search state for probe UI."""
    def __new__(cls, value, sources=(), searched=False):
        result = super().__new__(cls, value)
        result.sources = tuple(dict(s) for s in sources)
        result.searched = bool(searched)
        return result


def result(data, wire, search_calls=0):
    text = response_text(data, wire)
    sources, seen = [], set()
    blocks = data.get('content', []) if wire == 'claude' else [
        block for item in data.get('output', []) if isinstance(item, dict) and item.get('type') == 'message'
        for block in item.get('content', []) if isinstance(block, dict)]
    rendered = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        value = block.get('text')
        inserts = []
        for citation in block.get('citations' if wire == 'claude' else 'annotations', []) or []:
            source = safe_source(citation)
            if source and source['url'] not in seen:
                sources.append(source); seen.add(source['url'])
            if source and isinstance(value, str) and source['url'] not in value:
                end = len(value) if wire == 'claude' else citation.get('end_index')
                if isinstance(end, int) and not isinstance(end, bool) and 0 <= end <= len(value):
                    title = source['title'].replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
                    link = f"[{title}]({quote(source['url'], safe=':/?=&%#@+~;,!$*-._')})"
                    inserts.append((end, link))
        if isinstance(value, str) and block.get('type') in ('text', 'output_text'):
            for end, link in sorted(set(inserts), reverse=True):
                value = value[:end] + ' ' + link + value[end:]
            if value.strip():rendered.append(value.strip())
    if rendered:
        text = '\n'.join(rendered)
    # Only actual answer citations, never every search hit. Existing inline links
    # remain intact. Text tools and their saved .txt results retain source URLs.
    links = []
    for source in sources:
        if source['url'] in text:
            continue
        title = source['title'].replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
        links.append(f"- [{title}]({quote(source['url'], safe=':/?=&%#@+~;,!$*-._')})")
    if links:
        text += '\n\n来源：\n' + '\n'.join(links)
    return SearchText(text, sources, search_calls > 0)


class ClaudeStream:
    """Preserve entire assistant turns, including encrypted tool and citation data."""
    def __init__(self):
        self.blocks = {}
        self.inputs = {}

    def consume(self, event):
        kind, index = event.get('type'), event.get('index', 0)
        if kind not in ('content_block_start', 'content_block_delta', 'content_block_stop'):
            return
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 2048:
            raise ProviderAPIError('Agent 流式内容序号无效。', status='invalid_response')
        if kind == 'content_block_start':
            block = event.get('content_block')
            if not isinstance(block, dict) or index in self.blocks:
                raise ProviderAPIError('Agent 流式内容格式无效。', status='invalid_response')
            self.blocks[index] = dict(block)
        elif kind == 'content_block_delta':
            delta = event.get('delta', {})
            block = self.blocks.setdefault(index, dict(type='text', text=''))
            subtype = delta.get('type')
            field = {'text_delta': 'text', 'thinking_delta': 'thinking', 'signature_delta': 'signature'}.get(subtype)
            if field:
                block[field] = block.get(field, '') + delta.get(field, '')
            elif subtype == 'citations_delta':
                block.setdefault('citations', []).append(delta.get('citation'))
            elif subtype == 'input_json_delta':
                self.inputs[index] = self.inputs.get(index, '') + delta.get('partial_json', '')
        elif index in self.inputs:
            import json
            self.blocks[index]['input'] = json.loads(self.inputs.pop(index))

    def content(self):
        if self.inputs:
            raise ProviderAPIError('Agent 工具参数未完整返回。', status='incomplete_response')
        return [self.blocks[i] for i in sorted(self.blocks)]
