"""Daily chat -> memory (PLAN Memory section; stage S2d).

One function, turn(), takes ONE message the student sent to the bot and returns what the bot learnt from
it. It is channel-agnostic on purpose: the MaiBot/AstrBot adapter (S0 decision: mixed, only one bot owns
replies) calls it, `cnb chat "..."` calls it, a web page could call it. It never sends anything itself; it
returns a suggested `reply` and the caller (the one bot that owns replies) decides whether to say it.

What a turn produces:
 - the turn itself (chat_turns), stored locally in the tenant's own DB, deduplicated on (channel, ts, text)
   so an adapter that redelivers a message does not double-count it;
 - ONE conversation note (episodic memory): a short summary linked to its turn and to every fact it gave;
 - proposed profile facts via profile.catch: all 'claimed', the student's own sentence as evidence,
   source 'dm'. A claim that contradicts a backed-up or confirmed fact is held as a pending proposal
   instead of replacing it (profile.add_fact). Old facts decay by age (profile.effective_confidence).

Rules carried from the PLAN:
 - Passive catch reads only the student's OWN chat with the bot. Round 2: an ALLOW-LIST gate (gate()) decides
   per event; default = the owner's private chat only, no group. A group-looking free-text label is still refused.
 - Nothing leaves the machine here. The optional `summarize` hook (e.g. an LLM) only ever receives the
   scrubbed text (extract.scrub: phones/emails removed).
 - The bot never registers or commits on the student's behalf; nothing here acts on anything.
 - Per tenant: `db` is that tenant's own store; nothing is compared with or shared to anyone else.
 - Round 2: forgetting leaves a tombstone (profile.py), so a replay or backfill cannot re-teach it; only an
   explicit 'remember: ...' in a newer turn lifts it. A bare "ok"/"好的" never confirms anything.
"""
import re, time
from . import profile as P

# Checker fix (S2d): matched ANYWHERE in the label, not only at its start - "qq_group_123", "QQ群",
# "onebot:group:5", "guild" were accepted before. Used for the free-text `channel` label when no `where`
# event description is given (CLI, tests, legacy callers).
GROUPISH = re.compile(r"group|grp|qun|群|guild|频道|discuss|讨论组", re.I)

# Explicit memory commands the student can type in chat. Anchored at the start so ordinary sentences
# ("I forgot my Python homework") are never read as commands.
CMD = [
    (re.compile(r"^\s*(?:/?forget|忘记|忘掉|删除)\s*[:：]?\s*([A-Za-z_一-鿿+#]+)\s*$", re.I), "forget"),
    (re.compile(r"^\s*(?:/?confirm|确认)\s*[:：]?\s*([A-Za-z_一-鿿+#]+)\s*$", re.I), "confirm"),
    (re.compile(r"^\s*(?:/?dismiss|不对|驳回)\s*[:：]?\s*([A-Za-z_一-鿿+#]+)\s*$", re.I), "dismiss"),
    (re.compile(r"^\s*(?:/?profile|what do you know about me\??|你(?:都)?知道我(?:什么|哪些)[?？]?)\s*$", re.I), "show"),
]
# Round 2: explicit re-teach after a forget ("remember: 我熟练Python"). The sentence is read like any chat.
REMEMBER = re.compile(r"^\s*(?:/?remember|记住)\s*[:：]?\s*(.+)$", re.I | re.S)
# Round 2: a bare yes names no fact, so it confirms nothing (decision in build.md).
BARE_YES = re.compile(r"^\s*(?:ok(?:ay)?|yes|yep|yeah|y|sure|right|correct|confirm(?:ed)?|对|对的|是|是的|好|好的|嗯+"
                      r"|确认|没错|可以|行)\s*[.!。！~～]*\s*$", re.I)


# ---------------- allow-list gate (round 2) ----------------
# PLAN: passive catch reads only the student's own chat with the bot. NapCat runs on the owner's MAIN
# account and sees every chat of it (PLAN v0.9/v1.0), so "a private message" is NOT the same as "the owner
# talking to the bot": any friend's DM arrives too. Hence the default allows the private kind but needs the
# owner's id; with no owner id configured, nothing from QQ is read.
DEFAULT_ALLOW = {"private": True, "groups": [], "owners": []}
LOCAL = {"cli", "web", "local"}          # the student's own machine (cnb chat, a local page): no QQ sender at all


def _ids(v):
    """Checker fix (S2d r2): ids from the allow-list as a set of strings, whatever shape the JSON has.
    Before: "owners": 111 raised TypeError (would escape the listener hook), "owners": "111" became {'1'} (QQ id 1
    accepted, the real owner refused), "groups": "55" allowed group 5, and placeholder ids ("", 0, null) matched
    an event carrying the same empty id."""
    if v is None or isinstance(v, bool):
        return set()
    if isinstance(v, (str, int)):
        v = [v]
    try:
        out = {str(x).strip() for x in v if x is not None and not isinstance(x, bool)}
    except TypeError:
        return set()
    return out - {"", "0", "None", "null"}


