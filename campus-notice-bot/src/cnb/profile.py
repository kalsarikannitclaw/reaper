"""Ability profile (PLAN S2c): what the bot knows about this student, and how it learnt it.

Design rules taken straight from the PLAN, and the reasons they matter:
 - EVERY judgement stores its evidence (what the student actually said), a confidence, a source
   and a date. A level with no evidence is not a judgement, it is a guess.
 - STATUS claimed -> supported -> confirmed. A self-claim stays `claimed` until a concrete
   follow-up backs it up; a real result (a contest placing, a grade) makes it `confirmed`.
 - CONTRADICTIONS SUPERSEDE, never silently overwrite: the old row keeps its evidence and is
   marked superseded, so "why do you think that?" can always be answered, and a wrong correction
   can be undone.
 - The student can see every row, ask why, override it, and delete it ("forget X").
 - Honest limit, stated in the PLAN: a conversation can ESTIMATE a level. It cannot certify one.
   So fit is advisory and explainable, never a score the student is expected to trust blindly.
 - Per tenant only. Levels are never compared with, ranked against, or shared to another student.

Two ways facts arrive (PLAN v0.5):
 (a) the interview: a question bank per area, with a concrete probe as the follow-up;
 (b) passive catch from the student's OWN chat with the bot ("I did the Python course project"),
     stored as `claimed` with the quote as evidence, for confirmation in the next digest.
     Passive catch never reads other people's group messages.
"""
import hashlib, json, os, re, time
from . import db as D

LEVELS = ["none", "beginner", "intermediate", "advanced"]
BANK = os.path.join(D.ROOT, "question_bank.json")


