import datetime as dt, argparse, getpass, json, os, re, sys
from . import db as D, extract, digest
GROUPS = os.path.join(D.ROOT, "capture", "groups.json")
HOOKS = []     # [(subcommand, handler(args, db))]; empty here, filled by stages that add commands
def _wl(verb):                                   # whitelist versions (S2b): cnb wl-versions|wl-show|wl-confirm|wl-reject <school> [version]
    def run(a, db):
        from . import wlversions
        return wlversions.main([a.school, verb] + ([a.version] if getattr(a, "version", None) else []))
    return run
def _wl_args(with_version):
    def add(sp):
        sp.add_argument("school")
        if with_version: sp.add_argument("version")
    return add
def _page(a, db):                                # S12: pages the bot could not open; the user approves and supplies them (nothing is fetched)
    from . import manual
    code = manual.cli_run(a, db)
    if code: raise SystemExit(code)
def _page_args(sp):
    from . import manual
    manual.add_cli_args(sp)
HOOKS += [("page", _page, _page_args)]
HOOKS += [("wl-versions", _wl("list"), _wl_args(False)), ("wl-show", _wl("show"), _wl_args(True)),
          ("wl-confirm", _wl("confirm"), _wl_args(True)), ("wl-reject", _wl("reject"), _wl_args(True))]

def priority(db):
    c = json.load(open(GROUPS)); names = {}
    if db.execute("SELECT name FROM sqlite_master WHERE name='admins'").fetchone():
        names = {(r["group_id"], r["user_id"]): r["role"] + " " + (r["nick"] or "") for r in db.execute("SELECT * FROM admins")}
    ign = set(c.get("ignore_users", []))
    for g in c["groups"]:
        ids = sorted(set(c["priority_by_group"].get(str(g), [])) | set(c.get("priority_members", [])) - ign)
        print(f"Group {g}  ({len(ids)} priority)")
        for u in sorted(ids, key=lambda u: (0 if names.get((g, u), "").startswith("owner") else 1, u)):
            print(f"   {u}  {names.get((g, u), 'manual')}")

def _sweep_manual(db):
    """S12: items made from a page the user supplied are marked needs_confirm / source manual. Never breaks the run."""
    try:
        from . import manual
        manual.sweep(db)
    except Exception: pass

OCR_STATUS = os.path.join(D.ROOT, "capture", "ocr_status.json")
def _ocr_days(st):
    """Days ocr_log is kept, the same rule as capture/ocr.log_days(): env CNB_OCR_LOG_DAYS, config.json ocr_log_days, 30."""
    v = os.environ.get("CNB_OCR_LOG_DAYS")
    if v is None or not v.strip():
        try:
            with open(os.path.join(D.ROOT, "config.json"), encoding="utf-8") as f: v = json.load(f).get("ocr_log_days")
        except (OSError, ValueError, AttributeError): v = None
    if v is None or (isinstance(v, str) and not v.strip()): return 30
    try: n = int(str(v).strip())
    except ValueError: return 30
    return n if n >= 0 else 30

def ocr_status(db, path=None):
    """S8b: picture-reading (OCR) counters for `cnb status`, from capture/ocr_status.json and the ocr_log table.
    Read only; prints counts, reasons and times, never picture text."""
    try:
        with open(path or OCR_STATUS, encoding="utf-8") as f: st = json.load(f)
        if not isinstance(st, dict): st = {}
    except (OSError, ValueError): st = {}
    c = st.get("counts") or {}
    if not c and not st:
        out = ["pictures (OCR): no picture read yet"]
    else:
        fr = st.get("fail_reasons") or {}
        why = ", ".join(f"{k} {v}" for k, v in sorted(fr.items(), key=lambda x: -x[1])) if fr else ""
        out = [f"pictures (OCR): {c.get('ok', 0)} read ({c.get('low_conf', 0)} low confidence) | {c.get('no_text', 0)} no text"
               f" | {c.get('failed', 0)} failed" + (f" ({why})" if why else "")
               + ("" if st.get("enabled", True) else " | OCR TURNED OFF (CNB_OCR=0)")
               + ("" if st.get("tesseract", True) else " | tesseract NOT FOUND")]
        lf = st.get("last_failure") or {}
        if lf.get("at"):
            out.append(f"  last failure: {digest.fmt(lf['at'])} ({lf.get('reason') or 'unknown'})")
    days = _ocr_days(st)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_log'").fetchone():
        n, old = db.execute("SELECT count(*), min(coalesce(last_ts, first_ts, msg_ts)) FROM ocr_log").fetchone()
        line = f"  ocr_log: {n} row(s), kept {days} days" if days else f"  ocr_log: {n} row(s), kept forever (ocr_log_days 0)"
        if old: line += f", oldest {digest.fmt(old)}"
    else:
        line = "  ocr_log: no table yet"
    p = st.get("prune") or {}
    if p.get("at"): line += f" | last prune {digest.fmt(p['at'])}: {p.get('rows', 0)} row(s) removed"
    out.append(line)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='held'").fetchone():
        k = db.execute("SELECT count(*) FROM held WHERE state='held' AND text LIKE '[image text (OCR: photo only%'").fetchone()[0]
        if k: out.append(f"  {k} text-free photo post(s) from priority senders held for a look (cnb review)")
    return out

