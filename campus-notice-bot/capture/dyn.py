"""Dynamic priority members + held posts (2026-11-10).

A priority member (owner/admin by role, or a named member) whose messages keep missing every keyword is not
adding calendar value, so:
  * each such message is a STRIKE (idempotent: a re-fetched message never strikes twice);
  * MORE THAN STRIKE_LIMIT strikes inside STRIKE_DAYS demotes the member: removed from priority_by_group /
    priority_members and listed under "demoted" in groups.json (with date and count). lookup_admins.py respects
    that list, so a re-run does not quietly promote them again;
  * the dropped text is HELD (priority members only, nobody else's), so the student can decide on opening NapCat
    whether a post that matched no keyword should still become a task (`cnb review`).
Nothing is held for ordinary members: their non-keyword messages are never stored.
"""
import hashlib, json, os, re, sqlite3, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS = os.path.join(HERE, "groups.json")
STRIKE_LIMIT = 5        # "more than 5 times"
STRIKE_DAYS = 30

PLACEHOLDER = re.compile(r"\[[a-z_]+\]|\s|[^\w一-鿿]")

def ensure(db):
    db.execute("CREATE TABLE IF NOT EXISTS strikes(group_id INTEGER, user_id INTEGER, ts INTEGER, h TEXT, PRIMARY KEY(group_id,user_id,ts,h))")
    db.execute("CREATE TABLE IF NOT EXISTS held(id INTEGER PRIMARY KEY, group_id INTEGER, user_id INTEGER, nick TEXT, ts INTEGER, text TEXT, state TEXT DEFAULT 'held', UNIQUE(group_id,user_id,ts,text))")
    db.commit()

def is_demoted(group, user):
    try: return str(user) in json.load(open(GROUPS, encoding="utf-8")).get("demoted", {}).get(str(group), {})
    except (OSError, ValueError): return False

def save_groups(c, path=None):
    """Atomic save shared by demote() and lookup_admins.py. listener.py re-reads groups.json on every message and drops
    everything if it is unreadable, so a half-written file must never be visible. Unique temp name per save (two savers
    can overlap), fsync, then rename over; the temp file is removed on any failure."""
    path = path or GROUPS
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".groups.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

def demote(group, user, n):
    c = json.load(open(GROUPS, encoding="utf-8"))
    g = str(group)
    if user in c.get("priority_by_group", {}).get(g, []): c["priority_by_group"][g].remove(user)
    if user in c.get("priority_members", []): c["priority_members"].remove(user)
    c.setdefault("demoted", {}).setdefault(g, {})[str(user)] = {"at": time.strftime("%Y-%m-%d"), "strikes": n}
    save_groups(c)

def irrelevant(db, group, user, nick, ts, text):
    """Call when a PRIORITY member's message was dropped by the keyword gate. Returns True if this demoted them."""
    if len(PLACEHOLDER.sub("", text or "")) < 4: return False          # a bare image/file/emoji is not "irrelevant talk"
    ensure(db)
    db.execute("INSERT OR IGNORE INTO held(group_id,user_id,nick,ts,text) VALUES(?,?,?,?,?)", (group, user, nick, ts, text))
    db.execute("INSERT OR IGNORE INTO strikes VALUES(?,?,?,?)", (group, user, ts, hashlib.sha1((text or "").encode()).hexdigest()[:12]))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM strikes WHERE group_id=? AND user_id=? AND ts>=?", (group, user, int(time.time()) - STRIKE_DAYS * 86400)).fetchone()[0]
    if n > STRIKE_LIMIT and not is_demoted(group, user):
        demote(group, user, n); print(f"demoted {user} in {group}: {n} irrelevant messages in {STRIKE_DAYS} days", flush=True)
        return True
    return False
