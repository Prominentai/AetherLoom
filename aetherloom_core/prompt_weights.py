"""Selection-based prompt weighting, independent of Qt.

All offsets count Python characters, not UTF-16 code units.  The returned
selection is relative to ``replacement`` and covers the edited body, so the
next shortcut adjusts the same group instead of adding another wrapper.
"""
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import re


@dataclass(frozen=True)
class WeightEdit:
    start: int
    end: int
    replacement: str
    selection_start: int
    selection_end: int


@dataclass(frozen=True)
class _Group:
    start: int
    end: int
    body_start: int
    body_end: int
    weight: Decimal
    kind: str


_DECIMAL = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_SD_NUMBER = re.compile(_DECIMAL + r"(?:[eE][+-]?\d+)?")
_NAI_NUMBER = re.compile(_DECIMAL)
_NAI_NUMBER_END = re.compile(r"(" + _DECIMAL + r")$")
_ONE = Decimal(1)


def _escaped(text, index):
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 != 0


def _number(value, *, sd=False):
    value = value.strip()
    pattern = _SD_NUMBER if sd else _NAI_NUMBER
    if len(value) > 64 or pattern.fullmatch(value) is None:
        return None
    try:
        result = Decimal(value)
        # Do not expand malformed/user-supplied huge exponents into a document.
        if not result.is_finite() or abs(result.as_tuple().exponent) > 100:
            return None
        return result
    except InvalidOperation:
        return None


def _format(value):
    # Match the practical precision of prompt editors, without float drift.
    with localcontext() as context:
        context.prec = 256
        if abs(value.adjusted()) > 200:
            raise InvalidOperation("Prompt weight magnitude is too large")
        value = value.quantize(Decimal("0.0000000001"))
        if value == 0:
            return "0"
        return format(value, "f").rstrip("0").rstrip(".")


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _bracket_groups(text, syntax):
    # ComfyUI's core parses parentheses only; [] are literal text, unlike
    # AUTOMATIC1111's prompt syntax.  NovelAI does use [] for weakening.
    opens, closes = ("(", ")") if syntax == "sd" else ("{[", "}]")
    stack, groups, unmatched = [], [], []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char in opens:
            stack.append([index, char, None])
        elif syntax == "sd" and char == ":" and stack:
            stack[-1][2] = index
        elif char in closes:
            expected = opens[closes.index(char)]
            if not stack or stack[-1][1] != expected:
                unmatched.append(index)
                index += 1
                continue
            start, opening, colon = stack.pop()
            body_start, body_end = start + 1, index
            weight = Decimal("1.1" if syntax == "sd" else "1.05")
            if opening == "[":
                with localcontext() as context:
                    context.prec = 80
                    weight = _ONE / weight
            if syntax == "sd" and opening == "(":
                if colon is not None:
                    explicit = _number(text[colon + 1:body_end], sd=True)
                    if explicit is not None:
                        weight, body_end = explicit, colon
            groups.append(_Group(start, index + 1, body_start, body_end,
                                 weight, "bracket"))
        index += 1
    unmatched.extend(index for index, _, _ in stack)
    return groups, unmatched


