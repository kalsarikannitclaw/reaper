#!/usr/bin/env python3
"""Capture-health summary + daily log template + miss / false-alarm list (S7).

STRICTLY READ-ONLY. It reads:
  capture/bridge_status.json   (written by stack/bridge_guard.py)
  journalctl --user            (list-boots, and the guard / capture units; never writes, never systemctl)
  reports/alerts.log
  capture/messages.db          (opened with mode=ro)
It never starts/stops anything, never touches Docker/NapCat, never sends anything, never reads stack/.env.
Standard library only.

Usage:
  python3 tools/health.py                  health summary (text), exit 1 if any FLAG is raised
  python3 tools/health.py --days 7         look back 7 days (default 3)
  python3 tools/health.py --log            print a markdown daily-log template pre-filled from the checks
  python3 tools/health.py --json           machine-readable
  python3 tools/health.py misses           miss / false-alarm candidates (markdown, to be labelled by a human)
  python3 tools/health.py --root DIR       use another project root (tests, work copies)

Why the WSL-restart check exists (finding 2026-11-14, see stages/S7/build.md):
`journalctl --user --list-boots` shows the WSL2 VM booting at least 12 times between 2026-11-10 and 11-14, many of
them lasting minutes. Each VM shutdown kills the NapCat container (Docker reports the container as exited;
a daemon/VM kill typically shows as exit code 255 - the exit code itself is NOT recorded in the journal, so
this script flags the *pattern*: boot count, short boots, and 'container exited' lines in the guard unit).
The guard then restarts NapCat, and every restart is a fresh QQ login which counts against the cap of 6 per 24 h.
"""
import argparse, datetime as dt, json, os, re, sqlite3, subprocess, sys, time, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS_RE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
SHORT_BOOT_MIN = 30          # a VM boot that lived less than this is suspicious
STALE_CHECK_SECS = 300       # guard polls every 60 s; no update for 5 min = guard itself is down
QUIET_HOURS = 24             # no captured message for this long = suspicious (could be a quiet group)
LOGIN_WARN = 4               # warn at 4 of the 6-per-24h cap
BOOTS_WARN = 3               # more VM boots than this in the window = flag


# ---------- helpers ----------
def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:                                # journalctl missing / no user bus
        return 127, f"{type(e).__name__}: {e}"


def _fmt(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def _ago(secs):
    if secs is None: return "n/a"
    secs = int(secs)
    if secs < 90: return f"{secs}s"
    if secs < 5400: return f"{secs // 60}min"
    if secs < 172800: return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def mask(x):
    """QQ numbers (5+ digits) never appear in output."""
    return re.sub(r"\d{5,}", "<qq>", x) if isinstance(x, str) else x


def _ro(root):
    p = os.path.join(root, "capture", "messages.db")
    if not os.path.exists(p): return None
    c = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(p)) + "?mode=ro", uri=True); c.row_factory = sqlite3.Row  # quote: path may contain "#"
    return c


# ---------- checks (each returns dict with facts + flags list) ----------
def check_bridge(root, now):
    try:
        return _check_bridge(root, now)
    except Exception as e:                       # wrong-shape JSON must not crash the run
        return {"flags": [f"bridge_status.json has an unexpected shape ({type(e).__name__}): guard output not understood"], "facts": {}}