def ddl(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS profile_facts(
        id INTEGER PRIMARY KEY, area TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
        level TEXT, evidence TEXT, confidence REAL, source TEXT, status TEXT DEFAULT 'claimed',
        created INTEGER, superseded_by INTEGER REFERENCES profile_facts(id));
    CREATE TABLE IF NOT EXISTS conversation_notes(
        id INTEGER PRIMARY KEY, ts INTEGER, text TEXT, summary TEXT, fact_id INTEGER REFERENCES profile_facts(id));
    CREATE INDEX IF NOT EXISTS pf_live ON profile_facts(key) WHERE superseded_by IS NULL;
    CREATE TABLE IF NOT EXISTS chat_turns(
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, channel TEXT NOT NULL, text TEXT NOT NULL,
        UNIQUE(channel, ts, text));
    CREATE TABLE IF NOT EXISTS note_facts(
        note_id INTEGER NOT NULL REFERENCES conversation_notes(id),
        fact_id INTEGER NOT NULL REFERENCES profile_facts(id), PRIMARY KEY(note_id, fact_id));
    -- S2d round 2: what the student asked the bot to forget, WITHOUT the text (see tombstones below).
    --  kind 'fact':   hash of the normalised profile key; forgot_ts = message time of the forget;
    --                 lifted_ts = message time of an explicit re-tell newer than the forget (NULL = still in force)
    --  kind 'source': hash of (channel, ts, normalised text) of a message that was deleted or must never be
    --                 re-read (a forgotten turn, the forget request itself), so a redelivery is recognised
    CREATE TABLE IF NOT EXISTS forget_tombstones(
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, hash TEXT NOT NULL,
        forgot_ts INTEGER NOT NULL, lifted_ts INTEGER, UNIQUE(kind, hash));
    """)
    # S2d columns, added in place so an existing tenant DB keeps its rows.
    #  pending=1: a chat claim that CONTRADICTS a fact the student already backed up or confirmed. It is
    #  kept and shown, but it does not replace the stronger fact until the student says so (no silent
    #  overwrite in either direction).
    fcols = [r[1] for r in db.execute("PRAGMA table_info(profile_facts)")]
    if "pending" not in fcols:
        db.execute("ALTER TABLE profile_facts ADD COLUMN pending INTEGER DEFAULT 0")
    #  acked=1 (round 2): the student explicitly said "yes, that is what I meant" about THIS shown fact
    #  ('confirm python' in chat). It takes the fact off the please-confirm list; it is NOT evidence, so the
    #  status (claimed/supported/confirmed) does not change. See acknowledge().
    if "acked" not in fcols:
        db.execute("ALTER TABLE profile_facts ADD COLUMN acked INTEGER DEFAULT 0")
    ncols = [r[1] for r in db.execute("PRAGMA table_info(conversation_notes)")]
    for c in ("turn_id", "channel"):
        if c not in ncols:
            db.execute(f"ALTER TABLE conversation_notes ADD COLUMN {c} {'INTEGER' if c == 'turn_id' else 'TEXT'}")
    db.commit()


STRONG = ("supported", "confirmed")


def _live(db, key):
    """The fact the bot currently USES for this key (never a pending proposal)."""
    return db.execute("SELECT * FROM profile_facts WHERE key=? AND superseded_by IS NULL AND COALESCE(pending,0)=0"
                      " ORDER BY id DESC", (key,)).fetchone()


def add_fact(db, area, key, value=None, level=None, evidence=None, confidence=0.5,
             source="interview", status="claimed", created=None):
    """Insert a fact. If a live fact for the same key exists and this one says something
    different, the old one is superseded (kept, with its evidence) rather than overwritten.

    One exception (S2d): a mere CLAIM never replaces a fact the student has already backed up or
    confirmed. It is stored as pending - visible, on the confirm list - and only takes over when the
    student confirms it. Otherwise one passing remark in chat ("I've used Verilog a bit") would quietly
    downgrade a confirmed 'intermediate'."""
    ddl(db)
    if level and level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    old = _live(db, key)
    differs = bool(old) and (old["level"] != level or (old["value"] or "") != (value or ""))
    # held against a backed-up/confirmed fact, and (round 2) against a claim the student explicitly acknowledged
    hold = differs and status == "claimed" and (old["status"] in STRONG or bool(old["acked"]))
    cur = db.execute("INSERT INTO profile_facts(area,key,value,level,evidence,confidence,source,status,created,pending)"
                     " VALUES(?,?,?,?,?,?,?,?,COALESCE(?,strftime('%s','now')),?)",
                     (area, key, value, level, evidence, confidence, source, status, created, 1 if hold else 0))
    new = cur.lastrowid
    if hold:
        # an older pending proposal for the same key is replaced by this newer one, never stacked
        db.execute("UPDATE profile_facts SET superseded_by=? WHERE key=? AND pending=1 AND superseded_by IS NULL AND id<>?",
                   (new, key, new))
        db.commit()
        return new
    # a fact that is now in force makes any pending proposal for this key stale
    db.execute("UPDATE profile_facts SET superseded_by=? WHERE key=? AND pending=1 AND superseded_by IS NULL",
               (new, key))
    if old and differs:
        db.execute("UPDATE profile_facts SET superseded_by=? WHERE id=?", (new, old["id"]))
    elif old:
        # same claim again: keep one row, but let repetition and new evidence raise its standing.
        # A claim repeated in chat stays 'claimed' (PLAN: claimed until the student confirms); only a
        # repeated interview answer earns 'supported'. Status never goes DOWN on repetition.
        db.execute("UPDATE profile_facts SET superseded_by=? WHERE id=?", (new, old["id"]))
        db.execute("UPDATE profile_facts SET acked=? WHERE id=?", (old["acked"] or 0, new))
        if old["status"] in STRONG and status == "claimed":
            # Round 2 (round-1 open issue 3): saying it again is not new backing, so the backed fact keeps its
            # ORIGINAL date and keeps decaying; before, a bare repeat renewed a 900-day-old confirmed fact.
            db.execute("UPDATE profile_facts SET status=?, confidence=max(COALESCE(confidence,0),?), created=? WHERE id=?",
                       (old["status"], old["confidence"] or 0, old["created"], new))
        elif old["status"] == "claimed" and status == "claimed" and evidence:
            if source == "dm":
                db.execute("UPDATE profile_facts SET confidence=? WHERE id=?",
                           (min(0.6, max(old["confidence"] or .35, confidence or 0) + .1), new))
            else:
                db.execute("UPDATE profile_facts SET status='supported', confidence=? WHERE id=?",
                           (min(0.9, (old["confidence"] or .5) + .2), new))
    db.commit()
    return new


# ---------------- decay ----------------
# A judgement gets less trustworthy as it ages: people learn, and availability changes every term.
# Confidence halves every HALF_LIFE days; a fact below STALE is kept and shown ("please re-confirm")
# but no longer used for fit or grade. Nothing is deleted by decay - only the student deletes.
HALF_LIFE = {"claimed": 120, "supported": 365, "confirmed": 730}
VOLATILE = {"hours_per_week": 60, "has_team": 60}
STALE = 0.15


def effective_confidence(row, now=None):
    now = now or time.time()
    c = row["confidence"] or 0
    hl = VOLATILE.get(row["key"]) or HALF_LIFE.get(row["status"] or "claimed", 120)
    age_days = max(0.0, (now - (row["created"] or now)) / 86400)
    return round(c * 0.5 ** (age_days / hl), 2)


def is_stale(row, now=None):
    return effective_confidence(row, now) < STALE


def facts(db, live=True, area=None):
    """live=True: every row not superseded, INCLUDING pending proposals (they are shown to the student).
    Use levels()/value_of() for what the bot actually acts on."""
    ddl(db)
    q = "SELECT * FROM profile_facts" + (" WHERE superseded_by IS NULL" if live else " WHERE 1=1")
    p = []
    if area:
        q += " AND area=?"
        p.append(area)
    return db.execute(q + " ORDER BY area, key, id DESC", p).fetchall()


def levels(db, now=None):
    """key -> (level, status, confidence). What fit() reads: in-force facts only (no pending
    proposals, nothing stale), with confidence decayed by age."""
    out = {}
    for r in facts(db):
        if r["level"] and not r["pending"] and r["key"] not in out and not is_stale(r, now):
            out[r["key"]] = (r["level"], r["status"], effective_confidence(r, now))
    return out


def value_of(db, key, default=None, now=None):
    ddl(db)
    r = _live(db, key)
    return r["value"] if r and r["value"] is not None and not is_stale(r, now) else default


# ---------------- tombstones (S2d round 2) ----------------
# Forget deletes the turn, so the (channel, ts, text) dedupe went with it: a redelivered message or a DM
# history backfill would have taught the bot the forgotten fact again. A tombstone remembers THAT something
# was forgotten and WHEN, without keeping WHAT was said:
#  - fact tombstone: sha256 of the normalised key. Passive catch never re-learns that key from a message
#    dated at or before the forget (replay, backfill), lifted or not.
#  - source tombstone: sha256 of (channel, ts, normalised text) of every turn the forget deleted, and of the
#    forget request itself, so an exact redelivery is dropped whole (a replayed 'forget python' cannot delete
#    a fact the student re-taught later).
# LIFTING (decision): only an EXPLICIT re-tell in a turn NEWER than the forget lifts it ('remember: ...' /
# '记住...' in chat, or answering that interview question). A passing mention after the forget is not
# learnt; the bot says how to re-teach it. Even once lifted, messages dated before the forget stay blocked.
# Honest limit: keys come from a small vocabulary, so a fact hash can be reversed by guessing the key; it
# hides the student's words, not the fact that 'python' was forgotten. The source hash covers free text.

def _h(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def norm_text(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def source_hash(channel, ts, text):
    return _h("src", str(channel), int(ts), norm_text(text))


def tombstone_source(db, channel, ts, text, forgot_ts=None):
    ddl(db)
    db.execute("INSERT OR IGNORE INTO forget_tombstones(kind,hash,forgot_ts) VALUES('source',?,?)",
               (source_hash(channel, ts, text), int(forgot_ts if forgot_ts is not None else time.time())))
    db.commit()


def is_forgotten_source(db, channel, ts, text):
    ddl(db)
    return db.execute("SELECT 1 FROM forget_tombstones WHERE kind='source' AND hash=?",
                      (source_hash(channel, ts, text),)).fetchone() is not None


def _fact_stone(db, key):
    return db.execute("SELECT * FROM forget_tombstones WHERE kind='fact' AND hash=?", (_h("fact", key),)).fetchone()


def blocked(db, key, ts=None):
    """Why passive catch must NOT learn `key` from a message dated `ts`, or None if it may.
    'before_forget': the message is not newer than the forget (replay / backfill) - never, even if lifted.
    'forgotten':     newer, but the student has not explicitly re-told it since the forget."""
    ddl(db)
    t = _fact_stone(db, key)
    if not t:
        return None
    ts = int(ts if ts is not None else time.time())
    if ts <= t["forgot_ts"]:
        return "before_forget"
    return None if t["lifted_ts"] is not None else "forgotten"


def lift(db, key, ts=None):
    """Explicit re-tell: allowed only from a turn NEWER than the forget. -> True if a tombstone was lifted."""
    ddl(db)
    t = _fact_stone(db, key)
    ts = int(ts if ts is not None else time.time())
    if not t or ts <= t["forgot_ts"]:
        return False
    db.execute("UPDATE forget_tombstones SET lifted_ts=? WHERE id=?", (ts, t["id"]))
    db.commit()
    return True


def forgotten_keys(db, candidates=None):
    """Which of these keys (default: every known key) carry a tombstone that is still in force (not lifted).
    Only hashes are stored, so this tests candidates; it cannot list free-text keys nobody names."""
    ddl(db)
    live = {r["hash"] for r in db.execute("SELECT hash FROM forget_tombstones WHERE kind='fact' AND lifted_ts IS NULL")}
    cands = set(KNOWN.values()) | set(candidates or [])
    return sorted(k for k in cands if _h("fact", k) in live)


def names_any(text, key):
    """Does the text name this key (any alias of 2+ characters)?"""
    low = (text or "").lower()
    return any(n in low for n in _names(key))


def forget_is_stale(db, key, ts=None):
    """Checker fix (S2d r2): a forget request OLDER than the explicit re-tell that lifted this key's tombstone
    (a backfilled older 'forget python', or the old request redelivered under another channel label, so the
    source tombstone does not match). The student has re-taught the key since, so it must not delete that."""
    ddl(db)
    t = _fact_stone(db, key)
    ts = int(ts if ts is not None else time.time())
    return bool(t) and t["lifted_ts"] is not None and ts < t["lifted_ts"]


def forget(db, key, ts=None):
    """The student's right to delete. The rows go, and so do the notes - and the chat turns - that
    produced them: a quote the student asked us to forget must not survive in a summary either.
    Round 2: leaves a tombstone (key hash + time, sources hashed) so a replay/backfill cannot re-teach it.
    ts: message time of the forget request (default now)."""
    ddl(db)
    ts = int(ts if ts is not None else time.time())
    if forget_is_stale(db, key, ts):            # checker fix (S2d r2): older than the re-tell -> nothing deleted
        return 0
    db.execute("INSERT INTO forget_tombstones(kind,hash,forgot_ts) VALUES('fact',?,?) ON CONFLICT(kind,hash)"
               " DO UPDATE SET forgot_ts=max(forgot_ts, excluded.forgot_ts), lifted_ts=NULL", (_h("fact", key), ts))
    ids = [r["id"] for r in db.execute("SELECT id FROM profile_facts WHERE key=?", (key,))]
    if not ids:
        _purge_mentions(db, key, ts)
        db.commit()
        return 0
    marks = ",".join("?" * len(ids))
    notes = {r[0] for r in db.execute(f"SELECT id FROM conversation_notes WHERE fact_id IN ({marks})", ids)}
    notes |= {r[0] for r in db.execute(f"SELECT note_id FROM note_facts WHERE fact_id IN ({marks})", ids)}
    if notes:
        nm = ",".join("?" * len(notes))
        turns = [r[0] for r in db.execute(f"SELECT turn_id FROM conversation_notes WHERE id IN ({nm}) AND turn_id IS NOT NULL",
                                          list(notes))]
        db.execute(f"DELETE FROM note_facts WHERE note_id IN ({nm})", list(notes))
        db.execute(f"DELETE FROM conversation_notes WHERE id IN ({nm})", list(notes))
        if turns:
            _delete_turns(db, turns, ts)
    db.execute(f"DELETE FROM note_facts WHERE fact_id IN ({marks})", ids)
    db.execute("UPDATE profile_facts SET superseded_by=NULL WHERE superseded_by IN (%s)" % marks, ids)
    db.execute("DELETE FROM profile_facts WHERE key=?", (key,))
    _purge_mentions(db, key, ts)
    db.commit()
    return len(ids)


_CLAUSE = re.compile(r"[，,、：:]")
REDACTED = "[rest of this sentence removed: you asked the bot to forget it]"


def _names(key):
    """Every way the forgotten thing is written (python / c++ / 算法 ...). One-letter names ('c') are left
    out: matching them would wipe unrelated text."""
    return sorted({n for n in [key, *[a for a, k in KNOWN.items() if k == key]] if n and len(n) >= 2}, key=len, reverse=True)


def _delete_turns(db, turn_ids, forgot_ts):
    """Delete chat turns, leaving a source tombstone for each so a redelivery is recognised."""
    for t in turn_ids:
        r = db.execute("SELECT channel, ts, text FROM chat_turns WHERE id=?", (t,)).fetchone()
        if r:
            db.execute("INSERT OR IGNORE INTO forget_tombstones(kind,hash,forgot_ts) VALUES('source',?,?)",
                       (source_hash(r["channel"], r["ts"], r["text"]), forgot_ts))
        db.execute("DELETE FROM chat_turns WHERE id=?", (t,))


def redact_turn(db, turn_id, keys, forgot_ts=None):
    """One turn named something the student asked the bot to forget (a replay, a backfill, or a passing
    mention before they re-taught it). Delete THAT turn and its note (source tombstoned, so a redelivery is
    dropped), and trim the forgotten words out of the evidence of facts it produced. Unlike forget(), other
    turns are untouched: after a lift, newer turns about the key are legitimate."""
    ddl(db)
    names = sorted({n for k in keys for n in _names(k)}, key=len, reverse=True)
    hit = lambda s: any(n in (s or "").lower() for n in names)   # noqa: E731
    notes = [r[0] for r in db.execute("SELECT id FROM conversation_notes WHERE turn_id=?", (turn_id,))]
    fids = {r[0] for n in notes for r in db.execute("SELECT fact_id FROM note_facts WHERE note_id=?", (n,))}
    for n in notes:
        db.execute("DELETE FROM note_facts WHERE note_id=?", (n,))
        db.execute("DELETE FROM conversation_notes WHERE id=?", (n,))
    _delete_turns(db, [turn_id], int(forgot_ts if forgot_ts is not None else time.time()))
    for f in fids:
        r = db.execute("SELECT evidence FROM profile_facts WHERE id=?", (f,)).fetchone()
        if r and hit(r["evidence"]):
            keep = [c.strip() for c in _CLAUSE.split(r["evidence"]) if c.strip() and not hit(c)]
            db.execute("UPDATE profile_facts SET evidence=? WHERE id=?",
                       ("，".join(keep + [REDACTED]) if keep else REDACTED, f))
    db.commit()


def _purge_mentions(db, key, forgot_ts=None):
    """Checker fix (S2d): the linked-row delete above misses three places the forgotten word survived:
    (1) another fact's evidence that shares the sentence ("我用过Python，每周6小时" -> the hours fact),
    (2) `cnb tell` notes of sibling facts, which store the whole message,
    (3) turns/notes no longer linked to a fact (a dismissed proposal, 'confirm python' command turns).
    Turns and notes that still name it are deleted; a surviving fact keeps only the clauses of its
    evidence that do not name it."""
    names = _names(key)
    if not names:
        return
    hit = lambda s: any(n in (s or "").lower() for n in names)   # noqa: E731
    turns = [r["id"] for r in db.execute("SELECT id, text FROM chat_turns") if hit(r["text"])]
    notes = {r["id"] for r in db.execute("SELECT id, text, summary FROM conversation_notes") if hit(r["text"]) or hit(r["summary"])}
    for t in turns:
        notes |= {r[0] for r in db.execute("SELECT id FROM conversation_notes WHERE turn_id=?", (t,))}
    _delete_turns(db, turns, int(forgot_ts if forgot_ts is not None else time.time()))
    for n in notes:
        db.execute("DELETE FROM note_facts WHERE note_id=?", (n,))
        db.execute("DELETE FROM conversation_notes WHERE id=?", (n,))
    for r in db.execute("SELECT id, evidence FROM profile_facts").fetchall():
        if hit(r["evidence"]):
            keep = [c.strip() for c in _CLAUSE.split(r["evidence"]) if c.strip() and not hit(c)]
            db.execute("UPDATE profile_facts SET evidence=? WHERE id=?",
                       ("，".join(keep + [REDACTED]) if keep else REDACTED, r["id"]))


def confirm(db, key, status="confirmed"):
    """The student says yes. If a pending proposal exists for this key (they told the bot something
    new that contradicts what it had), confirming accepts THAT newest statement; the old fact is
    superseded and kept. Otherwise the fact in force is confirmed."""
    ddl(db)
    p = db.execute("SELECT * FROM profile_facts WHERE key=? AND pending=1 AND superseded_by IS NULL ORDER BY id DESC",
                   (key,)).fetchone()
    if p:
        old = _live(db, key)
        db.execute("UPDATE profile_facts SET pending=0, status=?, confidence=max(COALESCE(confidence,0),0.9),"
                   " created=strftime('%s','now') WHERE id=?",
                   (status, p["id"]))
        if old:
            db.execute("UPDATE profile_facts SET superseded_by=? WHERE id=?", (p["id"], old["id"]))
    else:
        # confirming is fresh evidence that it is still true, so it also restarts the decay clock
        db.execute("UPDATE profile_facts SET status=?, confidence=max(COALESCE(confidence,0),0.9),"
                   " created=strftime('%s','now') WHERE key=? AND superseded_by IS NULL", (status, key))
    db.commit()
    return bool(p) or _live(db, key) is not None


def acknowledge(db, key, now=None):
    """Chat 'confirm <key>' (round-2 decision, see build.md). The student says "yes, that is what I meant"
    about ONE fact the bot showed them. It is NOT evidence: a self-claim stays 'claimed' (PLAN: claimed until
    backed by an answer or result), so it can never earn grade A on its own.
      - a pending proposal for the key: it is accepted (in force, acked, still claimed); the old fact is
        superseded and kept;
      - an in-force 'claimed' fact: acked, off the please-confirm list, decay clock restarted (it is
        fresh word that it is still true), confidence raised to the claim cap 0.6;
      - an in-force supported/confirmed fact: nothing changes (a bare yes does not renew real backing).
    -> 'accepted' | 'acked' | 'already_backed' | 'nothing'."""
    ddl(db)
    now = int(now if now is not None else time.time())
    p = db.execute("SELECT * FROM profile_facts WHERE key=? AND pending=1 AND superseded_by IS NULL ORDER BY id DESC",
                   (key,)).fetchone()
    if p:
        old = _live(db, key)
        # checker fix (S2d r2): max(), so a confirm with an old timestamp (backfill) cannot move the date BACK
        db.execute("UPDATE profile_facts SET pending=0, acked=1, confidence=max(COALESCE(confidence,0),0.6),"
                   " created=max(COALESCE(created,0),?) WHERE id=?", (now, p["id"]))
        if old:
            db.execute("UPDATE profile_facts SET superseded_by=? WHERE id=?", (p["id"], old["id"]))
        db.commit()
        return "accepted"
    cur = _live(db, key)
    if not cur:
        return "nothing"
    if cur["status"] in STRONG:
        return "already_backed"
    db.execute("UPDATE profile_facts SET acked=1, confidence=max(COALESCE(confidence,0),0.6),"
               " created=max(COALESCE(created,0),?) WHERE id=?", (now, cur["id"]))
    db.commit()
    return "acked"


def dismiss(db, key):
    """The student says no to a pending proposal: it goes, the fact in force stays. -> rows removed."""
    ddl(db)
    ids = [r[0] for r in db.execute("SELECT id FROM profile_facts WHERE key=? AND pending=1", (key,))]
    for i in ids:
        db.execute("DELETE FROM note_facts WHERE fact_id=?", (i,))
        db.execute("DELETE FROM profile_facts WHERE id=?", (i,))
    db.commit()
    return len(ids)


# ---------------- question bank ----------------
DEFAULT_BANK = {
    "_comment": "Interview questions per area. 'probe' is the concrete follow-up that turns a "
                "self-claim into a supported one: it asks for something only someone who has "
                "actually done the thing can answer. Edit freely; the bot reads this file.",
    "areas": [
        {"area": "programming", "questions": [
            {"key": "python", "ask": "How would you describe your Python?", "probe": "What is the last thing you built with it, and what did it do?"},
            {"key": "cpp", "ask": "And C or C++?", "probe": "What did you use it for most recently?"},
            {"key": "algorithms", "ask": "How comfortable are you with algorithms and data structures?", "probe": "Which problems or judges have you practised on, roughly how many?"}]},
        {"area": "hardware", "questions": [
            {"key": "verilog", "ask": "Have you written any Verilog or VHDL?", "probe": "Which board or tool did you use, and what did you make run on it?"},
            {"key": "electronics", "ask": "How about circuits and electronics generally?", "probe": "What have you actually built or measured?"}]},
        {"area": "maths", "questions": [
            {"key": "maths_modelling", "ask": "How much mathematical modelling have you done?", "probe": "Which model or paper did you work on, and what was your part in it?"}]},
        {"area": "language", "questions": [
            {"key": "english_writing", "ask": "How is your written English for a report or paper?", "probe": "What is the longest thing you have written in English, and for what?"}]},
        {"area": "availability", "questions": [
            {"key": "hours_per_week", "ask": "Realistically, how many hours a week can you give a contest?", "kind": "number"},
            {"key": "has_team", "ask": "Do you already have a team you would enter with?", "kind": "yesno"}]},
        {"area": "goals", "questions": [
            {"key": "goals", "ask": "What are you hoping to get out of this (postgraduate recommendation, experience, a specific contest)?", "kind": "text"}]},
    ]
}


def bank():
    if not os.path.exists(BANK):
        json.dump(DEFAULT_BANK, open(BANK, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        return DEFAULT_BANK
    try:
        return json.load(open(BANK, encoding="utf-8"))
    except ValueError:
        raise SystemExit("question_bank.json is not valid JSON")


SUBSTANTIVE = re.compile(r"\d|项目|比赛|课程|实习|实验|写了|做了|用过|参加|project|built|wrote|used|course|contest|intern|team|board|paper", re.I)


def judge(answer, probe_answer=None):
    """Turn an answer into (level, status, confidence, evidence). Deliberately conservative.

    A claim alone is `claimed`. It becomes `supported` only when the probe answer is concrete:
    it names something specific (a project, a course, a count, a tool) rather than repeating the
    claim. This is the PLAN's "grill with concrete follow-ups": a student who says "advanced" and
    then cannot say what they built is recorded as claiming advanced, not as being advanced.
    No LLM here, so the rule is inspectable and testable; S5 may add a model for the hard cases."""
    a = (answer or "").strip().lower()
    level = None
    if re.search(r"^(no|none|never|不会|没有|没学过|0)$|never|不会|没接触", a):
        level = "none"
    elif re.search(r"advanced|fluent|very good|熟练|精通|很好|competition|竞赛级", a):
        level = "advanced"
    elif re.search(r"intermediate|ok|okay|fine|中等|一般|还行|会用|comfortable", a):
        level = "intermediate"
    elif re.search(r"beginner|basic|little|入门|初学|基础|一点|刚开始", a):
        level = "beginner"
    if level is None:
        for i, L in enumerate(LEVELS):
            if L in a:
                level = L
    if level is None:
        return None, None, None, answer
    p = (probe_answer or "").strip()
    if level == "none":
        # "I have never used it" needs no backing up: there is no artefact to point at. Holding
        # it at 'claimed' would put it on the student's confirm list for no reason.
        return level, "supported", 0.7, answer
    supported = bool(p) and len(p) >= 8 and bool(SUBSTANTIVE.search(p))
    if level in ("advanced", "intermediate") and not supported:
        return level, "claimed", 0.4, (answer or "") + (f" | probe: {p}" if p else " | probe: not answered")
    return level, ("supported" if supported else "claimed"), (0.8 if supported else 0.5), \
        (answer or "") + (f" | probe: {p}" if p else "")


def interview(db, ask, areas=None):
    """Run the interview. `ask(question) -> answer string` is supplied by the caller, so the same
    code serves the CLI now and a QQ DM later (PLAN: interview moves to DM at S5)."""
    ddl(db)
    done = 0
    for area in bank()["areas"]:
        if areas and area["area"] not in areas:
            continue
        for q in area["questions"]:
            kind = q.get("kind", "level")
            a = ask(q["ask"])
            if a is None or not str(a).strip():
                continue
            lift(db, q["key"])                  # answering the question IS an explicit re-tell (round 2)
            if kind == "number":
                m = re.search(r"\d+", str(a))
                if m:
                    add_fact(db, area["area"], q["key"], value=m.group(0), evidence=str(a),
                             confidence=0.8, status="supported")
                    done += 1
                continue
            if kind == "yesno":
                v = "yes" if re.search(r"^(y|yes|是|有|对)", str(a).strip().lower()) else "no"
                add_fact(db, area["area"], q["key"], value=v, evidence=str(a), confidence=0.8, status="supported")
                done += 1
                continue
            if kind == "text":
                add_fact(db, area["area"], q["key"], value=str(a).strip(), evidence=str(a),
                         confidence=0.7, status="supported")
                done += 1
                continue
            probe_a = ask(q["probe"]) if q.get("probe") else None
            level, status, conf, ev = judge(str(a), probe_a and str(probe_a))
            if level:
                add_fact(db, area["area"], q["key"], level=level, evidence=ev, confidence=conf, status=status)
                done += 1
    return done


# ---------------- passive catch (PLAN v0.5 (b)) ----------------
# Negated forms come FIRST and their span is masked, so "没用过Python" / "never used Python" is read as
# 'none' and not, by the plain "用过Python" pattern inside it, as 'beginner'.
NEG = r"(?<!没)(?<!不)(?<!未)"
CATCH = [
    (r"(?:没有?|从没|未|不会)(?:做过|写过|用过|学过|接触过)?\s*([A-Za-z一-鿿+#]{2,20})", "none"),
    (r"\bi(?: have never|['’]ve never| never| haven['’]t| have not| don['’]t know| do not know) (?:used |written |learn(?:ed|t) |done |touched )?(?:any )?([A-Za-z+#]{2,20})", "none"),
    (r"(?:我)?(?:熟练|精通)\s*([A-Za-z一-鿿+#]{2,20})", "advanced"),
    (r"i'?m (?:good|experienced|fluent) (?:at|with|in) ([A-Za-z+#]{2,20})", "advanced"),
    (NEG + r"(?:我)?(?:做过|写过|用过|学过)\s*([A-Za-z一-鿿+#]{2,20})", "beginner"),
    (r"\bi(?: have|['’]ve)? (?:used|written|built|done|learn(?:ed|t)) (?:a |an |the |some )?([A-Za-z+#]{2,20})", "beginner"),
    (r"(?:每周|一周)(?:大概|大约|只有|能有)?\s*(\d+)\s*(?:个)?小时", "hours_per_week"),
    (r"(?<![\d.])(\d+)\s*(?:hours?|hrs?) (?:a|per|each) week", "hours_per_week"),
    (r"(没有队友|还没(?:有)?队|没组队|no team|don'?t have a team|without a team)", "team_no"),
    (r"(已经组(?:好)?队|有队友|有(?:一个|个)?团队|(?:have|got|found) a team|my team)", "team_yes"),
]
KNOWN = {"python": "python", "c++": "cpp", "cpp": "cpp", "c": "cpp", "verilog": "verilog", "vhdl": "verilog",
         "算法": "algorithms", "algorithms": "algorithms", "数学建模": "maths_modelling",
         "建模": "maths_modelling", "modelling": "maths_modelling", "modeling": "maths_modelling",
         "英语": "english_writing", "english": "english_writing", "fpga": "verilog", "电路": "electronics",
         "electronics": "electronics", "单片机": "electronics"}
_SENT = re.compile(r"[^。！？!?；;\n]+?(?:[。！？!?；;\n]|\.(?=\s)|$)")


def area_of(key):
    """The bank's area for a key, so a caught fact lands where the interview would have put it."""
    try:
        for a in bank()["areas"]:
            if any(q["key"] == key for q in a["questions"]):
                return a["area"]
    except (SystemExit, KeyError, TypeError):
        pass
    return "other"


def _sentence(text, pos):
    """The sentence a match sits in: evidence is the student's own words, but only the words that
    support THIS fact, so forgetting one fact does not leave its quote inside another fact's evidence."""
    for m in _SENT.finditer(text or ""):
        if m.start() <= pos < m.end():
            return m.group(0).strip()
    return (text or "").strip()


def _known(tok):
    """'python和c++' / '数学建模比赛': the token class also eats the words after a skill name, so fall
    back to the longest known name the token STARTS with."""
    if tok in KNOWN:
        return KNOWN[tok]
    best = max((n for n in KNOWN if len(n) >= 2 and tok.startswith(n)), key=len, default=None)
    return KNOWN[best] if best else None


def propose(text):
    """Pure reading, no DB: -> [(area, key, level, value, evidence)] the text appears to state."""
    low = (text or "").lower()
    masked, out, seen = low, [], set()
    for pat, meaning in CATCH:
        for m in re.finditer(pat, masked, re.I):
            tok = m.group(1).strip().lower()
            if meaning == "hours_per_week":
                k = ("hours_per_week", None, tok)
            elif meaning in ("team_no", "team_yes"):
                k = ("has_team", None, "yes" if meaning == "team_yes" else "no")
            else:
                key = _known(tok)
                if not key:
                    continue
                k = (key, meaning, None)
            ev = _sentence(text, m.start())
            masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
            if k[0] in seen:
                continue                      # first (most specific) reading of a key in this text wins
            seen.add(k[0])
            out.append((area_of(k[0]), k[0], k[1], k[2], ev))
    return out


def catch(db, text, source="dm", created=None, note=True, skipped=None):
    """Read ONE message the student sent to the bot and propose facts from it.
    Everything found is `claimed` with the quote as evidence, and shows up in the next digest for
    confirm/dismiss: a passing remark is a lead, not a judgement. Never run on group messages.
    note=False lets chat.turn() write one note for the whole turn instead of one per fact.
    Round 2: a key with a forget tombstone is NOT learnt (see blocked()); pass a list as `skipped` to get
    the (key, reason) pairs that were left out."""
    ddl(db)
    out = []
    for area, key, level, value, ev in propose(text):
        why = blocked(db, key, created)
        if why:
            if skipped is not None:
                skipped.append((key, why))
            continue
        out.append(add_fact(db, area, key, level=level, value=value, evidence=ev,
                            confidence=0.4 if value else 0.35, source=source, status="claimed", created=created))
    if note:
        for fid in out:
            db.execute("INSERT INTO conversation_notes(ts,text,summary,fact_id) VALUES(strftime('%s','now'),?,?,?)",
                       (text, "passive catch", fid))
    db.commit()
    return out


def unconfirmed(db):
    """Facts waiting for a yes/no from the student, for the digest's 'please confirm' list
    (including pending proposals that contradict a fact already in force)."""
    return [r for r in facts(db) if r["status"] == "claimed" and (r["pending"] or not r["acked"] or is_stale(r))]


def _label(r, now=None):
    if r["pending"]:
        return "pending"
    if is_stale(r, now):
        return "stale"
    if r["status"] in STRONG or r["acked"]:
        return "confirmed"
    return "pending"


LABEL_MEANING = {
    "pending": "waiting for the student: a self-claim not yet acknowledged, or a proposed change held until they say yes",
    "stale": "old and not re-confirmed: kept for the record, NOT used for fit or grade",
    "confirmed": "in force and either backed up (status supported/confirmed) or acknowledged by the student"
                 " (status stays claimed: acknowledged is not proof)"}


def snapshot_facts(db, now=None):
    """Every live fact, labelled for an AI reader (research.memory_snapshot): label pending | stale |
    confirmed (LABEL_MEANING), plus the evidence status, whether fit/grade uses it, and the evidence,
    scrubbed of phones/emails here already. A held proposal is never presented as current."""
    from .extract import scrub                 # lazy, as in chat.py
    out = []
    for r in facts(db):
        lab = _label(r, now)
        # checker fix (S2d r2): free-text values (goals) were passed raw; a phone/email in them reached the reader
        val = scrub(r["value"])[:160] if isinstance(r["value"], str) else r["value"]
        out.append({"key": r["key"], "area": r["area"], "level": r["level"], "value": val,
                    "label": lab, "status": r["status"], "acknowledged": bool(r["acked"]),
                    "proposal_held": bool(r["pending"]),
                    "used_for_grading": not r["pending"] and not is_stale(r, now),
                    "confidence": effective_confidence(r, now), "source": r["source"],
                    "because_you_said": scrub(r["evidence"] or "")[:160]})
    return out


def summary(db):
    rows = facts(db)
    if not rows:
        return "The bot knows nothing about your abilities yet. Run: cnb interview"
    out = []
    for r in rows:
        what = r["level"] or r["value"]
        tag = (" PROPOSED, not used until you confirm (cnb confirm %s / say 'dismiss %s')" % (r["key"], r["key"])
               if r["pending"] else (" STALE, please re-confirm" if is_stale(r) else ""))
        ack = ", you agreed" if r["acked"] and r["status"] == "claimed" else ""
        out.append(f"  {r['key']:18} {str(what):14} [{r['status']}{ack}, confidence {effective_confidence(r):.2f}, "
                   f"{r['source']}]{tag}\n      because you said: {(r['evidence'] or '')[:100]}")
    return "What the bot thinks it knows about you:\n" + "\n".join(out)
