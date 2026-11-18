"""AI research loop (PLAN backlog 13 + 14): the model, plugged into three things it did not have
before - the bot's OWN memory, a polite fetcher, and permission to follow links further.

Why a tool loop rather than one big prompt: the questions here ("what kind of body runs this
contest, and does my school recognise it?") cannot be answered from the message alone, and
stuffing a whole crawl into one prompt would both cost a fortune and lose track of WHICH page
each claim came from. With tools the model asks for one thing at a time, and every page it reads
is recorded with its URL, fetch time and content hash, so each sentence in the verdict can be
traced back to something a human can open.

Hard rules, enforced in code rather than asked for in the prompt:
 - SOURCE ALLOWLIST. The model may only open hosts in research_sources.json. It cannot wander
   onto a blog, a content farm, or the contest's own marketing page and present it as a finding.
   The user's choice (2026-11-10) was authoritative lists only: the ministry / learned society /
   the school's own documents.
 - PROVENANCE OR IT DIDN'T HAPPEN. A finding with no source URL is dropped before it is stored.
 - UNKNOWN IS AN ANSWER. "Not found on the authoritative lists" is a legitimate, useful verdict,
   and is never upgraded to "not recognised" - the PLAN's distinction between `no` and `unknown`.
 - BUDGETED. A fixed ceiling on steps and fetches, so a loop cannot run away with the student's
   API credit or hammer a school server.
 - ONE TENANT. Memory reads are scoped to this DB, scrubbed of phones and emails like every other
   LLM call, and nothing from another student can enter the context. There is no shared cache.
 - ADVISORY. The output is evidence for the student, never a decision taken on their behalf.
"""
import hashlib, json, os, re, time, urllib.parse
from . import db as D, extract as E, web, readers

SOURCES = os.path.join(D.ROOT, "research_sources.json")
MAX_STEPS = 12
MAX_FETCH = 15
PAGE_CHARS = 6000          # per page handed to the model; enough for a notice, not a whole site

DEFAULT_SOURCES = {
    "_comment": "Hosts the researcher may open, most authoritative first. User decision "
                "(2026-11-10): authoritative lists only - no open web search, no content farms. "
                "Add your school's own domain here; that is the one that decides recognition.",
    "allow": [
        {"host": "moe.gov.cn", "what": "Ministry of Education: the contests it names are the top of the hierarchy"},
        {"host": "cahe.edu.cn", "what": "中国高等教育学会: the 全国普通高校大学生竞赛榜单, the standard Chinese ranking of university contests"},
        {"host": "chinadegrees.cn", "what": "degree and programme records"},
        {"host": "edu.cn", "what": "education network domains, including most universities"}
    ],
    "school": {"_comment": "The tenant's own school. Its documents outrank every national list for "
                           "the recognition question, because recognition is per school.",
               "name": None, "host": None, "follow_hosts": [], "notice_urls": [], "whitelist_urls": []}
}