def _check_bridge(root, now):
    out = {"flags": [], "facts": {}}
    p = os.path.join(root, "capture", "bridge_status.json")
    try:
        st = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        out["flags"].append(f"bridge_status.json unreadable ({e}): is the guard service running?")
        return out
    if not isinstance(st, dict): raise TypeError("not an object")
    out["facts"] = {k: st.get(k) for k in ("state", "detail", "logins_24h", "login_cap", "next_try")}
    out["facts"]["detail"] = mask(out["facts"].get("detail"))      # never echo the QQ account number
    out["facts"]["state"] = mask(out["facts"].get("state"))
    st["checked"] = float(st.get("checked") or 0); st["last_online"] = float(st["last_online"]) if st.get("last_online") else None
    age = now - st["checked"]
    out["facts"]["status_age"] = _ago(age)
    out["facts"]["last_online"] = _fmt(st.get("last_online")) + (f" ({_ago(now - st['last_online'])} ago)" if st.get("last_online") else "")
    if st.get("state") != "online":
        out["flags"].append(f"bridge state is '{mask(st.get('state'))}': {mask(st.get('detail'))}")
    if age > STALE_CHECK_SECS:
        out["flags"].append(f"bridge status is {_ago(age)} old: the guard is not updating it")
    n, cap = int(st.get("logins_24h") or 0), int(st.get("login_cap") or 6)
    if n >= min(LOGIN_WARN, cap):
        out["flags"].append(f"{n}/{cap} QQ logins in the last 24h: close to the cap; every extra restart risks the account")
    return out