def gate(where, allow=None):
    """Pure decision, no I/O: may the bot LEARN from / ANSWER this chat event?
    where: {kind: 'private'|'group'|'cli'|'web'|..., chat_id, sender_id}
    allow: {private: bool, groups: [group ids], owners: [owner QQ ids]} merged over DEFAULT_ALLOW.
    -> {ok, learn, answer, reason}. ok == answer. learn implies answer.
      private: only from an owner id; nobody else's DM is read or answered.
      group:   refused unless the group id is on allow['groups']; even then only the owner's own messages
               are learnt from (other people's group messages never are, PLAN v0.5).
      anything else (guild, temp session, discuss, unknown): refused."""
    # checker fix (S2d r2): a malformed allow-list or event description refuses, it never raises
    allow = {**DEFAULT_ALLOW, **(allow if isinstance(allow, dict) else {})}
    where = where if isinstance(where, dict) else {}
    kind = where.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    sender, chat_id = where.get("sender_id"), where.get("chat_id")
    owners = _ids(allow.get("owners"))

    def res(learn, answer, reason):
        return {"ok": bool(answer), "learn": bool(learn), "answer": bool(answer), "reason": reason}

    if kind in LOCAL:
        return res(True, True, "local channel on the student's own machine")
    if kind in ("private", "dm", "friend"):
        if allow.get("private") is not True:       # only JSON true; the string "false" used to count as on
            return res(False, False, "private chats are switched off in the allow-list")
        if not owners:
            return res(False, False, "no owner id in the allow-list: on the owner's main account every friend's"
                                     " private message arrives here, so none is read")
        if sender is None or str(sender) not in owners:
            return res(False, False, "private message from someone who is not the owner: never read, never answered")
        return res(True, True, "the owner's private chat with the bot")
    if kind == "group":
        groups = _ids(allow.get("groups"))
        if chat_id is None or str(chat_id) not in groups:
            return res(False, False, "group not on the chat allow-list: the bot neither learns from nor answers in it")
        if sender is not None and str(sender) in owners:
            return res(True, True, "allow-listed group, the owner's own message")
        return res(False, True, "allow-listed group, but not the owner's message: may answer, never learns from it")
    return res(False, False, f"chat kind {kind or '?'} is not on the allow-list")


def where_from_onebot(ev):
    """OneBot v11 message event -> `where` for gate()/turn(). Pure.
    Checker fix (S2d r2): only a received message (post_type 'message') of type private/group keeps its kind.
     - 'message_sent' (what the owner types on the same account) carries user_id = the owner for EVERY chat the
       owner writes in, so it looked like "the owner's private chat" even when sent to a friend: now 'unknown',
       refused, until someone decides how to read target_id (build.md open issue 2);
     - a temporary session arrives as private with sub_type 'group'/'other': now 'temp', refused;
     - any other message_type is 'unknown', so an event can never claim a local kind such as 'cli'."""
    if not isinstance(ev, dict) or ev.get("post_type") != "message":
        return {"kind": "unknown", "chat_id": None, "sender_id": None}
    kind = ev.get("message_type")
    if kind == "private" and ev.get("sub_type") not in (None, "friend"):
        kind = "temp"
    elif kind not in ("private", "group"):
        kind = "unknown"
    return {"kind": kind, "chat_id": ev.get("group_id") if kind == "group" else ev.get("user_id"),
            "sender_id": ev.get("user_id")}


def _scrub(t):
    from .extract import scrub                 # lazy: extract pulls config helpers we do not need otherwise
    return scrub(t or "")


def _key(word):
    """The profile key a memory command names, or None when the word is not a command target.

    Checker fix (S2d): no prefix guessing here. Before, "忘记Python怎么写了" (I forgot how to write Python)
    and "forget pythonista" resolved to key 'python' and DELETED it, and "不对劲" / "确认一下" / "删除了"
    were swallowed as commands. Now the word must BE a known skill name or an existing key; a plain ASCII
    word is taken literally (so "forget rust" still answers "nothing recorded"); anything else, i.e. a
    Chinese phrase that merely starts with a command word, is ordinary chat."""
    w = (word or "").strip().lower()
    if w in P.KNOWN:
        return P.KNOWN[w]
    if re.fullmatch(r"[a-z_+#]+", w):
        return w
    return None


def _describe(r):
    what = r["level"] or r["value"]
    s = f"{r['key']}={what}"
    return s + (" (contradicts what I had; held until you confirm)" if r["pending"] else "")


