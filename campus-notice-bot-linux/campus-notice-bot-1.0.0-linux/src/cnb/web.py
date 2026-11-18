"""Polite fetcher with provenance (PLAN S5 + backlog 13). School-independent: it knows how to
fetch and remember, not what any particular site means.

Everything this project claims from the web has to be traceable, because the claims are about
whether a student's time and money are worth spending. So every fetch records the URL, the time,
the HTTP status and a SHA-256 of the body, and every later re-fetch is compared against that hash
to tell "unchanged" from "revised" (the PLAN requires the whitelist to be re-checked for revisions).

Politeness, in order of importance:
 - robots.txt is consulted and obeyed. A disallowed URL is not fetched, and the refusal is
   recorded so the reason is visible rather than looking like a network failure.
 - one request at a time per host, with a minimum gap between them (MIN_GAP).
 - a generic client name. NEVER the user's name, email or anything identifying them: this tool
   runs on behalf of one student and has no business announcing who they are to a school server.
 - a size cap and a timeout, so a bad URL cannot hang or fill the disk.
 - on-disk cache, so re-running a parser during development does not re-hit the site at all.

Out of scope on purpose: logins, cookies, paywalls, campus-VPN-only pages, and anything that
looks like getting around an access control. If a page needs a login, the adapter reports that
and asks the account owner to supply the content instead.
"""
import datetime as dt, hashlib, json, os, re, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from . import db as D, tunnel as T

CACHE = os.path.join(D.ROOT, "cache", "web")
UA = "campus-notice-bot/0.1 (student notice reader; contact: the site owner's own admin)"
MIN_GAP = 2.0          # seconds between requests to the same host
TIMEOUT = 30
MAX_BYTES = 20 * 1024 * 1024
_last_hit = {}
_robots = {}


