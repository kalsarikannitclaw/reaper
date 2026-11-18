"""Stage S12: "open it yourself" for pages the bot must not (or cannot) open.

A page the bot needs but cannot fetch (the school's user-only site in blocked_hosts, a connection reset, a CAPTCHA,
a link in a notice to such a host, or a page the AI model asks for) becomes a PENDING request in the table
`manual_pages`. THIS MODULE NEVER FETCHES ANYTHING: it has no network code at all. The user decides:
  approve  -> the page's address is shown; the user opens it in THEIR OWN browser
  provide  -> the user pastes the text they see, or gives a saved file (.html .htm .txt .pdf, max 2 MB)
  skip     -> never asked again for 30 days
What the user gives is stored as a USER-SUPPLIED COPY with provenance ("provided by the user on <date>, claimed address
<url>, NOT verified by the bot"). It is then (a) searchable evidence for the chat advisor (ref `page:<id>`) and
(b) offered to the existing extractor; items made from it are always needs_confirm with source 'manual'.
Page text is untrusted DATA, never instructions. No cookies or credentials are asked for or stored.

Stdlib only, pathlib/UTF-8, no POSIX-only calls (Windows + Linux).
"""
import base64, hashlib, ipaddress, json, os, re, sys, time, urllib.parse
from . import db as D, readers

MANUAL_GROUP = -12              # group id of the user-supplied copies in `msg` (never a real QQ group)
MAX_PENDING = 20
SKIP_SECONDS = 30 * 86400
MAX_FILE = 2 * 1024 * 1024      # bytes of a saved file
MAX_PASTE = 500_000             # characters of pasted text we are willing to look at
MAX_TEXT = 60_000               # characters kept per page
MAX_EXTRACT = 8_000             # characters handed to the extractor
MIN_TEXT = 10
MAX_URL = 500
FILE_EXTS = (".html", ".htm", ".txt", ".pdf")
STATUSES = ("pending", "approved", "skipped", "provided")
USER_ONLY_FALLBACK = ["college.example-school.edu.cn"]       # same default as tunnel.DEFAULT_BLOCKED, for a package without tunnel.py


def ensure(db):
    """CREATE IF NOT EXISTS; also the `source` column on items (set to 'manual' for items made from a supplied page)."""
    db.executescript("""
    CREATE TABLE IF NOT EXISTS manual_pages(id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, why TEXT, asked_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', decided_at INTEGER, provided_at INTEGER, filename TEXT, kind TEXT, text TEXT,
        sha256 TEXT, truncated INTEGER DEFAULT 0, msg_id INTEGER);
    """)
    if "source" not in [r[1] for r in db.execute("PRAGMA table_info(items)")]:
        db.execute("ALTER TABLE items ADD COLUMN source TEXT")
    db.commit()


# ---------------------------------------------------------------- addresses
_BAD_SUFFIX = (".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".arpa", ".test", ".invalid", ".example")


def normalise_url(url):
    """-> (normalised url, None) or (None, reason). http(s) only; no credentials; no IP literal, no localhost / private-looking
    name; plain ASCII host (a look-alike spelling is refused, never converted); fragment dropped; scheme and host lower-cased."""
    if not isinstance(url, str):
        return None, "not an address"
    s = url.strip()
    if not s or len(s) > MAX_URL or re.search(r"[\s\x00-\x1f\x7f]", s):
        return None, "not a usable address"
    try:
        p = urllib.parse.urlsplit(s)
        port = p.port
    except ValueError:
        return None, "not a usable address"
    if p.scheme.lower() not in ("http", "https"):
        return None, "only http and https addresses are accepted"
    if p.username is not None or p.password is not None or "@" in p.netloc:
        return None, "an address with a user name or password is refused"
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return None, "no host name"
    if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or host.startswith((".", "-")) or any(l.startswith("xn--") or len(l) > 63 or l.startswith("-") or l.endswith("-") for l in host.split(".")):
        return None, "the host name must be plain ASCII (look-alike spellings are refused)"
    try:
        ipaddress.ip_address(host)
        return None, "an IP address is refused: only named school/public sites"
    except ValueError:
        pass
    if "." not in host or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host) or host == "localhost" or host.endswith(_BAD_SUFFIX) or re.fullmatch(r"[0-9]+", host.rsplit(".", 1)[-1]):
        return None, "a local, private or numeric host is refused"
    scheme = p.scheme.lower()
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, p.path or "/", p.query, "")), None


