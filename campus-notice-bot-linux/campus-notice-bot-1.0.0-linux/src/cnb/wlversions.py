"""Whitelist VERSION FOLDER + confirm step (S2b round 2). Pure stdlib, no network, no LLM.

Layout (default root `data/whitelist_versions`), one folder per school:
    <root>/<school>/<YYYYMMDDTHHMMSSZ>-<sha8>.json   one immutable file per distinct list content, never rewritten/deleted
    <root>/<school>/decisions.jsonl                  append-only log of the user's confirm / reject actions
    <root>/<school>/current.json                     POINTER to the confirmed version (a file, not a symlink: NTFS/WSL safe)
A version file = {"record": {school, sha256, sha8, source_url, fetched_at, stored_at, source_version_id}, "whitelist": {...}}.
sha256 is over the canonical list content (entries, valid_from, valid_to, scope), not provenance, so re-fetching an
identical list is a duplicate (no new version) and any revision gets a NEW hash and a NEW UNCONFIRMED version.

Trust rules (PLAN: nothing is trusted before the user confirms):
  * ingest() always stores UNCONFIRMED, whatever the input says, and never touches `current.json`.
  * Only confirm()/confirm_flow() (a user action) writes a 'confirm' decision and moves the pointer.
  * Trust state is derived from the decisions log (latest decision per version wins), not from a flag in the version file.
  * current_whitelist() returns None unless pointer, decision and content hash all agree (tamper/corruption -> unknown, no crash).
"""
import datetime, hashlib, html, json, os, re

from . import whitelist as W

DEFAULT_ROOT = os.path.join("data", "whitelist_versions")
_STEM = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_AFFIRM = {"y", "yes", "confirm"}
_DENY = {"n", "no", "reject"}


def _slug(school):
    s = re.sub(r"[^\w\-]+", "_", str(school or "").strip()).strip("_.")
    return s or "unknown_school"


def _dir(root, school): return os.path.join(root or DEFAULT_ROOT, _slug(school))


def _now(now):
    if isinstance(now, datetime.datetime): return now
    return datetime.datetime.now(datetime.timezone.utc)


def content_hash(wl):
    """sha256 of the canonical list content. Provenance, version id and confirm flags are deliberately excluded."""
    body = {"entries": wl.get("entries") or [], "valid_from": wl.get("valid_from"),
            "valid_to": wl.get("valid_to"), "scope": wl.get("scope")}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: f.write(text)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return None


def _stems(d):
    return sorted(fn[:-5] for fn in (os.listdir(d) if os.path.isdir(d) else []) if fn.endswith(".json") and _STEM.match(fn[:-5]))


def _decisions(d):
    out = []
    try:
        with open(os.path.join(d, "decisions.jsonl"), encoding="utf-8", errors="replace") as f:   # checker fix: bad bytes crashed
            for line in f:
                try: x = json.loads(line)
                except ValueError: continue
                if isinstance(x, dict): out.append(x)
    except OSError: pass
    return out


def _state(d, stem):
    """'confirmed' | 'rejected' | 'unconfirmed' from the LAST decision for this version (a later action overrides)."""
    st = "unconfirmed"
    for x in _decisions(d):
        if x.get("version") == stem and x.get("action") in ("confirm", "reject"):
            st = "confirmed" if x["action"] == "confirm" else "rejected"
    return st


def _load(d, stem):
    """-> (wl with confirmed overlay, record, state) or None. Hash mismatch (file edited by hand) -> None."""
    raw = _read(os.path.join(d, stem + ".json"))
    if not isinstance(raw, dict) or not isinstance(raw.get("whitelist"), dict) or not isinstance(raw.get("record"), dict): return None
    wl = W.normalize(raw["whitelist"]); rec = raw["record"]
    h = content_hash(wl)   # checker fix: sha8 was only checked as a suffix, so sha8="" let an edited file + pointer pass
    if rec.get("sha256") != h or rec.get("sha8") != h[:8] or not stem.endswith("-" + h[:8]): return None
    state = _state(d, stem)
    wl["version_id"] = stem
    wl["confirmed_by_user"] = state == "confirmed"
    if state == "confirmed":
        wl["confirmed_at"] = next((x.get("at") for x in reversed(_decisions(d)) if x.get("version") == stem and x.get("action") == "confirm"), None)
    else: wl["confirmed_at"] = None
    return wl, rec, state