def parse_boot_ids(verbose_text):
    """`journalctl -o verbose` -> {boot_id: (first, last)} from the entries' headers and _BOOT_ID fields."""
    ids, cur = {}, None
    for ln in verbose_text.splitlines():
        m = re.match(r"^\w{3} (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
        if m: cur = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"); continue
        m = re.match(r"\s+_BOOT_ID=([0-9a-f]{32})", ln)
        if m and cur:
            f, l = ids.get(m.group(1), (cur, cur)); ids[m.group(1)] = (min(f, cur), max(l, cur))
    return ids


def parse_boots(text, with_ids=False):
    """`journalctl --list-boots` -> [(idx, first, last)] (or with the boot id as 4th item), local time, tz label ignored."""
    boots = []
    for ln in text.splitlines():
        m = re.match(r"\s*(-?\d+)\s+([0-9a-f]{32})\s", ln)
        ts = TS_RE.findall(ln)
        if m and len(ts) >= 2:
            f = dt.datetime.strptime(ts[0], "%Y-%m-%d %H:%M:%S"); l = dt.datetime.strptime(ts[1], "%Y-%m-%d %H:%M:%S")
            boots.append((int(m.group(1)), f, l, m.group(2)) if with_ids else (int(m.group(1)), f, l))
    return boots


def check_journal(root, now, days, _boots_text=None, _guard_text=None, _verbose_text=None):
    """WSL VM restarts, container exits, login attempts, from the user journal.
    list-boots can MISS boots (2026-11-13 06:11 was visible only in the guard journal), so boot ids seen in the guard
    unit's own entries are merged in and the count is reported as 'at least N'."""
    out = {"flags": [], "facts": {}, "boots": [], "by_day": {}}
    cutoff = dt.datetime.fromtimestamp(now) - dt.timedelta(days=days)
    since = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    if _boots_text is None:
        rc, _boots_text = _run(["journalctl", "--user", "--list-boots", "--no-pager"])
        if rc != 0:
            out["flags"].append("journalctl --user --list-boots failed: cannot assess WSL restarts")
            return out
    allboots = parse_boots(_boots_text, with_ids=True)
    if not allboots:
        out["flags"].append("journalctl --list-boots returned no boots (empty or 'No journal files'): WSL restarts could NOT be assessed")
        return out
    boots = {bid: [i, f, l, "list-boots"] for i, f, l, bid in allboots if l >= cutoff}
    if _verbose_text is None:
        rc, _verbose_text = _run(["journalctl", "--user", "-u", "campus-bridge-guard", "--no-pager", "-o", "verbose", "--since", since], timeout=60)
        if rc != 0: _verbose_text = ""
    for bid, (f, l) in parse_boot_ids(_verbose_text).items():
        if bid not in boots: boots[bid] = [None, f, l, "guard journal only"]
    rows = sorted(boots.values(), key=lambda b: b[1])
    cur_idx = max((b[0] for b in rows if b[0] is not None), default=None)
    short = []
    for i, f, l, src in rows:
        mins = (l - f).total_seconds() / 60
        out["boots"].append({"idx": i, "start": f.isoformat(" "), "end": l.isoformat(" "), "minutes": round(mins, 1), "source": src})
        if i != cur_idx and mins < SHORT_BOOT_MIN: short.append((f, mins))     # the current boot is still running
    gaps = [(b1[1] - b0[2]).total_seconds() / 60 for b0, b1 in zip(rows, rows[1:])]
    out["facts"]["vm_boots_in_window"] = f"at least {len(rows)}"
    out["facts"]["boots_only_in_guard_journal"] = sum(1 for b in rows if b[3] != "list-boots")
    out["facts"]["short_boots"] = len(short)
    out["facts"]["longest_down_gap_min"] = round(max(gaps), 1) if gaps else 0

    if _guard_text is None:
        rc, _guard_text = _run(["journalctl", "--user", "-u", "campus-bridge-guard", "--no-pager", "-o", "short-iso", "--since", since], timeout=30)
        if rc != 0:
            _guard_text = ""
            out["flags"].append("guard journal could not be read: container-exit and restart counts are unknown")
    exited = restarted = qr = c255 = 0
    for ln in _guard_text.splitlines():
        day = ln[:10]
        if "restarted the bridge" in ln:
            restarted += 1; out["by_day"][day] = out["by_day"].get(day, 0) + 1
        elif "container exit" in ln:
            exited += 1
        if "needs_qr" in ln: qr += 1
        if re.search(r"(exit(ed)?\W{0,3}(code\W*)?|\()255\b", ln): c255 += 1
    out["facts"].update({"guard_restarts": restarted, "guard_wait_lines_container_exited": exited,
                         "guard_needs_qr_lines": qr, "explicit_exit_255_lines": c255})
    nb = len(rows)
    if nb - 1 > BOOTS_WARN or short:
        out["flags"].append(
            f"WSL VM restarted at least {max(nb - 1, 0)}x in {days}d ({len(short)} boots shorter than {SHORT_BOOT_MIN} min). "
            f"A VM shutdown kills NapCat; only {restarted} of them led to a guard restart/QQ login so far (cap 6/24h; a restart can also happen inside a short boot). "
            "See 'Mitigations' in the log template.")
    if restarted >= 2:
        out["flags"].append(f"guard restarted NapCat {restarted}x in {days}d (each = one QQ login); per-day: {out['by_day']}")
    if qr:
        out["flags"].append(f"guard reported needs_qr on {qr} poll lines: QQ session invalidated at least once, owner QR scan was required")
    if c255:
        out["flags"].append(f"{c255} journal line(s) mention exit code 255 (container killed with the VM)")
    return out


def check_capture(root, now, days):
    out = {"flags": [], "facts": {}, "groups": []}
    c = _ro(root)
    if c is None:
        out["flags"].append("capture/messages.db missing"); return out
    last, total = c.execute("SELECT max(ts), count(*) FROM msg").fetchone()
    out["facts"] = {"messages_total": total, "last_message": _fmt(last), "last_message_age": _ago(now - last) if last else "n/a"}
    since = now - days * 86400
    out["facts"]["messages_in_window"] = c.execute("SELECT count(*) FROM msg WHERE ts>=?", (since,)).fetchone()[0]
    out["facts"]["unprocessed"] = c.execute("SELECT count(*) FROM msg WHERE processed=0").fetchone()[0]
    out["facts"]["spam_flagged_in_window"] = c.execute("SELECT count(*) FROM msg WHERE spam=1 AND ts>=?", (since,)).fetchone()[0]
    for r in c.execute("SELECT group_id, count(*) n, max(ts) last FROM msg GROUP BY group_id ORDER BY last DESC"):
        out["groups"].append({"group": r["group_id"], "messages": r["n"], "last": _fmt(r["last"]), "age": _ago(now - r["last"])})
    if last and now - last > QUIET_HOURS * 3600:
        out["flags"].append(f"no message captured for {_ago(now - last)}: a dead bridge looks like a quiet week, cross-check bridge state")
    if out["facts"]["unprocessed"]:
        out["flags"].append(f"{out['facts']['unprocessed']} captured messages not yet processed by the daily batch")
    # daily gap view: days in the window with zero messages
    d0 = dt.date.fromtimestamp(now)
    empty = [str(d0 - dt.timedelta(days=i)) for i in range(days)
             if not c.execute("SELECT 1 FROM msg WHERE date(ts,'unixepoch','localtime')=? LIMIT 1", (str(d0 - dt.timedelta(days=i)),)).fetchone()]
    out["facts"]["days_with_no_messages"] = empty
    if len(empty) >= 2:
        out["flags"].append(f"{len(empty)} of the last {days} days have zero captured messages: {', '.join(empty)}")
    return out


CN = dt.timezone(dt.timedelta(hours=8))      # alerts.log is written in China time (digest.Z), the laptop clock is not


def check_alerts(root, now, days):
    """alerts.log lines are written BEFORE delivery; notify.alerts() records an item in `notified` (at>0) only when
    every toast of the run succeeded. So repeated lines mean FAILED DELIVERY and retries, not noisy re-alerting."""
    out = {"flags": [], "facts": {}}
    p = os.path.join(root, "reports", "alerts.log")
    if not os.path.exists(p):
        out["facts"] = {"alert_lines_in_window": 0}; return out
    cut = now - days * 86400
    batches, line_ct = {}, {}
    for ln in open(p, encoding="utf-8", errors="replace"):
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(.*)", ln.rstrip())
        if not m: continue
        try: t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=CN).timestamp()
        except ValueError: continue
        if t < cut: continue
        batches.setdefault(m.group(1), []).append(m.group(2))
        if not m.group(2).startswith("+"): line_ct[m.group(2)] = line_ct.get(m.group(2), 0) + 1
    out["facts"] = {"alert_batches_in_window": len(batches), "alert_lines_in_window": sum(map(len, batches.values()))}
    rep = sorted(((n, t) for t, n in line_ct.items() if n >= 3), reverse=True)[:5]
    out["facts"]["most_repeated_alert_lines_in_window"] = [f"{n}x {t}" for n, t in rep]
    delivered = None
    c = _ro(root)
    if c is not None:
        try:
            delivered = c.execute("SELECT count(*) FROM notified WHERE at>=?", (cut,)).fetchone()[0]
            out["facts"]["items_confirmed_delivered_in_window"] = delivered
        except sqlite3.Error: pass
    if len(batches) >= 2 and delivered == 0:
        out["flags"].append(f"alerts logged in {len(batches)} batches but NONE confirmed delivered in the window (notified has no row with at>0): "
                            "toast delivery is probably failing and items are being retried (see notify.alerts); cause not diagnosed")
    elif rep and delivered is not None and delivered < len(rep):
        out["flags"].append(f"alert lines repeat ({rep[0][0]}x '{rep[0][1]}') while few items are confirmed delivered: likely failed toast retries")
    return out