def ddl(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sources(
        id INTEGER PRIMARY KEY, url TEXT NOT NULL, kind TEXT, title TEXT,
        fetched INTEGER, status INTEGER, content_type TEXT, sha256 TEXT, bytes INTEGER,
        note TEXT, path TEXT);
    CREATE INDEX IF NOT EXISTS src_url ON sources(url, fetched);
    CREATE TABLE IF NOT EXISTS site_notices(
        url TEXT PRIMARY KEY, kind TEXT, title TEXT, sha256 TEXT, prev_sha256 TEXT,
        first_seen INTEGER, last_seen INTEGER, revisions INTEGER DEFAULT 0);
    """)
    db.commit()


def _cache_path(url):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, hashlib.sha256(url.encode()).hexdigest()[:32])


def allowed(url):
    """-> (bool, reason). robots.txt, fetched once per host and remembered for this process.
    A host with no robots.txt allows everything; a host we cannot ask is treated as allowed,
    which matches what every mainstream crawler does, but the reason is recorded either way."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, f"scheme {p.scheme!r} is not fetched"
    if T.is_blocked(url) or T.bad_host(url):
        return False, "host is blocked or not in plain ASCII form (research_sources.json -> blocked_hosts)"
    host = f"{p.scheme}://{p.netloc}"
    if host not in _robots:
        _robots[host] = _read_robots(host)
    rp = _robots[host]
    if rp is None:
        return True, "no usable robots.txt, so nothing is disallowed"
    return (True, "allowed by robots.txt") if rp.can_fetch(UA, url) else (False, "disallowed by robots.txt")


def _read_robots(host):
    """-> RobotFileParser, or None when the host has no usable robots.txt.

    Why not just RobotFileParser.read(): some sites answer /robots.txt with an HTML error page
    instead of a 404 (www.moe.gov.cn redirects https->http and serves a GB-encoded "page not
    found"). The stdlib parser accepts that as a rules file and ends up with disallow_all set, so
    the whole host would be refused although it disallowed nothing - a silent loss of an entire
    authoritative source, and the failure would look like politeness rather than a bug.
    So: fetch it, and trust it ONLY if it is actually a robots file. A real one is still obeyed
    strictly; this is not a way to ignore one."""
    try:
        req = urllib.request.Request(host + "/robots.txt", headers={"User-Agent": UA})
        with T.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return None
            raw = r.read(512 * 1024)
            ctype = (r.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        # 401/403 on robots.txt genuinely means "you are not welcome"; 4xx/5xx means no rules file
        if e.code in (401, 403):
            rp = urllib.robotparser.RobotFileParser()
            rp.disallow_all = True
            return rp
        return None
    except Exception:                          # noqa: BLE001 - DNS, TLS, timeout
        return None
    text = raw.decode("utf-8", "replace")
    looks_like_robots = re.search(r"^\s*(user-agent|disallow|allow|sitemap)\s*:", text, re.I | re.M)
    if not looks_like_robots or "<html" in text[:600].lower() or "html" in ctype:
        return None                            # an error page or a web page, not a rules file
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def _wait(url):
    # One gap per SITE (registrable domain), not per host: www., news., info. and jwc. of one
    # university usually sit on the same servers, so following subdomains must not multiply the
    # request rate. (S5 round 2, when the crawl started following other *.example-school.edu.cn hosts.)
    key = site_of(url)
    gap = time.time() - _last_hit.get(key, 0)
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_hit[key] = time.time()


# Second-level labels under which an organisation registers its own name (edu.cn, ac.uk, ...).
# A small fixed table instead of the full Public Suffix List, which would be a dependency; it
# errs STRICT: an unknown multi-part suffix gives a smaller "site", i.e. fewer hosts followed.
_SUFFIX2 = {
    "cn": {"edu", "com", "gov", "org", "net", "ac", "mil",
           "bj", "sh", "tj", "cq", "he", "sx", "nm", "ln", "jl", "hl", "js", "zj", "ah", "fj", "jx",
           "sd", "ha", "hb", "hn", "gd", "gx", "hi", "sc", "gz", "yn", "xz", "sn", "gs", "qh", "nx",
           "xj", "tw", "hk", "mo"},
    "hk": {"edu", "com", "gov", "org", "net", "idv"}, "mo": {"edu", "com", "gov", "org", "net"},
    "tw": {"edu", "com", "gov", "org", "net", "idv"}, "uk": {"ac", "co", "gov", "org", "ltd", "plc", "net", "sch", "nhs"},
    "jp": {"ac", "co", "go", "or", "ne", "ed", "ad"}, "kr": {"ac", "co", "go", "or", "re", "ne"},
    "au": {"edu", "com", "gov", "org", "net"}, "nz": {"ac", "co", "govt", "org", "net"},
    "sg": {"edu", "com", "gov", "org", "net"}, "in": {"ac", "edu", "co", "gov", "org", "net", "res"},
}


def site_of(url_or_host):
    """The registrable domain ("site") of a URL or host: news.example-school.edu.cn -> example-school.edu.cn,
    www.moe.gov.cn -> moe.gov.cn, a.b.example.com -> example.com. An IP address or a single-label
    host (localhost) is its own site. Port and case are ignored."""
    host = T.ascii_host(url_or_host) or ""
    if not host or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host) or "." not in host:
        return host
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SUFFIX2.get(labels[-1], ()):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def on_same_site(a, b):
    """True when two URLs/hosts share a registrable domain (news.example-school.edu.cn ~ www.example-school.edu.cn,
    never xexample-school.edu.cn ~ example-school.edu.cn and never example-school.edu.cn.evil.com ~ example-school.edu.cn)."""
    sa = site_of(a)
    return bool(sa) and sa == site_of(b)


def fetch(url, db=None, max_age=86400, force=False):
    """-> dict(url, ok, status, content_type, body: bytes|None, sha256, cached, changed, note).

    `max_age` serves the cache: a page fetched more recently than that is returned from disk
    without touching the network. `changed` compares this body's hash with the newest hash
    recorded for the same URL, which is how a revised whitelist is noticed."""
    if T.is_blocked(url) or T.bad_host(url):   # hard block: no cache, no robots lookup, no DNS, no request
        return dict(url=url, ok=False, status=None, content_type=None, body=None, sha256=None, cached=False,
                    changed=False, fetched=int(time.time()),
                    note="host is blocked or not in plain ASCII form (research_sources.json -> blocked_hosts): no request sent")
    meta_path = _cache_path(url) + ".json"
    body_path = _cache_path(url) + ".bin"
    prev = None
    if os.path.exists(meta_path):
        try:
            prev = json.load(open(meta_path, encoding="utf-8"))
        except ValueError:
            prev = None
    if prev and not force and os.path.exists(body_path) and time.time() - prev.get("fetched", 0) < max_age:
        body = open(body_path, "rb").read()
        out = dict(prev, body=body, cached=True, changed=False, ok=prev.get("status") == 200)
        out.pop("wall", None)
        return _wall_check(out)

    ok_robots, why = allowed(url)
    if not ok_robots:
        out = dict(url=url, ok=False, status=None, content_type=None, body=None, sha256=None,
                   cached=False, changed=False, fetched=int(time.time()), note=why)
        _record(db, out)
        return out

    _wait(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with T.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                out = dict(url=url, ok=False, status=r.status, content_type=r.headers.get("Content-Type"),
                           body=None, sha256=None, cached=False, changed=False,
                           fetched=int(time.time()), note=f"larger than {MAX_BYTES} bytes, not read")
                _record(db, out)
                return out
            status, ctype = r.status, (r.headers.get("Content-Type") or "").split(";")[0].strip()
            final = r.geturl()
            route = getattr(r, "route", None)
    except urllib.error.HTTPError as e:
        out = dict(url=url, ok=False, status=e.code, content_type=None, body=None, sha256=None,
                   cached=False, changed=False, fetched=int(time.time()),
                   note=f"HTTP {e.code} {e.reason}" + (" (page may need a login or the campus network)"
                                                       if e.code in (401, 403) else ""))
        _record(db, out)
        return out
    except Exception as e:                     # noqa: BLE001 - DNS, TLS, timeout, reset...
        out = dict(url=url, ok=False, status=None, content_type=None, body=None, sha256=None,
                   cached=False, changed=False, fetched=int(time.time()),
                   note=f"{type(e).__name__}: {e}")
        _record(db, out)
        return out

    sha = hashlib.sha256(body).hexdigest()
    changed = bool(prev and prev.get("sha256") and prev["sha256"] != sha)
    out = dict(url=url, ok=status == 200, status=status, content_type=ctype, sha256=sha,
               bytes=len(body), cached=False, changed=changed, fetched=int(time.time()),
               note="revised since last fetch" if changed else why)
    if route:
        out["route"] = route                   # e.g. "relay IPv6": which way the page really came
    if final and final != url:
        # urllib follows redirects (http->https, a moved page, or a bounce to a login server).
        # Record where we really ended up: provenance must name the page that was read.
        out["final_url"] = final
        if urllib.parse.urlparse(final).netloc != urllib.parse.urlparse(url).netloc:
            out["note"] = f"redirected to {final}; " + (out["note"] or "")
    os.makedirs(CACHE, exist_ok=True)
    open(body_path, "wb").write(body)
    json.dump({k: v for k, v in out.items() if k != "body"}, open(meta_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    out["body"] = body
    _wall_check(out)
    _record(db, out)
    return out


def _wall_check(out):
    """An anti-bot challenge, a CAPTCHA page or a login form comes back as HTTP 200. Mark it as
    NOT ok, with the reason, so nothing downstream treats it as an (empty) document. The body is
    kept for inspection; nothing here tries to get past the wall."""
    body, ctype = out.get("body") or b"", (out.get("content_type") or "").lower()
    if body and out.get("status") and 200 <= out["status"] < 300 and ("html" in ctype or b"<html" in body[:4096].lower()):
        from . import readers
        wall = readers.access_wall(body)
        if wall:
            out["ok"], out["wall"], out["note"] = False, wall, f"blocked: {wall}"
    return out


def _record(db, out):
    if db is None:
        return
    ddl(db)
    db.execute("INSERT INTO sources(url,kind,fetched,status,content_type,sha256,bytes,note,path)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (out["url"], out.get("content_type"), out["fetched"], out.get("status"),
                out.get("content_type"), out.get("sha256"), out.get("bytes"), out.get("note"),
                _cache_path(out["url"]) + ".bin"))
    db.commit()


def provenance(out):
    """One line a human can check, attached to anything we claim from this page."""
    when = dt.datetime.fromtimestamp(out.get("fetched") or 0, dt.timezone(dt.timedelta(hours=8)))
    return f"{out['url']} · fetched {when:%Y-%m-%d %H:%M} · " + \
           (f"via {out['route']} · " if out.get("route") else "") + \
           (f"sha256 {out['sha256'][:12]}" if out.get("sha256") else f"not read: {out.get('note')}")


def crawl(start, db=None, depth=1, max_pages=25, same_host=True, want=None, max_age=86400, hosts=None,
          same_site=True):
    """Follow links from `start`, breadth first, and return every page read, with provenance.

    Bounded on purpose, because an unbounded crawl of a university site is both rude and useless:
      depth      how many link-hops from the start page (1 = the start page and what it links to)
      max_pages  a hard ceiling on fetches, whatever the depth allows
      same_host  never wander off the place we were pointed at (False = follow any http(s) link;
                 nothing in this project calls it that way)
      same_site  with same_host: "the place" is the start page's registrable domain, so
                 www.example-school.edu.cn may follow news./info./jwc.example-school.edu.cn (user approval
                 2026-11-13). same_site=False keeps the strict single-host crawl.
      hosts      optional list of hosts (or parent domains) that ARE allowed besides that
      want       optional predicate (text, href) -> bool, to follow only the links that matter,
                 e.g. only notice detail pages and attachments rather than the whole navigation
    Every host is still asked for its OWN robots.txt (fetch -> allowed), the gap between requests
    is per site, and the cache and provenance are the same as for any fetch. A page that REDIRECTS
    off the site is recorded with the reason but neither read nor expanded.
    Attachments (PDF/docx/xlsx) are read but never expanded, so they cost one fetch and no links.
    """
    from . import readers
    seen, out, queue = set(), [], [(start, 0)]
    host0 = urllib.parse.urlparse(start).netloc

    def inside(u):
        if not same_host:
            return True
        return (urllib.parse.urlparse(u).netloc == host0 or host_ok(u, hosts)
                or (same_site and on_same_site(u, start)))

    while queue and len(out) < max_pages:
        url, d = queue.pop(0)
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        got = fetch(key, db=db, max_age=max_age)
        page = {"url": key, "depth": d, "fetch": got, "provenance": provenance(got)}
        final = got.get("final_url") or key
        if final != key and not inside(final):
            page["doc"] = {"kind": None, "text": "", "tables": [], "links": [],
                           "note": f"redirected off the site to {final}; not read and not followed"}
        elif got.get("ok") and got.get("body"):
            page["doc"] = readers.read(got["body"], got.get("content_type") or "", final)
        else:
            page["doc"] = {"kind": None, "text": "", "tables": [], "links": [],
                           "note": got.get("note")}
        out.append(page)
        if d >= depth or page["doc"]["kind"] != "html":
            continue                            # attachments and the depth limit stop here
        for text, href in page["doc"]["links"]:
            if not href or href.startswith(("javascript:", "mailto:", "#")):
                continue
            nxt = urllib.parse.urljoin(final, href).split("#")[0]
            if urllib.parse.urlparse(nxt).scheme not in ("http", "https") or not inside(nxt):
                continue
            if want and not want(text, nxt):
                continue
            if nxt not in seen:
                queue.append((nxt, d + 1))
    return out


NOTICE_HINT = re.compile(r"通知|公告|竞赛|比赛|赛事|名单|目录|文件|附件|办法|认定|学科竞赛|"
                         r"notice|announce|contest|competition|list|attachment", re.I)
FILE_HINT = re.compile(r"\.(pdf|docx?|xlsx?)(\?|$)", re.I)


def looks_relevant(text, href):
    """Default `want` for a campus site: follow links that read like a notice, a contest list or
    an attachment, and ignore the navigation furniture. Keeps a crawl to a few dozen fetches."""
    return bool(NOTICE_HINT.search(text or "") or NOTICE_HINT.search(href or "")
                or FILE_HINT.search(href or ""))


def host_ok(url, hosts):
    """Is `url` on one of `hosts`? A host matches itself and its subdomains ('example-school.edu.cn' allows
    news.example-school.edu.cn), never a look-alike ('xexample-school.edu.cn')."""
    h = T.ascii_host(url)                       # IDNA/NFKC form; None (unconvertible) never matches
    return bool(h) and not T.bad_host(url) and any(h == x.lower() or h.endswith("." + x.lower()) for x in (hosts or []))


def check(url):
    """Reachability spike, used before any parser is written for a site: can we get it at all,
    are we allowed to, what did it turn out to be. Fetches nothing into the DB."""
    ok, why = allowed(url)
    out = fetch(url, max_age=0) if ok else {"url": url, "ok": False, "note": why, "fetched": int(time.time())}
    return {"url": url, "robots": why, "ok": out.get("ok"), "status": out.get("status"),
            "content_type": out.get("content_type"), "bytes": out.get("bytes"), "note": out.get("note")}


# ============================ site adapter (S5) ============================
# Driven entirely by research_sources.json -> "school": a second school is a config entry, not
# code. Per list page:  list page -> notice pages -> the files each notice cites -> items.
#
#   "school": {"name", "host", "follow_hosts": [...],
#              "notice_urls": [ "https://..." | {"url", "name", "notice_link" (regex on the URL),
#                                                 "content" (regex on the body's class/id),
#                                                 "max_notices", "max_files"} ]}
#
# What comes out is EVIDENCE, not decisions: each notice with the exact time/place/deadline
# strings it (or its cited files) states, where each was read, what could not be read and why,
# and whether the notice changed since the last scan (SHA-256 of the notice BODY, not the page,
# because campus pages carry sidebars and counters that change on every visit).
NOTICE_LINK_DEFAULT = r"/(?:info|content|article|news|tzgg|xwdt|notice)[/_]?\S*\d+\.(?:s?html?|jsp|php)|/\d{4,}/\S*\.s?html?$"


def _seen(db, url, kind, title, sha):
    """Revision detection by SHA-256 -> 'new' | 'unchanged' | 'revised' (None without a DB)."""
    if db is None or not sha:
        return None
    ddl(db)
    now = int(time.time())
    row = db.execute("SELECT sha256 FROM site_notices WHERE url=?", (url,)).fetchone()
    if row is None:
        db.execute("INSERT INTO site_notices(url,kind,title,sha256,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                   (url, kind, title, sha, now, now))
        state = "new"
    elif row[0] == sha:
        db.execute("UPDATE site_notices SET last_seen=? WHERE url=?", (now, url))
        state = "unchanged"
    else:
        db.execute("UPDATE site_notices SET prev_sha256=sha256, sha256=?, last_seen=?, revisions=revisions+1,"
                   " title=COALESCE(?,title) WHERE url=?", (sha, now, title, url))
        state = "revised"
    db.commit()
    return state


def _norm_src(src):
    return {"url": src} if isinstance(src, str) else dict(src)


def read_file(f, db=None, hosts=None, keywords=None, max_age=7 * 86400, kw=None, notice_hy=None):
    """Fetch and read ONE cited file, and search it for the notice's time/place.
    -> the file dict, extended with status ('read'|'blocked'|'unreadable'|'not fetched'),
       note, kind, provenance, revision and found{time,place,deadline}: entries with file+loc."""
    from . import readers
    f = dict(f)
    if hosts is not None and not host_ok(f["url"], hosts):
        f.update(status="not fetched", note=f"host {urllib.parse.urlparse(f['url']).hostname} is not in "
                                            "follow_hosts (research_sources.json); open it yourself")
        return f
    got = fetch(f["url"], db=db, max_age=max_age)
    f["provenance"] = provenance(got)
    if got.get("wall"):
        f.update(status="blocked", note=got["wall"])
        return f
    if not got.get("ok") or not got.get("body"):
        f.update(status="not fetched", note=got.get("note"))
        return f
    doc = readers.read(got["body"], got.get("content_type") or "", got.get("final_url") or f["url"])
    f["kind"] = doc["kind"]
    if doc.get("blocked"):
        f.update(status="blocked", note=doc["blocked"])
        return f
    if not (doc.get("text") or doc.get("tables")):
        f.update(status="unreadable", note=doc.get("note") or "no text found in the file")
        return f
    f.update(status="read", note=doc.get("note"), sha256=got.get("sha256"),
             revision=_seen(db, f["url"], "file", f.get("name"), got.get("sha256")))
    found = readers.when_where(doc, keywords=keywords or None, kw=kw)
    if keywords and not any(found[k] for k in ("time", "place", "deadline")):
        # none of the notice's own words is on a line with a time/place: fall back to every
        # labelled time/place in the file, flagged so a person checks it belongs to this notice
        allf = readers.when_where(doc, kw=kw)
        found = {k: [dict(e, how=e["how"] + " (no keyword from the notice on this line: check it)")
                     for e in v if e["how"].startswith("labelled")][:10] for k, v in allf.items()}
    for v in found.values():
        for e in v:
            e["file"], e["url"] = f.get("name"), f["url"]
    # year-less dates take the year the FILE's header states; only when the file's header states no
    # year at all (and raises no doubt) does the citing notice's header year apply, said as such
    hy = readers.header_year(doc, f.get("name"))
    origin = f"file '{f.get('name')}'"
    if not (hy.get("year") or hy.get("range") or hy.get("note")) and notice_hy:
        hy, origin = notice_hy, "the notice citing it"
    f["header_year"] = _hy_public(hy)
    for k in ("time", "deadline"):
        readers.attach_years(found[k], hy, origin)
    if not any(found.values()):
        f["note"] = ((f.get("note") + "; ") if f.get("note") else "") + \
            "read, but it has no labelled time/place line and none near the notice's own keywords"
    f["found"] = found
    f["_doc"] = doc
    return f


def _hy_public(hy):
    """header_year() result, trimmed for the JSON output (None when the header states no year)."""
    if not (hy.get("year") or hy.get("range") or hy.get("note")):
        return None
    return {k: hy.get(k) for k in ("year", "range", "where", "quote", "note") if hy.get(k)}


def read_notice(url, title=None, src=None, db=None, hosts=None, max_age=86400, kw=None, interests=None):
    """One notice page -> its exact time/place/deadline, with provenance, and the files it cites.
    Nothing is filled in: what neither the notice nor its readable files state is listed under
    `unknown`, with the reason (file blocked / unreadable / poster image / 另行通知)."""
    from . import readers
    src = _norm_src(src or {"url": url})
    n = {"url": url, "title": title, "time": [], "place": [], "deadline": [], "contact_place": [],
         "files": [], "cites": [], "deferred": [], "images": 0, "unknown": [], "problems": [],
         "revision": None}
    got = fetch(url, db=db, max_age=max_age)
    n["provenance"] = provenance(got)
    if not got.get("ok") or not got.get("body"):
        n["problems"].append(f"notice not read: {got.get('wall') or got.get('note')}")
        n["unknown"] += ["time", "place"]
        return n
    doc = readers.read(got["body"], got.get("content_type") or "", url, content_re=src.get("content"))
    if doc.get("blocked"):
        n["problems"].append(f"notice not read: {doc['blocked']}")
        n["unknown"] += ["time", "place"]
        return n
    body = doc.get("main")
    if not body:
        n["problems"].append("notice body not found by the content pattern; only LABELLED time/place lines "
                             "of the whole page are used (menus and footers would add noise otherwise). "
                             "Set 'content' for this source in research_sources.json.")
    if not n["title"]:
        n["title"] = next((ln for ln in (doc["text"] or "").split("\n") if len(ln) > 6), url)[:120]
    n["sha256_body"] = hashlib.sha256((body or doc["text"] or "").encode()).hexdigest()
    n["revision"] = _seen(db, url, "notice", n["title"], n["sha256_body"])
    own = readers.when_where(doc, kw=kw)
    for k, v in own.items():
        n[k] = [dict(e, file=None, url=url) for e in v if body or e["how"].startswith("labelled")]
    hy = readers.header_year({"kind": doc["kind"], "text": body or ""}, n["title"]) if body else \
        readers.header_year({"kind": "text", "text": ""}, n["title"])
    n["header_year"] = _hy_public(hy)
    for k in ("time", "deadline"):
        readers.attach_years(n[k], hy, "the notice")
    cites = readers.cited_files(doc, got.get("final_url") or url)
    n["cites"], n["deferred"] = cites["phrases"], cites["deferred"]
    n["images"] = len(doc.get("images") or [])
    keywords = readers.notice_keywords(body or "", n["title"] or "", kw, interests)
    n["keywords"] = keywords
    from . import research
    n["whitelist_candidates"] = research.whitelist_candidates(doc, url) if body else []
    for f in cites["files"][:int(src.get("max_files") or 6)]:
        rf = read_file(f, db=db, hosts=hosts, keywords=keywords, kw=kw, notice_hy=hy)
        fdoc = rf.pop("_doc", None)
        if fdoc:
            n["whitelist_candidates"] += research.whitelist_candidates(fdoc, rf["url"], rf.get("name"))
        n["files"].append(rf)
        for k, v in (rf.get("found") or {}).items():
            n[k] += v
    if cites["unlinked"]:
        n["problems"].append("the notice cites a file (" + "、".join(p["text"] for p in cites["phrases"][:3]) +
                             ") but no file link was found on the page")
    for k in ("time", "place", "deadline", "contact_place"):
        # most trustworthy first: labelled lines of the notice, labelled lines of its files, then patterns
        n[k].sort(key=lambda e: (not e["how"].startswith("labelled"), e.get("file") is not None))
    for need in ("time", "place"):
        if not n[need]:
            why = []
            why += [f"cited file '{f['name']}' {f['status']}: {f.get('note')}" for f in n["files"]
                    if f.get("status") != "read"]
            if n["images"]:
                why.append(f"the notice body has {n['images']} image(s) (e.g. a poster); text in images "
                           "is not read (no OCR)")
            why += [f"the notice says: {d['text']}" for d in n["deferred"]]
            n["unknown"].append(need + (" - " + "; ".join(why) if why else " - not stated in the notice"))
    return n


def scan_list(src, school=None, db=None, max_age=86400, kw=None, interests=None, only_new=False):
    """One list page -> the notices it links -> read_notice() each."""
    from . import readers
    src, school = _norm_src(src), school or {}
    lu = src["url"]
    hosts = list(school.get("follow_hosts") or []) + [urllib.parse.urlparse(lu).hostname]
    rep = {"list": lu, "name": src.get("name"), "notices": [], "problems": []}
    if src.get("enabled") is False:
        rep["problems"].append("list disabled in research_sources.json (enabled: false): not fetched")
        return rep
    got = fetch(lu, db=db, max_age=min(max_age, 6 * 3600))      # lists change daily; re-read them
    rep["provenance"] = provenance(got)
    if not got.get("ok") or not got.get("body"):
        rep["problems"].append(f"list page not read: {got.get('wall') or got.get('note')}")
        return rep
    doc = readers.read(got["body"], got.get("content_type") or "", lu)
    if doc.get("blocked"):
        rep["problems"].append(f"list page not read: {doc['blocked']}")
        return rep
    pat = re.compile(src.get("notice_link") or NOTICE_LINK_DEFAULT, re.I)
    base = got.get("final_url") or lu
    titles = {}
    for t, h in doc["links"]:
        if not h or h.startswith(("javascript:", "mailto:", "#")):
            continue
        u = urllib.parse.urljoin(base, h).split("#")[0]
        if pat.search(u) and host_ok(u, hosts):
            if len(t or "") > len(titles.get(u) or ""):
                titles[u] = t
            titles.setdefault(u, t)
    if not titles:
        rep["problems"].append(f"no notice links matched {pat.pattern!r} on the list page")
    for u, t in list(titles.items())[:int(src.get("max_notices") or 15)]:
        n = read_notice(u, t or None, src, db=db, hosts=hosts, max_age=max_age, kw=kw, interests=interests)
        if only_new and n["revision"] == "unchanged":
            continue
        rep["notices"].append(n)
    return rep


def scan_school(cfg=None, db=None, max_age=86400, kw=None, interests=None, only_new=False, names=None):
    """Every list page in research_sources.json -> school -> notice_urls. Also collects contest
    whitelist candidates from any readable table that has a contest-name column (they are
    PROPOSED only: research.propose_whitelist marks them UNCONFIRMED until the user confirms)."""
    from . import research
    cfg = cfg or research.sources()
    school = cfg.get("school") or {}
    out = {"school": school.get("name"), "scanned": int(time.time()), "lists": []}
    if not school.get("notice_urls") and not school.get("whitelist_urls"):
        out["problems"] = ["research_sources.json -> school -> notice_urls is empty: the school's notice "
                           "list URLs are needed"]
        return out
    out["whitelist_docs"] = []
    for wu in school.get("whitelist_urls") or []:
        w = _norm_src(wu)
        rf = read_file({"name": w.get("name") or w["url"], "url": w["url"], "ext": None, "kind": "file",
                        "how": "whitelist_urls"}, db=db, hosts=None, kw=kw)
        fdoc = rf.pop("_doc", None)
        rf["whitelist_candidates"] = research.whitelist_candidates(fdoc, w["url"], rf.get("name")) if fdoc else []
        rf.pop("found", None)
        out["whitelist_docs"].append(rf)
    for src in school.get("notice_urls") or []:
        s = _norm_src(src)
        if names and s.get("name") not in names and s["url"] not in names:
            continue
        out["lists"].append(scan_list(s, school, db=db, max_age=max_age, kw=kw, interests=interests,
                                      only_new=only_new))
    return out


def summary(scan):
    """Plain-text summary of scan_school() output, for a terminal."""
    lines = [f"school: {scan.get('school')}"] + [f"  ! {p}" for p in scan.get("problems", [])]
    for w in scan.get("whitelist_docs", []):
        lines.append(f"WHITELIST DOC {w['url']} -> {w.get('status')}: {w.get('note') or ''} "
                     f"({len(w.get('whitelist_candidates') or [])} candidate entries)")
    for L in scan.get("lists", []):
        lines.append(f"\nLIST {L.get('name') or ''} {L['list']}\n  {L.get('provenance')}")
        lines += [f"  ! {p}" for p in L["problems"]]
        for n in L["notices"]:
            lines.append(f"\n  * {n['title']}  [{n['revision'] or '-'}]\n    {n['url']}")
            for k in ("time", "place", "deadline"):
                for e in n[k][:4]:
                    src = f"file '{e['file']}' {e['loc']}" if e.get("file") else e["loc"]
                    yr = f"  [year {e['year']} from header]" if e.get("year") else \
                        (f"  [no year: {e['year_note']}]" if e.get("year_note") else "")
                    lines.append(f"    {k:8} {e['value']}   ({e['label'] or e['how']}; {src}){yr}")
            for f in n["files"]:
                lines.append(f"    file    {f['name']} -> {f.get('status')}" + (f": {f['note']}" if f.get("note") else ""))
            if n.get("whitelist_candidates"):
                lines.append(f"    whitelist candidates: {len(n['whitelist_candidates'])} (UNCONFIRMED)")
            lines += [f"    unknown {u}" for u in n["unknown"]]
            lines += [f"    !       {p}" for p in n["problems"]]
    return "\n".join(lines)


if __name__ == "__main__":                    # python3 -m cnb.web scan [--fresh]
    # Uses its OWN small DB (cache/site/site.db, or $CNB_SITE_DB) for fetch records and notice
    # hashes, never capture/messages.db: a site scan has no business writing into QQ capture state.
    import sqlite3, sys
    args = sys.argv[1:]
    if not args or args[0] != "scan":
        raise SystemExit("usage: python3 -m cnb.web scan [--fresh]")
    out_dir = os.path.join(D.ROOT, "cache", "site")
    os.makedirs(out_dir, exist_ok=True)
    conn = sqlite3.connect(os.environ.get("CNB_SITE_DB") or os.path.join(out_dir, "site.db"))
    res = scan_school(db=conn, max_age=0 if "--fresh" in args else 86400)
    print(summary(res))
    path = os.path.join(out_dir, "scan-latest.json")
    json.dump(res, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"\nwritten: {path}")