def _numeric_groups(text):
    """Parse NAI's flat numeric regions; a bare :: returns to weight one.

    A second numeric marker can change an open region's weight.  Such a region
    is intentionally not rewritten by the shortcut: introducing a closing ::
    around it could reset somebody else's bracket or numeric context.
    """
    groups, markers, unsafe = [], [], []
    active = None
    offset = 0
    while True:
        delimiter = text.find("::", offset)
        if delimiter < 0:
            break
        finish = delimiter + 2
        previous = offset
        offset = finish
        if _escaped(text, delimiter):
            continue
        # Bounded look-behind avoids a quadratic regex scan on long numeric
        # prompts without a delimiter.
        lower = max(previous, delimiter - 65)
        match = _NAI_NUMBER_END.search(text[lower:delimiter])
        begin = lower + match.start(1) if match else delimiter
        number = match[1] if match else None
        # A digit at the end of a word is part of that word, not a new weight.
        # An all-number body is likewise closed by its following delimiter.
        if number is not None and ((begin > 0
                and (text[begin - 1].isalnum() or text[begin - 1] in "_."))
                or (active is not None and begin == active.body_start)):
            number = None
        value = _number(number) if number is not None else None
        markers.append((begin if number is not None else delimiter, finish))
        if number is not None:
            if active is not None:
                unsafe.append((active.start, finish))
            active = _Group(begin, 0, finish, 0,
                            value if value is not None else _ONE, "numeric")
            if value is None:
                unsafe.append((begin, len(text)))
        elif active is not None:
            groups.append(_Group(active.start, finish, active.body_start,
                                 delimiter, active.weight, "numeric"))
            active = None
    if active is not None:
        unsafe.append((active.start, len(text)))
    return groups, markers, unsafe


def _render(body, weight, syntax):
    number = _format(weight)
    if number == "1":
        return body, 0
    prefix, suffix = (("(", ":" + number + ")") if syntax == "sd"
                      else (number + "::", "::"))
    if syntax == "nai" and _NAI_NUMBER_END.search(body[-65:]):
        # Keep a numeric tag ending (v2, 42) distinct from a new NAI weight
        # marker.  This space is outside the selected text and survives edits.
        suffix = " " + suffix
    return prefix + body + suffix, len(prefix)


def _edit_group(text, group, delta, syntax):
    body = text[group.body_start:group.body_end]
    replacement, offset = _render(body, group.weight + delta, syntax)
    start, end = _trim(body, 0, len(body))
    return WeightEdit(group.start, group.end, replacement,
                      offset + start, offset + end)


def _nai_split(text, group, start, end, delta):
    """Split flat emphasis instead of nesting reset-based numeric syntax."""
    before, _ = _render(text[group.body_start:start], group.weight, "nai")
    middle, offset = _render(text[start:end], group.weight + delta, "nai")
    after, _ = _render(text[end:group.body_end], group.weight, "nai")
    if start == group.body_start:
        before = ""
    if end == group.body_end:
        after = ""
    return WeightEdit(group.start, group.end, before + middle + after,
                      len(before) + offset, len(before) + offset + end - start)


def _intersects(start, end, other_start, other_end):
    return start < other_end and other_start < end


def adjust_weight(text, start, end, direction, syntax="sd", step=0.05):
    """Adjust selected prompt text, or return ``None`` when editing is unsafe.

    ``sd`` uses ``(text:1.05)`` and ``nai`` uses ``1.05::text::``.  Existing
    wrappers are updated when their full body (or full wrapper) is selected.
    Leading/trailing selection whitespace stays outside newly added wrappers.
    Empty selections and selections cutting an escape or syntax delimiter are
    ignored.  NovelAI numeric regions are never nested; a partial selection in
    a simple weighted region splits it into independently weighted regions.
    """
    with localcontext() as context:
        context.prec = 256
        try:
            return _adjust_weight(text, start, end, direction, syntax, step)
        except DecimalException:
            # An unusually large user-supplied weight must never escape into a
            # Qt key event and terminate the application.
            return None