def run_all(root, days, now=None):
    now = now or int(time.time())
    return {"generated": _fmt(now), "days": days, "bridge": check_bridge(root, now),
            "journal": check_journal(root, now, days), "capture": check_capture(root, now, days),
            "alerts": check_alerts(root, now, days)}


def all_flags(r):
    return [f for k in ("bridge", "journal", "capture", "alerts") for f in r[k]["flags"]]


# ---------- misses / false alarms ----------
ATTACH = re.compile(r"^\s*(\[(image|file|json|face|video|record|at|reply)\]\s*)+$")   # needs at least one tag: empty text is NOT an attachment
SIGNAL = re.compile(r"截止|报名|通知|提交|考试|作业|讲座|比赛|竞赛|活动|地点|时间|集合|取消|改期|延期|补交|deadline|due|exam|\d{1,2}月\d{1,2}[日号]|\d{1,2}号|\d{1,2}[:：点]\d{0,2}")
FOLLOW = re.compile(r"^\s*(\[at\]\s*)*.{0,24}$", re.S)


def misses(root, limit=80):
    c = _ro(root)
    if c is None: return {"error": "no db"}
    unlinked = c.execute("""SELECT m.id,m.group_id,m.ts,m.priority,m.role,m.text,m.processed,m.spam FROM msg m
                            WHERE m.id NOT IN (SELECT msg_id FROM item_sources) ORDER BY m.ts""").fetchall()
    def norm(t): return re.sub(r"\s+", "", t or "")
    linked = {norm(r[0]) for r in c.execute("SELECT m.text FROM msg m WHERE m.id IN (SELECT msg_id FROM item_sources)") if len(norm(r[0])) >= 20}
    res = {"reposts_of_linked": [], "empty_text": [], "likely_miss": [], "attachment_only": [], "fragment": [], "not_processed": [], "other_dropped": [], "spam": 0}
    for r in unlinked:
        t = (r["text"] or "")
        row = {"id": r["id"], "ts": _fmt(r["ts"]), "group": r["group_id"], "role": r["role"], "priority": r["priority"], "text": re.sub(r"\s+", " ", t)[:110]}
        if r["spam"]: res["spam"] += 1; continue
        if not t.strip(): res["empty_text"].append(row)               # nothing to extract; not an attachment claim
        elif len(norm(t)) >= 20 and any(l in norm(t) or norm(t) in l for l in linked): res["reposts_of_linked"].append(row)   # same notice already became an item
        elif not r["processed"]: res["not_processed"].append(row)
        elif ATTACH.match(t): res["attachment_only"].append(row)      # PLAN: attachments are flagged, not parsed (known gap)
        elif SIGNAL.search(t) and len(t) > 24: res["likely_miss"].append(row)
        elif SIGNAL.search(t): res["fragment"].append(row)             # short reply carrying a date: lost context
        else: res["other_dropped"].append(row)
    fa = {"low_confidence": [], "undated_needs_confirm": [], "no_source": [], "no_time_signal_in_quote": [], "dismissed_or_cancelled": [], "duplicate_titles": []}
    for it in c.execute("SELECT * FROM items"):
        row = {"id": it["id"], "type": it["type"], "title": it["title"], "start": it["start"], "due": it["due"], "conf": it["confidence"], "status": it["status"]}
        if (it["confidence"] if it["confidence"] is not None else 1) < 0.7: fa["low_confidence"].append(row)
        if it["needs_confirm"] and not (it["start"] or it["due"]): fa["undated_needs_confirm"].append(row)
        if not c.execute("SELECT 1 FROM item_sources WHERE item_id=?", (it["id"],)).fetchone(): fa["no_source"].append(row)
        if it["type"] in ("event", "deadline") and not SIGNAL.search(it["quote"] or ""): fa["no_time_signal_in_quote"].append(row)
        if it["status"] in ("dismissed", "cancelled"): fa["dismissed_or_cancelled"].append(row)
    for r in c.execute("SELECT title, count(*) n, group_concat(id) ids FROM items GROUP BY title HAVING n>1"):
        fa["duplicate_titles"].append({"title": r["title"], "ids": r["ids"]})
    res["false_alarm_candidates"] = fa
    notified = c.execute("SELECT count(*) FROM notified").fetchone()[0]
    res["notified_total"] = notified
    return res


