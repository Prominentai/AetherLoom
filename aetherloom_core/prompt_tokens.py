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

    Spaces and underscores belong to the same tag. The span excludes outer
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


def _skip_tag_spaces(text, index):
    while (index < len(text) and text[index].isspace()
           and text[index] not in _NEWLINES):
        index += 1
    return index


def _closing_weight_end(following, syntax):
    """Return the end of complete closing weight/schedule delimiters."""
    end = 0
    while True:
        index = _skip_tag_spaces(following, end)
        rest = following[index:]
        closing = ("}", "]", "::") if syntax == "nai" else (")", "]")
        marker = next((value for value in closing if rest.startswith(value)), None)
        if marker:
            end = index + len(marker)
            continue
        if syntax == "sd" and rest.startswith(":"):
            number = _NUMBER.match(following, _skip_tag_spaces(following, index + 1))
            finish = _skip_tag_spaces(following, number.end()) if number else len(following)
            if number and following[finish:finish + 1] in (")", "]"):
                end = finish + 1
                continue
        return end


def completion_tail(following, *, syntax="sd"):
    """Return (replacement tail, consumed characters) after a completed tag.

    Keep closing weight syntax intact and put the separator after it. Reusing
    an existing comma also consumes it so the editor's caret ends up ready for
    the next tag, instead of still editing the tag that was just completed.
    """
    if syntax not in ("sd", "nai"):
        raise ValueError("Unknown prompt syntax")

    end = _closing_weight_end(following, syntax)
    index = _skip_tag_spaces(following, end)
    if following[index:index + 1] in (",", "，"):
        finish = _skip_tag_spaces(following, index + 1)
        tail = following[:finish]
        if finish == len(following) and finish == index + 1:
            tail += " "
        return tail, finish
    if not completion_suffix(following[end:], syntax=syntax):
        return following[:end], end
    # Preserve existing horizontal whitespace while placing the comma before
    # it. Closing delimiters and the separator remain one undoable edit.
    return following[:end] + "," + (following[end:index] or " "), index


def completion_insertion(text, start, end, tag, *, syntax="sd"):
    """Build an insertion without implicitly selecting the search query.

    A caret inside a tag adds the new tag after that complete tag (and its
    closing weight syntax). Only an explicit selection is replaced. Search
    still uses the complete multiword query; it never determines deletion.
    Returned offsets use Python characters, not Qt UTF-16 positions.
    """
    if syntax not in ("sd", "nai"):
        raise ValueError("Unknown prompt syntax")
    start, end = sorted((max(0, min(start, len(text))), max(0, min(end, len(text)))))
    leading = ""
    if start == end:
        token = completion_token(text, start, syntax=syntax)
        if token is not None:
            start = end = token.end + _closing_weight_end(text[token.end:], syntax)
            leading = ", "
        else:
            previous = text[:start]
            stripped = previous.rstrip(" \t\u3000")
            boundaries = list(_boundaries(stripped, syntax))
            marker = boundaries[-1][2] if boundaries and boundaries[-1][1] == len(stripped) else ""
            openings = ("{", "[") if syntax == "nai" else ("(", "[")
            if syntax == "nai" and marker == "::":
                begin = boundaries[-2][1] if len(boundaries) > 1 else 0
                if _NUMBER.fullmatch(stripped[begin:-2].strip()):
                    marker = "{"
            if not stripped or marker in (*openings, "|", *_NEWLINES):
                leading = ""
            elif marker in (",", "，"):
                leading = " " if previous == stripped else ""
            else:
                leading = ", "
    tail, consumed = completion_tail(text[end:], syntax=syntax)
    return start, end + consumed, leading + tag + tail
