"""Digest: open items, priority first, grouped by group; every item carries the 3-part original reference. Also .ics."""
import datetime as dt, json, re, os
from collections import defaultdict
from . import db as D
Z = dt.timezone(dt.timedelta(hours=8))     # the default zone (Asia/Shanghai); other modules still import it. Pages use tz() below.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TZ = {"name": None, "obj": None, "at": 0.0}
def tz_name():
    """Tenant timezone chosen in onboarding (S2a): data/onboard.json, else config.json tz, else Asia/Shanghai."""
    try:
        from . import onboard
        return onboard.tz_name()
    except Exception: return "Asia/Shanghai"
def tz():
    """tzinfo for the tenant timezone (S6b: digest no longer fixes +8). Re-read at most every 5 s; with no timezone
    database only Asia/Shanghai works (fixed +8), anything else falls back to it. set_tz(name) overrides it (tests)."""
    import time
    if _TZ["obj"] is not None and (_TZ["name"] == "!" or time.time() - _TZ["at"] < 5): return _TZ["obj"]
    n = tz_name()
    try:
        from zoneinfo import ZoneInfo
        o = ZoneInfo(n)
    except Exception: o = Z
    _TZ.update(obj=o, at=time.time()); return o
def set_tz(name=None):
    """Pin the zone (name) or release the pin (None). For tests."""
    if name is None: _TZ.update(name=None, obj=None, at=0.0); return
    try:
        from zoneinfo import ZoneInfo
        _TZ.update(name="!", obj=ZoneInfo(name), at=0.0)
    except Exception: _TZ.update(name="!", obj=Z, at=0.0)
def model_name():
    """The model chosen in onboarding, else config.json, else unknown (shown on pages; never invented)."""
    try:
        from . import onboard
        m = onboard.load().get("model")
        if isinstance(m, str) and m: return m
        c = json.load(open(onboard.CFG, encoding="utf-8")); m = c.get("model") if isinstance(c, dict) else None
        return m if isinstance(m, str) and m else "unknown"
    except Exception: return "unknown"
def fmt(ts): return dt.datetime.fromtimestamp(ts, tz()).strftime("%Y-%m-%d %H:%M")
def when(it): return it["start"] or it["due"] or "time unknown, please confirm"
def when_short(it):
    """'9.12 09:00–11:00' style, the way the student writes their own schedule. Date-only -> '9.12'."""
    t = it["start"] or it["due"]
    if not t or not t[:4].isdigit(): return "time unknown"
    out = f"{int(t[5:7])}.{int(t[8:10])}"
    if len(t) >= 16 and t[11:16] != "00:00": out += " " + t[11:16]
    e = it["end"]
    if e and len(e) >= 16 and e[:10] == t[:10] and e[11:16] != t[11:16]: out += "–" + e[11:16]
    elif e and e[:10] != t[:10] and e[:4].isdigit(): out += f" → {int(e[5:7])}.{int(e[8:10])}" + (" " + e[11:16] if e[11:16] != "00:00" else "")
    return out
def where_short(it): return it["location"] or "—"
def keywords(q, n=3):
    """2-3 short terms you can type into QQ's own search: no URLs, CJK runs cut to 5 characters, longest first."""
    w = re.sub(r"https?://\S+", " ", q or "")
    c = [r[:5] for r in re.findall(r"[\u4e00-\u9fff]{2,}", w)] + re.findall(r"[A-Za-z0-9]{3,}", w)
    out = []
    for x in sorted(c, key=len, reverse=True):
        if x not in out: out.append(x)
    return " ".join(out[:n])

