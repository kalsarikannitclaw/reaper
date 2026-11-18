"""Backfill: fetch the group history QQ still holds for whitelisted groups and store what the
listener missed while the PC / bridge was off. Same rules as listener.py: whitelisted groups only,
same priority logic, INSERT OR IGNORE on UNIQUE(group_id,user_id,ts,text) so a re-run or an
overlap with live capture never duplicates.

Read-only on the QQ side (OneBot get_group_msg_history). Paced gently: one call every ~2s.

Run: python3 capture/backfill.py                 # from the last stored message (minus 1h overlap)
     python3 capture/backfill.py --days 7        # reach back N days regardless
     python3 capture/backfill.py --dry-run       # count only, store nothing (pictures are still read, nothing written)
     python3 capture/backfill.py --reocr         # S8: read the pictures of already-stored [image] rows (no new rows)
     python3 capture/backfill.py --reocr --no-history   # same, but only with the links saved in msg.raw (no QQ call)
Pictures (S8): every picture of a whitelisted-group message is OCR'd (capture/ocr.py) and the keyword gate sees
caption + picture text, exactly like the live listener.
Also runs automatically from the listener's perspective: call it after any outage.
"""
import argparse, asyncio, json, os, sqlite3, time
import sys
import websockets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kw, dyn, ocr, coverage

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS = os.path.join(HERE, "groups.json")
DB = os.path.join(HERE, "messages.db")
ENV = os.path.join(HERE, "..", "stack", ".env")
# Limits (expanded 2026-11-10). 100/page x 400 pages = 40,000 msgs per group per run, a runaway guard only;
# QQ itself decides how far back history goes. Override with env CNB_BF_PAGES / CNB_BF_PAGE.
MAX_PAGES = int(os.environ.get("CNB_BF_PAGES", 400))
PAGE = int(os.environ.get("CNB_BF_PAGE", 100))
OVERLAP = 3600
LAST = {"ok": False, "started": 0}      # S3b: did the last main() run cover every group completely?


def token():
    for l in open(ENV):
        if l.startswith("CAPTURE_WS_TOKEN="):
            return l.split("=", 1)[1].strip()
    raise SystemExit("CAPTURE_WS_TOKEN missing in stack/.env")


def text_of(m):
    msg = m.get("message")
    if isinstance(msg, list):
        return "".join(s["data"].get("text", "") if s["type"] == "text" else f"[{s['type']}]" for s in msg)
    return m.get("raw_message", "")


def _connect():
    return websockets.connect("ws://127.0.0.1:3001/", additional_headers={"Authorization": f"Bearer {token()}"})


async def call(ws, action, params):
    e = f"bf-{time.time()}"
    await ws.send(json.dumps({"action": action, "params": params, "echo": e}))
    while True:
        r = json.loads(await asyncio.wait_for(ws.recv(), 60))
        if r.get("echo") == e:
            return r