def _day(s, name):
    """ISO date YYYY-MM-DD -> date, or raise ValueError with a message that never echoes more than the option name."""
    try:
        if not isinstance(s, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s): raise ValueError
        return dt.date.fromisoformat(s)
    except ValueError: raise ValueError(f"{name}: expected a date like 2026-11-11") from None

def parse_query(text):
    """'/search' style text -> dict. Tokens group:ID sender:X from:DATE to:DATE on:DATE type:T are pulled out, the rest is the keyword text.
    Used by `cnb search` and reusable by a QQ DM. No LLM."""
    o = {"text": []}
    for t in (text or "").split():
        m = re.fullmatch(r"(group|sender|from|to|on|type):(.+)", t, re.I)
        if m: o[m.group(1).lower()] = m.group(2)
        else: o["text"].append(t)
    o["text"] = " ".join(o["text"]); return o

def day_bounds(frm, to, tzname):
    """Inclusive local dates -> [start_ts, end_ts) epoch seconds in the tenant's timezone (a day is that zone's calendar day)."""
    if not frm and not to: return None, None     # checker fix: a search without dates needs no timezone data at all
    z = _zone(tzname); f = lambda d: int(dt.datetime(d.year, d.month, d.day, tzinfo=z).timestamp())
    try: return (f(frm) if frm else None), (f(to + dt.timedelta(days=1)) if to else None)
    except (OverflowError, OSError): raise ValueError("date out of range") from None     # checker fix: --to 9999-12-31 was a traceback

