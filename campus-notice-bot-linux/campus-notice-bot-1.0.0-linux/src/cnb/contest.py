"""Contest module (PLAN core function B). Three separate questions, never collapsed into one score:
 1 RECOGNITION: on the school's whitelist? yes | no | unknown (unknown when no whitelist is loaded yet)
 2 FIT: from the ability profile (profile.py) -> explainable band + reasons + gaps, and an A-D grade
   derived from them (S2d); 'not assessed' / no grade when nothing is recorded yet
 3 ACTION: deadline, team size, fee, from the message.
Unlisted/new contests are NEVER dropped and never auto-trusted: they land in 'discovered' with legitimacy hints."""
import json, os, re
from . import db as D
WL = os.path.join(D.ROOT, "contest_whitelist.json")
CERT_MILL = re.compile(r"证书|包过|保过|付费|缴费|报名费|认证|含金量高|代报|assured|guarantee|certificate", re.I)

def load_whitelist():
    if not os.path.exists(WL): return {"entries": []}
    return json.load(open(WL, encoding="utf-8"))

def norm(x): return re.sub(r"[\W_]+", "", (x or "").lower())
def _cjk(k): return sum(1 for c in k if "一" <= c <= "鿿")

def usable(k, strict):
    """Is this name/alias specific enough to match on?
    A flat 'at least 4 characters' rule (used until 2026-11-10) threw away most short Chinese
    aliases: 蓝桥杯, 挑战杯, 美赛, MCM, 国才杯 are 2-3 characters and are exactly how people
    write them. Chinese is denser, so count scripts separately, and ask for more evidence when
    scanning free text (strict) than when checking the extractor's own `name` field, where a
    short alias is the whole value rather than a fragment that might appear by chance."""
    c = _cjk(k)
    if c: return c >= (3 if strict else 2)
    return len(k) >= (4 if strict else 3)

def match(extra, quote, wl=None):
    """-> (recognition, entry|None). Longest name/alias hit wins (so the full name of one contest beats a shared short alias of another).
    The extracted contest name is checked first; the message text only if the name gives no hit. LLM-free."""
    wl = wl or load_whitelist(); ents = wl.get("entries", [])
    if not ents: return "unknown", None
    for hay, strict in ((norm((extra or {}).get("name", "")), False), (norm(quote), True)):
        if not hay: continue
        best = (0, None)
        for e in ents:
            for n in [e["name"], *e.get("aliases", [])]:
                k = norm(n)
                if usable(k, strict) and k in hay and len(k) > best[0]: best = (len(k), e)
        if best[1]: return "yes", best[1]
    return "no", None

def hints(extra, quote, recognition):
    extra = extra or {}; h = []
    if not extra.get("organiser"): h.append("organiser not stated")
    if extra.get("fee"): h.append(f"fee: {extra['fee']}")
    if CERT_MILL.search(quote or ""): h.append("wording typical of pay-for-certificate offers: check before paying")
    if recognition != "yes": h.append("not confirmed on the school's list: check before investing time")
    return h

# What a contest asks for, read from its own text. Keys match profile_facts keys (profile.py).
NEEDS = [
    ("python", r"python"),
    ("cpp", r"\bc\+\+|\bc语言|cpp"),
    ("algorithms", r"算法|数据结构|程序设计|acm|icpc|编程|algorithm|programming contest"),
    ("verilog", r"verilog|vhdl|fpga|可编程逻辑|数字电路|硬件描述"),
    ("electronics", r"电子设计|电路|电赛|嵌入式|单片机|硬件|circuit|electronic"),
    ("maths_modelling", r"数学建模|建模|美赛|mcm|icm|cumcm|modelling|modeling"),
    ("english_writing", r"英文|英语|全英|international|english|\bpaper\b|论文"),
]
BANDS = ["poor", "stretch", "possible", "good"]


def needs_of(extra, quote):
    """Which abilities this contest appears to ask for. Read from the message's own words, so a
    missing skill can always be pointed at a phrase the student can check."""
    hay = ((extra or {}).get("name") or "") + " " + (quote or "")
    return [k for k, pat in NEEDS if re.search(pat, hay, re.I)]


