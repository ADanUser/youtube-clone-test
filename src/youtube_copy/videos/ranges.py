"""Single HTTP byte ranges; malformed/multiple ranges fall back to the full body."""

import re


class UnsatisfiableRangeError(ValueError):
    pass


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None or len(value) > 8192:
        return None
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value.strip(), re.IGNORECASE)
    if not match or not any(match.groups()):
        return None
    left, right = match.groups()

    # Saturating decimal conversion avoids Python's digit limit and unbounded integers.
    def number(text: str) -> int:
        significant = text.lstrip("0") or "0"
        return size + 1 if len(significant) > len(str(size)) else int(significant)

    if not left:
        suffix = number(right)
        if suffix == 0 or size == 0:
            raise UnsatisfiableRangeError
        return max(0, size - suffix), size - 1
    if right:
        first, last = left.lstrip("0") or "0", right.lstrip("0") or "0"
        if (len(last), last) < (len(first), first):
            return None
    start = number(left)
    end = number(right) if right else size - 1
    if start >= size:
        raise UnsatisfiableRangeError
    return start, min(end, size - 1)
