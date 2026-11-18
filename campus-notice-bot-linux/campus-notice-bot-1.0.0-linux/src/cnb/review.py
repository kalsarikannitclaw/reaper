"""Held-post review: posts from PRIORITY members that matched no keyword (capture/dyn.py keeps them, nobody
else's). On opening NapCat (`napcat-ui`) the student is asked whether any should become tasks anyway.
y = add as a task (dated by hand later, marked 'confirm'), n = dismiss for good, s = decide later."""
import time
from . import db as D

def ensure(db):
    db.execute("CREATE TABLE IF NOT EXISTS held(id INTEGER PRIMARY KEY, group_id INTEGER, user_id INTEGER, nick TEXT, ts INTEGER, text TEXT, state TEXT DEFAULT 'held', UNIQUE(group_id,user_id,ts,text))")
    db.commit()

def count(db):
    ensure(db); return db.execute("SELECT COUNT(*) FROM held WHERE state='held'").fetchone()[0]

def pending(db):
    ensure(db); return db.execute("SELECT * FROM held WHERE state='held' ORDER BY ts DESC").fetchall()

def add_task(db, h):
    """Approved by the student: stored as a message (so the 'original' link works) and an undated, to-confirm item."""
    db.execute("INSERT OR IGNORE INTO msg(group_id,user_id,nick,ts,text,raw,priority,role,processed) VALUES(?,?,?,?,?,?,?,?,1)",
               (h["group_id"], h["user_id"], h["nick"], h["ts"], h["text"], None, 1, None))
    mid = db.execute("SELECT id FROM msg WHERE group_id=? AND user_id=? AND ts=? AND text=?", (h["group_id"], h["user_id"], h["ts"], h["text"])).fetchone()["id"]
    dup = db.execute("SELECT item_id FROM item_sources WHERE msg_id=?", (mid,)).fetchone()
    if dup:                                                    # already an item for this message: do not duplicate
        db.execute("UPDATE held SET state='added' WHERE id=?", (h["id"],)); db.commit(); return dup["item_id"]
    title = " ".join((h["text"] or "").replace("[at]", "").split())[:14] or "task"
    iid = D.add_item(db, dict(type="event", title=title, quote=h["text"], sender=h["user_id"], group_id=h["group_id"], priority=1,
                              needs_confirm=1, confidence=0.3, action="你选择了把这条帖子加入任务：请查看原消息并补全时间"), [mid])
    db.execute("UPDATE held SET state='added' WHERE id=?", (h["id"],)); db.commit(); return iid

def run(db, ask=input, out=print):
    rows = pending(db)
    if not rows: out("No held posts."); return 0
    out(f"{len(rows)} post(s) from priority members matched no keyword. Add any as tasks?  [y]es / [n]o, forget it / [s]kip for now / [q]uit\n")
    added = 0
    for h in rows:
        out(f"  group {h['group_id']} · {time.strftime('%m-%d %H:%M', time.gmtime(h['ts'] + 8 * 3600))} · {h['nick'] or h['user_id']}\n  > {(h['text'] or '')[:200]}")
        a = (ask("  add as task? [y/n/s/q] ") or "s").strip().lower()[:1]
        if a == "q": break
        if a == "y": add_task(db, h); added += 1
        elif a == "n": db.execute("UPDATE held SET state='dismissed' WHERE id=?", (h["id"],)); db.commit()
    out(f"\nadded {added} task(s). {count(db)} still held."); return added