def _summary(rows, text):
    if rows:
        return "said: " + "; ".join(_describe(r) for r in rows)
    t = re.sub(r"\s+", " ", _scrub(text)).strip()
    return "chat, nothing about abilities: " + (t[:60] + ("…" if len(t) > 60 else ""))


CONFIRM_REPLY = {
    "accepted": "OK: from now on I use what you told me last about {k}. It stays marked as your own claim "
                "(not proof) until an interview answer or a real result backs it up.",
    "acked": "Noted: {k} is right as you said it. It stays marked as your own claim (not proof) until an "
             "interview answer or a real result backs it up.",
    "already_backed": "{k} is already backed up by an answer or result; nothing to confirm.",
    "nothing": "I have nothing about {k} waiting for your confirmation.",
}


def _result(**kw):
    base = {"turn_id": None, "note_id": None, "summary": None, "proposed": [], "held": [], "command": None,
            "reply": None, "duplicate": False, "forgotten": [], "redacted": False}
    base.update(kw)
    return base


def turn(db, text, ts=None, channel="dm", summarize=None, where=None, allow=None):
    """-> {turn_id, note_id, summary, proposed[], held[], command, reply, duplicate, forgotten[], redacted}.

    text: what the student wrote. ts: unix seconds of the message (default now).
    channel: free label for where it came from ('dm', 'cli', 'qq_dm', 'web'...); a group-looking label is
      refused. An adapter should pass `where` (+ `allow`) instead: the allow-list gate decides, and a refused
      event raises ValueError before anything is stored.
    summarize: optional callable(scrubbed_text) -> short string, e.g. an LLM; never sees phones/emails."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty chat turn")
    if where is not None:
        g = gate(where, allow)
        if not g["learn"]:
            raise ValueError("refused by the chat allow-list: " + g["reason"])
        if channel in (None, "", "dm"):
            channel = f"qq_{str(where.get('kind')).lower()}:{where.get('chat_id')}"
        channel = str(channel).strip()
    else:
        channel = str(channel if channel is not None else "dm").strip() or "dm"
        if GROUPISH.search(channel):
            raise ValueError("passive catch reads only the student's own chat with the bot, never a group")
    ts = int(ts if ts is not None else time.time())
    P.ddl(db)

    # round 2: a message whose turn was deleted by a forget (or the forget request itself) is dropped whole
    if P.is_forgotten_source(db, channel, ts, text):
        return _result(duplicate=True, reply="(already seen this message; it was about something you asked me to "
                                             "forget, so nothing is recorded)")
    dup = db.execute("SELECT id FROM chat_turns WHERE channel=? AND ts=? AND text=?", (channel, ts, text)).fetchone()
    if dup:
        n = db.execute("SELECT * FROM conversation_notes WHERE turn_id=?", (dup[0],)).fetchone()
        return _result(turn_id=dup[0], note_id=n["id"] if n else None, summary=n["summary"] if n else None,
                       duplicate=True, reply="(already seen this message; nothing new recorded)")

    tid = db.execute("INSERT INTO chat_turns(ts,channel,text) VALUES(?,?,?)", (ts, channel, text)).lastrowid
    db.commit()

    command, reply, ids, skipped, lifted = None, None, [], [], []
    m = REMEMBER.match(text)
    if m and P.propose(m.group(1)):
        command, sentence = "remember", m.group(1)
        lifted = [k for k in dict.fromkeys(x[1] for x in P.propose(sentence)) if P.lift(db, k, ts)]
        ids = P.catch(db, sentence, source="dm", created=ts, note=False, skipped=skipped)
    elif BARE_YES.match(text):
        # Round-2 decision: a bare yes names no fact, so it changes nothing, whatever the bot said last.
        command = "bare_yes"
        waiting = sorted({r["key"] for r in P.unconfirmed(db)})
        reply = ("I did not change anything: to confirm, name the fact, e.g. 'confirm " + waiting[0] + "'. Waiting: "
                 + ", ".join(waiting) + ".") if waiting else None
    else:
        for rx, what in CMD:
            cm = rx.match(text)
            if cm:
                key = _key(cm.group(1)) if cm.groups() else None
                if cm.groups() and key is None:
                    break                               # starts like a command but names nothing: ordinary chat
                command = what
                if what == "forget":
                    # blank the request's own text first: forget() also deletes every turn that still names the
                    # key; and tombstone the request so a replay of it cannot delete a fact re-taught later
                    P.tombstone_source(db, channel, ts, text, ts)
                    db.execute("UPDATE chat_turns SET text=? WHERE id=?", (f"[forget request] #{tid}", tid))
                    old_request = P.forget_is_stale(db, key, ts)    # checker fix (S2d r2), see profile.forget_is_stale
                    n = P.forget(db, key, ts=ts)
                    reply = f"Forgotten: {n} record(s) about {key!r}, with the chat notes they came from." if n \
                        else f"I had nothing recorded about {key!r}."
                    if old_request:
                        reply = (f"That forget request is older than the message in which you taught me {key!r} again, "
                                 f"so I deleted nothing. Say 'forget {key}' again if you still want it gone.")
                elif what == "confirm":
                    # Round-2 decision: only an explicit confirm of a NAMED, shown fact, and it records the
                    # student's agreement, not proof (profile.acknowledge).
                    reply = CONFIRM_REPLY[P.acknowledge(db, key, now=ts)].format(k=repr(key))
                elif what == "dismiss":
                    n = P.dismiss(db, key)
                    reply = f"Dropped the proposed change to {key!r}; what I had before stays." if n \
                        else f"There is no pending proposal for {key!r}."
                else:
                    reply = P.summary(db)
                break

    if command is None:
        ids = P.catch(db, text, source="dm", created=ts, note=False, skipped=skipped)

    rows = [db.execute("SELECT * FROM profile_facts WHERE id=?", (i,)).fetchone() for i in ids]
    rows = [r for r in rows if r is not None]
    if command == "forget":
        summary = "asked the bot to forget something"   # the forgotten thing is not repeated in memory
    elif command:
        summary = f"memory command: {command}"
    else:
        summary = _summary(rows, text)
        if summarize:
            try:
                extra = summarize(_scrub(text))
                if extra:
                    summary = (summary + " | " + _scrub(str(extra)).strip())[:300]
            except Exception:                  # noqa: BLE001 - a summariser failure never loses the turn
                pass

    if command == "forget":
        stored_text = "[forget request]"           # do not keep the name of the forgotten thing in the turn log
        db.execute("UPDATE chat_turns SET text=? WHERE id=?", (stored_text + f" #{tid}", tid))
    nid = db.execute("INSERT INTO conversation_notes(ts,text,summary,fact_id,turn_id,channel) VALUES(?,?,?,?,?,?)",
                     (ts, None, summary, None, tid, channel)).lastrowid
    for i in ids:
        db.execute("INSERT OR IGNORE INTO note_facts(note_id,fact_id) VALUES(?,?)", (nid, i))
    db.commit()

    # Round 2: a turn that names something still forgotten (or an older message about a key re-taught since)
    # is not kept: other facts in it are learnt, the forgotten words are not stored anywhere.
    hush = set(k for k, _why in skipped)
    if command != "forget":
        toks = re.findall(r"[a-z_+#]{2,}", text.lower())
        hush |= {k for k in P.forgotten_keys(db, toks) if P.names_any(text, k)}
    redacted = bool(hush)
    if redacted:
        P.redact_turn(db, tid, sorted(hush), ts)
        tid = nid = None
        summary = None

    rows = [db.execute("SELECT * FROM profile_facts WHERE id=?", (r["id"],)).fetchone() for r in rows]
    rows = [r for r in rows if r is not None]
    proposed = [dict(r) for r in rows if not r["pending"]]
    held = [dict(r) for r in rows if r["pending"]]
    forgotten = sorted({k for k, why in skipped if why == "forgotten"})
    if reply is None:
        bits = [_describe(r) for r in rows]
        if bits:
            reply = ("Noted (as your own claim, not yet confirmed): " + "; ".join(bits) +
                     ". Say 'confirm <key>' if right, 'forget <key>' to delete" +
                     (", 'dismiss <key>' to keep what I had." if held else "."))
        if forgotten:
            reply = ((reply + " ") if reply else "") + (
                "You asked me to forget " + ", ".join(forgotten) + " earlier, so I did not record it. "
                "To teach me again, say 'remember: <your sentence>'.")
    if command == "remember":
        bits = [_describe(r) for r in rows]
        reply = ("Re-learnt after your earlier forget: " + ", ".join(lifted) + ". " if lifted else "") + \
            ("Noted (as your own claim, not yet confirmed): " + "; ".join(bits) + "." if bits
             else "Nothing new recorded (that message is not newer than your forget).")
    return _result(turn_id=tid, note_id=nid, summary=summary, proposed=proposed, held=held, command=command,
                   reply=reply, forgotten=forgotten, redacted=redacted)


def notes(db, limit=20):
    """Recent conversation notes, newest first, each with its turn and the facts it produced."""
    P.ddl(db)
    out = []
    for n in db.execute("SELECT * FROM conversation_notes WHERE turn_id IS NOT NULL ORDER BY ts DESC, id DESC LIMIT ?",
                        (limit,)):
        fids = [r[0] for r in db.execute("SELECT fact_id FROM note_facts WHERE note_id=?", (n["id"],))]
        out.append({"id": n["id"], "ts": n["ts"], "channel": n["channel"], "turn_id": n["turn_id"],
                    "summary": n["summary"], "fact_ids": fids})
    return out
