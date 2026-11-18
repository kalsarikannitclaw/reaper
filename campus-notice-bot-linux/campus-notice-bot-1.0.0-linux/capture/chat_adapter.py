"""QQ direct-message adapter for the conversation channel (stage S10). DISABLED by default and NEVER sends.

handle_event(ev, ...) takes ONE OneBot v11 event and returns what the bot WOULD say:
    {"send": False, "refused": bool, "reason": str, "suggested_reply": str|None}
`send` is always False: this module opens no socket, imports no HTTP/websocket client and calls no OneBot action.
Delivering a reply (spare account as the bot's voice vs the main account, reply ownership, login-storm and abuse
risk) is the account owner's decision, see stages/S10/build.md. It is not wired into capture/listener.py.

Gates, in order (all must pass, else nothing is stored, nothing is read by the LLM):
 1. capture/chat_allow.json exists, has "enabled": true (JSON true) and the owner's id in "owners";
 2. chat.gate(where_from_onebot(ev), allow): a private message from an owner only; groups, temp sessions,
    guild, the owner's own outgoing 'message_sent', other people's DMs are refused;
 3. the event carries text (images/faces alone are ignored).
"""
import json, os, re, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from cnb import chat as CH, converse as CV        # noqa: E402

ALLOW_PATH = os.path.join(ROOT, "capture", "chat_allow.json")
CQ = re.compile(r"\[CQ:[^\]]*\]")
_sessions = {}                                     # owner id -> conversation session (in memory only)


def load_allow(path=None):
    """The allow-list, or {} (= everything refused) when the file is missing or malformed."""
    try:
        with open(path or ALLOW_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def event_text(ev):
    """Plain text of a OneBot message: a CQ-coded string or a list of segments. Non-text segments are dropped."""
    m = ev.get("message") if isinstance(ev, dict) else None
    if isinstance(m, list):
        s = "".join(str(g.get("data", {}).get("text", "")) for g in m if isinstance(g, dict) and g.get("type") == "text")
    elif isinstance(m, str):
        s = CQ.sub("", m)
    else:
        s = ev.get("raw_message", "") if isinstance(ev, dict) and isinstance(ev.get("raw_message"), str) else ""
        s = CQ.sub("", s)
    return s.strip()


def handle_event(ev, db=None, allow=None, llm=None, activities_path=None):
    def no(reason):
        return {"send": False, "refused": True, "reason": reason, "suggested_reply": None}

    allow = load_allow() if allow is None else allow
    if not isinstance(allow, dict) or allow.get("enabled") is not True:
        return no("chat adapter is disabled (capture/chat_allow.json missing or \"enabled\" is not true)")
    where = CH.where_from_onebot(ev)
    if where.get("kind") != "private":                  # this adapter is for the owner's DM only, even if a group is listed
        return no("this adapter answers the owner's private chat only, never a group")
    g = CH.gate(where, allow)
    if not g["learn"]:
        return no(g["reason"])
    text = event_text(ev)
    if not text:
        return no("no text in the message")
    if db is None:
        from cnb import db as D
        db = D.connect()
    key = str(where.get("sender_id"))
    ses = _sessions.setdefault(key, CV.new_session())
    r = CV.reply(db, text, ses, llm, ts=ev.get("time"), where=where, allow=allow, activities_path=activities_path)
    if r["refused"]:
        return no(r["reason"])
    return {"send": False, "refused": False, "reason": "suggested reply only; nothing is sent", "suggested_reply": r["reply"]}