def resolve(root, school, ref):
    """Version reference -> stem. Accepts the full stem or a unique sha8 / sha prefix (>=6 chars). Never builds paths from free text."""
    ref = str(ref or "").strip(); d = _dir(root, school); stems = _stems(d)
    if ref in stems: return ref
    if re.fullmatch(r"[0-9a-f]{6,64}", ref):
        hit = [s for s in stems if s.split("-", 1)[1].startswith(ref[:8]) and (len(ref) <= 8 or (_load(d, s) or (0, {}))[1].get("sha256", "").startswith(ref))]
        if len(hit) == 1: return hit[0]
    return None


def ingest(root, school, raw_wl, source_url=None, fetched_at=None, now=None):
    """Store a parsed list as a NEW UNCONFIRMED version. -> {status, version, sha256, path, state}.
    status 'new' (created), 'duplicate' (same content already stored, in any state: nothing written).
    Never confirms and never moves `current.json`: a revised list cannot silently replace the confirmed one."""
    wl = W.normalize(raw_wl)
    wl["confirmed_by_user"] = False; wl["confirmed_at"] = None        # a parser can never self-confirm
    if source_url: wl["provenance"]["source_url"] = str(source_url)   # the URL actually fetched wins
    h = content_hash(wl); d = _dir(root, school)
    os.makedirs(d, exist_ok=True)
    for s in _stems(d):
        if s.endswith("-" + h[:8]):
            ld = _load(d, s)
            if ld and ld[1]["sha256"] == h:
                return {"status": "duplicate", "version": s, "sha256": h, "path": os.path.join(d, s + ".json"), "state": ld[2]}
    t = _now(now); stem = t.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + h[:8]
    rec = {"school": str(school), "sha256": h, "sha8": h[:8], "source_url": source_url or wl["provenance"].get("source_url"),
           "fetched_at": str(fetched_at) if fetched_at else None, "stored_at": t.isoformat(), "source_version_id": wl["version_id"]}
    p = os.path.join(d, stem + ".json")
    if os.path.exists(p): return {"status": "duplicate", "version": stem, "sha256": h, "path": p, "state": _state(d, stem)}
    _write_atomic(p, json.dumps({"record": rec, "whitelist": wl}, ensure_ascii=False, indent=1))
    return {"status": "new", "version": stem, "sha256": h, "path": p, "state": "unconfirmed"}


def list_versions(root, school):
    """All versions, oldest first: [{version, state, sha256, source_url, fetched_at, entries, is_current}]. Unreadable files are skipped."""
    d = _dir(root, school); cur = (_read(os.path.join(d, "current.json")) or {}).get("version"); out = []
    for s in _stems(d):
        ld = _load(d, s)
        if ld: out.append({"version": s, "state": ld[2], "sha256": ld[1]["sha256"], "source_url": ld[1].get("source_url"),
                           "fetched_at": ld[1].get("fetched_at"), "entries": len(ld[0]["entries"]), "is_current": s == cur and ld[2] == "confirmed"})
    return out


def pending(root, school):
    return [v for v in list_versions(root, school) if v["state"] == "unconfirmed"]


def current_whitelist(root, school):
    """The confirmed whitelist the pointer names (with trust overlay), or None. Feed it to whitelist.classify(); it still checks dates."""
    d = _dir(root, school); p = _read(os.path.join(d, "current.json"))
    if not isinstance(p, dict) or not _STEM.match(str(p.get("version"))): return None
    ld = _load(d, p["version"])
    if not ld or ld[2] != "confirmed" or ld[1]["sha256"] != p.get("sha256"): return None
    return ld[0]


