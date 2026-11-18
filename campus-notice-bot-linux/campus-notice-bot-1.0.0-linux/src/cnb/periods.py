"""Class-period numbers written run together ("晚上910节" = periods 9 and 10) shown as a range ("9-10节").

Only for DISPLAY: the stored quote stays word for word as written (the extractor requires a verbatim quote).
Rule: digits directly before 节 that split into two CONSECUTIVE period numbers (each 1..13) and are NOT themselves a valid
single period (1..13). So 910 -> 9-10, 1112 -> 11-12, 1213 -> 12-13, 34 -> 3-4, but 12/13 (a real period, or 1-2: ambiguous)
and 45 vs 4-5? 45 is not a period, so 4-5. Anything else is left alone.
"""
import re

_RUN = re.compile(r"(?<![\d.:：/\-])(\d{2,4})(?=\s*节)")


def _split(d):
    if int(d) <= 13:
        return None
    for k in range(1, len(d)):
        a, b = d[:k], d[k:]
        if a[0] != "0" and b[0] != "0" and 1 <= int(a) <= 13 and int(b) == int(a) + 1 and int(b) <= 13:
            return f"{int(a)}-{int(b)}"
    return None


def fix(text):
    if not isinstance(text, str) or "节" not in text:
        return text
    return _RUN.sub(lambda m: _split(m.group(1)) or m.group(1), text)
