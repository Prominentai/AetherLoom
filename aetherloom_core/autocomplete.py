import os
import re
from aetherloom_core.ui.completion_popup import AutocompletePopup
from aetherloom_core.prompt_tokens import completion_suffix


def completion_options(settings=None):
    """Normalize persisted options, preserving behavior for older settings files."""
    raw = settings.get('autocomplete', {}) if isinstance(settings, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    rows = raw.get('visible_tags', 15)
    try:
        rows = 15 if isinstance(rows, bool) else max(1, min(50, int(rows)))
    except (TypeError, ValueError, OverflowError):
        rows = 15
    return {'escape_parentheses': raw.get('escape_parentheses', True) if isinstance(raw.get('escape_parentheses', True), bool) else True,
            'replace_spaces': raw.get('replace_spaces', False) if isinstance(raw.get('replace_spaces', False), bool) else False,
            'visible_tags': rows}


def format_tag(word, *, escape_parentheses=True, replace_spaces=False):
    """Format a tag independently of the editor's surrounding prompt syntax."""
    text = word.replace(' ', '_') if replace_spaces else word.replace('_', ' ')
    # An odd backslash run already escapes the parenthesis. Preserve it rather
    # than turning an already escaped tag back into weighting syntax.
    if escape_parentheses:
        text = re.sub(r'(\\*)([()])', lambda match: match[1] +
                      ('\\' if len(match[1]) % 2 == 0 else '') + match[2], text)
    return text.strip()


def format_completion(word, following_text, *, escape_parentheses=True, replace_spaces=False):
    """Escape literal tag parentheses and reuse an adjacent existing comma."""
    return format_tag(word, escape_parentheses=escape_parentheses,
                      replace_spaces=replace_spaces) + completion_suffix(following_text)


class AutocompleteManager:
    """管理词库加载和搜索"""

    def __init__(self, file_path='autocomplete.txt'):
        self.suggestions = []  # 存储 (word, count) 元组
        self.load_words(file_path)

    def load_words(self, file_path):
        words = []
        if not file_path or not os.path.exists(file_path):
            self.suggestions = words
            return
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.rsplit(',', 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        word, count = parts[0], int(parts[1])
                    else:
                        word, count = line, 0
                    words.append((word, count))
        except Exception as e:
            print(f"Error loading {file_path}: {e}")
        # Build completely before publishing: searches may be running while a
        # dictionary is reloaded and must never index a partially sorted list.
        words.sort(key=lambda x: (-x[1], x[0].lower()))
        self.suggestions = words

    def get_matches(self, prefix, limit=50):
        from aetherloom_core.tag_search import search
        return search(self, prefix, limit)


# 全局单例管理器（基于当前目录的 autocomplete.txt）
_manager = None
_manager_path = None


def get_manager(current_dir):
    global _manager, _manager_path
    path = os.path.normcase(os.path.abspath(os.path.join(current_dir or os.getcwd(), 'autocomplete.txt')))
    if _manager is None or _manager_path != path:
        _manager = AutocompleteManager(path)
        _manager_path = path
    return _manager