def get_version(root, school, ref):
    s = resolve(root, school, ref)
    ld = _load(_dir(root, school), s) if s else None
    return ld[0] if ld else None


def _prov(entry, wl):
    """Where an entry came from: its own provenance fields if it has them, else the version's. Unknown stays None."""
    base = wl.get("provenance") or {}
    return {k: (entry.get(k) if entry.get(k) is not None else base.get(k)) for k in W.PROV_KEYS}


def review(root, school, ref):
    """Everything the confirm screen shows: the unconfirmed version, the diff against the confirmed one, provenance per changed entry."""
    s = resolve(root, school, ref); d = _dir(root, school)
    ld = _load(d, s) if s else None
    if not ld: return None
    new, rec, state = ld; cur = current_whitelist(root, school)
    df = W.diff(cur, new)
    nm = {W._key(e["name"]): e for e in new["entries"]}
    om = {W._key(e["name"]): e for e in (cur or {}).get("entries", [])}
    return {"version": s, "state": state, "record": rec, "whitelist": new, "against": cur["version_id"] if cur else None,
            "diff": df, "first_list": cur is None,
            "added": [{"entry": e, "provenance": _prov(e, new)} for e in df["added"]],
            "removed": [{"entry": e, "provenance": _prov(e, cur)} for e in df["removed"]],
            "changed": [dict(c, provenance=_prov(nm[W._key(c["name"])], new), old_provenance=_prov(om[W._key(c["name"])], cur)) for c in df["changed"]]}


def render_text(rv):
    n = rv["whitelist"]; rec = rv["record"]
    L = [f"WHITELIST VERSION {rv['version']}  [{rv['state'].upper()}: not trusted until you confirm]",
         f"school: {rec['school']}   entries: {len(n['entries'])}   hash: {rec['sha256'][:16]}",
         f"source: {rec.get('source_url') or 'unknown'}   fetched: {rec.get('fetched_at') or 'unknown'}   valid: {n.get('valid_from') or '?'} to {n.get('valid_to') or '?'}"]
    if not n.get("valid_to"): L.append("WARNING: no valid_to, so a confirmed list would stay trusted forever.")
    if rv["first_list"]: L.append("No confirmed list exists yet: everything below is new. Nothing has been trusted before.")
    else: L.append(f"Compared with confirmed {rv['against']}: {rv['diff']['summary']}")
    fmt = lambda p: ", ".join(f"{k}={v}" for k, v in p.items() if v is not None) or "provenance unknown"
    for e in rv["added"]: L.append(f"  + {e['entry']['name']}  ({fmt(e['provenance'])})")
    for e in rv["removed"]: L.append(f"  - {e['entry']['name']}  (was: {fmt(e['provenance'])})")
    for c in rv["changed"]:
        L.append(f"  ~ {c['name']}  ({fmt(c['provenance'])})")
        for f, v in c["fields"].items(): L.append(f"      {f}: {v['old']!r} -> {v['new']!r}")
    for p in n["problems"]: L.append(f"  ! problem: {p['why']} (index {p['index']})")
    for w in n["warnings"]: L.append(f"  ! warning: {w['why']}: {w['key']}")
    return "\n".join(_CTRL.sub("�", x) for x in L)   # checker fix: newlines/ANSI/bidi in parsed names could spoof the screen


_CTRL = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def render_html(rv):
    """Static, read-only page (no scripts, no forms): the confirm action stays in the text UI/CLI."""
    e = html.escape
    rows = "".join(f"<tr><td>{k}</td><td>{e(str(x['entry']['name']))}</td><td>{e(str(x['provenance']))}</td></tr>"
                   for k, lst in (("added", rv["added"]), ("removed", rv["removed"])) for x in lst)
    rows += "".join(f"<tr><td>changed</td><td>{e(c['name'])}</td><td>{e(json.dumps(c['fields'], ensure_ascii=False))}</td></tr>" for c in rv["changed"])
    return ("<!doctype html><meta charset=utf-8><title>Whitelist version</title><pre>" + e(render_text(rv)) +
            "</pre><table border=1><tr><th>change</th><th>entry</th><th>provenance / fields</th></tr>" + rows + "</table>")


