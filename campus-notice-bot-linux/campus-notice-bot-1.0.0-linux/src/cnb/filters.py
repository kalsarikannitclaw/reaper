"""Spam filter + interest topics. Two layers:
 1 rules (no LLM): sender blocklist, blocked keywords, a short list of STRONG spam patterns. Applies to EVERYONE incl. owners/admins.
   Deliberately narrow: words like 代考/贷款 also appear in legit notices (exam rules), so those are left to the LLM.
 2 LLM: extraction may return type 'spam' (ads, scams, chain letters, promos) even from an admin; official notices that merely contain
   a QR code or link are NOT spam.
Filtered messages are kept (spam=1 + reason) and listed on the pages, so a wrongly filtered notice can be found; allow_keywords overrides rules."""
import json, os, re
from . import db as D
FILTERS = os.path.join(D.ROOT, "filters.json"); INTERESTS = os.path.join(D.ROOT, "interests.json")
STRONG = re.compile(r"刷单|博彩|赌博|套现|裸聊|日赚|月入[过万\d]|兼职.{0,8}(日结|轻松赚)|加(微信|薇|V|v).{0,15}(返现|赚钱|兼职|领红包)|点击.{0,10}领取.{0,6}(红包|现金)")

def _load(p, default):
    try: return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default
    except ValueError: raise SystemExit(f"{os.path.basename(p)} is not valid JSON")

def load_filters(): return _load(FILTERS, {"block_senders": [], "block_keywords": [], "allow_keywords": []})
def load_interests(): return _load(INTERESTS, {"topics": []}).get("topics", [])

def spam_reason(text, sender, f):
    t = text or ""
    if any(k and k in t for k in f.get("allow_keywords", [])): return None
    if sender in {int(x) for x in f.get("block_senders", [])}: return "blocked sender"
    for k in f.get("block_keywords", []):
        if k and k in t: return f"blocked keyword: {k}"
    m = STRONG.search(t)
    return f"spam pattern: {m.group(0)[:20]}" if m else None

def topics_of(text, topics):
    low = (text or "").lower()
    return [t["name"] for t in topics if any(k.lower() in low for k in [t["name"], *t.get("keywords", [])] if k)]

def prefilter(db):
    """Run on unprocessed messages: mark spam (processed, never sent to the LLM), tag interest topics. Returns (spam, tagged)."""
    f, tp = load_filters(), load_interests(); sp = tg = 0
    for m in db.execute("SELECT id,user_id,text FROM msg WHERE processed=0 AND spam=0").fetchall():
        r = spam_reason(m["text"], m["user_id"], f)
        if r: db.execute("UPDATE msg SET spam=1, spam_reason=?, processed=1 WHERE id=?", (r, m["id"])); sp += 1; continue
        t = topics_of(m["text"], tp)
        if t: db.execute("UPDATE msg SET topics=? WHERE id=?", (json.dumps(t, ensure_ascii=False), m["id"])); tg += 1
    db.commit(); return sp, tg