def _zone(tzname):
    """checker fix: tzinfo for an IANA name. Without a timezone database (no system tzdata, no tzdata package) every search
    crashed, also plain keyword search. Asia/Shanghai is a fixed UTC+8 (no DST since 1991), so it keeps working; any other
    unknown/unavailable zone is a clean ValueError."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tzname)
    except Exception:
        if tzname == "Asia/Shanghai": return dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
        raise ValueError("timezone data for this zone is not available on this computer") from None

def search(db, text="", on=None, frm=None, to=None, group=None, sender=None, priority=False, items=False, tz=None, limit=20):
    """Search stored messages (or, with items=True, extracted items) by keyword and/or date and/or group/sender. Pure SQL/FTS5, per tenant (one DB).
    Dates are ISO strings, inclusive, in the tenant timezone. Returns rows; raises ValueError (clean message) on a bad date/range."""
    from . import onboard
    tz = tz or onboard.tz_name()
    if on and (frm or to): raise ValueError("use --on alone, or --from/--to, not both")
    if on: d1 = d2 = _day(on, "on")
    else: d1 = _day(frm, "from") if frm else None; d2 = _day(to, "to") if to else None
    if d1 and d2 and d1 > d2: raise ValueError("from is after to")
    text = (text or "").replace("\x00", "").strip(); sender = str(sender).strip() if sender not in (None, "") else None
    # checker fixes: a NUL in the text broke the FTS query; a huge group/sender number or a non-ASCII "digit" (e.g. a superscript 2) was a
    # traceback / raw int() message; a negative or non-numeric limit returned everything / crashed
    if group is not None and not (isinstance(group, int) and -2**63 <= group < 2**63): raise ValueError("group: expected a group number")
    numeric = bool(sender) and sender.isascii() and sender.isdigit() and len(sender) <= 18
    if not isinstance(limit, int) or isinstance(limit, bool): raise ValueError("limit: expected a number")
    limit = max(1, min(limit, 500))
    esc = lambda t: "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if items:
        w, a = [], []
        if text: w.append("(title LIKE ? ESCAPE '\\' OR quote LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\')"); a += [esc(text)] * 3
        if group is not None: w.append("group_id=?"); a.append(group)
        if numeric: w.append("sender=?"); a.append(int(sender))
        elif sender: return []                       # items store a numeric sender only
        if priority: w.append("priority=1")
        if d1 or d2:    # the item's own date (start/due, or a start..end span) falls in the range; ISO text compares as text
            lo, hi = (d1.isoformat() if d1 else "0000-00-00"), (d2.isoformat() if d2 else "9999-99-99")
            w.append("(substr(COALESCE(start,due),1,10) BETWEEN ? AND ? OR substr(due,1,10) BETWEEN ? AND ?"
                     " OR (start IS NOT NULL AND substr(start,1,10)<=? AND substr(COALESCE(end,start),1,10)>=?))"); a += [lo, hi, lo, hi, hi, lo]
        return db.execute("SELECT * FROM items" + (" WHERE " + " AND ".join(w) if w else "") + " ORDER BY COALESCE(start,due,'') DESC, id DESC LIMIT ?", a + [limit]).fetchall()
    w, a = [], []
    if group is not None: w.append("m.group_id=?"); a.append(group)
    if priority: w.append("m.priority=1")
    if sender:
        if numeric: w.append("m.user_id=?"); a.append(int(sender))
        else: w.append("m.nick LIKE ? ESCAPE '\\'"); a.append(esc(sender))
    lo, hi = day_bounds(d1, d2, tz)
    if lo is not None: w.append("m.ts>=?"); a.append(lo)
    if hi is not None: w.append("m.ts<?"); a.append(hi)
    tail = (" AND " + " AND ".join(w) if w else "") + " ORDER BY m.ts DESC LIMIT ?"
    if len(text) >= 3: return db.execute("SELECT m.* FROM msg_fts f JOIN msg m ON m.id=f.rowid WHERE msg_fts MATCH ?" + tail, ['"' + text.replace('"', '""') + '"'] + a + [limit]).fetchall()
    return db.execute("SELECT m.* FROM msg m WHERE " + ("m.text LIKE ? ESCAPE '\\'" if text else "1=1") + tail, ([esc(text)] if text else []) + a + [limit]).fetchall()

def _tzfmt(ts, tzname):
    return dt.datetime.fromtimestamp(ts, _zone(tzname)).strftime("%Y-%m-%d %H:%M")

def main(argv=None):
    p = argparse.ArgumentParser(prog="cnb"); s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("contests"); s.add_parser("spam"); w = s.add_parser("whitelist"); w.add_argument("name"); w.add_argument("--alias", action="append", default=[]); w.add_argument("--organiser"); w.add_argument("--level"); w.add_argument("--credit"); s.add_parser("priority"); s.add_parser("process"); s.add_parser("status"); s.add_parser("daily"); s.add_parser("weekly"); s.add_parser("review"); s.add_parser("held-count"); s.add_parser("notify-test")
    rc = s.add_parser("research"); rc.add_argument("question"); rc.add_argument("--item", type=int); rc.add_argument("--verbose", action="store_true")
    ck = s.add_parser("checkurl"); ck.add_argument("url")
    s.add_parser("profile"); iv = s.add_parser("interview"); iv.add_argument("--area", action="append")
    fg = s.add_parser("forget"); fg.add_argument("key")
    cf = s.add_parser("confirm"); cf.add_argument("key")
    tl = s.add_parser("tell"); tl.add_argument("text", help="something you want the bot to know, in your own words")
    ch = s.add_parser("chat"); ch.add_argument("text", help="one chat message to the bot (S2d test entry; the QQ adapter calls cnb.chat.turn)"); ch.add_argument("--channel", default="cli")
    tk = s.add_parser("talk", help="chat with the bot (local page on 127.0.0.1, or --terminal): ask for clarification and advice, build your level profile"); tk.add_argument("talk_args", nargs=argparse.REMAINDER, help="--terminal | --no-llm | --db PATH | --port N | --open")
    r = s.add_parser("report"); r.add_argument("--write", action="store_true")
    q = s.add_parser("search", help="keyword and/or date search over stored messages (no LLM); tokens like from:2026-11-01 to:2026-11-07 group:123 are understood in the text")
    q.add_argument("text", nargs="?", default=""); q.add_argument("--group", type=int); q.add_argument("--priority", action="store_true")
    q.add_argument("--on", help="one day, YYYY-MM-DD"); q.add_argument("--from", dest="frm", help="first day, YYYY-MM-DD"); q.add_argument("--to", help="last day (inclusive), YYYY-MM-DD")
    q.add_argument("--sender", help="QQ number, or part of the nickname"); q.add_argument("--items", action="store_true", help="search extracted items (by their own date) instead of raw messages")
    q.add_argument("--limit", type=int, default=20, help="how many results to show (default 20, at most 500)")     # checker fix: a busy day showed 20 rows with no way to see the rest
    ob = s.add_parser("onboard", help="first-run setup beyond the key: model, timezone, school, groups to watch (resumable)"); ob.add_argument("--redo", action="append", default=[]); ob.add_argument("--status", action="store_true")
    kk = s.add_parser("key", help="API key onboarding: status | set | remove | test (key is never shown)"); kk.add_argument("action", nargs="?", default="status", choices=["status", "set", "remove", "test"]); kk.add_argument("--stdin", action="store_true", help="read the key from stdin, not a hidden prompt")
    st = s.add_parser("setup", help="first-run setup: API key, model, timezone, school, groups (same as `cnb onboard`)"); st.add_argument("--redo", action="append", default=[]); st.add_argument("--status", action="store_true")
    # EXTENSION HOOKS. An entry is (name, handler(args, db)) or (name, handler(args, db), add_args(parser)). checker fix: an entry in HOOKS
    # alone was rejected by argparse ("invalid choice"), so each hook now gets its parser here; a name that already exists is left alone.
    for h in HOOKS:
        if h[0] not in s.choices:
            hp = s.add_parser(h[0])
            if len(h) > 2 and h[2]: h[2](hp)
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("key", "setup", "onboard"):       # checker fix: argparse echoes a stray argument in its error text, so a key typed
        allowed = {"-h", "--help"} | ({"status", "set", "remove", "test", "--stdin"} if argv[0] == "key" else {"--status", "--redo", "key", "model", "tz", "school", "groups"})   # on the command line landed in stderr/logs
        if any(x not in allowed for x in argv[1:]):
            print(f"cnb {argv[0]}: unexpected argument (not shown). Usage: cnb key [status|set|remove|test] [--stdin] | cnb setup [--status] [--redo STEP]\n"
                  "The key is never given on the command line. If you just typed it there, it is now in your shell history: rotate it.", file=sys.stderr)
            raise SystemExit(2)
    _argv = list(sys.argv[1:] if argv is None else argv)
    if _argv[:1] == ["talk"] and not ({"-h", "--help"} & set(_argv[1:2])):   # pass every flag through to tools/talk.py
        _argv = ["talk"]
        a = p.parse_args(_argv); a.talk_args = list(sys.argv[2:] if argv is None else argv[1:])
    else:
        a = p.parse_args(argv)
    if a.cmd == "key":                           # needs no database
        from . import keysetup as K
        K.run(a); return
    if a.cmd in ("setup", "onboard"):
        from . import keysetup as K, onboard as OB
        if any(r not in OB.STEPS for r in a.redo): print("--redo takes one of: " + ", ".join(OB.STEPS), file=sys.stderr); raise SystemExit(2)
        if not a.status:
            try: odb = D.connect()
            except Exception: odb = None
            OB.run(lambda q: input(q + "\n> "), ask_secret=getpass.getpass, redo=a.redo, db=odb)
        OB.summary(); n = K.perm_note()
        if n and not a.status: print(n)
        return
    for h in HOOKS:                              # extension point: other stages register (name, handler(args, db)) in HOOKS
        if a.cmd == h[0]: return h[1](a, D.connect())
    db = D.connect()
    if a.cmd == "priority": priority(db)
    elif a.cmd == "checkurl":
        from . import web
        for k, v in web.check(a.url).items(): print(f"  {k:14} {v}")
    elif a.cmd == "research":
        from . import research as RS
        out = RS.research(db, a.question, verbose=a.verbose)
        f = out["finding"]
        if not f:
            print("no structured finding." , out.get("note") or ""); print((out.get("raw") or "")[:600])
        else:
            print(f"what it is   : {f.get('what_it_is')}")
            print(f"organiser    : {f.get('organiser_kind') or 'unknown'}")
            print(f"school       : {f.get('school_kind') or 'n/a'}")
            print(f"recognition  : {f.get('recognition')}  ({f.get('recognition_basis')})")
            for c in f.get("credibility") or []: print(f"  check       {c}")
            for c in f.get("checks") or []:      print(f"  to do       {c}")
            for u in f.get("unknown") or []:     print(f"  unknown     {u}")
            for sx in f.get("sources") or []:    print(f"  source      {sx.get('url')} ({sx.get('supports')})")
            if a.item: print("saved as finding", RS.save(db, a.item, out))
        print(f"\n[{out['pages_read']} page(s) read, {out['memory_reads']} memory read(s)]")
    elif a.cmd == "profile":
        from . import profile as P
        print(P.summary(db)); u = P.unconfirmed(db)
        if u: print("\nWaiting for your confirmation (cnb confirm <key> / cnb forget <key>):\n  " + ", ".join(r["key"] for r in u))
    elif a.cmd == "interview":
        from . import profile as P
        print("Answer in your own words; press Enter to skip a question. Nothing is shared with anyone.\n")
        n = P.interview(db, lambda q: input(q + "\n> "), areas=a.area)
        print(f"\nrecorded {n} fact(s). See them with: cnb profile")
    elif a.cmd == "forget":
        from . import profile as P
        print(f"deleted {P.forget(db, a.key)} row(s) for {a.key!r}")
    elif a.cmd == "confirm":
        from . import profile as P
        P.confirm(db, a.key); print(f"{a.key} marked confirmed")
    elif a.cmd == "tell":
        from . import profile as P
        ids = P.catch(db, a.text)
        print(f"noted. {len(ids)} proposed fact(s), all marked 'claimed' until you confirm them: cnb profile")
    elif a.cmd == "talk":                         # S10 advisor chat (local only; never sends anything to QQ)
        import importlib.util
        sp = importlib.util.spec_from_file_location("cnb_talk", os.path.join(D.ROOT, "tools", "talk.py"))
        tm = importlib.util.module_from_spec(sp); sp.loader.exec_module(tm)
        db.close(); sys.exit(tm.main(a.talk_args))
    elif a.cmd == "chat":
        from . import chat as CH
        r = CH.turn(db, a.text, channel=a.channel)
        print(f"note #{r['note_id']}: {r['summary']}")
        if r["reply"]: print(r["reply"])
    elif a.cmd == "spam":
        for m in db.execute("SELECT * FROM msg WHERE spam=1 ORDER BY ts DESC LIMIT 30"): print(f"#{m['id']} {digest.fmt(m['ts'])} g{m['group_id']} u{m['user_id']} {m['role'] or ''} | {m['spam_reason']} | {(m['text'] or '')[:60]}")
    elif a.cmd == "contests":
        from . import contest as C
        for i in db.execute("SELECT * FROM items WHERE type='contest' AND status IN ('new','confirmed')"):
            d = C.describe(i, db); print(f"[{d['recognition']:7}] {d['extra'].get('name') or i['title']} | register by {d['extra'].get('reg_deadline') or '?'}" + "".join(f"\n          ⚠ {h}" for h in d["hints"])
                  + (f"\n          {d['grade_text']}" if d["fit"].get("assessed") else ""))
    elif a.cmd == "whitelist":
        from . import contest as C
        wl = C.load_whitelist(); wl.setdefault("entries", []).append({k: v for k, v in dict(name=a.name, aliases=a.alias, organiser=a.organiser, level=a.level, credit=a.credit).items() if v})
        wl["source"] = wl.get("source", "manual"); json.dump(wl, open(C.WL, "w", encoding="utf-8"), indent=2, ensure_ascii=False); print("whitelist entries:", len(wl["entries"]))
    elif a.cmd == "status":
        print("messages:", db.execute("SELECT count(*) FROM msg").fetchone()[0], "| unprocessed:", db.execute("SELECT count(*) FROM msg WHERE processed=0").fetchone()[0], "| open items:", len(digest.items(db)))
        try:                                      # S3b: how far capture is known to be gap-free
            import json as _j, time as _t
            _c = _j.load(open(os.path.join(D.ROOT, "capture", "coverage.json"), encoding="utf-8")).get("covered_until")
            print("capture coverage: complete up to " + digest.fmt(_c) + f" ({int((_t.time() - _c) // 60)} min ago)" if _c else "capture coverage: not recorded yet")
        except Exception: print("capture coverage: not recorded yet")
        try:
            for line in ocr_status(db): print(line)
        except Exception as e: print(f"pictures (OCR): status unreadable ({type(e).__name__})")
    elif a.cmd == "daily":                       # scheduled run: all unprocessed messages in batches, then the report
        total, limit, fails = 0, 80, 0                # up to 200 batches/run (16,000 messages); a failed batch retries smaller
        for _ in range(200):
            if not db.execute("SELECT 1 FROM msg WHERE processed=0 LIMIT 1").fetchone(): break
            try: total += extract.process(db, limit=limit); fails = 0; _sweep_manual(db)
            except (ValueError, KeyError, OSError) as e:
                fails += 1; limit = max(10, limit // 2); print("batch failed:", type(e).__name__, "-> retry with", limit, flush=True)
                if fails >= 4: break
        digest.write_all(db, os.path.join(D.ROOT, "reports"))
        from . import notify
        try: print("alerts:", notify.alerts(db), "item(s)")
        except Exception as e: print("alerts skipped:", type(e).__name__)      # an alert failure must never fail the daily run
        now = dt.datetime.now(digest.Z)
        try:                                                               # weekly report, with catch-up: written whenever the most recent Sunday 18:00 (China time) has passed
            from . import weekly as WK                                     # and that target week's file is missing, even if the PC was off on Sunday
            p = WK.ensure(db, dt.datetime.now(WK.Z), os.path.join(D.ROOT, "reports"))
            if p: print("weekly:", p)
        except Exception as e: print("weekly skipped:", type(e).__name__)  # a weekly failure must never fail the daily run
        print(f"daily done: {total} new items, {len(digest.items(db))} open, unprocessed left: {db.execute('SELECT count(*) FROM msg WHERE processed=0').fetchone()[0]}")
    elif a.cmd == "held-count":
        from . import review; print(review.count(db))
    elif a.cmd == "review":
        from . import review; review.run(db)
    elif a.cmd == "weekly": from . import weekly as WK; print("wrote", WK.write_weekly(db, dt.datetime.now(WK.Z)))
    elif a.cmd == "notify-test":
        from . import notify
        print("toast shown" if notify.toast("Campus Notice Bot", "Alert test · 提醒测试", "9.12 09:00–11:00 · 观影 · 南门体育馆") else "toast FAILED: run this in your own WSL terminal; Windows interop (powershell.exe) must work")
    elif a.cmd == "process": print("new items:", extract.process(db)); _sweep_manual(db)
    elif a.cmd == "report":
        md = digest.markdown(db)
        if a.write:
            digest.write_all(db, os.path.join(D.ROOT, "reports")); print("wrote reports/latest.html (+ original/ pages), latest.md, latest.ics")
        else: print(md)
    elif a.cmd == "search":
        from . import onboard
        o = parse_query(a.text); tz = onboard.tz_name()
        try:
            grp = a.group if a.group is not None else (int(o["group"]) if o.get("group", "").isdigit() else None)
            if "group" in o and grp is None: raise ValueError("group: expected a group number")
            rows = search(db, o["text"], on=a.on or o.get("on"), frm=a.frm or o.get("from"), to=a.to or o.get("to"), group=grp,
                          sender=a.sender or o.get("sender"), priority=a.priority, items=a.items or o.get("type") == "item", tz=tz, limit=a.limit)
        except ValueError as e: print("search:", e, file=sys.stderr); raise SystemExit(2)
        for m in rows:
            if a.items or o.get("type") == "item": print(f"item #{m['id']} {m['type']} {(m['start'] or m['due'] or '(no date)')} g{m['group_id']}: {m['title'][:100]}")
            else: print(f"{_tzfmt(m['ts'], tz)} g{m['group_id']} u{m['user_id']}{' ★' if m['priority'] else ''}: {(m['text'] or '')[:120]}")     # checker fix: a row with NULL text crashed a date-only search
        print(f"{len(rows)} result(s)" + (f" (showing the first {len(rows)}; narrow the search or raise --limit)" if len(rows) >= max(1, min(a.limit, 500)) else ""))
if __name__ == "__main__": main()
