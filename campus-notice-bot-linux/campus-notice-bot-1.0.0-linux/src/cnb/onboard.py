"""First-run onboarding beyond the API key (S2a round 2): model, timezone, school, groups to watch.

Design:
 - Answers live in ONE small JSON file (default data/onboard.json, override with env CNB_ONBOARD): per tenant, written
   atomically after EVERY step, so a closed terminal or a dropped QQ DM loses nothing (resumable) and a re-run only asks
   what is still open (idempotent). `redo=` re-asks chosen steps.
 - The API key is NOT stored here, ever: the key step delegates to keysetup (env file only) and this file records only
   "key present: yes/no" implicitly by asking keysetup. Key entry needs a hidden prompt (`ask_secret`); a caller with none
   (a QQ DM) gets a pointer to the local `cnb key set` and no key is requested through `ask`.
 - The groups step writes a PROPOSED list into this file. It never opens capture/groups.json for writing: the user owns
   that file. The proposal is shown next to what is already watched so the user can copy it over by hand.
 - `ask(question) -> str` is the only I/O the flow needs, so the CLI (input) and a QQ DM can share it.
 - An empty answer to model/tz accepts the default; an empty answer to school/groups skips the step for now (it stays
   open, and nothing is invented).
"""
import json, os, re, tempfile
from . import db as D

PATH = os.environ.get("CNB_ONBOARD", os.path.join(D.ROOT, "data", "onboard.json"))
GROUPS = os.path.join(D.ROOT, "capture", "groups.json")
CFG = os.path.join(D.ROOT, "config.json")
DEFAULT_TZ = "Asia/Shanghai"
MODEL_HINTS = ["deepseek-chat", "deepseek-reasoner"]       # DeepSeek's documented chat models; any other id may be typed
STEPS = ("key", "model", "tz", "school", "groups")

def _path(path): return path or PATH

def load(path=None):
    try: s = json.load(open(_path(path), encoding="utf-8"))
    except (OSError, ValueError): return {}
    return s if isinstance(s, dict) else {}

def save(state, path=None):
    p = _path(path); d = os.path.dirname(os.path.abspath(p)); os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".onboard.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

def valid_tz(name):
    """IANA name that the system knows, e.g. Asia/Shanghai. Case is fixed to the canonical spelling when possible."""
    from zoneinfo import ZoneInfo, available_timezones
    if not isinstance(name, str): return None    # checker fix: {"tz": 5} in a damaged state/config file crashed every search
    name = name.strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_+\-/]{1,64}", name) or name.lower() in ("localtime", "factory"): return None   # checker fix: 'localtime'/'Factory' are files in the zone folder, not places
    zones = available_timezones()
    if not zones: return DEFAULT_TZ if name.lower() == DEFAULT_TZ.lower() else None    # checker fix: no timezone database -> only the default (fixed UTC+8, see cli._zone)
    for z in zones:
        if z.lower() == name.lower():
            try: ZoneInfo(z); return z
            except Exception: return None
    return None

def tz_name(path=None):
    """The tenant's timezone for day boundaries (search by date): chosen one, else config.json tz, else Asia/Shanghai."""
    t = load(path).get("tz")
    if not t:
        try: c = json.load(open(CFG, encoding="utf-8")); t = c.get("tz") if isinstance(c, dict) else None
        except (OSError, ValueError): t = None
    return valid_tz(t) or DEFAULT_TZ

def valid_model(m):
    m = (m or "").strip()
    return m if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\-/]{0,63}", m) else None

def parse_groups(text):
    """Group numbers separated by commas/spaces/semicolons. Returns (ids, rejected_tokens). QQ group numbers are 5-11 digits."""
    ids, bad = [], []
    for t in re.split(r"[,\s;，、]+", (text or "").strip()):
        if not t: continue
        if re.fullmatch(r"[1-9][0-9]{4,10}", t):       # checker fix: 0000000 was accepted as group 0
            if int(t) not in ids: ids.append(int(t))
        else: bad.append(t)
    return ids, bad

def watched_now():
    """Groups already in capture/groups.json (read only). Unreadable -> empty."""
    try:
        c = json.load(open(GROUPS, encoding="utf-8")); return [int(g) for g in c.get("groups", [])]
    except (OSError, ValueError, TypeError, AttributeError): return []

def seen_groups(db):
    """Groups that have stored messages: [(group_id, count)], busiest first. No names are invented."""
    if db is None: return []
    try: return [(r[0], r[1]) for r in db.execute("SELECT group_id, count(*) c FROM msg WHERE group_id IS NOT NULL GROUP BY group_id ORDER BY c DESC LIMIT 20")]
    except Exception: return []

SECRET_NOTE = ("That looks like an API key. It was NOT stored and is not repeated here. If you typed it in a chat, delete that message and "
               "rotate the key. Keys are entered only with `cnb key set` on your own computer.")