def bridge_banner():
    """Capture health, shown on every page. Without this a dead QQ bridge is indistinguishable
    from a quiet week: the pages would just show nothing new and look correct (incident
    2026-11-10, PLAN backlog 12). Written by stack/bridge_guard.py."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "capture", "bridge_status.json")
    try:
        s = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    state = s.get("state", "unknown")
    if state == "online":
        try:                                       # S3b: online now, but is the gap from an earlier outage closed?
            cp = os.path.join(os.path.dirname(p), "coverage.json")
            cov = json.load(open(cp, encoding="utf-8")).get("covered_until") or 0
            lag = int(dt.datetime.now(dt.timezone.utc).timestamp()) - cov
            if cov and lag > 7200:
                return f'<p class="bad">Messages after {fmt(cov)} are still being collected (catching up after an outage); this page may be missing some.</p>'
        except (OSError, ValueError): pass
        age = int(dt.datetime.now(dt.timezone.utc).timestamp()) - (s.get("checked") or 0)
        return "" if age < 900 else f'<p class="bad">Capture status last checked {age // 60} min ago: the bridge guard may not be running.</p>'
    last = f'Last online {fmt(s["last_online"])}. ' if s.get("last_online") else "Not yet seen online. "
    word = {"needs_qr": "QQ capture is DOWN and needs the account owner",
            "capped": "QQ capture is STOPPED to protect the account",
            "waiting": "QQ capture is down, waiting before the next retry",
            "offline": "QQ capture is down"}.get(state, f"QQ capture state: {state}")
    return (f'<p class="bad"><b>{_h.escape(word)}.</b> {_h.escape(last)}'
            f'{_h.escape(str(s.get("detail") or ""))} '
            f'Nothing new is being captured, so this page is not a complete picture.</p>')


def _t(v):
    try:
        d = dt.datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=tz())
    except (TypeError, ValueError): return None

def is_past(it, now=None):
    """True when EVERY date the item has is behind us. Undated items are never past: the student still has to
    find out when. Rules: a deadline is past once its due time has gone; an event with a clock time is past
    3h after it starts (or when its end has gone); a date-only event (stored as 00:00) is past once that day
    is over; a contest is past only when its registration deadline AND its contest date are both over."""
    now = now or dt.datetime.now(tz()); ends = []
    for k in ("due", "end", "start"):
        v = it[k]; d = _t(v)
        if d is None: continue
        if k == "start":
            if it["end"] and _t(it["end"]): continue               # the end time decides
            ends.append(d + dt.timedelta(days=1) if d.hour == 0 and d.minute == 0 else d + dt.timedelta(hours=3))
        elif k == "end" and d.hour == 0 and d.minute == 0: ends.append(d + dt.timedelta(days=1))
        else: ends.append(d)
    try: x = json.loads(it["extra"] or "null") or {}
    except ValueError: x = {}
    d = _t(x.get("reg_deadline")) if isinstance(x, dict) else None
    if d: ends.append(d)
    return bool(ends) and max(ends) < now

def items(db, include_done=False, include_past=False):
    q = "SELECT * FROM items WHERE status IN ('new','confirmed')" if not include_done else "SELECT * FROM items"
    rows = dedupe(db.execute(q + " ORDER BY priority DESC, COALESCE(due,start,'9999'), id ").fetchall())
    return rows if include_past else [r for r in rows if not is_past(r)]

def _same(a, b):
    """Same thing announced twice: identical time, and either the same place or one title containing the
    other ('期中习题课' / '微积分期中习题课'). Different groups or reposts, e.g. the same notice posted three times."""
    ta, tb = (a["due"] or a["start"]), (b["due"] or b["start"])
    if not ta or ta != tb or a["type"] != b["type"]: return False
    if a["title"] == b["title"]: return True
    if a["title"] in b["title"] or b["title"] in a["title"]: return True
    return bool(a["location"]) and a["location"] == b["location"]

def dedupe(rows):
    """Display-time only: nothing is deleted. The first item in priority order is kept; later ones that
    are the same thing are folded into it, so the page lists the event once."""
    kept = []
    for r in rows:
        if not any(_same(k, r) for k in kept): kept.append(r)
    return kept

# ---------- S6b: state, history, origin, scope (shared by the list, markdown and calendar) ----------
from . import scope as S
STATES = {"open": ("Open", "○"), "expired": ("EXPIRED, not marked done", "!"), "past": ("Past event", "◷"), "done": ("Done", "✔"), "cancelled": ("CANCELLED", "✖")}
def state(it, now=None):
    """open / expired (open but every date has gone: still undone) / done / cancelled. Colour is never the only cue:
    every state has a word and a symbol (see STATES)."""
    if it["status"] in ("cancelled", "done"): return it["status"]
    if not is_past(it, now): return "open"
    return "past" if it["type"] == "event" else "expired"      # a lecture/gathering that has happened is quiet grey; a task, deadline or registration still undone is red

def notified(db):
    """item_id -> epoch of the alert (notify.py's table); {} when there is none yet. 0 = baseline, not a real alert."""
    try: return {r[0]: r[1] for r in db.execute("SELECT item_id, at FROM notified")}
    except Exception: return {}

def by_state(db, limit=30, now=None):
    """{'open','expired','done','cancelled': [rows], 'more': {state: n}}. Nothing is deleted; expired, past, done and
    cancelled are capped at `limit` each (most recent date first) and the rest is counted."""
    now = now or dt.datetime.now(tz()); res = {"open": [], "expired": [], "past": [], "done": [], "cancelled": [], "more": {}}
    for r in items(db, include_past=True): res[state(r, now)].append(r)
    for k in ("expired", "past"):
        res[k].sort(key=lambda r: r["due"] or r["start"] or "", reverse=True); res["more"][k] = max(0, len(res[k]) - limit); res[k] = res[k][:limit]
    for st in ("done", "cancelled"):
        rows = dedupe(db.execute("SELECT * FROM items WHERE status=? ORDER BY COALESCE(due,start,'0000') DESC, id DESC", (st,)).fetchall())
        res["more"][st] = max(0, len(rows) - limit); res[st] = rows[:limit]
    return res

FIELD = {"location": "place", "start": "start", "end": "end", "due": "deadline", "title": "title", "action": "to do", "requirements": "requirements"}
def _val(field, v):
    if v is None or v == "": return "(blank)"
    d = _t(str(v)) if field in ("start", "end", "due") else None
    if d: return f"{d.month}.{d.day}" + ("" if d.hour == 0 and d.minute == 0 else f" {d:%H:%M}")
    return str(v)[:80]

def notes(db, it, cfg=None, tags=None):
    """History of one item as display lines: [{'kind': 'change'|'conflict'|'absorbed', 'text', 'scope', 'counts'}].
    A change counts only when the message that made it came from an institute-scope source (scope.counts_as);
    otherwise it is still shown, labelled, never hidden. 'link' rows (origin relinking) are not shown."""
    cfg = cfg or S.load(); out = []
    for h in D.history(db, it["id"]):
        sc = S.UNKNOWN
        if h["msg_id"]:
            m = db.execute("SELECT group_id FROM msg WHERE id=?", (h["msg_id"],)).fetchone()
            sc = S.of_group(m["group_id"], cfg) if m else S.UNKNOWN
        when = fmt(h["ts"]) if h["ts"] else ""
        if h["kind"] == "change":
            out.append(dict(kind="change", scope=sc, scope_label=S.label(sc, cfg), counts=S.counts_as("change", sc), when=when,
                            text=f"from {_val(h['field'], h['old'])} to {_val(h['field'], h['new'])} ({FIELD.get(h['field'], h['field'])})"))
        elif h["kind"] == "conflict" and h["field"] == "status":
            out.append(dict(kind="conflict", scope=sc, counts=False, when=when, text=f"a notice may say this was cancelled ({str(h['new'] or '')[11:100].strip()}); not applied, please check"))
        elif h["kind"] == "conflict":
            out.append(dict(kind="conflict", scope=sc, counts=False, when=when, text=f"a later notice says {FIELD.get(h['field'], h['field'])} is {_val(h['field'], h['new'])}; kept {_val(h['field'], h['old'])}, please confirm"))
        elif h["field"] == "absorbed":
            out.append(dict(kind="absorbed", scope=sc, counts=False, when=when, text="also mentioned: " + str(h["new"] or "")[:120]))
    return out

def note_text(n):
    """One display line. A change from a source outside institute scope says so and is not counted as a change."""
    if n["kind"] == "change":
        return ("Changed " if n["counts"] else f"Change reported by a non-institute source ({n['scope_label']}); applied, not counted for exams: ") + n["text"] + (f" ({n['when']})" if n["when"] else "")
    return {"conflict": "Check: ", "absorbed": "Related: "}[n["kind"]] + n["text"]

def origin_of(db, it):
    """(item id or None, title) of the task this item belongs to, or None. Only what S2 stored; never guessed."""
    try: oid, ot = it["origin"], it["origin_title"]
    except (IndexError, KeyError): return None
    if oid:
        r = db.execute("SELECT id,title FROM items WHERE id=?", (oid,)).fetchone()
        if r: return (r["id"], r["title"])
    return (None, ot) if ot else None

def _exam_words():
    try:
        from . import weekly
        return weekly.focus_words()
    except Exception: return []
def is_exam(it, words):
    t = f"{it['title'] or ''} {it['quote'] or ''}".lower()
    return it["type"] != "contest" and any(w.lower() in t for w in words)

def _mq(x):
    """One line of plain text safe inside the .md (raw HTML and link syntax neutralised)."""
    r = " ".join(str(x if x is not None else "").split())
    for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("[", "\\["), ("]", "\\]"), ("`", "'"), ("~", "∼")): r = r.replace(a, b)
    return r

