"""Whitelist versioning + provenance (S2b). Pure stdlib, no network, no LLM.

A whitelist is DATA with a version id, a validity window, provenance (where it came from) and a
confirmed-by-user flag. PLAN rule: a parsed list is shown to the user once ("please confirm this is the
current list") and is NOT trusted until they do. This module enforces that in code:

  trust_state(wl, today) -> (trusted, reason)   trusted only if confirmed AND inside valid-from/to
  classify(extra, quote, wl, today)             wraps contest.match; every contest gets a bucket, none dropped:
       'listed'             hit on a trusted list                      recognition 'yes'
       'listed_unconfirmed' hit on an unconfirmed/expired list         recognition 'unknown' (never trusted)
       'discovered'         no hit (or no usable list)                 recognition 'no' (trusted list) | 'unknown'
  diff(old, new)                                added / removed / changed entries between two versions

Missing facts stay None: nothing is guessed (provenance fields the source did not give are null)."""
import copy, datetime, json, os, re

PROV_KEYS = ("source_url", "publish_date", "file_name", "page_row")


def _d(x):
    """ISO date/datetime string or date -> date, else None (unparseable is treated as unknown, not as 'always')."""
    if isinstance(x, datetime.datetime): return x.date()
    if isinstance(x, datetime.date): return x
    if isinstance(x, str) and x.strip():
        try: return datetime.date.fromisoformat(x.strip()[:10])
        except ValueError: return None
    return None


def _key(name): return re.sub(r"[\W_]+", "", (name or "").lower())


def normalize(raw):
    """Raw dict (new format or the legacy contest_whitelist.json) -> versioned whitelist dict.
    Legacy files have no provenance and no confirmation, so they come out UNCONFIRMED.
    Entries without a usable name cannot be matched: they are moved to `problems`, listed, not silently lost."""
    raw = raw if isinstance(raw, dict) else {}       # checker fix: a non-dict file used to crash
    prov_in = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    prov = {k: prov_in.get(k) for k in PROV_KEYS}
    if prov_in.get("note") is not None:
        prov["note"] = prov_in["note"]               # checker fix: note survived only the first normalize, lost on reload
    elif not prov["source_url"] and raw.get("source") and not prov_in:
        prov["note"] = str(raw["source"])          # legacy free-text source, kept verbatim
    entries, problems, warnings = [], [], []
    seen = {}
    for i, e in enumerate(raw.get("entries") or []):
        if isinstance(e, dict) and isinstance(e.get("name"), str) and _key(e["name"]):
            e = dict(e); al = e.get("aliases") or []
            if isinstance(al, str): al = [al]        # checker fix: "MCM" used to become ["M","C","M"]
            elif not isinstance(al, list):
                problems.append({"index": i, "entry": e, "why": "aliases is not a list; ignored"}); al = []
            e["aliases"] = [a for a in al if isinstance(a, str) and a.strip()]
            for n in [e["name"], *e["aliases"]]:     # checker: flag (not drop) names/aliases shared by entries
                k = _key(n); j = seen.setdefault(k, e["name"])
                if j != e["name"]: warnings.append({"key": n, "entries": [j, e["name"]], "why": "shared name/alias: first entry wins on a tie"})
                elif n is e["name"] and any(x["name"] == e["name"] for x in entries):
                    warnings.append({"key": n, "entries": [n, n], "why": "duplicate entry name"})
            entries.append(e)
        else:
            problems.append({"index": i, "entry": e, "why": "no usable name"})
    return {
        "version_id": str(raw.get("version_id") or raw.get("version") or "unversioned"),
        "valid_from": raw.get("valid_from"), "valid_to": raw.get("valid_to"),
        "provenance": prov,
        "confirmed_by_user": raw.get("confirmed_by_user") is True,   # only a literal True counts
        "confirmed_at": raw.get("confirmed_at"),
        "scope": raw.get("scope"),
        "entries": entries, "problems": problems, "warnings": warnings,
    }


def load(path):
    """Load a whitelist file. Missing/unreadable file -> empty UNCONFIRMED list (recognition stays 'unknown')."""
    try:
        with open(path, encoding="utf-8") as f: return normalize(json.load(f))
    except (OSError, ValueError):
        w = normalize({}); w["problems"].append({"index": None, "entry": None, "why": f"cannot read {path}"}); return w


def trust_state(wl, today=None):
    """-> (trusted: bool, reason). Unconfirmed lists are never trusted, whatever their dates say."""
    today = _d(today) or datetime.date.today()
    if not isinstance(wl, dict) or not wl.get("entries"): return False, "empty list"
    # checker fix: was `not wl.get(...)`, so a raw dict with "false"/"yes"/1 counted as confirmed
    if wl.get("confirmed_by_user") is not True: return False, "not confirmed by the user"
    vf, vt = wl.get("valid_from"), wl.get("valid_to")
    if vf and not _d(vf): return False, "valid_from is not a date"
    if vt and not _d(vt): return False, "valid_to is not a date"
    if vf and today < _d(vf): return False, "not valid yet"
    if vt and today > _d(vt): return False, "expired"
    return True, "confirmed and in validity window"