def looks_secret(a):
    """checker fix: a key pasted at the wrong prompt (model/timezone/school/groups) was saved in onboard.json as the model or school and
    printed back by the summary and the 'not a timezone' / 'ignored' lines. True for the stored key itself and for sk-... shaped text."""
    from . import keysetup as K
    a = (a or "").strip()
    if not a: return False
    try: k = K.read_key()
    except Exception: k = None
    return bool(k and len(k) >= 8 and k in a) or bool(re.search(r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{12,}", a))

def pending(path=None, key_present=False):
    s = load(path)
    return [k for k in STEPS if not (key_present if k == "key" else s.get(k) not in (None, "", []) )]

def run(ask, ask_secret=None, out=print, path=None, redo=(), db=None, key_present=None, key_setup=None):
    """Walk the open steps. ask(question)->str. ask_secret: hidden-input callable for the key (None = key step is not asked here).
    key_present/key_setup are injectable for tests; by default they use keysetup. Returns the state dict. Stops cleanly
    (state kept) on EOF/Ctrl-C."""
    from . import keysetup as K
    state = load(path); redo = set(redo)
    if key_present is None: key_present = bool(K.read_key())
    def need(step): return step in redo or (not key_present if step == "key" else state.get(step) in (None, "", []))
    raw_ask = ask
    def ask(q):                                   # checker fix: every answer passes the secret guard before it is used, stored or echoed
        for _ in range(3):
            a = raw_ask(q) or ""
            if not looks_secret(a): return a
            out(SECRET_NOTE)
        return ""                                 # three times a key: treat as no answer (default / skip)
    try:
        if need("key"):
            if ask_secret is None: out("API key: not asked here. Enter it on your own computer with `cnb key set` (never in a chat).")
            else: K.setup(read_secret=ask_secret, out=out) if key_setup is None else key_setup()
        if need("model"):
            a = (ask(f"Which model? Press Enter for {state.get('model') or MODEL_HINTS[0]}. Known: {', '.join(MODEL_HINTS)}.") or "").strip()
            m = valid_model(a) if a else (state.get("model") or MODEL_HINTS[0])
            while m is None:
                a = (ask("That is not a model id (letters, digits, . _ : - /). Try again, or Enter for the default.") or "").strip()
                m = valid_model(a) if a else MODEL_HINTS[0]
            state["model"] = m; save(state, path)
            out(f"Option for {m} (any OpenAI-compatible model): when a page blocks the bot, it can ask you to open that page yourself and give it the text, "
                "then check it. Nothing is opened without your approval. Set \"manual_page_assist\": false in config.json to turn this off.")
        if need("tz"):
            cur = state.get("tz") or DEFAULT_TZ
            a = (ask(f"Your timezone (IANA name, e.g. Asia/Shanghai, America/New_York)? Enter for {cur}.") or "").strip()
            z = valid_tz(a) if a else (valid_tz(cur) or DEFAULT_TZ)
            while z is None:      # checker fixes: the answer is not echoed back; Enter always ends the loop (it never ended without a timezone database)
                a = (ask(f"That is not a timezone this computer knows. Use the Area/City form, e.g. Asia/Shanghai. Enter for {valid_tz(cur) or DEFAULT_TZ}.") or "").strip()
                z = valid_tz(a) if a else (valid_tz(cur) or DEFAULT_TZ)
            state["tz"] = z; save(state, path)
        if need("school"):
            a = " ".join((ask("Your school (and institute/faculty if you like)? Enter to skip for now.") or "").split())
            if a: state["school"] = a[:200]; save(state, path)
            else: out("school: skipped for now (it stays unknown).")
        if need("groups"):
            seen, have = seen_groups(db), watched_now()
            if seen: out("Groups with stored messages: " + ", ".join(f"{g} ({n} msgs)" for g, n in seen))
            if have: out("Already watched (capture/groups.json): " + ", ".join(map(str, have)))
            a = ask("Which QQ groups should be watched? Group numbers separated by commas. Enter to skip for now.") or ""
            ids, bad = parse_groups(a)
            if bad: out(f"ignored {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} that {'is' if len(bad) == 1 else 'are'} not a group number (5-11 digits).")   # checker fix: not echoed
            if ids:
                state["groups"] = ids; state["groups_proposed_note"] = "PROPOSED only. capture/groups.json is yours to edit; nothing here changes what is captured."
                save(state, path)
            else: out("groups: skipped for now.")
    except (EOFError, KeyboardInterrupt):
        out("\nsetup paused; run it again to continue where you stopped.")
    return state

def _assist_on():
    try:
        from . import manual
        return manual.assist_enabled(CFG)
    except Exception: return True

def summary(path=None, out=print, key_present=None):
    from . import keysetup as K
    s = load(path); kp = bool(K.read_key()) if key_present is None else key_present
    have = set(watched_now())
    out(f"key      : {'set' if kp else 'not set'}")
    out(f"model    : {s.get('model') or '(not chosen)'}")
    out("pages    : " + ("open-it-yourself option ON" if _assist_on() else "open-it-yourself option OFF") + " (config.json manual_page_assist)")
    out(f"timezone : {s.get('tz') or DEFAULT_TZ + ' (default, not chosen)'}")
    out(f"school   : {s.get('school') or '(unknown)'}")
    g = s.get("groups") or []
    out("groups   : " + (", ".join(f"{x}{'' if x in have else ' (new: add to capture/groups.json yourself)'}" for x in g) + "  [PROPOSED]" if g else "(none proposed)"))
    p = pending(path, kp)
    out("open steps: " + (", ".join(p) if p else "none"))
    return p