def sources():
    if not os.path.exists(SOURCES):
        json.dump(DEFAULT_SOURCES, open(SOURCES, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        return DEFAULT_SOURCES
    try:
        return json.load(open(SOURCES, encoding="utf-8"))
    except ValueError:
        raise SystemExit("research_sources.json is not valid JSON")


def allowed_host(url, cfg=None):
    cfg = cfg or sources()
    try:
        from . import tunnel as T
        host = T.ascii_host(url)               # IDNA/NFKC form, hostname not netloc ('example-school.edu.cn:x@evil.com')
        if T.bad_host(url):
            return False
    except ValueError:
        return False
    if not host:
        return False
    hosts = [a["host"].lower() for a in cfg.get("allow", [])]
    s = (cfg.get("school") or {}).get("host")
    if s:
        hosts.append(s.lower())
    return any(host == h or host.endswith("." + h) for h in hosts)


# ---------------- the bot's own memory, as something the model can query ----------------
def memory_snapshot(db, query=None, items_limit=15, msgs_limit=10):
    """A bounded, relevant slice of what the bot knows. The PLAN's retrieval rule: pick only the
    rows that matter and send those, never the whole store. Scrubbed, like every LLM call."""
    out = {"profile": [], "items": [], "messages": [], "whitelist_entries": 0}
    try:
        from . import profile as P
        out["profile"] = P.snapshot_facts(db)          # labelled pending / stale / confirmed (S2d); evidence already scrubbed
    except Exception:                          # noqa: BLE001 - no profile yet is normal
        pass
    try:
        from . import contest as C
        out["whitelist_entries"] = len(C.load_whitelist().get("entries", []))
    except Exception:                          # noqa: BLE001
        pass
    try:
        rows = db.execute("SELECT * FROM items WHERE status IN ('new','confirmed')"
                          " ORDER BY COALESCE(due,start,'9999') LIMIT ?", (items_limit,)).fetchall()
        for i in rows:
            out["items"].append({"id": i["id"], "type": i["type"], "title": i["title"],
                                 "when": i["start"] or i["due"], "quote": E.scrub(i["quote"] or "")[:200]})
    except Exception:                          # noqa: BLE001
        pass
    if query:
        try:
            rows = db.execute("SELECT m.* FROM msg_fts f JOIN msg m ON m.id=f.rowid"
                              " WHERE msg_fts MATCH ? ORDER BY m.ts DESC LIMIT ?",
                              (f'"{query}"', msgs_limit)).fetchall()
        except Exception:                      # noqa: BLE001 - FTS syntax, empty index
            rows = db.execute("SELECT * FROM msg WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                              (f"%{query}%", msgs_limit)).fetchall()
        for m in rows:
            out["messages"].append({"id": m["id"], "group": m["group_id"], "ts": m["ts"],
                                    "text": E.scrub(m["text"] or "")[:300]})
    return out


# ---------------- tools the model may call ----------------
TOOLS = [
    {"type": "function", "function": {
        "name": "read_memory",
        "description": "Read what the bot already knows about THIS student: ability profile (with the "
                       "evidence behind each level), open notices and contests, and messages matching a "
                       "keyword. Use this before searching the web: the answer is often already here.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "keyword to search the student's captured messages for; omit for profile and open items only"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "open_page",
        "description": "Fetch one URL from the allowed authoritative sources and return its text, "
                       "tables and links. Attachments (PDF/DOCX/XLSX) are read too.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "crawl_from",
        "description": "Follow the relevant links from one page (one hop) on an allowed host, to find "
                       "a notice or an attachment you cannot reach directly. Returns a list of pages with "
                       "their titles and a text snippet; call open_page for the full text of one.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_pages": {"type": "integer", "description": "at most 10"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "list_sources",
        "description": "List which hosts you are allowed to open and what each is authoritative for.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

SYSTEM = """You research one contest, project or notice for one student, using only the sources you are allowed to open.

Answer these separately and never merge them:
 1 WHAT IT IS: what kind of event, and what kind of body runs it - a ministry, a national learned
   society, a university, a provincial body, a company, or an unaccredited promoter. Say which.
 2 WHAT KIND OF SCHOOL/INSTITUTION is behind it, if a school is involved.
 3 RECOGNITION: does the student's own school recognise it? Only the school's own document can
   settle this. A national ranking list is evidence about the contest's standing, NOT about this
   school's recognition.
 4 CREDIBILITY: anything a student should check before paying or committing - a fee for a
   guaranteed certificate, no named organiser, registration only through agents, a brand-new body
   with no record. State what you saw, not a feeling.

Rules:
 - Every claim must name the URL you read it on. A claim you cannot source must be dropped.
 - "Not found on the authoritative sources I can reach" is a correct and useful answer. Never turn
   it into "not recognised" or "not legitimate": absence of a record is not evidence of a fake.
 - Never guess at the contents of a page you could not open. Say it could not be opened and why.
 - Check the student's own memory first with read_memory; the school and the captured notice may
   already be there.
 - You are advising, not deciding. Do not tell the student to register or not to register; tell
   them what is true and what to check.

When you are done, reply with JSON only:
{"what_it_is":str,"organiser_kind":str|null,"school_kind":str|null,"recognition":"yes|no|unknown",
 "recognition_basis":str,"credibility":[str],"checks":[str],"sources":[{"url":str,"supports":str}],
 "unknown":[str]}"""


def _tool_result(db, name, args, state):
    cfg = state["cfg"]
    if name == "list_sources":
        return {"allow": cfg.get("allow", []), "school": cfg.get("school", {})}
    if name == "read_memory":
        state["memory_reads"] += 1
        return memory_snapshot(db, args.get("query"))
    if name in ("open_page", "crawl_from"):
        url = args.get("url") or ""
        if not allowed_host(url, cfg):
            return {"error": "host not on the allowed source list; use list_sources to see what you may open",
                    "url": url}
        if state["fetches"] >= MAX_FETCH:
            return {"error": f"fetch budget of {MAX_FETCH} pages is used up; answer with what you have"}
        if name == "open_page":
            state["fetches"] += 1
            got = web.fetch(url, db=db)
            if not got.get("ok") or not got.get("body"):
                return {"url": url, "opened": False, "why": got.get("note"), "status": got.get("status")}
            if got.get("final_url") and not allowed_host(got["final_url"], cfg):
                # an allowed URL that redirects off the allowlist is not a source either
                return {"url": url, "opened": False,
                        "why": f"redirected to {got['final_url']}, which is not on the allowed source list"}
            doc = readers.read(got["body"], got.get("content_type") or "", got.get("final_url") or url)
            state["seen"].append(web.provenance(got))
            if doc.get("blocked"):
                return {"url": url, "opened": False, "why": doc["blocked"]}
            cites = readers.cited_files(doc, got.get("final_url") or url) if doc["kind"] == "html" else None
            return {"url": url, "opened": True, "kind": doc["kind"], "note": doc["note"],
                    # the notice body when the page has one, so menus and footers do not reach the model
                    "text": (doc.get("main") or doc["text"])[:PAGE_CHARS],
                    "cited_files": [{"name": f["name"], "url": f["url"]} for f in cites["files"]] if cites else [],
                    "tables": doc["tables"][:6], "links": doc["links"][:60],
                    "provenance": web.provenance(got)}
        n = min(int(args.get("max_pages") or 6), 10, MAX_FETCH - state["fetches"])
        # the crawl may now follow other subdomains of the start page's site (news./info./jwc. of
        # a university), but never past the source allowlist: every followed link is re-checked.
        pages = web.crawl(url, db=db, depth=1, max_pages=max(1, n),
                          want=lambda t, h: web.looks_relevant(t, h) and allowed_host(h, cfg))
        state["fetches"] += len(pages)
        state["seen"] += [p["provenance"] for p in pages]
        return {"pages": [{"url": p["url"], "kind": p["doc"]["kind"],
                           "snippet": (p["doc"]["text"] or "")[:400],
                           "note": p["doc"]["note"]} for p in pages]}
    return {"error": f"unknown tool {name}"}


def chat(messages, tools, model, key, base):
    body = {"model": model, "messages": messages, "tools": tools, "max_tokens": 4000}
    import urllib.request
    req = urllib.request.Request(base + "/chat/completions", method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 data=json.dumps(body, ensure_ascii=False).encode())
    return json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]


def research(db, question, llm=None, max_steps=MAX_STEPS, verbose=False):
    """Run the loop. `llm(messages, tools) -> assistant message dict` is injectable so the whole
    thing can be tested with no key and no network (tests/test_core.py does exactly that)."""
    cfg = sources()
    if llm is None:
        model, _tz, key, base, kn = E.load_cfg()
        if not (model and key):
            raise SystemExit(f"Set model in config.json and {kn} in stack/.env to use research.")
        llm = lambda m, t: chat(m, t, model, key, base)       # noqa: E731
    state = {"cfg": cfg, "fetches": 0, "memory_reads": 0, "seen": []}
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": E.scrub(question)}]
    for step in range(max_steps):
        m = llm(msgs, TOOLS)
        msgs.append(m)
        calls = m.get("tool_calls") or []
        if not calls:
            return _finish(m.get("content") or "", state)
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            res = _tool_result(db, fn, args, state)
            if verbose:
                print(f"  [{step}] {fn}({args}) -> {str(res)[:120]}")
            msgs.append({"role": "tool", "tool_call_id": c["id"],
                         "content": json.dumps(res, ensure_ascii=False)[:12000]})
    return _finish("", state, note=f"stopped after {max_steps} steps without a final answer")


def _finish(content, state, note=None):
    out = {"pages_read": state["fetches"], "memory_reads": state["memory_reads"],
           "provenance": state["seen"], "note": note}
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            out["finding"] = json.loads(m.group(0))
        except ValueError:
            out["finding"] = None
            out["raw"] = content
    else:
        out["finding"] = None
        out["raw"] = content
    f = out.get("finding")
    if f:
        # provenance or it didn't happen: drop sourceless claims rather than storing them
        f["sources"] = [s for s in (f.get("sources") or []) if s.get("url")]
        if not f["sources"]:
            f["recognition"] = "unknown"
            f.setdefault("unknown", []).append("no source URL was produced, so nothing here is verified")
    return out


def save(db, item_id, out):
    """Store a finding against a contest item, with its sources. Kept separate from the item's own
    extracted fields so the report can always show which came from the message and which from the web."""
    db.executescript("""CREATE TABLE IF NOT EXISTS findings(
        id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES items(id), created INTEGER,
        finding TEXT, provenance TEXT, pages_read INTEGER)""")
    if not out.get("finding"):
        return None
    cur = db.execute("INSERT INTO findings(item_id,created,finding,provenance,pages_read)"
                     " VALUES(?,strftime('%s','now'),?,?,?)",
                     (item_id, json.dumps(out["finding"], ensure_ascii=False),
                      json.dumps(out.get("provenance") or [], ensure_ascii=False), out.get("pages_read")))
    db.commit()
    return cur.lastrowid


# ---------------- contest whitelist from the school's own documents (S5) ----------------
WL = os.path.join(D.ROOT, "contest_whitelist.json")
_NAME_COL = re.compile(r"竞赛名称|赛事名称|竞赛项目名称|比赛名称|赛项名称|竞赛项目|竞赛\s*$|赛事\s*$")
_LEVEL_COL = re.compile(r"级别|等级|类别|档次|分类|层次")
_ORG_COL = re.compile(r"主办单位|主办方|组织单位|主办")


def whitelist_candidates(doc, url, file_name=None):
    """Rows of any table with a contest-NAME column (竞赛名称 / 赛事名称 / 竞赛项目 ...), as
    candidate whitelist entries with provenance (url, file, table+row, the row as read).
    A table without such a column yields nothing: a project list or a duty roster is not a
    whitelist, and guessing that it is would be inventing."""
    out = []
    for ti, rows in enumerate(doc.get("tables") or [], 1):
        hdr_i = next((i for i, r in enumerate(rows[:6]) if any(_NAME_COL.search(c or "") for c in r)), None)
        if hdr_i is None:
            continue
        hdr = [re.sub(r"\s+", "", c or "") for c in rows[hdr_i]]
        col = lambda rx: next((i for i, c in enumerate(hdr) if rx.search(c)), None)   # noqa: E731
        nc, lc, oc = col(_NAME_COL), col(_LEVEL_COL), col(_ORG_COL)
        for ri, row in enumerate(rows[hdr_i + 1:], hdr_i + 2):
            name = (row[nc] if nc < len(row) else "").strip()
            if not name or not re.search(r"[\u4e00-\u9fffA-Za-z]", name) \
                    or re.fullmatch(r"(?:竞赛|赛事|比赛|赛项)(?:项目)?(?:名称)?|名称", re.sub(r"\s+", "", name)):
                continue
            e = {"name": name, "aliases": [],
                 "source": {"url": url, "file": file_name, "loc": f"table {ti} row {ri}", "row": row}}
            if lc is not None and lc < len(row) and row[lc]:
                e["level"] = row[lc]
            if oc is not None and oc < len(row) and row[oc]:
                e["organiser"] = row[oc]
            out.append(e)
    return out


def _wl_load(path):
    if not os.path.exists(path):
        return {"version": 0, "entries": []}
    return json.load(open(path, encoding="utf-8"))


def _wl_sha(entries):
    return hashlib.sha256(json.dumps(sorted(e["name"] for e in entries), ensure_ascii=False).encode()).hexdigest()


def propose_whitelist(entries, source, path=None):
    """Store entries read from the school's document as a PROPOSED list, marked UNCONFIRMED. The
    list in use ("entries") is NOT touched: PLAN says a parsed whitelist is shown to the user once
    ("please confirm this is the current list") before it is trusted. Revision detection: the same
    names as the last proposal or the confirmed list -> 'unchanged', nothing rewritten.
    -> {"status": "proposed"|"unchanged"|"nothing found", "added": [...], "removed": [...]}"""
    path = path or WL
    if not entries:
        return {"status": "nothing found", "added": [], "removed": []}   # never an empty list
    wl = _wl_load(path)
    sha = _wl_sha(entries)
    if (wl.get("proposed") or {}).get("sha256") == sha or wl.get("confirmed_sha256") == sha:
        return {"status": "unchanged", "added": [], "removed": []}
    cur = {e["name"] for e in wl.get("entries", [])}
    new = {e["name"] for e in entries}
    diff = {"added": sorted(new - cur), "removed": sorted(cur - new)}
    wl["proposed"] = {"status": "UNCONFIRMED: read from the school's document; please confirm this is the "
                                "current list before it replaces the one in use",
                      "source": source, "found": time.strftime("%Y-%m-%d %H:%M"), "sha256": sha,
                      "entries": entries, **diff}
    json.dump(wl, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"status": "proposed", **diff}


def confirm_whitelist(path=None):
    """The user said yes: the proposed list becomes the list in use (version + 1). The previous
    list is kept under "history", so a wrong confirmation can be undone by hand."""
    path = path or WL
    wl = _wl_load(path)
    p = wl.pop("proposed", None)
    if not p:
        raise ValueError("no proposed whitelist to confirm")
    wl.setdefault("history", []).append({k: wl.get(k) for k in ("version", "source", "status", "loaded", "entries")})
    wl.update(version=int(wl.get("version") or 0) + 1, entries=p["entries"], source=p["source"],
              status=f"CONFIRMED by the user {time.strftime('%Y-%m-%d')}", loaded=time.strftime("%Y-%m-%d"),
              confirmed_sha256=p["sha256"])
    json.dump(wl, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return wl["version"]
