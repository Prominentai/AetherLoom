"""Prompt tag spans shared by completion editors, without Qt dependencies.

Offsets are Python string offsets. Callers convert them to UTF-16 positions
when selecting text with QTextCursor. Whitespace separates words within a tag;
only tag separators and the selected prompt language's syntax split a span.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TokenSpan:
    start: int
    end: int
    query: str


_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_NEWLINES = "\r\n\u2028\u2029"


def _boundaries(text, syntax):
    """Yield syntax boundaries, excluding escaped punctuation and literal ()."""
    stack = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char in ",，" + _NEWLINES:
            yield index, index + 1, char
        elif syntax == "nai":
            if text.startswith("::", index):
                yield index, index + 2, "::"
                index += 1
            elif char in "{}[]|":
                yield index, index + 1, char
        elif char in "([])":
            if char in "([":
                stack.append(char)
            elif stack:
                stack.pop()
            yield index, index + 1, char
        elif char == "|" or (char == ":" and stack):
            # A literal colon in a tag such as re:zero is not prompt weighting.
            yield index, index + 1, char
        index += 1


def completion_token(text, cursor, *, syntax="sd"):
    """Return the complete tag around a caret, and its query before the caret.

    Spaces and underscores belong to the same tag. Replacement excludes outer
    whitespace, emphasis wrappers and numeric weight fields. A caret inside a
    delimiter, at an empty tag, or in a numeric weight field returns ``None``.
    ``syntax='nai'`` keeps parentheses literal and understands ``1.2::tag::``.
    """
    if syntax not in ("sd", "nai"):
        raise ValueError("Unknown prompt syntax")
    cursor = max(0, min(int(cursor), len(text)))
    start, end = 0, len(text)
    left = right = ""
    for begin, finish, marker in _boundaries(text, syntax):
        if begin < cursor < finish:
            return None
        if finish <= cursor:
            start, left = finish, marker
        elif begin >= cursor:
            end, right = begin, marker
            break
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if cursor <= start or start >= end:
        return None
    value = text[start:end]
    query = text[start:min(cursor, end)].strip()
    if not query:
        return None
    if _NUMBER.fullmatch(value):
        if ((syntax == "nai" and (left == "::" or right == "::"))
                or (syntax == "sd" and left == ":")):
            return None
    return TokenSpan(start, end, query)


def completion_suffix(following, *, syntax="sd"):
    """Append a separator only when no tag/weight boundary already follows."""
    if syntax not in ("sd", "nai"):
        raise ValueError("Unknown prompt syntax")
    offset = 0
    while (offset < len(following) and following[offset].isspace()
           and following[offset] not in _NEWLINES):
        offset += 1
    stripped = following[offset:]
    endings = (",", "，", "|", *_NEWLINES)
    endings += ("}", "]", "::") if syntax == "nai" else (")", "]", ":")
    if stripped.startswith(endings):
        return ""
    # Preserve existing trailing spaces instead of doubling the inserted space.
    return "," if following and not stripped else ", "