def misses_md(root):
    m = misses(root)
    if "error" in m: return f"# Miss / false-alarm list\n\n{m['error']}\n"
    L = ["# Miss / false-alarm list (machine candidates; a human labels each row)", "",
         f"Generated {_fmt(time.time())} from capture/messages.db (read-only). Heuristic: a stored, processed, non-spam message with no row in",
         "`item_sources` is a *candidate* miss; an item is a *candidate* false alarm by the rules below. Fill the Verdict column: MISS / OK-noise / FALSE-ALARM / OK.", ""]
    def table(title, rows, note):
        L.extend([f"## {title} ({len(rows)})", note, ""])
        if rows:
            L.append("| msg | time | group | role | text | Verdict |"); L.append("|---|---|---|---|---|---|")
            for r in rows: L.append(f"| {r['id']} | {r['ts']} | {r['group']} | {r['role']} | {r['text'].replace('|', '/')} | |")
        L.append("")
    table("A. Likely misses", m["likely_miss"], "Long text with a date/deadline/action word but no item. Most likely real misses.")
    table("B. Fragments (short replies with a date)", m["fragment"], "e.g. '14号中午2点前' answers an earlier message; the extractor sees no context. Check whether the parent item was updated.")
    table("C. Attachment-only (image/file/json)", m["attachment_only"], "Known gap (PLAN: attachments not parsed in v0). Open the original in QQ to see whether any carried a notice.")
    table("C2. Reposts of an already-linked notice", m["reposts_of_linked"], "Same opening text as a message that became an item: not misses.")
    table("C3. Empty text", m["empty_text"], "No text captured (maybe a stripped message type); nothing to extract.")
    table("D. Not yet processed", m["not_processed"], "Daily batch has not seen these.")
    table("E. Other dropped text", m["other_dropped"], "No date/action signal. Probably chatter; spot-check.")
    fa = m["false_alarm_candidates"]
    L.append("## F. False-alarm candidates (items)\n")
    for k, lab in (("low_confidence", "confidence < 0.7"), ("undated_needs_confirm", "flagged needs_confirm and no start/due date (shown in the calendar/alerts as 'time unknown')"), ("no_time_signal_in_quote", "event/deadline whose quote has no time/date word"),
                   ("no_source", "item with no source message (violates the link-to-original rule)"),
                   ("dismissed_or_cancelled", "dismissed/cancelled"), ("duplicate_titles", "same title more than once")):
        L.append(f"### {lab} ({len(fa[k])})")
        for r in fa[k]:
            L.append("- " + " | ".join(f"{a}={b}" for a, b in r.items()) + " | Verdict: ")
        L.append("")
    L.append(f"Alerts sent so far (table `notified`): {m['notified_total']}. Repeated alert lines are failed-delivery retries, reported by `health.py` under alerts.\n")
    return "\n".join(L)


