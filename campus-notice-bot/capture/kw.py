"""Keyword gate shared by listener.py and backfill.py. match(text) -> (tier, word) or None.
Config: capture/keywords.json (re-read on every call, so edits apply without a restart).
Fails OPEN: if the file is missing or unreadable the gate is off, so a typo can never silently
swallow every message."""
import json, os, re
TIME_CUE = re.compile(r"\d{1,2}\s*[点时]|\d{1,2}\s*[:：]\s*\d{2}|(?<!\d)\d{3,4}\s*[-–~至到]\s*\d{3,4}(?!\d)|\d{1,2}月\s*\d{1,2}|(?<![\d.])\d{1,2}\.\d{1,2}(?![\d.])|\d{1,2}[号日]|周[一二三四五六日天]|星期[一二三四五六日天]|明[天晚早]|后天|今[天晚]|下周|本周|每周|\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
HERE = os.path.dirname(os.path.abspath(__file__))
KW = os.path.join(HERE, "keywords.json")
INTERESTS = os.path.join(HERE, "..", "interests.json")

def _cfg():
    try: return json.load(open(KW, encoding="utf-8"))
    except (OSError, ValueError): return None

def match(text, priority=False):
    c = _cfg()
    if not c or not c.get("enabled", True): return ("off", "")
    t = text or ""
    for x in c.get("exclude", []): t = t.replace(x, " ")
    low = t.lower()
    for tier, words in c.get("tiers", {}).items():
        for w in words:
            if w and w.lower() in low: return (tier, w)
    if c.get("use_interest_keywords", True):
        try:
            for tp in json.load(open(INTERESTS, encoding="utf-8")).get("topics", []):
                for w in [tp["name"], *tp.get("keywords", [])]:
                    if w and len(w) >= 3 and w.lower() in low: return ("E interest:" + tp["name"], w)
        except (OSError, ValueError): pass
    # a place AND a time: the "when what where" shape of an event, from any sender
    if TIME_CUE.search(t):
        v = next((x for x in c.get("venues", []) if x in t), None)
        if not v and c.get("venue_room_regex"):
            r = re.search(c["venue_room_regex"], t); v = r.group(0) if r else None
        if v: return ("H venue+time", v)
    if priority and c.get("priority_time_cue", True):
        m = TIME_CUE.search(t)
        if m: return ("F priority+time", m.group(0))
    return None
