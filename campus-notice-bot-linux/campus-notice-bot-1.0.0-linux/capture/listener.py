"""Listen-only capture: NapCat OneBot WS -> SQLite, whitelisted groups only.
Whitelist file (groups.json) is re-read per message, so groups can be added later without restart.
Run: python3 capture/listener.py   (token from stack/.env CAPTURE_WS_TOKEN)
"""
import asyncio, json, os, sqlite3, subprocess, sys, time
import websockets

HERE0 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE0)          # python -I drops the script dir; backfill.py lives beside us
import backfill, kw, dyn, ocr, coverage

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS = os.path.join(HERE, "groups.json")
DB = os.path.join(HERE, "messages.db")
ENV = os.path.join(HERE, "..", "stack", ".env")
ROOT = os.path.dirname(HERE)

# --- watcher behaviour (2026-11-10) -------------------------------------------------------
SWEEP_EVERY = 1800     # also re-sweep history every 30 min: catches anything a dropped socket missed
PRUNE_EVERY = 6 * 3600 # S8b: drop ocr_log rows older than ocr.log_days() (default 30) on start and every 6 h
QUIET_SECS = 90        # refresh the calendar once capture has been quiet this long ...
MIN_GAP = 600          # ... and at most once per 10 min (each run calls the LLM)
state = {"dirty": 0.0, "last_run": 0.0, "running": False, "conn": 0, "connected": False, "synced": False, "resync": None}
RETRY_AFTER_FAIL = 60  # S3b: a failed or incomplete sweep is retried after a minute, not after 30

def mark_dirty():
    state["dirty"] = time.time()

async def backfill_task():
    """On start and every SWEEP_EVERY: pull the history QQ still holds for whitelisted groups,
    from the last stored message (minus 1h) forward. Own connection, so live events are never
    swallowed while it pages."""
    while True:
        conn0 = state["conn"]
        ok = False
        try:
            n = await backfill.main(None, False, quiet=True)
            ok = bool(backfill.LAST["ok"])
            print(f"backfill sweep: {n} new message(s)" + ("" if ok else " (incomplete, will retry)"), flush=True)
            if n: mark_dirty()
        except Exception as e:
            print("backfill failed:", type(e).__name__, e, flush=True)
        # S3b: live capture counts as gap-free only if the socket stayed connected through the whole sweep
        state["synced"] = bool(ok and state["connected"] and state["conn"] == conn0)
        try:
            await asyncio.wait_for(state["resync"].wait(), SWEEP_EVERY if ok else RETRY_AFTER_FAIL)
        except asyncio.TimeoutError:
            pass
        state["resync"].clear()

async def heartbeat_task():
    """S3b: while the socket has been connected without a break since a complete sweep, every minute is covered."""
    while True:
        await asyncio.sleep(60)
        if state["connected"] and state["synced"]:
            try: coverage.advance(DB, time.time() - 5)
            except OSError: pass

async def prune_task(db):
    """S8b: keep ocr_log from growing forever. Runs on the event loop's own connection (sqlite is not shared across
    threads); a prune is a single DELETE on a small table, and ocr.prune never raises."""
    while True:
        ocr.prune(db)
        await asyncio.sleep(PRUNE_EVERY)

async def refresh_task():
    """Combine capture with the existing pipeline: new messages -> `cnb daily` (extract items,
    rewrite reports/calendar.html, latest.ics, latest.html) after a quiet spell."""
    while True:
        await asyncio.sleep(15)
        d = state["dirty"]
        if not d or state["running"]: continue
        now = time.time()
        if now - d < QUIET_SECS or now - state["last_run"] < MIN_GAP: continue
        state["running"] = True; state["dirty"] = 0.0
        try:
            env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), NO_PROXY="localhost,127.0.0.1")
            r = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "cnb.cli", "daily"],
                                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
            print("refresh:", (r.stdout.strip().splitlines() or [r.stderr.strip()[-200:]])[-1], flush=True)
        except Exception as e:
            print("refresh failed:", type(e).__name__, e, flush=True)
        state["last_run"] = time.time(); state["running"] = False

def token():
    for l in open(ENV, encoding="utf-8"):
        if l.startswith("CAPTURE_WS_TOKEN="):
            return l.split("=", 1)[1].strip()
    raise SystemExit("CAPTURE_WS_TOKEN missing in stack/.env")

def load_cfg():
    try:
        with open(GROUPS, encoding="utf-8") as f:      # utf-8 on Windows too (nicknames are Chinese)
            c = json.load(f)
        return ({int(g) for g in c["groups"]},
                {int(u) for u in c.get("priority_members", [])},
                {int(g): {int(u) for u in us} for g, us in c.get("priority_by_group", {}).items()},
                set(c.get("priority_roles", ["owner", "admin"])),
                {int(u) for u in c.get("ignore_users", [])})
    except Exception as e:
        print("groups.json unreadable, dropping everything:", e, flush=True)
        return set(), set(), {}, set(), set()

def text_of(ev):
    msg = ev.get("message")
    if isinstance(msg, list):
        return "".join(s["data"].get("text", "") if s["type"] == "text" else f"[{s['type']}]" for s in msg)
    return ev.get("raw_message", "")