# ---------- rendering ----------
def text_report(r):
    L = [f"Campus bot health, {r['generated']} (window {r['days']}d)", ""]
    for k, title in (("bridge", "BRIDGE"), ("journal", "WSL / JOURNAL"), ("capture", "CAPTURE"), ("alerts", "ALERTS")):
        L.append(f"[{title}]")
        for a, b in r[k]["facts"].items(): L.append(f"  {a}: {b}")
        if k == "journal":
            for b in r[k]["boots"][-8:]: L.append(f"  boot {str(b['idx'] if b['idx'] is not None else '?'):>3}: {b['start']} -> {b['end']}  ({b['minutes']} min, {b['source']})")
        L.append("")
    fl = all_flags(r)
    L.append(f"FLAGS ({len(fl)})" if fl else "FLAGS: none")
    L.extend("  ! " + f for f in fl)
    return "\n".join(L)


MITIGATIONS = """\
### Mitigations for WSL VM restarts (documented only; this script and the S7 stage change NO configuration)
- All boots end with an orderly systemd stop of the guard, which points to a clean WSL/Windows shutdown (idle timeout, `wsl --shutdown`,
  sleep/shutdown/update), not a crash. The trigger is not identified yet; log the Windows side (Event Viewer, power events) to find out.
- Windows side: Settings > Power: Sleep = Never while plugged in; lid-close action = Do nothing; Hibernate off. A laptop that sleeps
  suspends the WSL VM, and a resume can come back as a fresh boot.
- WSL idles out: in `%UserProfile%\\.wslconfig` under `[wsl2]` set `vmIdleTimeout=-1` (UNVERIFIED that -1 means never shut down; otherwise use a very large number of ms;
  needs `wsl --shutdown` + restart to take effect, which would itself kill NapCat, so apply only when the owner chooses and with the guard cap in mind).
  Current file has only `networkingMode=mirrored`.
- Keep-alive: a Windows Scheduled Task at logon running `wsl.exe -d <distro> --exec sleep infinity` (hidden) keeps a WSL session open.
  Closing the last terminal no longer ends the VM.
- systemd user lingering inside WSL does not stop VM shutdown by itself; it only starts services after boot.
- Guard behaviour to keep: the guard counts each post-reboot restart as a login (cap 6/24h). Consider (owner decision) that a
  VM-reboot restart is the benign case and a *container crash while the VM stays up* is the dangerous one; they can be told apart by
  comparing `list-boots` with the exit time. Not changed here.
- Long-term: run the bridge on an always-on host (small VPS or home server), not on the laptop. See the multi-user memo.
"""