def fit(extra, quote, prof_levels=None, hours=None, has_team=None):
    """-> {band, reasons[], gaps[], assessed}. Explainable by construction (PLAN): the student is
    told WHICH ability each judgement rests on and which one is missing, never just a number.
    Advisory only: the bot may rank and explain, it never registers or commits on anyone's behalf.

    Deliberately NOT a weighted score. A score hides the one thing the student needs to know -
    what exactly is missing - and invites trust the evidence cannot support, since every level
    here came from a conversation, not a certificate."""
    if not prof_levels:
        return {"band": None, "reasons": [], "gaps": [], "assessed": False,
                "note": "fit not assessed: no ability profile yet (run: cnb interview)"}
    need = needs_of(extra, quote)
    have, missing, weak, declared_none = [], [], [], []
    for k in need:
        lvl, status, _conf = prof_levels.get(k, (None, None, None))
        if lvl in ("intermediate", "advanced"):
            have.append((k, lvl, status))
        elif lvl == "beginner":
            weak.append((k, lvl, status))
        elif lvl == "none":
            declared_none.append(k)          # the student told us they have none: say that,
        else:                                # which is different from us simply not knowing
            missing.append(k)
    reasons = [f"needs {k}: you are {lvl}" + (" (self-reported, not yet backed up)" if status == "claimed" else "")
               for k, lvl, status in have]
    gaps = [f"needs {k}: you told us you have none" for k in declared_none]
    gaps += [f"needs {k}: nothing recorded for you yet, so this may be wrong" for k in missing]
    gaps += [f"needs {k}: you are only beginner" for k, _l, _s in weak]

    practical = []
    team = (extra or {}).get("team_size")
    if team and re.search(r"[2-9]|\d{2,}|team|组队|人", str(team)) and has_team == "no":
        gaps.append(f"needs a team of {team}: you have no team yet")
        practical.append("team")
    elif team and has_team == "yes":
        reasons.append(f"needs a team of {team}: you already have one")
    if hours:
        try:
            if int(re.sub(r"\D", "", str(hours)) or 0) < 5:
                gaps.append(f"you have about {hours} hours a week: most contests need more")
                practical.append("hours")
        except ValueError:
            pass
    if (extra or {}).get("fee"):
        gaps.append(f"there is a fee of {extra['fee']}: your decision, not an ability gap")

    if not need:
        band = "possible"
        reasons.append("the message does not say which abilities are needed; nothing rules you out")
    elif not missing and not weak and not declared_none:
        band = "good"
    elif have and (len(missing) + len(declared_none)) <= 1:
        band = "possible"
    elif have:
        band = "stretch"
    else:
        band = "poor"
    return {"band": band, "reasons": reasons, "gaps": gaps, "assessed": True,
            "note": "advisory: levels come from your own answers, so this estimates fit, it does not certify it",
            # the parts the grade is derived from, so grade() never has to re-parse sentences
            "detail": {"needs": need, "have": [k for k, _l, _s in have],
                       "have_claimed_only": bool(have) and all(s == "claimed" for _k, _l, s in have),
                       "have_claimed": [k for k, _l, s in have if s == "claimed"],
                       "weak": [k for k, _l, _s in weak], "missing": missing, "none": declared_none,
                       "practical": practical}}


# ---------------- compatibility grade (S2d) ----------------
GRADE_MEANING = {"A": "looks like a good fit", "B": "probably workable, with something to sort out",
                 "C": "a stretch, or too little is known", "D": "a poor fit for now"}
NO_PROFILE = "no grade: nothing recorded yet about your abilities (tell the bot in chat, or run: cnb interview)"
DISCLAIMER = "an estimate from what you told the bot, not a certification"


