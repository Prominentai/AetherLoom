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
_SD_NUMBER = re.compile(_NUMBER.pattern + r"(?:[eE][+-]?\d+)?")
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
        elif char == "|":
            yield index, index + 1, char
        elif char == ':' and stack:
            # Round brackets can contain literal names such as (re:zero).
            # Only their numeric suffix is a weight; [] also support schedules.
            number_start = _skip_tag_spaces(text, index + 1)
            number = _SD_NUMBER.match(text, number_start)
            finish = _skip_tag_spaces(text, number.end()) if number else number_start
            if stack[-1] == '[' or finish == len(text) or text[finish:finish + 1] == ')':
                yield index, index + 1, char
        index += 1


def _tag_region(text, cursor, syntax):
    start, end, left, right = 0, len(text), "", ""
    for begin, finish, marker in _boundaries(text, syntax):
        if begin < cursor < finish:
            return None
        if finish <= cursor:
            start, left = finish, marker
        elif begin >= cursor:
            end, right = begin, marker
            break
    return start, end, left, right


def _protected_position(text, cursor, syntax):
    # Never split an escape, a numeric weight, or the middle of NAI's ::.
    before = text[:cursor]
    if (len(before) - len(before.rstrip('\\'))) % 2:
        return True
    region = _tag_region(text, cursor, syntax)
    if region is None:
        return True
    start, end, left, right = region
    value = text[start:end].strip()
    number = _SD_NUMBER if syntax == 'sd' else _NUMBER
    return bool((syntax == 'sd' and left == ':' and (
        number.fullmatch(value) or (not value and right in (')', ''))))
        or (syntax == 'nai' and number.fullmatch(value)
            and (left == '::' or right == '::')))


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
    if _protected_position(text, cursor, syntax):
        return None
    start, end, left, right = _tag_region(text, cursor, syntax)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if cursor <= start or start >= end:
        return None
    query = text[start:min(cursor, end)].strip()
    if not query:
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
    if stripped.startswith(tuple(_NEWLINES)):
        return ","
    endings = (",", "，", "|")
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


def _tag_space_start(text, index):
    """Skip preceding horizontal whitespace without removing escaped spaces."""
    while index > 0 and text[index - 1].isspace() and text[index - 1] not in _NEWLINES:
        boundary = index - 1
        escape_start = boundary
        while escape_start > 0 and text[escape_start - 1] == "\\":
            escape_start -= 1
        if (boundary - escape_start) % 2:
            break
        index = boundary
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
            number = _SD_NUMBER.match(following, _skip_tag_spaces(following, index + 1))
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
        # Reuse the comma and its following spacing, not whitespace before it.
        tail = following[:end] + following[index:finish]
        if finish == len(following) and finish == index + 1:
            tail += " "
        return tail, finish
    if index < len(following) and following[index] in _NEWLINES:
        # A line break still needs a tag separator. Keep existing horizontal
        # spacing after the comma without consuming the next line or indent.
        return following[:end] + "," + following[end:index], index
    if not completion_suffix(following[end:], syntax=syntax):
        return following[:end], end
    # Preserve existing horizontal whitespace while placing the comma before
    # it. Closing delimiters and the separator remain one undoable edit.
    return following[:end] + "," + (following[end:index] or " "), index


def _matches_typed_tag(value, tag):
    from .tag_search import matches_tag_query
    if not matches_tag_query(value, tag):
        return False
    # Search can ignore punctuation; replacing text must not lose it. Only
    # parentheses' optional formatting escapes are equivalent to literal ones.
    def symbols(text):
        text = re.sub(r'\\([()])', r'\1', text)
        return re.findall(r'[^\w\s]', text)
    remaining = iter(symbols(tag))
    if not all(any(symbol == candidate for candidate in remaining) for symbol in symbols(value)):
        return False
    # Repeated words are real content, even though search deduplicates terms.
    words = re.findall(r'[^\W_]+', tag.casefold())
    for term in sorted(re.findall(r'[^\W_]+', value.casefold()), key=len, reverse=True):
        match = next((i for i, word in enumerate(words) if word.startswith(term)), None)
        if match is None:
            return False
        words.pop(match)
    return True


def _partial_tag_selection(text, start, end, syntax):
    for cursor in (start, end):
        region = _tag_region(text, cursor, syntax)
        if region is not None:
            begin, finish, _, _ = region
            if text[begin:cursor].strip() and text[cursor:finish].strip():
                return True
    return False


def completion_insertion(text, start, end, tag, *, syntax="sd"):
    """Complete matching input without deleting unrelated tag content.

    Complete matching input before the caret without consuming the tag text
    after it. Unrelated input is kept and the candidate is inserted at the
    caret. Explicit partial-tag selections are replaced exactly; full-tag
    selections also receive a trailing separator.
    Returned offsets use Python characters, not Qt UTF-16 positions.
    """
    if syntax not in ("sd", "nai"):
        raise ValueError("Unknown prompt syntax")
    start, end = sorted((max(0, min(start, len(text))), max(0, min(end, len(text)))))
    if start != end and _partial_tag_selection(text, start, end, syntax):
        return start, end, tag
    leading = ""
    if start == end:
        if _protected_position(text, start, syntax):
            return start, end, ''
        token = completion_token(text, start, syntax=syntax)
        if token is not None:
            if _matches_typed_tag(text[token.start:min(start, token.end)], tag):
                start = token.start
            else:
                leading = ", "
        else:
            previous = text[:start]
            stripped = previous[:_tag_space_start(previous, len(previous))]
            boundaries = list(_boundaries(stripped, syntax))
            marker = boundaries[-1][2] if boundaries and boundaries[-1][1] == len(stripped) else ""
            openings = ("{", "[") if syntax == "nai" else ("(", "[", ":")
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
        if leading == ", ":
            # Put the separator before trailing horizontal whitespace while
            # leaving the insertion anchored at the caret, including in tags.
            start = _tag_space_start(text, start)
    tail, consumed = completion_tail(text[end:], syntax=syntax)
    return start, end + consumed, leading + tag + tail