def _host(url):
    n, _ = normalise_url(url)
    return urllib.parse.urlsplit(n).hostname if n else None


def user_only_hosts():
    """The hosts configured as user-only: research_sources.json -> blocked_hosts (the S5 list; absent key = the built-in default)."""
    try:
        from . import tunnel as T
        return list(T.blocked_hosts())
    except Exception:                                   # noqa: BLE001 - a package without tunnel.py
        try:
            with open(os.path.join(D.ROOT, "research_sources.json"), encoding="utf-8") as f:
                v = json.load(f).get("blocked_hosts")
            return [str(x) for x in v] if isinstance(v, list) else list(USER_ONLY_FALLBACK)
        except (OSError, ValueError, AttributeError):
            return list(USER_ONLY_FALLBACK)


_SUFFIXES = {"edu.cn", "gov.cn", "com.cn", "org.cn", "net.cn", "ac.cn", "cn", "com", "org", "net", "edu"}


def own_hosts():
    """Hosts that may be asked for by the model: only the sites the user CONFIGURED in research_sources.json (the school's host, its
    notice/whitelist page hosts, follow_hosts, blocked_hosts, and 'allow' entries that are a specific site) - exact hosts, plus www.<school host>.
    A public suffix such as edu.cn never counts, so another university's host is not accepted. Read-only; never writes the file."""
    try:
        with open(os.path.join(D.ROOT, "research_sources.json"), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    c = c if isinstance(c, dict) else {}
    sc = c.get("school") if isinstance(c.get("school"), dict) else {}
    raw = [sc.get("host")] + list(sc.get("follow_hosts") or []) + [u.get("url") for u in (sc.get("notice_urls") or []) if isinstance(u, dict)] \
        + [u if isinstance(u, str) else (u or {}).get("url") for u in (sc.get("whitelist_urls") or [])] \
        + [a.get("host") for a in (c.get("allow") or []) if isinstance(a, dict)] + list(user_only_hosts())
    out = set()
    for r in raw:
        if not isinstance(r, str) or not r.strip():
            continue
        h = _host(r if "//" in r else "https://" + r)
        if h and h not in _SUFFIXES:
            out.add(h)
    if sc.get("host") and _host("https://" + str(sc["host"])):
        out.add("www." + _host("https://" + str(sc["host"])))
    return out


def user_only(url):
    """True when the address is on a host (or subdomain of one) configured as user-only."""
    h = _host(url)
    if not h:
        return False
    return any(h == b.lower().rstrip(".") or h.endswith("." + b.lower().rstrip(".")) for b in user_only_hosts() if b)


def assist_enabled(path=None):
    """config.json -> manual_page_assist (default true). A string 'false'/'0'/'off'/'no' also means off."""
    try:
        from . import extract
        with open(path or extract.CFG, encoding="utf-8") as f:
            c = json.load(f)
        v = c.get("manual_page_assist", True) if isinstance(c, dict) else True
    except (OSError, ValueError, ImportError):
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(v) if isinstance(v, (bool, int)) else True


# ---------------------------------------------------------------- the queue
def _why(w):
    w = re.sub(r"[\x00-\x1f\x7f​-‏‪-‮⁠﻿]+", " ", str(w or ""))
    return " ".join(w.split())[:300]


def request(db, url, why="", now=None):
    """Create (or find) a PENDING request. Never fetches. -> {ok, id, status, created, url, reason}.
    ok False: refused address, cap of 20 pending reached, or the user skipped it less than 30 days ago."""
    ensure(db)
    now = int(now if now is not None else time.time())
    n, bad = normalise_url(url)
    if not n:
        return {"ok": False, "id": None, "status": None, "created": False, "url": None, "reason": bad}
    row = db.execute("SELECT * FROM manual_pages WHERE url=?", (n,)).fetchone()
    if row and row["status"] == "skipped" and now - (row["decided_at"] or 0) < SKIP_SECONDS:
        return {"ok": False, "id": row["id"], "status": "skipped", "created": False, "url": n, "reason": "skipped by the user less than 30 days ago"}
    if row and row["status"] != "skipped":
        return {"ok": True, "id": row["id"], "status": row["status"], "created": False, "url": n, "reason": None}
    if db.execute("SELECT count(*) FROM manual_pages WHERE status='pending'").fetchone()[0] >= MAX_PENDING:
        return {"ok": False, "id": None, "status": None, "created": False, "url": n, "reason": f"{MAX_PENDING} requests are already waiting: answer some first"}
    if row:                                              # a skip older than 30 days: ask again
        db.execute("UPDATE manual_pages SET status='pending', why=?, asked_at=?, decided_at=NULL WHERE id=?", (_why(why), now, row["id"]))
        rid = row["id"]
    else:
        rid = db.execute("INSERT INTO manual_pages(url,why,asked_at,status) VALUES(?,?,?, 'pending')", (n, _why(why), now)).lastrowid
    db.commit()
    return {"ok": True, "id": rid, "status": "pending", "created": True, "url": n, "reason": None}


def get(db, pid):
    ensure(db)
    try:
        return db.execute("SELECT * FROM manual_pages WHERE id=?", (int(pid),)).fetchone()
    except (TypeError, ValueError, OverflowError):
        return None


def list_pages(db, statuses=("pending", "approved")):
    """Rows for the panel / CLI. No page text."""
    ensure(db)
    q = ",".join("?" * len(statuses))
    return [{"id": r["id"], "url": r["url"], "why": r["why"] or "", "asked_at": r["asked_at"], "status": r["status"],
             "provided_at": r["provided_at"], "chars": len(r["text"] or "")}
            for r in db.execute(f"SELECT * FROM manual_pages WHERE status IN ({q}) ORDER BY id", statuses)]


def approve(db, pid, now=None):
    """pending -> approved. The USER opens the address in their own browser; this only records that they agreed."""
    r = get(db, pid)
    if not r:
        return {"ok": False, "reason": "no such request"}
    if r["status"] not in ("pending", "approved"):
        return {"ok": False, "reason": f"the request is {r['status']}"}
    db.execute("UPDATE manual_pages SET status='approved', decided_at=? WHERE id=?", (int(now if now is not None else time.time()), r["id"]))
    db.commit()
    return {"ok": True, "id": r["id"], "url": r["url"], "status": "approved"}


def skip(db, pid, now=None):
    r = get(db, pid)
    if not r:
        return {"ok": False, "reason": "no such request"}
    if r["status"] == "provided":
        return {"ok": False, "reason": "already provided"}
    db.execute("UPDATE manual_pages SET status='skipped', decided_at=? WHERE id=?", (int(now if now is not None else time.time()), r["id"]))
    db.commit()
    return {"ok": True, "id": r["id"], "status": "skipped"}


# ---------------------------------------------------------------- what the user gives
_ZW = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def clean_text(t, cap=MAX_TEXT):
    """-> (text, truncated). Control and invisible characters removed, blank lines collapsed, length capped."""
    t = _ZW.sub("", (t or "").replace("\r\n", "\n").replace("\r", "\n"))
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", t)
    t = re.sub(r"[ \t 　]+", " ", t)
    t = re.sub(r" *\n[ \n]*", "\n", t).strip()
    return (t[:cap], True) if len(t) > cap else (t, False)


def decode_bytes(data):
    """-> (text, None) or (None, reason). UTF-16/UTF-8 BOM, UTF-8, then GB18030, then Big5 (campus pages and Notepad 'Unicode' files).
    Binary data (gzip, zip, executables, NUL bytes, mostly control or unreadable characters) is refused, never shown as garbage."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), None
        except UnicodeDecodeError:
            return None, "the file looks damaged (UTF-16 text expected)"
    if data[:2] == b"\x1f\x8b" or data[:2] in (b"PK", b"MZ") or data[:4] in (b"\x7fELF", b"\xd0\xcf\x11\xe0") or b"\x00" in data[:4096]:
        return None, "this is not a text or HTML file (it looks like compressed or binary data): save the page as plain .html or .txt, or paste its text"
    for enc in ("utf-8-sig", "gb18030", "big5"):
        try:
            t = data.decode(enc)
        except UnicodeDecodeError:
            continue
        odd = sum(1 for ch in t[:20000] if (ord(ch) < 32 and ch not in "\n\r\t") or ch == "\ufffd" or "\ue000" <= ch <= "\uf8ff")
        if odd > 0.02 * max(1, len(t[:20000])):
            continue
        return t, None
    return None, "this is not readable text (unknown character encoding): save the page as .html or .txt in UTF-8, or paste its text"


_LOOKS_HTML = re.compile(r"<\s*(?:!doctype|html|head|body|div|p|table|script|style|span|a\s)\b", re.I)


def _read(text=None, data=None, filename=None):
    """-> (kind, plain text, error). HTML is reduced with readers.read_html(strip_hidden=True): scripts, styles, comments,
    hidden elements dropped. A wall (CAPTCHA, login, anti-bot challenge) is refused: it is not the page."""
    if data is not None:
        ext = os.path.splitext(filename or "")[1].lower()
        if ext not in FILE_EXTS:
            return None, None, "only .html, .htm, .txt and .pdf files are accepted"
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_FILE:
            return None, None, "the file is larger than 2 MB"
        data = bytes(data)
        if ext == ".pdf" or data[:5] == b"%PDF-":
            doc = readers.read_pdf(data)
            if doc["note"] and not doc["text"]:
                return None, None, ("PDF reading needs the optional package pypdf, which is not installed: paste the text instead, or save the page as .html/.txt"
                                    if "pypdf" in doc["note"] else doc["note"])
            return "pdf", doc["text"], None
        txt, err = decode_bytes(data)
        if err:
            return None, None, err
        if ext in (".html", ".htm") or _LOOKS_HTML.search(txt[:4096]):
            doc = readers.read_html(txt.encode("utf-8"), "text/html; charset=utf-8", strip_hidden=True)
            if doc.get("blocked"):
                return None, None, "this looks like a wall page (" + doc["blocked"].split(" (")[0] + "), not the document: open the real page and save that"
            return "html", doc["text"], None
        return "text", txt, None
    if not isinstance(text, str):
        return None, None, "nothing was given"
    if len(text) > MAX_PASTE:
        return None, None, "the pasted text is too long (over 500,000 characters)"
    if _LOOKS_HTML.search(text[:4096]):
        doc = readers.read_html(text.encode("utf-8"), "text/html; charset=utf-8", strip_hidden=True)
        if doc.get("blocked"):
            return None, None, "this looks like a wall page (" + doc["blocked"].split(" (")[0] + "), not the document"
        return "html", doc["text"], None
    return "text", text, None


def header(row):
    """The provenance line shown with every use of the copy."""
    when = time.strftime("%Y-%m-%d", time.localtime(row["provided_at"] or 0))
    return f"provided by the user on {when}, claimed address {row['url']}, NOT verified by the bot"


def provide(db, pid, text=None, data=None, filename=None, now=None, queue_extract=True):
    """Store what the user gives for an APPROVED (or already provided) request. -> {ok, id, chars, truncated, kind, reason}.
    The text is cleaned and capped, never fetched, never sent anywhere here. With queue_extract a short copy is queued as an
    unprocessed `msg` row (group MANUAL_GROUP) so the normal extractor can read it at the next run; sweep() then marks the
    items it makes needs_confirm / source 'manual'."""
    r = get(db, pid)
    if not r:
        return {"ok": False, "reason": "no such request"}
    if r["status"] not in ("approved", "provided"):
        return {"ok": False, "reason": "approve the request first (you decide when a page is shown)"}
    kind, raw, err = _read(text, data, filename)
    if err:
        return {"ok": False, "reason": err}
    t, trunc = clean_text(raw)
    if len(t) < MIN_TEXT:
        return {"ok": False, "reason": "no readable text in what was given"}
    wall = readers.access_wall(t, t)
    if wall:
        return {"ok": False, "reason": "this looks like a wall page (" + wall.split(" (")[0] + "), not the document"}
    now = int(now if now is not None else time.time())
    sha = hashlib.sha256(t.encode("utf-8")).hexdigest()
    old = r["msg_id"]
    if old and db.execute("SELECT 1 FROM msg WHERE id=? AND processed=0", (old,)).fetchone():
        db.execute("DELETE FROM msg WHERE id=?", (old,))
    mid = None
    db.execute("UPDATE manual_pages SET status='provided', provided_at=?, filename=?, kind=?, text=?, sha256=?, truncated=?, msg_id=NULL WHERE id=?",
               (now, os.path.basename(filename or "")[:120] or None, kind, t, sha, int(trunc), r["id"]))
    if queue_extract:
        row = get(db, r["id"])
        body = "[user-supplied copy of a web page] " + header(row) + "\n" + t[:MAX_EXTRACT]
        mid = db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,raw,priority,role,processed) VALUES(?,?,?,?,?,?,0,'manual',0)",
                         (MANUAL_GROUP, 0, "user-supplied page", now, body, json.dumps({"manual_page": r["id"]}))).lastrowid
        db.execute("UPDATE manual_pages SET msg_id=? WHERE id=?", (mid, r["id"]))
    db.commit()
    return {"ok": True, "id": r["id"], "chars": len(t), "truncated": trunc, "kind": kind, "queued_for_extraction": bool(mid), "reason": None}


def provide_b64(db, pid, b64, filename):
    """The browser sends a saved file as base64 JSON."""
    try:
        data = base64.b64decode(b64 or "", validate=True)
    except (ValueError, TypeError):
        return {"ok": False, "reason": "the file could not be read"}
    return provide(db, pid, data=data, filename=filename)


def sweep(db):
    """Items made from a user-supplied copy are never confident: needs_confirm=1, source 'manual'. Idempotent. -> how many changed."""
    ensure(db)
    # only items whose EVERY source is a supplied copy: a QQ item that merely matches a page (merge adds a source) keeps its own source
    ids = [r[0] for r in db.execute("SELECT s.item_id FROM item_sources s JOIN msg m ON m.id=s.msg_id GROUP BY s.item_id "
                                    "HAVING sum(m.group_id=?)>0 AND sum(m.group_id<>? OR m.group_id IS NULL)=0", (MANUAL_GROUP, MANUAL_GROUP))]
    n = 0
    for i in ids:
        cur = db.execute("SELECT source, needs_confirm FROM items WHERE id=?", (i,)).fetchone()
        if cur and (cur["source"] != "manual" or not cur["needs_confirm"]):
            db.execute("UPDATE items SET source='manual', needs_confirm=1 WHERE id=?", (i,))
            db.execute("INSERT INTO item_history(item_id,ts,field,old,new,kind) VALUES(?,strftime('%s','now'),'source',?,'manual','manual')", (i, cur["source"]))
            n += 1
    db.commit()
    return n


# ---------------------------------------------------------------- use as evidence
_STOP = {"the", "and", "for", "are", "was", "that", "this", "with", "have", "what", "when", "where", "how", "you", "can", "does", "about", "please"}


def _tokens(text):
    t = (text or "").lower()
    out = {w for w in re.findall(r"[a-z0-9+#]{2,}", t) if w not in _STOP}
    for run in re.findall(r"[一-鿿]+", t):
        out.update(run[i:i + 2] for i in range(len(run) - 1)) if len(run) > 1 else out.add(run)
    return out


def search_pages(db, question, limit=2, chars=1200):
    """Provided copies relevant to the question: [{ref, id, title, url, provided_on, provenance, excerpts, match}], best first.
    Excerpts are chunks of the stored text (verbatim), ranked by word overlap with the question."""
    ensure(db)
    qt = _tokens(question)
    out = []
    for r in db.execute("SELECT * FROM manual_pages WHERE status='provided' AND text IS NOT NULL ORDER BY provided_at DESC LIMIT 50"):
        text = r["text"] or ""
        host = _host(r["url"]) or ""
        mention = bool(host and host in (question or "").lower()) or bool(re.search(rf"page:{r['id']}\b", question or ""))
        chunks = [c.strip() for c in re.split(r"\n+", text) if c.strip()]
        pieces = []
        for c in chunks:                                   # a long line is cut into pieces, nothing is rewritten
            pieces += [c[i:i + 300] for i in range(0, len(c), 300)] if len(c) > 300 else [c]
        scored = [(len(qt & _tokens(p)), i, p) for i, p in enumerate(pieces)]
        total = sum(s for s, _i, _p in scored)
        if total <= 0 and not mention:
            continue
        pick, used = [], 0
        for s, i, p in sorted(scored, key=lambda x: (-x[0], x[1])):
            if (s <= 0 and pick) or used + len(p) > chars:
                continue
            pick.append((i, p)); used += len(p)
            if used >= chars:
                break
        pick.sort()
        title = (pieces[0] if pieces else "")[:80]
        out.append({"ref": f"page:{r['id']}", "id": r["id"], "match": max(total, 1), "title": title, "claimed_url": r["url"],
                    "provided_on": time.strftime("%Y-%m-%d", time.localtime(r["provided_at"] or 0)),
                    "provenance": header(r), "excerpts": [p for _i, p in pick]})
    out.sort(key=lambda x: -x["match"])
    return out[:limit]


# ---------------------------------------------------------------- finding pages the bot cannot open
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'）)】，。]+")


def scan_texts(db, texts, why="a link in a notice; the school site only lets a person open it", now=None):
    """Create pending requests for links in `texts` that point at a user-only host. Never fetches. -> list of request results (new ones only)."""
    made = []
    for t in texts:
        for u in _URL_IN_TEXT.findall(t or "")[:5]:
            if user_only(u):
                r = request(db, u, why, now)
                if r["ok"] and r["created"]:
                    made.append(r)
    return made


def scan_items(db, now=None):
    """`cnb page scan`: links in saved notices (item quotes) that point at a user-only host become pending requests."""
    ensure(db)
    return scan_texts(db, [r[0] for r in db.execute("SELECT quote FROM items WHERE status IN ('new','confirmed')")], now=now)


# ---------------------------------------------------------------- command line (same logic as the page)
def _fmt_when(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts or 0))


def cli_run(a, db, out=print, stdin=None):
    """a.action: list | approve | provide | skip | scan; a.id; a.file; a.paste. Returns an exit code. Nothing is ever fetched or opened."""
    ensure(db)
    act = a.action
    if act == "list":
        rows = list_pages(db, ("pending", "approved", "provided"))
        if not rows:
            out("no pages waiting"); return 0
        for r in rows:
            out(f"#{r['id']} [{r['status']}] {r['url']}" + (f"  ({r['why']})" if r["why"] else "") + f"  asked {_fmt_when(r['asked_at'])}")
        return 0
    if act == "scan":
        made = scan_items(db)
        out(f"{len(made)} new request(s) from links in saved notices" if made else "no new links to user-only sites")
        return 0
    if a.id is None:
        out(f"cnb page {act}: give the request number (see: cnb page list)"); return 2
    if act == "approve":
        r = approve(db, a.id)
        if not r["ok"]:
            out("not approved: " + r["reason"]); return 1
        out(f"approved. Open this address in YOUR OWN browser (the bot never opens it):\n  {r['url']}\n"
            f"then give me what you see: cnb page provide {r['id']} --file saved.html   (or --paste, then paste the text and end with Ctrl-D / Ctrl-Z)")
        return 0
    if act == "skip":
        r = skip(db, a.id)
        out("skipped; not asked again for 30 days" if r["ok"] else "not skipped: " + r["reason"]); return 0 if r["ok"] else 1
    if act == "provide":
        if bool(a.file) == bool(a.paste):
            out("cnb page provide ID needs exactly one of --file F or --paste"); return 2
        if a.file:
            try:
                with open(a.file, "rb") as f:
                    data = f.read(MAX_FILE + 1)
            except OSError:
                out("the file could not be read"); return 1
            r = provide(db, a.id, data=data, filename=a.file)
        else:
            src = stdin if stdin is not None else sys.stdin
            r = provide(db, a.id, text=src.read(MAX_PASTE + 1))
        if not r["ok"]:
            out("not stored: " + r["reason"]); return 1
        out(f"stored {r['chars']} characters{' (cut at the length cap)' if r['truncated'] else ''} as a copy YOU supplied (the bot cannot know it is genuine). "
            + ("Dated notices in it are turned into to-confirm items at the next run (cnb process / cnb daily)." if r["queued_for_extraction"] else ""))
        return 0
    return 2


def add_cli_args(p):
    p.add_argument("action", choices=["list", "approve", "provide", "skip", "scan"])
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--file"); p.add_argument("--paste", action="store_true")