def grade(f):
    """A/B/C/D, derived deterministically from fit()'s band and gaps. Never shown alone: the result
    always carries the reasons, the missing abilities and the disclaimer, and grade_text() renders all of them.

      A  band good, nothing practical missing (team, hours), and NO needed ability resting on an unconfirmed claim
      B  band good but a practical gap or any needed ability only self-claimed; or band possible with no practical gap
      C  band possible with a practical gap; band stretch; or the notice does not say what it needs
      D  band poor because of abilities you told us you lack or have only at beginner level
         (poor merely because nothing is recorded is C: unknown is not the same as bad)
    A fee is the student's decision, not a gap, so it never moves the grade."""
    if not f or not f.get("assessed"):
        return {"grade": None, "meaning": None, "band": None, "reasons": [], "gaps": [], "missing": [],
                "assessed": False, "note": NO_PROFILE}
    d = f.get("detail") or {}
    band, why = f["band"], []
    if not d.get("needs"):
        g = "C"
        why.append("capped at C: the notice does not say which abilities it needs")
    elif band == "good":
        if d.get("practical"):
            g = "B"
            why.append("not A: " + " and ".join("no team yet" if p == "team" else "few hours a week"
                                                 for p in d["practical"]))
        elif d.get("have_claimed_only"):
            g = "B"
            why.append("not A: rests only on things you said that are not yet backed up or confirmed")
        elif d.get("have_claimed"):
            # checker fix (S2d): ONE backed-up ability used to carry any number of bare claims to an A
            g = "B"
            why.append("not A: " + ", ".join(d["have_claimed"]) + " is only what you said, not yet backed up or confirmed")
        else:
            g = "A"
    elif band == "possible":
        g = "C" if d.get("practical") else "B"
    elif band == "stretch":
        g = "C"
    elif not d.get("none") and not d.get("weak"):
        g = "C"                               # poor only because NOTHING is recorded: unknown, not bad
        why.append("not D: nothing is recorded about what it needs, so this is unknown rather than poor")
    else:
        g = "D"
    missing = d.get("none", []) + d.get("missing", []) + d.get("weak", [])
    return {"grade": g, "meaning": GRADE_MEANING[g], "band": band, "reasons": f["reasons"] + why,
            "gaps": f["gaps"], "missing": missing, "assessed": True, "note": DISCLAIMER}


def grade_text(g):
    if not g or not g.get("assessed"):
        return (g or {}).get("note") or NO_PROFILE
    out = f"grade {g['grade']} ({g['meaning']}; {g['note']})"
    if g["reasons"]:
        out += " - why: " + "; ".join(g["reasons"])
    out += " - missing: " + (", ".join(g["missing"]) if g["missing"] else "nothing the notice names")
    if g["gaps"]:
        out += " - gaps: " + "; ".join(g["gaps"])
    return out


def grade_for(db, extra, quote):
    """Convenience: read this tenant's profile and grade one contest. No profile -> no grade."""
    from . import profile as P
    f = fit(extra, quote, P.levels(db), P.value_of(db, "hours_per_week"), P.value_of(db, "has_team"))
    return grade(f)


def describe(it, db=None):
    extra = json.loads(it["extra"]) if it["extra"] else {}
    from . import whitelist as W                                   # S2b: an unconfirmed/expired list never yields "on school list"
    cl = W.classify(extra, it["quote"], load_whitelist())
    rec, ent = cl["recognition"], cl["entry"]
    f = {"band": None, "reasons": [], "gaps": [], "assessed": False,
         "note": "fit not assessed: no ability profile yet (run: cnb interview)"}
    if db is not None:
        try:
            from . import profile as P
            f = fit(extra, it["quote"], P.levels(db), P.value_of(db, "hours_per_week"), P.value_of(db, "has_team"))
        except Exception:                      # noqa: BLE001 - a profile problem must never break a report
            pass
    g = grade(f)
    return {"extra": extra, "recognition": rec, "entry": ent, "hints": hints(extra, it["quote"], rec),
            "fit": f, "fit_text": fit_text(f), "grade": g, "grade_text": grade_text(g),
            "bucket": cl["bucket"], "list_trusted": cl["trusted"], "list_trust_reason": cl["trust_reason"]}


def fit_text(f):
    if not f.get("assessed"):
        return f.get("note", "not assessed")
    bits = "; ".join(f["reasons"] + f["gaps"])
    return f"{f['band']}" + (f" - {bits}" if bits else "")