def markdown(db):
    now = dt.datetime.now(tz()); st = by_state(db, now=now); cfg = S.load(); tags = S.audience_tags(cfg=cfg); nt = notified(db)
    its = st["open"]; out = [f"# Things you still need to act on ({now:%Y-%m-%d %H:%M})\n", f"_Times in {tz_name()} · model {model_name()}_\n"]
    names = {(r["group_id"], r["user_id"]): r["nick"] for r in db.execute("SELECT group_id,user_id,nick FROM admins")} if db.execute("SELECT name FROM sqlite_master WHERE name='admins'").fetchone() else {}
    def line(i, mark=""):
        src = db.execute("SELECT m.* FROM item_sources s JOIN msg m ON m.id=s.msg_id WHERE s.item_id=?", (i["id"],)).fetchall()
        s0 = src[0] if src else None; who = names.get((i["group_id"], i["sender"])) or i["sender"]; sc = S.of_item(db, i, cfg, tags)
        title = f"~~{_mq(i['title'])}~~" if mark in ("EXPIRED", "CANCELLED") else _mq(i["title"])
        l = (f"- {'[' + mark + '] ' if mark else ''}**{when_short(i)}** · **{title}** · {_mq(where_short(i))}  [{i['type']}] [{_mq(S.label(sc, cfg))}]" + ("  ⚠ confirm" if i["needs_confirm"] else "")
             + ("  (alerted, not done yet)" if not mark and nt.get(i["id"]) else ""))
        o = origin_of(db, i)
        if o: l += f"  \n  part of: {_mq(o[1])}"
        for n in notes(db, i, cfg, tags): l += f"  \n  {_mq(note_text(n))}"
        if is_manual(i): l += f"  \n  source: {MANUAL_LABEL}" + (f" · {fmt(s0['ts'])}" if s0 else "") + f"  \n  > {_mq(i['quote'])}"
        elif s0: l += f"  \n  source: group {i['group_id']} · {fmt(s0['ts'])} · {who} · search QQ for: `{keywords(i['quote'])}`  \n  > {_mq(i['quote'])}"
        return l
    if not its and not st["expired"] and not st["past"] and not st["done"] and not st["cancelled"]: return "\n".join(out) + "\nNothing open.\n"
    if not its: out.append("Nothing open.\n")
    words = _exam_words(); ex = [i for i in its if is_exam(i, words) and S.counts_as("exam", S.of_item(db, i, cfg, tags))]
    if ex:
        out.append(f"## Exams & tests ({S.label(S.INSTITUTE, cfg)} sources only)\n"); out += [line(i) for i in ex]; out.append("")
    held = len([i for i in its if is_exam(i, words)]) - len(ex)
    if held: out.append(f"_{held} exam-like item(s) are listed below with their scope shown: their source is not marked as your institute (capture/scope.json), so they are not counted as your exams._\n")
    exids = {i['id'] for i in ex}; its = [i for i in its if i['id'] not in exids]
    for title, sel in (("Priority (leaders / teachers)", [i for i in its if i["priority"]]), ("Other", [i for i in its if not i["priority"]])):
        if not sel: continue
        out.append(f"## {title}\n"); g = defaultdict(list)
        for i in sel: g[i["group_id"]].append(i)
        for gid, rows in g.items():
            out.append(f"### {group_title(gid)}"); out += [line(i) for i in rows]; out.append("")
    for key, title, mark in (("expired", "Expired, still not marked done", "EXPIRED"), ("past", "Past events", "PAST"), ("done", "Done", "DONE"), ("cancelled", "Cancelled (not happening)", "CANCELLED")):
        if st[key]:
            out.append(f"## {title}\n"); out += [line(i, mark) for i in st[key]]
            if st["more"].get(key): out.append(f"- _{st['more'][key]} more not shown_")
            out.append("")
    return "\n".join(out)