def priority_of(ev, cfg):
    groups, prio_all, prio_grp, prio_roles, ignore = cfg
    role = (ev.get("sender") or {}).get("role")        # owner / admin / member, supplied by QQ per message
    prio = int(ev["user_id"] not in ignore and (ev["user_id"] in prio_all or ev["user_id"] in prio_grp.get(ev["group_id"], set()) or role in prio_roles))
    return int(prio and not dyn.is_demoted(ev["group_id"], ev["user_id"])), role

async def handle(db, ev, cfg, ocr_fn=None):
    """One whitelisted-group message: keyword gate on caption + picture text (S8), then store / hold / drop.
    Returns what happened (ocr.store outcome). Pictures are read off the event loop (ocr.read_images -> to_thread)."""
    prio, role = priority_of(ev, cfg)
    s = ev.get("sender") or {}
    nick = s.get("card") or s.get("nickname")
    ts = ev.get("time", int(time.time()))
    r = await ocr.gate(db, ev.get("message"), ev.get("raw_message", ""), ev["group_id"], ev["user_id"], ts, prio,
                       lambda t, p: kw.match(t, bool(p)), ocr_fn=ocr_fn)
    out = ocr.store(db, dyn, ev["group_id"], ev["user_id"], nick, ts, ev.get("message"), prio, role, r)
    if out == "stored": mark_dirty()
    return out

def migrate(db):
    dyn.ensure(db)
    db.execute("""CREATE TABLE IF NOT EXISTS msg(
        id INTEGER PRIMARY KEY, group_id INTEGER, user_id INTEGER, nick TEXT,
        ts INTEGER, text TEXT, raw TEXT, priority INTEGER DEFAULT 0, role TEXT, UNIQUE(group_id, user_id, ts, text))""")
    if "priority" not in [r[1] for r in db.execute("PRAGMA table_info(msg)")]:
        db.execute("ALTER TABLE msg ADD COLUMN priority INTEGER DEFAULT 0")
    if "role" not in [r[1] for r in db.execute("PRAGMA table_info(msg)")]:
        db.execute("ALTER TABLE msg ADD COLUMN role TEXT")
    ocr.ensure(db)                                     # S8: nullable msg.ocr + ocr_log (CREATE IF NOT EXISTS / ADD COLUMN only)

pending = set()                                       # picture messages being read; strong refs so tasks are not GC'd

async def dispatch(db, ev):
    """Route one raw OneBot event. Messages with pictures run as their own task, so a slow download/OCR never holds
    up the websocket loop; everything else is handled inline as before."""
    if ev.get("post_type") != "message" or ev.get("message_type") != "group":
        return None                                   # private chats etc: dropped
    cfg = load_cfg()
    if ev["group_id"] not in cfg[0]:
        return None                                   # not whitelisted: dropped, never stored (and never OCR'd)
    if ocr.image_segments(ev.get("message")):
        t = asyncio.create_task(_guarded(db, ev, cfg)); pending.add(t); t.add_done_callback(pending.discard)
        return t
    return await handle(db, ev, cfg)

async def _guarded(db, ev, cfg):
    try:
        return await handle(db, ev, cfg)
    except Exception as e:                             # never lose the loop over one picture
        print("picture message failed:", type(e).__name__, e, flush=True)
        ocr.bump({"ok": False, "reason": "handler_error:" + type(e).__name__})
        try:                                           # ... nor the message: fall back to the pre-S8 caption-only gate
            prio, role = priority_of(ev, cfg)
            base = ocr.base_text(ev.get("message"), ev.get("raw_message", ""))
            if kw.match(base, bool(prio)) is not None:
                s = ev.get("sender") or {}
                db.execute("INSERT OR IGNORE INTO msg(group_id,user_id,nick,ts,text,raw,priority,role) VALUES(?,?,?,?,?,?,?,?)",
                           (ev["group_id"], ev["user_id"], s.get("card") or s.get("nickname"), ev.get("time", int(time.time())),
                            base + "[image text (OCR failed: handler_error)]", json.dumps(ev.get("message"), ensure_ascii=False), prio, role))
                db.commit(); mark_dirty(); return "stored_caption_only"
        except Exception as e2:
            print("caption fallback failed too:", type(e2).__name__, flush=True)
        return "error"

async def main():
    db = sqlite3.connect(DB, timeout=30)
    migrate(db)
    state["resync"] = asyncio.Event()
    asyncio.create_task(backfill_task()); asyncio.create_task(heartbeat_task()); asyncio.create_task(refresh_task()); asyncio.create_task(prune_task(db))
    url = "ws://127.0.0.1:3001/"
    while True:
        try:
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {token()}"}) as ws:
                print("connected", flush=True)
                state["conn"] += 1; state["connected"] = True; state["synced"] = False; state["resync"].set()   # S3b: every (re)connect re-reads the gap at once
                async for raw in ws:
                    await dispatch(db, json.loads(raw))
            state["connected"] = False; state["synced"] = False      # clean close: not covered until the next sweep
            await asyncio.sleep(5)
        except Exception as e:
            state["connected"] = False; state["synced"] = False
            print("ws error, retry in 5s:", type(e).__name__, flush=True)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