async def main(days, dry, quiet=False, ocr_fn=None, connect=None):
    cfg = json.load(open(GROUPS))
    groups = [int(g) for g in cfg["groups"]]
    prio_all = {int(u) for u in cfg.get("priority_members", [])}
    prio_grp = {int(g): {int(u) for u in us} for g, us in cfg.get("priority_by_group", {}).items()}
    roles = set(cfg.get("priority_roles", ["owner", "admin"]))
    ignore = {int(u) for u in cfg.get("ignore_users", [])}
    db = sqlite3.connect(DB, timeout=30)   # listener writes too
    if not dry:
        ocr.ensure(db)                     # S8 migration (nullable msg.ocr, ocr_log); safe on the live DB
    now = int(time.time())
    summary = {}
    LAST.update(ok=False, started=now)
    complete_all = True
    async with (connect or _connect)() as ws:
        for g in groups:
            if days is not None:
                since = now - days * 86400
            else:
                last = db.execute("SELECT MAX(ts) FROM msg WHERE group_id=?", (g,)).fetchone()[0]
                since = coverage.start_from(DB, last, now, overlap=OVERLAP)      # S3b: the watermark, not the newest stored row
            complete = False
            seq, fetched, kept, new, seen, reached, errors = 0, 0, 0, 0, set(), False, 0
            for _ in range(MAX_PAGES):
                r = await call(ws, "get_group_msg_history", {"group_id": g, "message_seq": seq, "count": PAGE})
                if r.get("status") != "ok":
                    print(g, "FAILED:", r.get("message") or r.get("wording")); break
                ms = r["data"].get("messages", [])
                fresh = [m for m in ms if isinstance(m, dict) and str(m.get("message_id")) not in seen]
                if not fresh:
                    complete = True              # QQ has nothing older to give: everything it holds is read
                    break
                for m in fresh:
                    seen.add(str(m.get("message_id")))
                fetched += len(fresh)
                for m in fresh:
                    try:                          # one malformed message must never end the sweep (checker S8 F2, L1)
                        if not isinstance(m, dict):
                            raise TypeError("message is not an object")
                        if int(m.get("time", 0)) < since:
                            reached = True; continue
                        if m.get("message_type") not in (None, "group") or int(m.get("group_id", g)) != g:
                            continue
                        sender = m.get("sender") or {}
                        uid = int(m["user_id"]); role = sender.get("role")
                        prio = int(uid not in ignore and (uid in prio_all or uid in prio_grp.get(g, set()) or role in roles))
                        prio = int(prio and not dyn.is_demoted(g, uid))
                        nick = sender.get("card") or sender.get("nickname")
                        r = await ocr.gate(db, m.get("message"), m.get("raw_message", ""), g, uid, m["time"], prio,
                                           lambda t, p: kw.match(t, bool(p)), ocr_fn=ocr_fn, dry=dry)
                        if r["existing"] or r["hit"] is not None:
                            kept += 1             # keyword gate (caption + picture text), same as live capture
                        if not dry:
                            new += ocr.store(db, dyn, g, uid, nick, m["time"], m.get("message"), prio, role, r) == "stored"
                    except Exception as e:
                        errors += 1
                        print(g, "message skipped:", type(e).__name__, e, flush=True)
                seqs = []
                for m in fresh:
                    try: seqs.append(int(m.get("message_seq", m.get("message_id", 0))))
                    except (TypeError, ValueError, AttributeError): pass
                if not seqs:
                    break
                nseq = min(seqs)
                if reached or nseq == seq:
                    complete = True
                    break
                seq = nseq
                await asyncio.sleep(2)
            if not dry:
                db.commit()
            complete_all = complete_all and complete
            summary[g] = {"fetched": fetched, "in_window": kept, "newly_stored": new, "reached_window_start": reached, "errors": errors}
            if not quiet: print(g, summary[g], flush=True)
            await asyncio.sleep(2)
    total_new = sum(v["newly_stored"] for v in summary.values())
    if complete_all and not dry and summary:
        LAST["ok"] = True
        coverage.advance(DB, now - 60)           # S3b: every group read back to its window start: no gap before this time
    elif not dry and not quiet:
        print("sweep incomplete: the coverage mark was NOT moved, the next sweep re-reads from the old one", flush=True)
    if not quiet:
        print("dry-run, nothing stored" if dry else f"stored {total_new} new. total rows now: {db.execute('SELECT COUNT(*) FROM msg').fetchone()[0]}")
    return total_new


async def fresh_links(ws, wanted, pace=2):
    """Read-only: page get_group_msg_history back to the oldest wanted message and return {picture file id: fresh url}
    for the pictures in `wanted` ({group: {file_id: ts}}). QQ CDN links expire; history hands out new ones."""
    found = {}
    for g, files in wanted.items():
        files = dict(files); lo = min(files.values()) - 600; seq, seen = 0, set()
        for _ in range(MAX_PAGES):
            r = await call(ws, "get_group_msg_history", {"group_id": g, "message_seq": seq, "count": PAGE})
            if r.get("status") != "ok":
                print(g, "history FAILED:", r.get("message") or r.get("wording")); break
            ms = [m for m in r["data"].get("messages", []) if m.get("message_id") not in seen]
            if not ms:
                break
            for m in ms:
                seen.add(m.get("message_id"))
                for d in ocr.image_segments(m.get("message")):
                    k = ocr.image_key(d)
                    if k in files and d.get("url"):
                        found[k] = d["url"]; files.pop(k)
            nseq = min(int(m.get("message_seq", m.get("message_id", 0))) for m in ms)
            if not files or min(m.get("time", 0) for m in ms) < lo or nseq == seq:
                break
            seq = nseq
            await asyncio.sleep(pace)
    return found