def _decide(root, school, stem, action, now):
    d = _dir(root, school); t = _now(now).isoformat(); p = os.path.join(d, "decisions.jsonl")
    lead = ""                                   # checker fix: a truncated last line used to swallow this decision
    if os.path.exists(p) and os.path.getsize(p):
        with open(p, "rb") as f: f.seek(-1, 2); lead = "" if f.read(1) == b"\n" else "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(lead + json.dumps({"at": t, "version": stem, "action": action, "by": "user"}, ensure_ascii=False) + "\n")
    return t


def confirm(root, school, ref, now=None):
    """USER ACTION: trust this version and point `current.json` at it. Refuses an empty/unreadable version."""
    s = resolve(root, school, ref); d = _dir(root, school); ld = _load(d, s) if s else None
    if not ld: raise ValueError(f"unknown or unreadable version {ref!r}")
    if not ld[0]["entries"]: raise ValueError("refusing to confirm an empty list")
    t = _decide(root, school, s, "confirm", now)
    _write_atomic(os.path.join(d, "current.json"), json.dumps({"version": s, "sha256": ld[1]["sha256"], "confirmed_at": t}, indent=1))
    return s


def reject(root, school, ref, now=None):
    """USER ACTION: mark rejected (file kept). If it was the current version the pointer is removed (list becomes unknown, not stale-trusted)."""
    s = resolve(root, school, ref); d = _dir(root, school)
    if not s or not _load(d, s): raise ValueError(f"unknown or unreadable version {ref!r}")
    _decide(root, school, s, "reject", now)
    p = os.path.join(d, "current.json")
    if (_read(p) or {}).get("version") == s: os.remove(p)
    return s


def confirm_flow(root, school, ref, ask=input, now=None, out=print):
    """Text confirm screen. `ask(prompt) -> str` is injectable. Only an exact yes/confirm confirms and only no/reject rejects;
    anything else (blank, EOF, typo) leaves the version UNCONFIRMED. -> {action: confirmed|rejected|pending, version}."""
    rv = review(root, school, ref)
    if not rv: raise ValueError(f"unknown or unreadable version {ref!r}")
    out(render_text(rv))
    if not rv["whitelist"]["entries"]:
        out("Empty list: cannot be confirmed."); return {"action": "pending", "version": rv["version"]}
    try: ans = ask("Is this the current official list? [yes = confirm / no = reject / else leave pending]: ")
    except (EOFError, KeyboardInterrupt): ans = ""
    a = str(ans).strip().lower()
    if a in _AFFIRM: return {"action": "confirmed", "version": confirm(root, school, rv["version"], now)}
    if a in _DENY: return {"action": "rejected", "version": reject(root, school, rv["version"], now)}
    return {"action": "pending", "version": rv["version"]}


def main(argv):
    """python -m cnb.wlversions <school> list | show <ver> | confirm <ver> | reject <ver> [--root DIR]"""
    a = list(argv); root = DEFAULT_ROOT
    if "--root" in a: i = a.index("--root"); root = a[i + 1]; del a[i:i + 2]
    if len(a) < 2: print(main.__doc__); return 2
    school, cmd = a[0], a[1]
    if cmd == "list":
        for v in list_versions(root, school): print(v["version"], v["state"], "CURRENT" if v["is_current"] else "", v["entries"], "entries")
        return 0
    if len(a) < 3: print(main.__doc__); return 2
    try:
        if cmd == "show": rv = review(root, school, a[2]); print(render_text(rv) if rv else "unknown version")
        elif cmd == "confirm": print(confirm_flow(root, school, a[2]))
        elif cmd == "reject": print("rejected", reject(root, school, a[2]))
        else: print(main.__doc__); return 2
    except ValueError as e: print("error:", e); return 1
    return 0


if __name__ == "__main__":
    import sys; raise SystemExit(main(sys.argv[1:]))
