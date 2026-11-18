"""S4 weekly report: what is still to do NEXT WEEK (Mon-Sun after the run date, Asia/Shanghai).

Reuses digest.py read-only (dedupe, is_past, when_short, keywords, html helpers, original_page) and contest.describe.
Rules (PLAN: never invent, every item links to its original):
 - only items with status new/confirmed that are not past at run time; done/cancelled/dismissed and past ones are
   counted, not listed
 - an item is "next week" when one of its dates (due, start..end span, contest registration deadline) falls in the window
 - an item with NO parseable date is never guessed: it goes to 'date unknown'
 - items still open but dated before the window (e.g. due Sunday evening) are shown separately, not silently dropped
 - contests get their own section (dated next week, then other open contests)
Output: weekly-<ISO year>-W<ISO week of the TARGET week>.md and .html, plus original/<msg id>.html for every item shown."""
import collections, datetime as dt, json, os, re
from . import digest as G
from . import db as D

Z = G.Z
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OPEN = ("new", "confirmed")
KW_PATH = os.path.join(D.ROOT, "capture", "keywords.json")
# Which tier-A (coursework & exams) words DENOTE an assessment. Words are read from keywords.json at run time; this set
# only filters them (作业/报告/课程 are coursework, not exams). keywords.json may override with "weekly_focus_words".
ASSESS = {"考试", "期中", "期末", "小测", "测验", "quiz", "补考", "重修", "答辩", "exam", "midterm", "final"}
REL = re.compile(r"下{1,2}个?(?:周|星期|礼拜)|本周|这周|下周|明天|后天|大后天")
Due = collections.namedtuple("Due", "week_start week_end md anchor")


def focus_words(path=None):
    """Exam/test words, read from keywords.json now (no restart, nothing cached)."""
    try: kw = json.load(open(path or KW_PATH, encoding="utf-8"))
    except (OSError, ValueError): return []
    if not isinstance(kw, dict): return []          # checker r3: valid JSON of the wrong shape must not crash the report
    ex = kw.get("weekly_focus_words")
    if isinstance(ex, list) and ex: return [w for w in ex if isinstance(w, str) and w]
    tiers = kw.get("tiers") if isinstance(kw.get("tiers"), dict) else {}
    tier = next((v for k, v in tiers.items() if k.lower().startswith("a ") or "exam" in k.lower()), [])
    return [w for w in tier if isinstance(w, str) and w.lower() in ASSESS] if isinstance(tier, list) else []


def is_exam(r, words):
    t = f"{r.get('title') or ''} {r.get('quote') or ''}".lower()
    return r.get("type") != "contest" and any(w.lower() in t for w in words)


def mdq(x):
    """One line of plain text safe to drop into the .md: newlines folded, raw HTML and link syntax neutralised."""
    s = " ".join(str(x if x is not None else "").split())
    for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("[", "\\["), ("]", "\\]"), ("`", "'")): s = s.replace(a, b)
    return s


