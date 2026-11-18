"""Institute scope (S6b): is a source about the tenant's own institute (college), or school-wide?

capture/scope.json (NEW file, hand-edited; capture/groups.json is never touched):
    {"institute_label": "<display name of your institute, optional>",
     "groups": {"<QQ group id>": "institute" | "school"},
     "sites":  {"<host or URL prefix>": "institute" | "school"}}
The shipped code names no school: the institute's name lives only in this data file (institute_label).
Anything not listed, or any other value, is "unknown": shown and labelled as unknown, never guessed.

Rules (PLAN item 6):
 - exams and changes count ONLY when the source is institute scope;
 - general events use the whole school (institute + school);
 - an unknown-scope source is shown with the label "scope unknown" and is not counted as an exam or a change.
Audience tags from tools/gather_activities.py (cache/site/activities.json, items[].audience.guess) are used for site
items only when scope.json says nothing about that site. The code reads a path argument so tests use fake ids."""
import json, os, re
from . import db as D

PATH = os.environ.get("CNB_SCOPE", os.path.join(D.ROOT, "capture", "scope.json"))
ACTIVITIES = os.path.join(D.ROOT, "cache", "site", "activities.json")
INSTITUTE, SCHOOL, OTHER, UNKNOWN = "institute", "school", "other", "unknown"
LABEL = {INSTITUTE: "Your institute", SCHOOL: "School-wide", OTHER: "Other college", UNKNOWN: "Scope unknown"}
_AUD = {"all": SCHOOL, "other college": OTHER}       # gather_activities audience guesses; the institute's own guess is matched by institute_label


def _norm(v):
    v = (v or "").strip().lower() if isinstance(v, str) else ""
    v = INSTITUTE if v == "college" else v
    return v if v in (INSTITUTE, SCHOOL) else UNKNOWN


def load(path=None):
    """-> {"groups": {str(id): scope}, "sites": {key: scope}, "label": str}. A missing, damaged or wrong-shape file means 'nothing known'."""
    try: c = json.load(open(path or PATH, encoding="utf-8"))
    except (OSError, ValueError): c = {}
    c = c if isinstance(c, dict) else {}
    lab = c.get("institute_label"); out = {"label": " ".join(lab.split())[:60] if isinstance(lab, str) else ""}
    for k in ("groups", "sites"):
        d = c.get(k) if isinstance(c.get(k), dict) else {}
        out[k] = {str(a).strip().lower() if k == "sites" else str(a).strip(): _norm(b) for a, b in d.items() if _norm(b) != UNKNOWN}
    return out


def label(scope, cfg=None):
    """Display label. The institute's own name (institute_label) when set, else 'Your institute'."""
    if scope == INSTITUTE: return (cfg if cfg is not None else load()).get("label") or LABEL[INSTITUTE]
    return LABEL.get(scope, LABEL[UNKNOWN])


def of_group(gid, cfg=None):
    return (cfg or load())["groups"].get(str(gid).strip(), UNKNOWN) if gid is not None else UNKNOWN


def of_url(url, cfg=None):
    """Longest matching key wins. A key is a URL prefix or a host; a host also matches its subdomains (never look-alikes)."""
    if not isinstance(url, str) or not url: return UNKNOWN
    u = url.strip().lower(); host = re.sub(r"^[a-z]+://", "", u).split("/")[0].split(":")[0]
    best, hit = -1, UNKNOWN
    for key, sc in (cfg or load())["sites"].items():
        k = re.sub(r"^[a-z]+://", "", key).rstrip("/")
        bare = re.sub(r"^[a-z]+://", "", u).split("?")[0].split("#")[0]
        ok = ((bare == k or bare.startswith(k + "/")) if "/" in k else host == k or host.endswith("." + k))      # a path key matches only at a path boundary
        if ok and len(k) > best: best, hit = len(k), sc
    return hit


def audience_tags(path=None, cfg=None):
    """url -> scope from the gather_activities output, if present. A guess equal to institute_label is the institute;
    'all' is school-wide; 'unknown' (or a name we cannot match) is not a tag and stays unknown."""
    lab = ((cfg if cfg is not None else load()).get("label") or "").lower()
    try: j = json.load(open(path or ACTIVITIES, encoding="utf-8"))
    except (OSError, ValueError): return {}
    out = {}
    for it in (j.get("items") if isinstance(j, dict) and isinstance(j.get("items"), list) else []):
        if not isinstance(it, dict) or not isinstance(it.get("url"), str): continue
        a = it.get("audience"); gs = str((a.get("guess") if isinstance(a, dict) else "") or "").strip().lower(); g = INSTITUTE if lab and gs == lab else _AUD.get(gs)
        if g: out[it["url"]] = g
    return out


def _extra_url(it):
    try: x = json.loads(it["extra"] or "null")
    except (ValueError, TypeError, KeyError, IndexError): return None
    if isinstance(x, dict):
        for k in ("url", "source_url", "site_url"):
            if isinstance(x.get(k), str): return x[k]
    return None


def of_item(db, it, cfg=None, tags=None):
    """Scope of an item: its own group, then its other source groups, then its site URL (config, then audience tag)."""
    cfg = cfg or load()
    s = of_group(it["group_id"], cfg)
    if s != UNKNOWN: return s
    try:
        for r in db.execute("SELECT DISTINCT m.group_id FROM item_sources x JOIN msg m ON m.id=x.msg_id WHERE x.item_id=?", (it["id"],)):
            s = of_group(r[0], cfg)
            if s != UNKNOWN: return s
    except Exception: pass
    u = _extra_url(it)
    if u:
        s = of_url(u, cfg)
        if s != UNKNOWN: return s
        return (tags if tags is not None else audience_tags()).get(u, UNKNOWN)
    return UNKNOWN


def counts_as(kind, scope):
    """kind 'exam' or 'change' -> institute only. Anything else (a general event) -> institute or school-wide; an unknown or
    other-college scope is still SHOWN by the callers, only labelled."""
    if kind in ("exam", "change"): return scope == INSTITUTE
    return scope in (INSTITUTE, SCHOOL)


def filter_exams(db, rows, is_exam, cfg=None):
    """(kept, held_back): exam rows from institute sources, and exam rows held back for another/unknown scope (never dropped silently).
    `is_exam(row)` is supplied by the caller (weekly.is_exam bound to its word list)."""
    cfg = cfg or load(); tags = audience_tags(); kept, held = [], []
    for r in rows:
        if not is_exam(r): continue
        (kept if counts_as("exam", of_item(db, r, cfg, tags)) else held).append(r)
    return kept, held