def _adjust_weight(text, start, end, direction, syntax, step):
    if syntax not in ("sd", "nai"):
        raise ValueError("Unknown prompt syntax")
    if not isinstance(text, str):
        raise TypeError("Prompt must be text")
    start, end = sorted((max(0, min(int(start), len(text))),
                         max(0, min(int(end), len(text)))))
    start, end = _trim(text, start, end)
    if start == end or not direction:
        return None
    # Splitting a backslash escape changes characters outside the selection.
    if _escaped(text, start) or (end < len(text) and _escaped(text, end)):
        return None
    try:
        increment = Decimal(str(step))
    except InvalidOperation as error:
        raise ValueError("Weight step must be a finite positive number") from error
    if not increment.is_finite() or increment <= 0 or increment > 100:
        raise ValueError("Weight step must be a finite positive number")
    delta = increment if direction > 0 else -increment
    groups, unmatched = _bracket_groups(text, syntax)
    if any(start <= position < end for position in unmatched):
        return None
    numeric, markers, unsafe = ([], [], [])
    if syntax == "nai":
        numeric, markers, unsafe = _numeric_groups(text)
        groups.extend(numeric)
        if any(_intersects(start, end, left, right) for left, right in unsafe):
            return None

    # Never rewrite half a delimiter or the number in an existing weight.
    for group in groups:
        if not _intersects(start, end, group.start, group.end):
            continue
        contains_group = start <= group.start and end >= group.end
        inside_body = start >= group.body_start and end <= group.body_end
        if not contains_group and not inside_body:
            return None

    exact = []
    enclosing = []
    for group in groups:
        body_start, body_end = _trim(text, group.body_start, group.body_end)
        if ((start == group.start and end == group.end)
                or (start == body_start and end == body_end)):
            exact.append(group)
        if group.body_start <= start and end <= group.body_end:
            enclosing.append(group)
    target = min(exact, key=lambda group: group.end - group.start) if exact else None

    if syntax == "sd":
        if target is not None:
            return _edit_group(text, target, delta, syntax)
        replacement, offset = _render(text[start:end], _ONE + delta, syntax)
        return WeightEdit(start, end, replacement, offset, offset + end - start)

    # :: resets brace emphasis, even when the braces began outside selection.
    # Flatten simple chains first.  Mixed/nested numeric regions or braces
    # around additional content are left alone rather than changing neighbours.
    relevant = [group for group in groups
                if _intersects(start, end, group.start, group.end)]
    if target is not None:
        parents = [group for group in groups
                   if group.start < target.start and group.end > target.end]
        if parents:
            chain = sorted([target] + parents, key=lambda group: group.start)
            if any(group.kind != "bracket" for group in chain):
                return None
            if any(_trim(text, outer.body_start, outer.body_end)
                   != (inner.start, inner.end)
                   for outer, inner in zip(chain, chain[1:])):
                return None
            weight = _ONE
            for group in chain:
                weight *= group.weight
            target = _Group(chain[0].start, chain[0].end, target.body_start,
                            target.body_end, weight, "bracket")
        # No nested syntax may be silently reset at the emitted numeric end.
        if any(target.body_start <= group.start and group.end <= target.body_end
               for group in groups):
            # Entire nested single-body brace chains can be safely flattened.
            children = sorted([group for group in groups
                               if target.body_start <= group.start
                               and group.end <= target.body_end],
                              key=lambda group: group.start)
            current = target
            for child in children:
                if (current.kind != "bracket" or child.kind != "bracket"
                        or _trim(text, current.body_start, current.body_end)
                        != (child.start, child.end)):
                    return None
                current = _Group(target.start, target.end, child.body_start,
                                 child.body_end, current.weight * child.weight,
                                 "bracket")
            target = current
        return _edit_group(text, target, delta, syntax)

    if enclosing:
        if len(enclosing) != 1:
            return None
        parent = enclosing[0]
        if any(group != parent and parent.body_start <= group.start
               and group.end <= parent.body_end for group in groups):
            return None
        if any(parent.body_start <= position < parent.body_end
               for position in unmatched):
            return None
        return _nai_split(text, parent, start, end, delta)

    # Standalone closes and selections spanning several numeric regions cannot
    # safely be treated as ordinary text.  Escaped :: remain literal.
    if relevant or any(_intersects(start, end, left, right) for left, right in markers):
        return None
    if any(position < start and text[position] in "{[" for position in unmatched):
        return None
    replacement, offset = _render(text[start:end], _ONE + delta, syntax)
    return WeightEdit(start, end, replacement, offset, offset + end - start)