def window(now=None):
    """(monday 00:00, next monday 00:00) of the week AFTER now's calendar date, in Asia/Shanghai.
    Run on a Sunday evening -> the coming Mon-Sun. Run on a Monday -> a week from that Monday."""
    now = (now or dt.datetime.now(Z))
    now = now.astimezone(Z) if now.tzinfo else now.replace(tzinfo=Z)
    mon = (now + dt.timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return mon, mon + dt.timedelta(days=7)


def paths(now=None, root=None):
    """(md path, html path) for the target week. Named by the ISO week of the window, not of the run date.
    (now = any time on the run date; for a catch-up pass the Sunday anchor from due().)"""
    y, w, _ = window(now)[0].isocalendar(); r = root or os.path.join(D.ROOT, "reports")
    return os.path.join(r, f"weekly-{y}-W{w:02d}.md"), os.path.join(r, f"weekly-{y}-W{w:02d}.html")


def due(now=None, reports_dir=None):
    """Catch-up rule. Due when now >= the most recent Sunday 18:00 (Asia/Shanghai) AND the Mon-Sun week after that Sunday has
    no weekly .md yet. Returns Due(week_start, week_end, md, anchor) or None. Several missed Sundays -> only the most recent."""
    now = now or dt.datetime.now(Z)
    now = now.astimezone(Z) if now.tzinfo else now.replace(tzinfo=Z)
    s = (now - dt.timedelta(days=(now.weekday() + 1) % 7)).replace(hour=18, minute=0, second=0, microsecond=0)
    if s > now: s -= dt.timedelta(days=7)
    a, b = window(s); md = paths(s, reports_dir)[0]
    # checker r3: an empty .md (crashed/interrupted write) does not count as written
    return None if os.path.exists(md) and os.path.getsize(md) > 0 else Due(a, b, md, s)


def ensure(db, now=None, reports_dir=None):
    """Write the weekly report if due() says so; return its md path, or None when nothing was due (so safe to call daily)."""
    now = now or dt.datetime.now(Z); d = due(now, reports_dir)
    if not d: return None
    return write_weekly(db, now, reports_dir, anchor=d.anchor)[0]


def _pdt(v):
    """parse a stored date; date-only stays date-only (second value True). None when absent or unparseable."""
    if not v or not isinstance(v, str): return None, False
    d = G._t(v.strip())
    if d: d = d.astimezone(Z)          # checker fix: a non-+08:00 stamp must be grouped/shown on its China-time day
    return d, bool(d and len(v.strip()) == 10)


def _row(it):
    """Plain dict of the item; a date-only due ('2026-11-15') means end of that day, not 00:00 (is_past would
    otherwise call a deadline over on its own morning)."""
    r = dict(it)
    d, only = _pdt(r.get("due"))
    if only: r["due"] = d.strftime("%Y-%m-%d") + "T23:59:00+08:00"
    return r


def _extra(r):
    try: x = json.loads(r.get("extra") or "null")
    except ValueError: x = None
    return x if isinstance(x, dict) else {}


def _points(r):
    """-> list of (datetime, kind) single dates, and (start, end) span or None. Kinds: due | reg | start | end."""
    pts = []
    for k, kind in (("due", "due"), ("start", "start"), ("end", "end")):
        d, _ = _pdt(r.get(k))
        if d: pts.append((d, kind))
    d, _ = _pdt(_extra(r).get("reg_deadline"))
    if d: pts.append((d, "reg"))
    s, _ = _pdt(r.get("start")); e, eo = _pdt(r.get("end"))
    span = None
    if s and e and e > s:
        span = (s, e + dt.timedelta(days=1) if (e.hour == 0 and e.minute == 0) else e)
    return pts, span


def place(r, win):
    """Classify one open, not-past row: ('week', datetime, label) | ('before', datetime) | ('later', datetime) | ('unknown',)."""
    pts, span = _points(r)
    if not pts: return ("unknown",)
    a, b = win
    inw = sorted((p for p in pts if a <= p[0] < b), key=lambda p: (p[0], p[1] != "reg"))
    cand = []
    if inw: cand.append((inw[0][0], {"due": "due", "reg": "register by", "start": "", "end": "ends"}[inw[0][1]]))
    if span and span[0] < b and span[1] > a and not any(span[0] == c[0] for c in cand):
        if span[0] >= a: cand.append((span[0], ""))
        else: cand.append((a, "ongoing since " + f"{span[0].month}.{span[0].day}"))
    if cand:
        cand.sort(key=lambda c: c[0]); return ("week", cand[0][0], cand[0][1])
    before = [p[0] for p in pts if p[0] < a]; after = [p[0] for p in pts if p[0] >= b]
    # not past + nothing in the window: either still open before it, or only later
    if before and (not span or span[1] <= a): return ("before", max(before))
    return ("later", min(after)) if after else ("before", max(before))


def collect(db, now=None, anchor=None):
    """The data behind the report, no rendering. Counts say what was left out and why."""
    now = now or dt.datetime.now(Z)
    now = now.astimezone(Z) if now.tzinfo else now.replace(tzinfo=Z)
    win = window(anchor or now)
    allrows = [dict(r) for r in db.execute("SELECT * FROM items ORDER BY priority DESC, COALESCE(due,start,'9999'), id")]
    openrows = [r for r in allrows if r["status"] in OPEN]
    n_closed = len(allrows) - len(openrows)
    folded = G.dedupe([_row(r) for r in openrows])
    n_dup = len(openrows) - len(folded)
    live = []; n_past = 0
    for r in folded:
        if G.is_past(r, now): n_past += 1
        else: live.append(r)
    res = {"now": now, "win": win, "days": {}, "before": [], "unknown": [], "contests_week": [], "contests_other": [],
           "interest": 0, "later": 0, "exams": [], "focus": focus_words(), "counts": {"past": n_past, "closed": n_closed, "dup": n_dup}}
    for r in live:
        p = place(r, win)
        if r["type"] == "contest":
            if p[0] == "week": res["contests_week"].append((p[1], p[2], r))
            elif p[0] != "later": res["contests_other"].append((p[1] if len(p) > 1 else None, r))
            else: res["contests_other"].append((p[1], r))
        elif p[0] == "week": res["days"].setdefault(p[1].date(), []).append((p[1], p[2], r))
        elif p[0] == "before": res["before"].append((p[1], r))
        elif p[0] == "later": res["later"] += 1
        elif r["type"] == "interest": res["interest"] += 1
        else: res["unknown"].append(r)
    # exam focus: pull assessment items out of the normal sections (no duplication); undated ones stay 'unknown'
    fw = res["focus"]
    if fw:
        for d in list(res["days"]):
            keep = []
            for t, lab, r in res["days"][d]:
                if is_exam(r, fw): res["exams"].append((t, lab, r, "week"))
                else: keep.append((t, lab, r))
            if keep: res["days"][d] = keep
            else: del res["days"][d]
        for key, kind in (("before", "before"),):
            keep = []
            for t, r in res[key]:
                if is_exam(r, fw): res["exams"].append((t, "", r, kind))
                else: keep.append((t, r))
            res[key] = keep
        keep = []
        for r in res["unknown"]:
            if is_exam(r, fw): res["exams"].append((None, "", r, "unknown"))
            else: keep.append(r)
        res["unknown"] = keep
        res["exams"].sort(key=lambda x: (x[3] == "unknown", x[0] is None, x[0] or now, x[2]["id"]))
    for v in res["days"].values(): v.sort(key=lambda x: (x[0], x[2]["type"] != "deadline", x[2]["id"]))
    res["before"].sort(key=lambda x: x[0]); res["contests_week"].sort(key=lambda x: x[0])
    res["contests_other"].sort(key=lambda x: (x[0] is None, x[0] or 0))
    return res


def _sources(db, r):
    return db.execute("SELECT m.* FROM item_sources s JOIN msg m ON m.id=s.msg_id WHERE s.item_id=? ORDER BY m.id", (r["id"],)).fetchall()


def _label(extra_label):
    return f" ({extra_label})" if extra_label else ""


def _when(r, lab="", t=None):
    """Display time. For an item placed in the window by a date that is not its start (a due/registration date),
    show THAT date, so 'register by 11.19 12:00' is never printed as the contest date 11.29."""
    if t is not None and lab in ("due", "register by", "ends"):
        w = f"{t.month}.{t.day}" + ("" if (t.hour, t.minute) == (0, 0) else f" {t:%H:%M}")
        return w + (f" ({lab})" if not (lab == "due" and r["type"] == "deadline") else "")
    w = G.when_short(r)
    if w == "time unknown":
        d, _ = _pdt(_extra(r).get("reg_deadline"))
        if d: w = f"{d.month}.{d.day}" + ("" if (d.hour, d.minute) == (0, 0) else f" {d:%H:%M}") + " (register by)"
    return w + _label(lab)


def _dayhead(d): return f"{DAYS[d.weekday()]} {d.month}.{d.day}"


def _contest_facts(db, r):
    from . import contest as C
    try: d = C.describe(r, db)
    except Exception: return None, "", "details unreadable"
    x = d["extra"]
    badge = {"yes": "on school list", "no": "NOT on school list", "unknown": "school list not loaded"}[d["recognition"]]
    rd, _ = _pdt(x.get("reg_deadline"))
    reg = (f"{rd.month}.{rd.day}" + ("" if (rd.hour, rd.minute) == (0, 0) else f" {rd:%H:%M}")) if rd else x.get("reg_deadline")
    facts = [f"register by {reg}" if reg else "registration deadline unknown",
             f"team: {x['team_size']}" if x.get("team_size") else "", f"organiser: {x['organiser']}" if x.get("organiser") else ""]
    return d, badge, " · ".join(f for f in facts if f)


def _closed_hint(r, now):
    """Registration already closed but the contest is still open: say so (digest.is_past keeps it listed)."""
    d, only = _pdt(_extra(r).get("reg_deadline"))
    if not d: return ""
    end = d + dt.timedelta(days=1) if only else d   # checker r3: a date-only deadline is open all of that day
    return f"registration closed on {d.month}.{d.day}" if end < now else ""


def _srcnote(db, r):
    """'source message date' when the date may have come from relative wording (下周二 ...): the TA can judge staleness."""
    srcs = _sources(db, r)
    if not srcs: return ""
    if REL.search(f"{r.get('quote') or ''} {srcs[0]['text'] or ''}"):
        return f"source message sent {G.fmt(srcs[0]['ts'])}; date may come from relative wording (下周/明天 ...), check it is not stale"
    return ""


def _summary_line(res):
    c = res["counts"]; bits = []
    if c["past"]: bits.append(f"{c['past']} already past")
    if c["closed"]: bits.append(f"{c['closed']} marked done/cancelled/dismissed")
    if c["dup"]: bits.append(f"{c['dup']} repeat announcement(s) folded into another item")
    if res["later"]: bits.append(f"{res['later']} dated after next week")
    if res["interest"]: bits.append(f"{res['interest']} undated interest thread(s) (see latest.html)")
    return "Left out: " + ("; ".join(bits) if bits else "nothing") + "."


def _title(res):
    a, b = res["win"]; return f"Next week: {a:%Y-%m-%d} (Mon) to {(b - dt.timedelta(days=1)):%Y-%m-%d} (Sun)"


def markdown(db, now=None, res=None):
    res = res or collect(db, now); out = [f"# {_title(res)}\n", f"_Generated {res['now']:%Y-%m-%d %H:%M} (Asia/Shanghai). Links go to the original-view pages (original/<id>.html)._\n"]
    def line(r, when):
        srcs = _sources(db, r); s0 = srcs[0] if srcs else None
        links = " ".join(f"[original{' ' + str(n + 1) if len(srcs) > 1 else ''}](original/{s['id']}.html)" for n, s in enumerate(srcs))
        ref = f"group {r['group_id']} · {G.fmt(s0['ts'])} · search QQ: `{mdq(G.keywords(r['quote']))}` · {links}" if s0 else "no source found"
        todo = f"  \n  to do: {mdq(r['action'])}" if r.get("action") else ""
        sn = _srcnote(db, r); sn = f"  \n  ⓘ {sn}" if sn else ""
        return (f"- **{mdq(when)}** · {mdq(r['title'])} · {mdq(G.where_short(r))}  [{mdq(r['type'])}]" + ("  ⚠ confirm" if r["needs_confirm"] else "") + todo + sn + f"  \n  {ref}")
    out.append("## Exams & tests\n")
    if not res["focus"]: out.append("No exam words configured (keywords.json tier A).\n")
    elif res["exams"]:
        out.append(f"Matched on: {mdq(' '.join(res['focus']))}. Missing date or place stays 'unknown'.\n")
        for t, lab, r, kind in res["exams"]:
            when = _when(r, lab, t) if kind == "week" else (_when(r) if kind == "before" else "date unknown")
            if kind == "before": when += " (earlier, still open)"
            out.append(line(r, when) + ("" if r.get("location") else "  \n  place: unknown"))
        out.append("")
    else: out.append("No exam or test items found for next week.\n")
    if not (res["days"] or res["contests_week"] or res["exams"]): out.append("Nothing dated next week.\n")
    for d in sorted(res["days"]):
        out.append(f"## {_dayhead(d)}\n")
        for t, lab, r in res["days"][d]: out.append(line(r, _when(r, lab, t)))
        out.append("")
    out.append("## Contests\n")
    def cline(r, lead):
        d, badge, facts = _contest_facts(db, r); srcs = _sources(db, r)
        links = " ".join(f"[original{' ' + str(n + 1) if len(srcs) > 1 else ''}](original/{s['id']}.html)" for n, s in enumerate(srcs))
        if d is None: return f"- **{mdq(lead)}** · {mdq(r['title'])} · {facts}  \n  group {r['group_id']} · {links}"
        hints = "".join(f"  \n  ⚠ {mdq(h)}" for h in _hints(d, r, res))
        return f"- **{mdq(lead)}** · {mdq(d['extra'].get('name') or r['title'])} [{badge}] · {mdq(facts)}  \n  fit: {mdq(d['fit_text'])}{hints}  \n  group {r['group_id']} · {links}"
    if res["contests_week"]:
        out.append("Dated next week:\n")
        for t, lab, r in res["contests_week"]: out.append(cline(r, _cwhen(r, lab, t)))
    else: out.append("No contest date or deadline falls next week.\n")
    if res["contests_other"]:
        out.append("\nOther open contests (not dated next week):\n")
        for t, r in res["contests_other"]: out.append(cline(r, _when(r) if t else "date unknown"))
    if res["before"]:
        out.append("\n## Still open from before the week (dated earlier, not yet past)\n")
        for t, r in res["before"]: out.append(line(r, _when(r)))
    out.append("\n## Date unknown: please confirm (no date is guessed)\n")
    if res["unknown"]:
        for r in res["unknown"]: out.append(line(r, "date unknown"))
    else: out.append("None.")
    out.append("\n" + _summary_line(res))
    return "\n".join(out) + "\n"


def _hints(d, r, res):
    h = list(d["hints"]); c = _closed_hint(r, res["now"])
    return h + [c] if c else h


def _cwhen(r, lab, t):
    """Contest lead time: the date only; 'register by' is already in the facts, so it is not printed twice."""
    w = _when(r, lab, t)
    return w.replace(" (register by)", "") if lab == "register by" else w


def html(db, now=None, res=None):
    res = res or collect(db, now); E = G.e; body = [f"<h1>{E(_title(res))}</h1><p class=\"sub\">Generated {res['now']:%Y-%m-%d %H:%M} (Asia/Shanghai)</p>", G.bridge_banner()]
    def card(r, when):
        srcs = _sources(db, r); s0 = srcs[0] if srcs else None
        links = " ".join(f'<a href="original/{s["id"]}.html">original{" " + str(n + 1) if len(srcs) > 1 else ""}</a>' for n, s in enumerate(srcs))
        meta = f'group {r["group_id"]} · {G.fmt(s0["ts"])} · QQ search: <code>{E(G.keywords(r["quote"]))}</code> · {links}' if s0 else "no source found"
        todo = f'<div class="meta">to do: {E(r["action"])}</div>' if r.get("action") else ""
        sn = _srcnote(db, r); todo += f'<div class="meta">ⓘ {E(sn)}</div>' if sn else ""
        return (f'<div class="card"><div class="t">{E(r["title"])} <small class="sub">· {E(r["type"])}</small></div>'
                f'<div class="when">{E(when)}{" · " + E(r["location"]) if r.get("location") else ""} {"<span class=warn>confirm</span>" if r["needs_confirm"] else ""}</div>{todo}'
                f'<div class="q">{G.linked(r["quote"])}</div><div class="meta">{meta}</div></div>')
    body.append("<h2>Exams &amp; tests</h2>")
    if not res["focus"]: body.append("<p>No exam words configured (keywords.json tier A).</p>")
    elif res["exams"]:
        body.append(f'<p class="sub">Matched on: {E(" ".join(res["focus"]))}. Missing date or place stays unknown.</p>')
        for t, lab, r, kind in res["exams"]:
            when = _when(r, lab, t) if kind == "week" else (_when(r) + " (earlier, still open)" if kind == "before" else "date unknown")
            body.append(card(r, when + ("" if r.get("location") else " · place unknown")))
    else: body.append("<p>No exam or test items found for next week.</p>")
    if not (res["days"] or res["contests_week"] or res["exams"]): body.append("<p>Nothing dated next week.</p>")
    for d in sorted(res["days"]):
        body.append(f"<h2>{E(_dayhead(d))}</h2>" + "".join(card(r, _when(r, lab, t)) for t, lab, r in res["days"][d]))
    body.append("<h2>Contests</h2>")
    def ccard(r, lead):
        d, badge, facts = _contest_facts(db, r); srcs = _sources(db, r)
        links = " ".join(f'<a href="original/{s["id"]}.html">original{" " + str(n + 1) if len(srcs) > 1 else ""}</a>' for n, s in enumerate(srcs))
        if d is None: return f'<div class="card"><div class="t">{E(r["title"])}</div><div class="when">{E(facts)}</div><div class="q">{G.linked(r["quote"])}</div><div class="meta">group {r["group_id"]} · {links}</div></div>'
        return (f'<div class="card"><div class="t">{E(d["extra"].get("name") or r["title"])} <span class="warn">{E(badge)}</span></div><div class="when">{E(lead)} · {E(facts)}</div>'
                + "".join(f'<div class="meta">⚠ {E(h)}</div>' for h in _hints(d, r, res)) + f'<div class="meta">fit: {E(d["fit_text"])}</div><div class="q">{G.linked(r["quote"])}</div><div class="meta">group {r["group_id"]} · {links}</div></div>')
    if res["contests_week"]: body.append("<h3>Dated next week</h3>" + "".join(ccard(r, _cwhen(r, lab, t)) for t, lab, r in res["contests_week"]))
    else: body.append("<p>No contest date or deadline falls next week.</p>")
    if res["contests_other"]: body.append("<h3>Other open contests (not dated next week)</h3>" + "".join(ccard(r, _when(r) if t else "date unknown") for t, r in res["contests_other"]))
    if res["before"]: body.append("<h2>Still open from before the week</h2><p class=\"sub\">Dated earlier, not yet past.</p>" + "".join(card(r, _when(r)) for t, r in res["before"]))
    body.append("<h2>Date unknown: please confirm</h2><p class=\"sub\">No date is guessed.</p>")
    body.append("".join(card(r, "date unknown") for r in res["unknown"]) or "<p>None.</p>")
    body.append(f'<p class="sub">{E(_summary_line(res))}</p>')
    return G.page(_title(res), "".join(body))


def shown_msg_ids(db, res):
    rows = [x[2] for x in res["exams"]] + [r for v in res["days"].values() for _, _, r in v] + [r for _, _, r in res["contests_week"]] + [r for _, r in res["contests_other"]] \
        + [r for _, r in res["before"]] + res["unknown"]
    ids = []
    for r in rows:
        for s in _sources(db, r):
            if s["id"] not in ids: ids.append(s["id"])
    return ids


def write_weekly(db, now=None, root=None, anchor=None):
    """Writes md + html (+ any missing original/<id>.html) into root (default <project>/reports). Returns (md, html).
    anchor = the Sunday that defines the target week (catch-up); default: derived from now.
    New original pages get their 'back' link pointed at this weekly report (existing ones are left alone)."""
    res = collect(db, now, anchor); pm, ph = paths(anchor or now, root); root = os.path.dirname(pm)
    os.makedirs(os.path.join(root, "original"), exist_ok=True)
    for mid in shown_msg_ids(db, res):
        p = os.path.join(root, "original", f"{mid}.html")
        if not os.path.exists(p): open(p, "w", encoding="utf-8").write(G.original_page(db, mid).replace("../latest.html", "../" + os.path.basename(ph)))
    # checker r3: render BOTH first, then replace atomically, html before md (the .md is what due() checks). Before, open(pm,"w")
    # ran before markdown() rendered, so a render crash left an empty .md and that week was never retried.
    mtxt, htxt = markdown(db, res=res), html(db, res=res)
    for p, txt in ((ph, htxt), (pm, mtxt)):
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f: f.write(txt)
        os.replace(tmp, p)
    return pm, ph