def log_template(r, root):
    mk = misses(root)
    nm = len(mk.get("likely_miss", [])) if "error" not in mk else "n/a"
    b, j, c, a = r["bridge"]["facts"], r["journal"]["facts"], r["capture"]["facts"], r["alerts"]["facts"]
    fl = all_flags(r)
    L = [f"# Daily log, {dt.date.today()}", "",
         "Auto-filled by `python3 tools/health.py --log`. Fill the blanks by hand (marked ___).", "",
         "## 1. Capture health (auto)", "",
         f"- Bridge state: {b.get('state')} ({b.get('detail')}); last online {b.get('last_online')}; status file age {b.get('status_age')}",
         f"- QQ logins in last 24h: {b.get('logins_24h')}/{b.get('login_cap')}",
         f"- WSL VM boots in last {r['days']}d: {j.get('vm_boots_in_window')} (lower bound; short boots: {j.get('short_boots')}; longest gap between boots: {j.get('longest_down_gap_min')} min)",
         f"- Guard restarts of NapCat: {j.get('guard_restarts')}; needs_qr poll lines: {j.get('guard_needs_qr_lines')}; explicit exit-255 lines: {j.get('explicit_exit_255_lines')}",
         f"- Messages captured last {r['days']}d: {c.get('messages_in_window')} (total {c.get('messages_total')}); last message {c.get('last_message')} ({c.get('last_message_age')} ago)",
         f"- Days with zero messages: {', '.join(c.get('days_with_no_messages', [])) or 'none'}",
         f"- Unprocessed messages: {c.get('unprocessed')}",
         f"- Alert batches in window: {a.get('alert_batches_in_window')}; confirmed delivered: {a.get('items_confirmed_delivered_in_window')}; repeated lines (= failed-delivery retries): {a.get('most_repeated_alert_lines_in_window') or 'none'}", "",
         "### Flags", ""]
    L.extend([f"- [ ] {f}" for f in fl] or ["- none"])
    L += ["", "## 2. Today by hand", "",
          "- Did the bot show something I had to act on? ___", "- Did I find a notice in QQ that the bot did NOT show (miss)? msg/time/group: ___",
          "- Did an alert/item turn out wrong or useless (false alarm)? item id: ___ why: ___",
          "- Did any link-to-original / calendar page fail to open? ___", "- Did QQ show any safety warning or forced re-login? ___ (if yes: stop, do not restart; see PLAN backlog 12)",
          f"- Machine miss candidates waiting for a verdict (`health.py misses`): {nm} likely", "",
          "## 3. Fix list (carry forward)", "", "| # | symptom | cause (if known) | fix / owner stage | done |", "|---|---|---|---|---|", "| | | | | |", "",
          MITIGATIONS]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", default="summary", choices=["summary", "misses"])
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--log", action="store_true"); ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args(argv)
    if a.cmd == "misses":
        print(misses_md(a.root)); return 0
    r = run_all(a.root, a.days)
    if a.json: print(json.dumps(r, ensure_ascii=False, indent=2))
    elif a.log: print(log_template(r, a.root))
    else: print(text_report(r))
    return 1 if all_flags(r) else 0


if __name__ == "__main__":
    sys.exit(main())