def ics(db):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//campus-notice-bot//EN"]
    st = by_state(db); canc = [i for i in st["cancelled"] if not is_past(dict(i, status="new"))]    # upcoming cancelled events are sent as STATUS:CANCELLED, never as normal events
    for i in st["open"] + canc:
        t = i["start"] or i["due"]
        if not t: continue
        try: d = dt.datetime.fromisoformat(t)
        except ValueError: continue
        d = d if d.tzinfo else d.replace(tzinfo=tz()); u = d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        L += ["BEGIN:VEVENT", f"UID:cnb-{i['id']}@local", f"DTSTAMP:{u}", f"DTSTART:{u}", f"SUMMARY:{'CANCELLED: ' if i['status'] == 'cancelled' else ''}{i['title']}", f"DESCRIPTION:{i['quote'][:200]!r}"] + (["STATUS:CANCELLED"] if i["status"] == "cancelled" else []) + ["END:VEVENT"]
    return "\r\n".join(L + ["END:VCALENDAR"]) + "\r\n"

# ---------- HTML report + original-view pages (PLAN: link to original; local pages, QQ has no permalink) ----------
import html as _h
CSS = """:root{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--card:#fff;--line:#e7e5e4;--acc:#b45309;--hi:#fef3c7}
@media(prefers-color-scheme:dark){:root{--bg:#171412;--fg:#f5f5f4;--mut:#a8a29e;--card:#211d1a;--line:#3a332e;--acc:#fbbf24;--hi:#3b2f10}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px;overflow-wrap:anywhere}h1{font-size:1.4rem;margin:0 0 4px}h2{font-size:1.05rem;margin:28px 0 8px;color:var(--acc)}h3{font-size:.9rem;margin:18px 0 6px;color:var(--mut);font-weight:600}
.sub{color:var(--mut);font-size:.9rem}.card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
.t{font-weight:600}.when{color:var(--acc);font-variant-numeric:tabular-nums}.meta{color:var(--mut);font-size:.85rem;margin-top:4px}
.q{border-left:3px solid var(--line);padding-left:10px;margin:8px 0 0;color:var(--mut)}.warn{background:var(--hi);border-radius:4px;padding:0 6px;font-size:.8rem}
a{color:var(--acc)}.bad{background:#dc2626;color:#fff;border-radius:8px;padding:10px 14px;margin:12px 0;font-size:.95rem}@media(prefers-color-scheme:dark){.bad{background:#7f1d1d}}.msg{padding:6px 10px;border-bottom:1px solid var(--line)}.msg.hit{background:var(--hi);border-radius:6px}.msg small{color:var(--mut)}
a:focus-visible,summary:focus-visible{outline:3px solid var(--acc);outline-offset:2px}:root{color-scheme:light dark}
body{font-family:"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC",system-ui,sans-serif;line-height:1.6}
.card{border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.05)}.card .t{font-size:1.05rem}.when{font-weight:600}
/* S6b: states (a word + a symbol always accompany the colour), scope badges, Chinese font stack, print */
:root{--red:#b91c1c;--grey:#57534e}@media(prefers-color-scheme:dark){:root{--red:#f87171;--grey:#a8a29e}}
body{font-family:"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC","Source Han Sans SC","WenQuanYi Micro Hei",system-ui,sans-serif}
.st{display:inline-block;font-size:.78rem;font-weight:700;border:1px solid currentColor;border-radius:4px;padding:0 6px;margin-right:6px;text-decoration:none}
.sc{display:inline-block;font-size:.75rem;border:1px solid var(--line);border-radius:4px;padding:0 6px;margin-left:4px;color:var(--mut)}.sc-institute,.sc-school{color:var(--fg)}.sc-unknown{border-style:dashed}
.card.st-expired{border-left:5px solid var(--red)}.card.st-expired .tt,.card.st-cancelled .tt{text-decoration:line-through;text-decoration-thickness:2px;text-decoration-skip-ink:none}.card.st-expired .tt{text-decoration-color:var(--red)}.card.st-cancelled .tt{text-decoration-color:var(--fg)}.card.st-past,.card.st-past .when,.card.st-past .t,.card.st-past .q{color:var(--grey)}.st-past .st{color:var(--grey);border-style:dotted}.st-expired .st{color:var(--red)}
.card.st-done{background:transparent;border-style:solid}.card.st-done,.card.st-done .when,.card.st-done .t,.card.st-done .q{color:var(--grey)}.st-done .st{color:var(--grey)}
.card.st-cancelled{border:2px dashed var(--grey);background:transparent}.card.st-cancelled,.card.st-cancelled .when,.card.st-cancelled .q{color:var(--grey)}.st-cancelled .st{color:var(--fg)}
.alerted{color:var(--red);font-size:.85rem;font-weight:600}.note{font-size:.88rem;margin-top:4px}.note.chg{font-weight:600}.note.off{color:var(--mut)}.orig{font-size:.88rem;margin-top:4px}
@media print{:root{--bg:#fff;--fg:#000;--mut:#444;--card:#fff;--line:#bbb;--acc:#000;--hi:#eee;--red:#000;--grey:#333}.card .tt{text-decoration-color:#000!important;text-decoration-thickness:2px}.bad{background:none;color:#000;border:2px solid #000}.card{break-inside:avoid;box-shadow:none}main{max-width:none;padding:0}a{color:#000}.card.st-expired{border-left-width:6px}}"""
def page(title, body): return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_h.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>'
def e(x): return _h.escape(str(x if x is not None else ""))
def linked(x):
    """escape first, then turn http(s) URLs into links (rel=noopener)."""
    return re.sub(r"(https?://(?:(?!&(?:lt|gt|quot|#x27);)[^\s<>\"'])+)", r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', e(x))

def original_page(db, msg_id, hit_ids=()):
    m = db.execute("SELECT * FROM msg WHERE id=?", (msg_id,)).fetchone()
    rows = "".join(f'<div class="msg{" hit" if r["id"] in (msg_id, *hit_ids) else ""}"><small>{fmt(r["ts"])} · {e(r["nick"] or r["user_id"])} ({r["user_id"]}){" · " + e(r["role"]) if r["role"] in ("owner","admin") else ""}</small><br>{linked(r["text"])}</div>' for r in [m])
    return page("Original message", f'<p><a href="../latest.html">← back to report</a></p><h1>Original message</h1><p class="sub">{e(MANUAL_LABEL) + ": the copy you supplied, not a QQ message." if m["group_id"] == MANUAL_GROUP else "Group " + str(m["group_id"]) + " · the message exactly as captured. Images and files show as placeholders: open QQ for those."}</p>{rows}')

def extras_html(db, i, shown=(), cfg=None, tags=None):
    """Origin link, 'changed from X to Y', conflicts and absorbed messages for one item (S6b)."""
    out = []; o = origin_of(db, i)
    if o:
        lk = f'<a href="#item-{o[0]}">{e(o[1])}</a>' if o[0] in shown else e(o[1])
        out.append(f'<div class="orig">Part of: {lk}</div>')
    for n in notes(db, i, cfg, tags):
        out.append(f'<div class="note {"chg" if n["kind"] == "change" and n["counts"] else "off"}">{"↻ " if n["kind"] == "change" else "· "}{e(note_text(n))}</div>')
    return "".join(out)

def contest_cards(db, cons, shown=(), cfg=None, tags=None):
    from . import contest as C
    out = ['<h2>Contests</h2>']; cfg = cfg or S.load(); tags = tags if tags is not None else S.audience_tags()
    def key(i):
        d = C.describe(i, db); return (d['extra'].get('reg_deadline') or i['due'] or '9999')
    for i in sorted(cons, key=key):
        d = C.describe(i, db); x = d['extra']; srcs = db.execute('SELECT m.* FROM item_sources s JOIN msg m ON m.id=s.msg_id WHERE s.item_id=? ORDER BY m.id', (i['id'],)).fetchall()
        badge = {'yes': 'on school list', 'no': 'NOT on school list', 'unknown': 'list not loaded'}[d['recognition']]
        facts = ' · '.join(f for f in [f"register by {x['reg_deadline']}" if x.get('reg_deadline') else 'deadline unknown', f"team: {x['team_size']}" if x.get('team_size') else '', f"organiser: {x['organiser']}" if x.get('organiser') else ''] if f)
        links = ' '.join(f'<a href="original/{s["id"]}.html">original</a>' for s in srcs)
        sc = S.of_item(db, i, cfg, tags); stt = state(i)
        mark = f'<span class="st st-{stt}">{STATES[stt][1]} {e(STATES[stt][0])}</span>' if stt != "open" else ""
        out.append(f'<div class="card st-{stt}" id="item-{i["id"]}"><div class="t">{mark}<span class="tt">{e(x.get("name") or i["title"])}</span> <span class="warn">{badge}</span><span class="sc sc-{sc}">{e(S.label(sc, cfg))}</span></div><div class="when">{e(facts)}</div>'
                   + ''.join(f'<div class="meta">⚠ {e(h)}</div>' for h in d['hints']) + extras_html(db, i, shown, cfg, tags) + f'<div class="meta">fit: {e(d["fit_text"])}</div><div class="q">{linked(PF(i["quote"]))}</div><div class="meta">group {i["group_id"]} · {fmt(srcs[0]["ts"])} · {links}</div></div>')
    return ''.join(out)

from .periods import fix as PF      # display-only: 910节 -> 9-10节

def done_btn(i, st):
    """'Mark done' / 'undo' button; the widget script shows it only when the page is served by the local program (it has to write the database)."""
    if st == "cancelled": return ""
    d = st == "done"
    return f'<button type="button" class="cnb-done" data-id="{i["id"]}" data-act="{"undo" if d else "done"}">{"↩ Not done after all" if d else "✔ Mark done"}</button>'

MANUAL_LABEL = "From a page you gave me (not verified by the bot)"
MANUAL_GROUP = -12                                  # S12: user-supplied page copies are stored under this pseudo group
def is_manual(i):
    """S12: an item made only from a page the user supplied (items.source = 'manual'; never a QQ item that merely matches a page)."""
    try: return i["source"] == "manual"
    except (IndexError, KeyError): return False
def group_title(gid): return "From pages you provided" if gid == MANUAL_GROUP else f"Group {gid}"

def card_html(db, i, names, shown, cfg, tags, nt, st):
    """One item card. State word + symbol + strike/dash/border: colour is never the only cue."""
    srcs = db.execute("SELECT m.* FROM item_sources s JOIN msg m ON m.id=s.msg_id WHERE s.item_id=? ORDER BY m.id", (i["id"],)).fetchall(); gid = i["group_id"]
    who = names.get((gid, i["sender"])) or i["sender"]; sc = S.of_item(db, i, cfg, tags)
    links = " ".join(f'<a href="original/{s["id"]}.html">original{" " + str(n + 1) if len(srcs) > 1 else ""}</a>' for n, s in enumerate(srcs))
    mark = f'<span class="st st-{st}">{STATES[st][1]} {e(STATES[st][0])}</span>' if st != "open" else ""
    alert = '<span class="alerted">alerted, not done yet</span> ' if st in ("open", "expired") and nt.get(i["id"]) else ""
    s0 = f' · {fmt(srcs[0]["ts"])}' if srcs else ""
    return (f'<div class="card st-{st}" id="item-{i["id"]}"><div class="t">{mark}<span class="tt">{e(PF(i["title"]))}</span> <small class="sub">· {e(i["type"])}</small><span class="sc sc-{sc}">{e(S.label(sc, cfg))}</span></div>'
        f'<div class="when">{e(when(i))}{" · " + e(i["location"]) if i["location"] else ""} {alert}{"<span class=warn>confirm</span>" if i["needs_confirm"] else ""}</div>'
        + extras_html(db, i, shown, cfg, tags) +
        f'<div class="q">{linked(i["quote"])}</div>' + (f'<div class="meta manual-src">{e(MANUAL_LABEL)}{s0} · {links}</div>' if is_manual(i) else
        f'<div class="meta">group {gid}{s0} · {e(who)} · QQ search: <code>{e(keywords(i["quote"]))}</code> · {links}</div>') + f'{done_btn(i, st)}</div>')

def html(db):
    now = dt.datetime.now(tz()); st = by_state(db, now=now); cfg = S.load(); tags = S.audience_tags(cfg=cfg); nt = notified(db)
    allrows = st["open"] + st["expired"] + st["past"] + st["done"] + st["cancelled"]; shown = {r["id"] for r in allrows}
    cons = [i for i in st["open"] if i['type'] == 'contest']; its = [i for i in st["open"] if i['type'] != 'contest']
    body = [f'<h1>Things you still need to act on</h1><p class="sub">{now:%Y-%m-%d %H:%M} ({e(tz_name())}) · {len(its) + len(cons)} open item(s) · model: {e(model_name())}</p>'
            f'<p class="sub">Key: ○ open · ! expired task or deadline, not marked done (red, crossed out) · ◷ past event (grey) · ✔ done (grey) · ✖ cancelled (crossed out, dashed). Scope: your institute (named in capture/scope.json), School-wide, or Scope unknown (never guessed).</p>', bridge_banner()]
    if not its and not cons: body.append('<p>Nothing open.</p>')
    words = _exam_words(); exams = [i for i in its if is_exam(i, words)]
    ex_ok = [i for i in exams if S.counts_as("exam", S.of_item(db, i, cfg, tags))]; ex_held = [i for i in exams if i not in ex_ok]
    names = {(r["group_id"], r["user_id"]): r["nick"] for r in db.execute("SELECT group_id,user_id,nick FROM admins")} if db.execute("SELECT name FROM sqlite_master WHERE name='admins'").fetchone() else {}
    if ex_ok or ex_held:
        body.append(f"<h2>Exams and tests: {e(S.label(S.INSTITUTE, cfg))} sources only</h2>")
        body += [card_html(db, i, names, shown, cfg, tags, nt, "open") for i in ex_ok]
        if not ex_ok: body.append('<p class="sub">None from a source marked as your institute (capture/scope.json).</p>')
        if ex_held: body.append(f'<p class="sub">{len(ex_held)} exam-like item(s) are listed below with their scope shown: their source is not marked as your institute (capture/scope.json), so they are not counted as your exams.</p>')
    exids = {i["id"] for i in ex_ok}; its = [i for i in its if i["id"] not in exids]
    if cons: body.append(contest_cards(db, cons, shown, cfg, tags))
    for title, sel in (("Priority: leaders and teachers", [i for i in its if i["priority"]]), ("Everything else", [i for i in its if not i["priority"]])):
        if not sel: continue
        body.append(f"<h2>{title}</h2>"); g = defaultdict(list)
        for i in sel: g[i["group_id"]].append(i)
        for gid, rows in g.items():
            body.append(f"<h3>{e(group_title(gid))}</h3>")
            for i in rows: body.append(card_html(db, i, names, shown, cfg, tags, nt, "open"))
    for key, title in (("expired", "Expired, still not marked done"), ("past", "Past events (already held)"), ("done", "Done"), ("cancelled", "Cancelled: not happening")):
        if st[key]:
            body.append(f"<h2>{title}</h2>")
            body += [card_html(db, i, names, shown, cfg, tags, nt, key) for i in st[key]]
            if st["more"].get(key): body.append(f'<p class="sub">{st["more"][key]} more not shown (kept in the database).</p>')
    return page("Campus notices", "".join(body))

def write_all(db, root):
    os.makedirs(os.path.join(root, "original"), exist_ok=True)
    from . import widget as WG                       # S10b: the chat widget rides inside the report pages
    open(os.path.join(root, "latest.html"), "w", encoding="utf-8").write(WG.inject(html(db)))
    st = by_state(db); shown = [r["id"] for k in ("open", "expired", "past", "done", "cancelled") for r in st[k]]     # S6b: a page for every card on the list
    ids = {r[0] for r in db.execute("SELECT DISTINCT msg_id FROM item_sources WHERE item_id IN (%s)" % ",".join("?" * len(shown)), shown)} if shown else set()
    for f in os.listdir(os.path.join(root, "original")): os.remove(os.path.join(root, "original", f))   # only pages for open items kept
    for mid in ids: open(os.path.join(root, "original", f"{mid}.html"), "w", encoding="utf-8").write(original_page(db, mid))
    from . import cal
    open(os.path.join(root, "calendar.html"), "w", encoding="utf-8").write(WG.inject(cal.html(db)))
    open(os.path.join(root, "latest.md"), "w", encoding="utf-8").write(markdown(db)); open(os.path.join(root, "latest.ics"), "w", encoding="utf-8").write(ics(db))


def weekly(db, now=None):
    """Sunday-evening report (PLAN section 4): the next 7 days in When / What / Where order, deadlines first
    within a day, a please-confirm list, and what was dropped as past. Contests keep their own section."""
    now = now or dt.datetime.now(tz()); end = now + dt.timedelta(days=7)
    its = items(db); out = [f"# Week ahead, {now:%Y-%m-%d} to {end:%m-%d}\n"]
    def d_of(i): return _t(i["due"] or i["start"])
    soon = sorted([i for i in its if d_of(i) and d_of(i) <= end and i["type"] != "contest"], key=lambda i: (d_of(i), i["type"] != "deadline"))
    out.append("## Next 7 days\n")
    if not soon: out.append("Nothing dated in the next 7 days.\n")
    for i in soon: out.append(f"- **{when_short(i)}** · {i['title']} · {where_short(i)}  [{i['type']}]" + ("  ⚠ confirm" if i["needs_confirm"] else ""))
    cons = [i for i in its if i["type"] == "contest"]
    if cons:
        out.append("\n## Contests\n")
        for i in cons: out.append(f"- **{when_short(i)}** · {i['title']}")
    later = [i for i in its if d_of(i) and d_of(i) > end and i["type"] != "contest"]
    if later:
        out.append("\n## Later\n")
        for i in sorted(later, key=d_of)[:12]: out.append(f"- **{when_short(i)}** · {i['title']} · {where_short(i)}")
    undated = [i for i in its if not d_of(i) and i["type"] != "interest"]
    if undated:
        out.append("\n## No date yet: please confirm\n")
        for i in undated: out.append(f"- {i['title']} ({i['action'] or 'open the original'})")
    hidden = len(items(db, include_past=True)) - len(its)
    if hidden: out.append(f"\n_{hidden} item(s) already past were left out._")
    return "\n".join(out) + "\n"

def write_weekly(db, now=None):
    now = now or dt.datetime.now(tz()); y, w, _ = now.isocalendar()
    p = os.path.join(ROOT_DIR, "reports", f"weekly-{y}-W{w:02d}.md")
    open(p, "w", encoding="utf-8").write(weekly(db, now)); return p