def _with_urls(message, urls):
    """A copy of a stored message whose picture links are replaced by fresh ones (same file ids, same base text)."""
    out = []
    for s in message:
        if isinstance(s, dict) and s.get("type") == "image" and ocr.image_key(s.get("data") or {}) in urls:
            s = dict(s, data=dict(s["data"], url=urls[ocr.image_key(s["data"])]))
        out.append(s)
    return out


async def reocr(history=True, quiet=False, ocr_fn=None, connect=None, db=None, pace=2):
    """Fill picture text into rows stored before S8 (or whose pictures could not be read yet). Never inserts a row:
    each row is UPDATEd in place (ocr.fill_row). Expired links: try the link in msg.raw first, then (history=True)
    a fresh link from read-only get_group_msg_history; a picture QQ no longer serves stays marked as failed."""
    db = db or sqlite3.connect(DB, timeout=30)
    ocr.ensure(db)
    rows = [r for r in db.execute("SELECT id,group_id,ts,text,raw FROM msg WHERE raw LIKE '%\"image\"%' ORDER BY id")
            if ocr.READ_OK not in (r[3] or "") and not (ocr.MARKER.search(r[3] or "") and "OCR failed" not in (r[3] or ""))]
    # (rows whose pictures were all read as "no text found" are final: a photo stays a photo, checker S8 L7)
    todo, stats = [], {"rows": len(rows), "filled": 0, "no_text": 0, "still_failed": 0, "skipped": 0, "fresh_links": 0}
    for mid, g, ts, text, raw in rows:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            stats["skipped"] += 1; continue
        if not ocr.image_segments(msg) or ocr.strip_ocr(text) != ocr.base_text(msg):
            stats["skipped"] += 1; continue            # no readable picture, or the text was changed by hand: leave it
        res = await ocr.read_images(db, msg, g, ts, retry=True, ocr_fn=ocr_fn)
        todo.append([mid, g, ts, msg, res])
    if history and any(not r.get("ok") and r.get("reason") not in ocr.NOTEXT for t in todo for r in t[4]):
        wanted = {}
        for mid, g, ts, msg, res in todo:
            for d, r in zip(ocr.image_segments(msg), res):
                if not r.get("ok") and r.get("reason") not in ocr.NOTEXT:     # only links that FAILED need a fresh one
                    wanted.setdefault(g, {})[ocr.image_key(d)] = ts
        try:
            async with (connect or _connect)() as ws:
                urls = await fresh_links(ws, wanted, pace)
        except (Exception, SystemExit) as e:          # no bridge / no token: the pictures simply stay marked unread
            print("history lookup failed (pictures stay unread):", type(e).__name__, flush=True); urls = {}
        stats["fresh_links"] = len(urls)
        for t in todo:
            if any(not r.get("ok") for r in t[4]) and any(ocr.image_key(d) in urls for d in ocr.image_segments(t[3])):
                t[4] = await ocr.read_images(db, _with_urls(t[3], urls), t[1], t[2], retry=True, ocr_fn=ocr_fn)
    for mid, g, ts, msg, res in todo:
        ocr.remember(db, res)                         # the row is stored: its pictures' text may be cached
        how = ocr.fill_row(db, mid, ocr.compose(msg, res, ts), ocr.summary(res, "reocr"))
        ok = any(r.get("ok") for r in res)
        notext = not ok and all(r.get("reason") in ocr.NOTEXT for r in res)
        stats["filled" if ok else "no_text" if notext else "still_failed"] += 1
        if not quiet:
            print(f"msg {mid}: {'read' if ok else 'NOT read'} ({', '.join(str(r.get('reason')) for r in res)}) -> {how}", flush=True)
    if not quiet:
        print("reocr:", stats, flush=True)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reocr", action="store_true", help="read pictures of already-stored [image] rows, in place")
    ap.add_argument("--no-history", action="store_true", help="with --reocr: do not ask QQ history for fresh links")
    a = ap.parse_args()
    if a.reocr:
        asyncio.run(reocr(history=not a.no_history))
    else:
        asyncio.run(main(a.days, a.dry_run))