def confirm(wl, when=None):
    """Return a COPY marked confirmed. Call only from a user action (the 'please confirm' step), never from a parser."""
    w = copy.deepcopy(wl); w["confirmed_by_user"] = True
    w["confirmed_at"] = str(_d(when) or datetime.date.today()); return w


def diff(old, new):
    """Entry-level difference old -> new, matched on normalised name. -> {added, removed, changed, summary}.
    `changed` lists the fields that differ (aliases compared as sets, order ignored)."""
    om = {_key(e["name"]): e for e in (old or {}).get("entries", [])}
    nm = {_key(e["name"]): e for e in (new or {}).get("entries", [])}
    added = [nm[k] for k in nm if k not in om]
    removed = [om[k] for k in om if k not in nm]
    changed = []
    for k in nm.keys() & om.keys():
        a, b, fields = om[k], nm[k], {}
        for f in (set(a) | set(b)) - {"no"}:
            va, vb = a.get(f), b.get(f)
            if f == "aliases": va, vb = set(va or []), set(vb or [])
            if va != vb: fields[f] = {"old": a.get(f), "new": b.get(f)}
        if fields: changed.append({"name": nm[k]["name"], "fields": fields})
    return {"added": added, "removed": removed, "changed": changed,
            "summary": f"+{len(added)} -{len(removed)} ~{len(changed)}"}


# ---- version store: one JSON file per version in a directory (e.g. whitelist_versions/) ----
def _safe(vid): return re.sub(r"[^\w.\-]", "_", str(vid))


def save_version(store_dir, wl):
    """Write a version. Refuses to overwrite an existing version id with DIFFERENT content (versions are immutable)."""
    os.makedirs(store_dir, exist_ok=True)
    p = os.path.join(store_dir, _safe(wl["version_id"]) + ".json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            a, b = normalize(json.load(f)), normalize(wl)
        if a == b: return p
        # checker fix: the user's confirm() of a stored version must be recordable under the SAME id
        # (only unconfirmed -> confirmed + confirmed_at may change; any content change still refuses)
        strip = lambda w: {k: v for k, v in w.items() if k not in ("confirmed_by_user", "confirmed_at")}
        if not (strip(a) == strip(b) and not a["confirmed_by_user"] and b["confirmed_by_user"]):
            raise ValueError(f"version {wl['version_id']} already exists with different content; use a new version_id")
    with open(p, "w", encoding="utf-8") as f: json.dump(wl, f, ensure_ascii=False, indent=1)
    return p


def versions(store_dir):
    """All stored versions, oldest first by (valid_from, version_id); unparseable files are skipped."""
    out = []
    for fn in sorted(os.listdir(store_dir)) if os.path.isdir(store_dir) else []:
        if fn.endswith(".json"):
            w = load(os.path.join(store_dir, fn))
            if w["entries"] or w["version_id"] != "unversioned": out.append(w)
    return sorted(out, key=lambda w: (str(_d(w["valid_from"]) or ""), w["version_id"]))


def current(store_dir, today=None):
    """Newest TRUSTED version (confirmed + in window). None if there is none: unconfirmed lists are never returned."""
    ok = [w for w in versions(store_dir) if trust_state(w, today)[0]]
    return ok[-1] if ok else None


def pending_confirmation(store_dir):
    """Versions waiting for the user's 'please confirm this is the current list', each with its diff to the current trusted one."""
    cur = current(store_dir)
    return [{"version": w, "diff": diff(cur, w)} for w in versions(store_dir) if not w["confirmed_by_user"]]


def classify(extra, quote, wl, today=None):
    """-> {bucket, recognition, entry, trusted, trust_reason, version_id, provenance}. Always returns a bucket: nothing dropped.
    Uses contest.match unchanged, then withholds 'yes' unless the list is trusted."""
    from . import contest as C
    wl = wl if isinstance(wl, dict) else {}                      # checker fix: None used to crash
    trusted, why = trust_state(wl, today)
    # checker fix: a non-string name/quote (bad LLM JSON) crashed contest.match, i.e. the contest was dropped
    extra = dict(extra) if isinstance(extra, dict) else {}
    if not isinstance(extra.get("name"), str): extra["name"] = "" if extra.get("name") is None else str(extra["name"])
    quote = quote if isinstance(quote, str) else ("" if quote is None else str(quote))
    _rec, ent = C.match(extra, quote, {"entries": wl.get("entries", [])})
    if ent and trusted: bucket, rec = "listed", "yes"
    elif ent: bucket, rec = "listed_unconfirmed", "unknown"
    else: bucket, rec = "discovered", ("no" if trusted else "unknown")
    return {"bucket": bucket, "recognition": rec, "entry": ent if ent else None, "trusted": trusted,
            "trust_reason": why, "version_id": wl.get("version_id"), "provenance": wl.get("provenance")}
