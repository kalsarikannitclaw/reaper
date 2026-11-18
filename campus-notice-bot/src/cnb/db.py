"""Store: reuses capture/messages.db (raw msgs) and adds items + FTS5 search. One DB = one tenant (v0: tenant 'default')."""
import json, os, re, sqlite3
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.environ.get("CNB_DB", os.path.join(ROOT, "capture", "messages.db"))

def connect(path=None):
    db = sqlite3.connect(path or DB_PATH); db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS msg(id INTEGER PRIMARY KEY, group_id INTEGER, user_id INTEGER, nick TEXT, ts INTEGER,
        text TEXT, raw TEXT, priority INTEGER DEFAULT 0, role TEXT, UNIQUE(group_id,user_id,ts,text));
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, type TEXT NOT NULL, title TEXT NOT NULL,
        start TEXT, end TEXT, due TEXT, location TEXT, sender INTEGER, group_id INTEGER, priority INTEGER DEFAULT 0,
        quote TEXT NOT NULL, confidence REAL, status TEXT DEFAULT 'new', needs_confirm INTEGER DEFAULT 0,
        extra TEXT, supersedes INTEGER, created INTEGER);
    CREATE TABLE IF NOT EXISTS item_sources(item_id INTEGER NOT NULL REFERENCES items(id), msg_id INTEGER NOT NULL REFERENCES msg(id), PRIMARY KEY(item_id,msg_id));
    CREATE TABLE IF NOT EXISTS item_history(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), ts INTEGER,
        field TEXT, old TEXT, new TEXT, msg_id INTEGER, kind TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(text, content='msg', content_rowid='id', tokenize='trigram');
    CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON msg BEGIN INSERT INTO msg_fts(rowid,text) VALUES (new.id,new.text); END;
    CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON msg BEGIN INSERT INTO msg_fts(msg_fts,rowid,text) VALUES('delete',old.id,old.text); END;
    """)
    cols = [r[1] for r in db.execute("PRAGMA table_info(msg)")]
    icols = [r[1] for r in db.execute("PRAGMA table_info(items)")]
    for c in ("action", "requirements"):
        if c not in icols: db.execute(f"ALTER TABLE items ADD COLUMN {c} TEXT")
    for c, t in (("spam", "INTEGER DEFAULT 0"), ("spam_reason", "TEXT"), ("topics", "TEXT")):
        if c not in cols: db.execute(f"ALTER TABLE msg ADD COLUMN {c} {t}")
    if "topics" not in icols: db.execute("ALTER TABLE items ADD COLUMN topics TEXT")
    for c, t in (("origin", "INTEGER"), ("origin_title", "TEXT")):      # S2: a registration/study/stage item points at the task it belongs to
        if c not in icols: db.execute(f"ALTER TABLE items ADD COLUMN {c} {t}")
    if "processed" not in cols: db.execute("ALTER TABLE msg ADD COLUMN processed INTEGER DEFAULT 0")
    db.execute("INSERT INTO msg_fts(msg_fts) VALUES('rebuild')") if db.execute("SELECT count(*) FROM msg_fts").fetchone()[0] == 0 and db.execute("SELECT count(*) FROM msg").fetchone()[0] else None
    db.commit(); return db

def add_item(db, it, msg_ids):
    """Hard rule (PLAN): an item without a resolvable source is refused."""
    if not msg_ids: raise ValueError("item has no source message")
    cur = db.execute("INSERT INTO items(type,title,start,end,due,location,sender,group_id,priority,quote,confidence,status,needs_confirm,extra,supersedes,action,requirements,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))",
        (it["type"], it["title"], it.get("start"), it.get("end"), it.get("due"), it.get("location"), it.get("sender"), it.get("group_id"),
         it.get("priority", 0), it["quote"], it.get("confidence"), it.get("status", "new"), it.get("needs_confirm", 0), it.get("extra"), it.get("supersedes"), it.get("action"), it.get("requirements")))
    if it.get("topics"): db.execute("UPDATE items SET topics=? WHERE id=?", (it["topics"], cur.lastrowid))
    for m in msg_ids: db.execute("INSERT OR IGNORE INTO item_sources VALUES(?,?)", (cur.lastrowid, m))
    return cur.lastrowid

def _norm(t): return re.sub(r"[\s\W_]+", "", (t or "").lower())

def _origin_candidates(db):
    """Open items that may be an origin, with their normalised names. checker S2-3: an item that names an origin of its
    own is a child (registration, stage), even while still unresolved; it is never an origin ("origin IS NULL" alone let
    a sibling stage or an unresolved 报名 become one). A contest whose origin_title is its own name stays a candidate."""
    out = []
    for r in db.execute("SELECT id,title,extra,group_id,origin_title FROM items WHERE origin IS NULL AND status NOT IN ('done','cancelled','dismissed')"):
        names = [_norm(r["title"])]
        try: names.append(_norm((json.loads(r["extra"] or "null") or {}).get("name")))
        except (ValueError, AttributeError): pass
        if r["origin_title"] and _norm(r["origin_title"]) not in names: continue
        out.append((r["id"], r["group_id"], [n for n in names if n]))
    return out

def find_origin(db, title, exclude_id=None, group_id=None, _cands=None):
    """The task a registration / study / stage item belongs to: an OPEN item (never the item itself, never one that is
    itself a child) whose title or contest name matches the stated origin title. Needs one clear best match, else None
    (unresolved stays unresolved: nothing is invented)."""
    t = _norm(title)
    if len(t) < 3: return None
    best = []
    for rid, gid, names in (_cands if _cands is not None else _origin_candidates(db)):
        if rid == exclude_id: continue
        sc = 0
        for n in names:
            if n == t: sc = max(sc, 3)
            elif len(n) >= 4 and len(t) >= 4 and (n in t or t in n): sc = max(sc, 2)
        if sc: best.append((sc + (1 if group_id is not None and gid == group_id else 0), rid))
    if not best: return None
    best.sort(reverse=True)
    return best[0][1] if len(best) == 1 or best[0][0] > best[1][0] else None

def link_origin(db, item_id, origin_id):
    old = db.execute("SELECT origin FROM items WHERE id=?", (item_id,)).fetchone()
    db.execute("UPDATE items SET origin=? WHERE id=?", (origin_id, item_id))
    db.execute("INSERT INTO item_history(item_id,ts,field,old,new,kind) VALUES(?,strftime('%s','now'),'origin',?,?,'link')", (item_id, None if old is None or old[0] is None else str(old[0]), str(origin_id)))

def relink_origins(db):
    """Resolve items that named an origin but had none to link to yet (the origin may be announced later). Returns how many."""
    n = 0; cands = None                                         # checker S2-3: candidates loaded once per pass, not once per child
    for r in db.execute("SELECT id,origin_title,group_id FROM items WHERE origin IS NULL AND origin_title IS NOT NULL AND origin_title<>'' AND status NOT IN ('done','cancelled','dismissed')").fetchall():
        if cands is None: cands = _origin_candidates(db)
        o = find_origin(db, r["origin_title"], r["id"], r["group_id"], cands)
        if o:
            link_origin(db, r["id"], o); n += 1
            cands = [c for c in cands if c[0] != r["id"]]       # a self-named item that just got linked is no longer a candidate
    return n

UPDATABLE = ("title", "location", "start", "end", "due", "action", "requirements")

def history(db, item_id):
    """What changed on an item, oldest first: field, old value, new value, the message that said so."""
    return db.execute("SELECT * FROM item_history WHERE item_id=? ORDER BY id", (item_id,)).fetchall()

def merge_into(db, hit, it, msg_ids, change):
    """Fold a repeat/changed notice into the existing item `hit` (a Row) instead of creating a duplicate.
    Never invents: only fields the new notice actually states (non-null) are used. Blank old fields are filled.
    A differing value is a CHANGE only when the notice is change-worded (`change`): the old value goes to
    item_history and the new one replaces it. Otherwise the old value stays and the item is flagged needs_confirm
    with a 'conflict' history row. Idempotent per message. Returns list of changed fields."""
    iid = hit["id"]; fresh = [m for m in msg_ids if not db.execute("SELECT 1 FROM item_sources WHERE item_id=? AND msg_id=?", (iid, m)).fetchone()]
    for m in msg_ids: db.execute("INSERT OR IGNORE INTO item_sources VALUES(?,?)", (iid, m))
    if not fresh: return []
    mid = fresh[0]; changed = []
    for k in UPDATABLE:
        new, old = it.get(k), hit[k]
        if not new or new == old: continue
        if k == "title" and old and not re.search(r"取消|cancel", new, re.I): continue    # a change notice's title describes the change, not the event: keep the event's name
        if not old:
            db.execute(f"UPDATE items SET {k}=? WHERE id=?", (new, iid))
        elif change:
            db.execute(f"UPDATE items SET {k}=? WHERE id=?", (new, iid))
            db.execute("INSERT INTO item_history(item_id,ts,field,old,new,msg_id,kind) VALUES(?,strftime('%s','now'),?,?,?,?,'change')", (iid, k, old, new, mid)); changed.append(k)
        elif k not in ("title", "action", "requirements"):
            db.execute("INSERT INTO item_history(item_id,ts,field,old,new,msg_id,kind) VALUES(?,strftime('%s','now'),?,?,?,?,'conflict')", (iid, k, old, new, mid))
            db.execute("UPDATE items SET needs_confirm=1 WHERE id=?", (iid,))
    title_now = db.execute("SELECT title FROM items WHERE id=?", (iid,)).fetchone()[0]
    if change and re.search(r"取消(?!.{0,4}资格)|cancel+ed", it.get("quote") or "", re.I) and not re.search(r"取消|cancel", title_now, re.I):
        # checker S2: the notice says cancelled but the model kept the plain title -> do not lose it silently; ask the student
        db.execute("INSERT INTO item_history(item_id,ts,field,old,new,msg_id,kind) VALUES(?,strftime('%s','now'),'status',?,?,?,'conflict')",
                   (iid, hit["status"], "cancelled? " + (it.get("quote") or "")[:80], mid))
        db.execute("UPDATE items SET needs_confirm=1 WHERE id=?", (iid,))
    if changed: db.execute("UPDATE items SET confidence=COALESCE(?,confidence) WHERE id=?", (it.get("confidence"), iid))
    db.execute("UPDATE items SET priority=MAX(priority,?) WHERE id=?", (it.get("priority", 0), iid))
    return changed
