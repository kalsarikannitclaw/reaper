"""Extractor: unprocessed priority-aware messages -> structured items via an LLM (DeepSeek by default, any OpenAI-compatible API works; user's own key, model/base_url/key name from config.json).
Rules enforced IN CODE, not trusted to the model: quote must be verbatim from a source message; no source -> dropped;
a date/time that does not appear anywhere in the source text is nulled and flagged needs_confirm (never invent times)."""
import json, os, re, urllib.request, datetime as dt
from . import db as D, filters as F
CFG = os.path.join(D.ROOT, "config.json")
ENV = os.path.join(D.ROOT, "stack", ".env")
SCHEMA_HINT = """Return JSON {"items":[{"msg_ids":[int],"type":"event|deadline|interest|contest|noise|spam","topics":[interest names that apply],"title":str,
"start":ISO8601|null,"end":ISO8601|null,"due":ISO8601|null,"location":str|null,"action":"the single thing the student must DO, as a short imperative WITHOUT repeating the date or place (those are their own fields), or null","requirements":"what must be prepared/met (materials, eligibility, team size, fee, dress, place), short, or null","quote":"verbatim substring of the message",
"confidence":0-1,"contest":{"name":str,"organiser":str|null,"reg_deadline":ISO|null,"team_size":str|null,"fee":str|null}|null}]}

NEVER INVENT. No time, date or place that the message does not state. Unknown = null, never a guess.
Resolve relative dates (下周三, 后天, next Wed, 本周日) against THAT MESSAGE's own timestamp, shown with it, not against today.
A date with no clock time: use 23:59 for a deadline (end of that day) and 00:00 for an event whose hour is not stated.

LANGUAGE: write title, action and requirements in THE SAME LANGUAGE AS THE MESSAGE. An English notice gets an
English title and action; a Chinese notice gets Chinese. Never translate: the student searches QQ with these words.

TITLE: a short noun phrase naming the thing (at most ~12 Chinese characters or ~6 words), in the message's language.
Not a sentence, not a copy of the message. "班会地点变更", "奖学金材料提交", "Scholarship interviews".

SAME EVENT, SEVERAL MESSAGES: one real-world event announced more than once is ONE item. Different groups relaying the
same notice, a repost, a reminder, or a second message adding detail ("微积分期中习题课…" and "期中习题课…" with the
same date, period and room) all describe the same event: return a single item and list EVERY such message id in
"msg_ids". If a message restates an item from the "ALREADY ON THE CALENDAR" list given with the messages, do not
create a new item; set "same_as" to that item's id (and put only the new message ids in "msg_ids"). Same title is
not required: same date + same place (or the same course and the same date) is the test.

ONE ITEM PER THING. A message announcing two exams, two deadlines or two activities produces TWO items, each with
its own title and time. A contest message that states both a registration deadline and a separate contest/defence date
produces two items. Never merge distinct dates into one item, and never emit one item per sentence of the same thing.

ALWAYS AN ITEM, never dropped:
 - CANCELLATIONS and POSTPONEMENTS ("今晚的自习室开放取消", "Friday's lab is cancelled"). Title says it is cancelled;
   the time is the time of the thing being cancelled, as stated. The student must know the thing is off.
 - CORRECTIONS / CHANGES of room or time ("地点由B201改为B305"): an item stating the new room or time.
 - A notice with NO DATE YET ("通知已发布，请关注后续安排", "a briefing will be announced later"): an item with
   null times. Do not drop it and do not invent a date.
 - A notice whose content is in an ATTACHMENT or image ("[file]课程表更新版，请自行下载"): an item whose action tells
   the student to open the original. Never guess what an image or file you cannot see contains.

MULTI-STAGE CONTESTS: when a message gives several stages each with its OWN exact date or deadline (提交作品, 校内答辩, 决赛), it is a multi-task
message: one item per stage, each with its own date, and every stage item sets "origin_title" to the contest's name.

ORIGIN: a REGISTRATION / sign-up (报名), a study, learning or preparation activity (学习, 备赛, 培训, 选拔) is never free-standing: it
belongs to an origin task (the contest, course, event or programme it is for). For such an item set "origin_title" to the
name of that origin exactly as the message names it ("全国大学生数学建模竞赛", "微积分期中考试"). Do not invent a name the
message does not give; if no origin is named, leave "origin_title" null.

SUBMISSION NODES: a homework/report submission deadline is called a 节点 and is numbered ("节点2", "节点4"). Write 节点
and its number TOGETHER, unbroken, in the title: "第6次作业节点2", never "第6次作业的节、点2", never dropping 节点, never
splitting it ("节 点") or replacing it with a vaguer word ("截止", "提交时间"). One 节点 = one item with its own date.

HOMEWORK: every message that mentions 作业 / homework / assignment / quiz-counted-as-homework and tells the student
 something to DO, BRING, SUBMIT, FIX or KNOW is an item, never noise, whoever sent it (teacher, TA, class rep,
 classmate relaying it). Includes: a published assignment, a submission deadline or node, a reference answer posted,
 a correction to a problem ("第7题请将dx移至括号外"), a reminder to bring it ("记得带数学作业"), a grading or
 quiz rule. type "deadline" when a submission time is stated, otherwise "event" or "deadline" with null times and the
 action telling the student what to do. Only a pure reaction ("作业好难哈哈", "收到") is noise.

type "noise" = chatter, greetings, acknowledgements ("收到", "thanks", emoji), questions from classmates,
 and answers to them. Nothing the student has to act on or remember.
type "spam" = advertising, promotions, scams, paid-work offers, chain letters, even from an owner/admin. Reason in "title".
 An official notice that merely contains a link or QR code is NOT spam.
 A CONTEST OR COMPETITION ANNOUNCEMENT IS NEVER SPAM, however dubious it looks. A contest that charges a fee for a
 guaranteed certificate, names no organiser, or uses agents is still type "contest": record the fee and the missing
 organiser in the contest fields and let the report warn about it. The student decides, not you.
type "contest" = any competition, contest, challenge, award or match: registration, stages, defences, results,
 deadline extensions, team selection. Fill "contest". team_size as a bare number when stated ("三人一队" -> "3").
 An extension restates the contest with the NEW deadline.
type "interest" = a post matching one of the student's interests (listed below), including team-recruiting posts from
 classmates and shared learning material, even with no date; set "topics" to the matching interest names.
If the post states a date or time (a lecture, a talk, a deadline) it is an "event" or "deadline" with "topics" set, NOT "interest";
"interest" is only for posts with no date or time of their own."""

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9}|\+?\d[\d -]{8,}\d)(?!\d)"); _MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
def scrub(t): return _MAIL.sub("<email>", _PHONE.sub("<phone>", t))      # PLAN: strip phones/emails before any LLM call

def load_cfg():
    """config.json: model, base_url (default OpenAI-compatible; v0 uses DeepSeek), key_env (name of the variable holding the key, default OPENAI_API_KEY; v0 uses DEEPSEEK_API_KEY), tz."""
    try: c = json.load(open(CFG)) if os.path.exists(CFG) else {}
    except ValueError: raise SystemExit("config.json is empty or not valid JSON. Expected e.g. {\"model\": \"...\", \"base_url\": \"...\", \"key_env\": \"...\"}")
    kn = c.get("key_env", "OPENAI_API_KEY")
    key = os.environ.get(kn) or (next((l.split("=",1)[1].strip() for l in open(ENV) if l.startswith(kn + "=")), None) if os.path.exists(ENV) else None)
    return c.get("model"), c.get("tz", "Asia/Shanghai"), key, c.get("base_url", "https://api.openai.com/v1").rstrip("/"), kn

def chat_call(prompt, model, key, base):
    body = {"model": model, "max_tokens": 8000, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": COUNT_SYSTEM if prompt.startswith(COUNT_MARK) else SCHEMA_HINT}, {"role": "user", "content": prompt}]}
    if "api.openai.com" in base: body["store"] = False            # OpenAI-only flag: no server-side storage
    req = urllib.request.Request(base + "/chat/completions", method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(body).encode())
    return json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]   # key is never logged

COUNT_MARK = "[[COUNT]]"
COUNT_SYSTEM = """You count tasks. For each message below, count the SEPARATE tasks it contains: a task is one thing the student must do,
attend, submit or remember, with its own date/time/deadline or its own action (two exams = 2, a registration deadline plus a
separate contest/defence date = 2, abstract due + full paper due = 2). One event described in several sentences, or one deadline
with its requirements, is ONE task.
A later stage or detail of the SAME contest/event mentioned only in passing, with no exact date or deadline of its own ("校内选拔12月初",
"选拔随后进行"), is part of that announcement's task, not a second task; registration, team size and fee of one contest are ONE task.
Count a stage separately only when the message gives it its own exact date or deadline ("11月30日提交作品，12月14日校内答辩" = 2). Chatter, thanks, questions and spam count 0. Answer JSON only: {"counts": {"<id>": <integer>}}."""

def count_tasks(llm, msgs):
    """Step 1 of extraction: how many tasks does each message hold? Returns {id: n}, or {} when the answer is unusable
    (then extraction runs exactly as before). Only scrubbed text is sent."""
    try:
        raw = json.loads(llm(COUNT_MARK + "\n" + "\n".join(f"[id={m['id']}] {scrub(m['text'])}" for m in msgs)))
        c = raw.get("counts") if isinstance(raw, dict) else None
        if not isinstance(c, dict): return {}
        ids = {m["id"] for m in msgs}
        return {int(k): max(0, min(int(v), 8)) for k, v in c.items() if str(k).lstrip("-").isdigit() and int(k) in ids and str(v).lstrip("-").isdigit()}
    except Exception:
        return {}

def render(msgs, tz, counts=None):
    z = dt.timezone(dt.timedelta(hours=8))
    counts = counts or {}
    return "\n".join(f"[id={m['id']}] {('[tasks=%d] ' % counts[m['id']]) if m['id'] in counts else ''}[{dt.datetime.fromtimestamp(m['ts'], z).isoformat(timespec='minutes')}] [group={m['group_id']}] [{'PRIORITY ' if m['priority'] else ''}{m['role'] or 'member'}] {scrub(m['text'])}" for m in msgs)

_REL_WORDS = re.compile(
    r"[周星期明后今下]|"                                       # 下周三, 明天, 后天, 今晚, 星期五
    r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\b(tomorrow|today|tonight|noon|midnight)\b|"
    r"\b(next|this|coming)\s+(week|month|mon|tue|wed|thu|fri|sat|sun)|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)

def _has_time_evidence(val, text):
    """Guard against invented times: a produced date must be traceable to something in the message.
    Evidence is either the month/day digits themselves, or a word that states a relative or named
    date. The word list covers English as well as Chinese: until 2026-11-10 it was CJK-only, so a
    correctly resolved English date ("Thursday at 3pm", "next Monday noon") had no evidence and was
    nulled by the very guard meant to protect it, silently dropping the time from English notices."""
    if not val: return True
    d = re.findall(r"\d+", val)[:5]                           # year, month, day, hour, minute
    return any(x.lstrip("0") in re.sub(r"\D", " ", text).split() or x in text for x in d[1:3]) \
        or bool(_REL_WORDS.search(text))

def _exact_day(val, text):
    """checker S2-3: the message states this exact month+day ("12月14日", "12/14", "12-14"), not just "12月初"."""
    m = re.match(r"\d{4}-(\d\d)-(\d\d)", val or "")
    if not m: return False
    mo, d = int(m.group(1)), int(m.group(2))
    return bool(re.search(rf"(?<!\d)0?{mo}\s*[月/.\-]\s*0?{d}(?!\d)", text or ""))

def skip_trivia(db):
    """No LLM for messages that cannot hold a notice: very short, or only placeholders like [image]/[face]. Marked processed."""
    n = 0
    for m in db.execute("SELECT id,text FROM msg WHERE processed=0").fetchall():
        if len(re.sub(r"\[[a-z_]+\]|\s|[^\w\u4e00-\u9fff]", "", m["text"] or "")) < 4:
            db.execute("UPDATE msg SET processed=1 WHERE id=?", (m["id"],)); n += 1
    db.commit(); return n

def process(db, llm=None, limit=80):
    tz = "Asia/Shanghai"
    if llm is None:
        model, tz, key, base, kn = load_cfg()
        if not (model and key): raise SystemExit(f"Set model in config.json and {kn} in stack/.env first (capture keeps working without them).")
        llm = lambda p: chat_call(p, model, key, base)
    skip_trivia(db); F.prefilter(db)
    msgs = db.execute("SELECT * FROM msg WHERE processed=0 ORDER BY priority DESC, ts LIMIT ?", (limit,)).fetchall()
    if not msgs: return 0
    by_id = {m["id"]: m for m in msgs}
    tp = F.load_interests(); head = ("Student's interests: " + "; ".join(f"{t['name']} ({', '.join(t.get('keywords', []))})" for t in tp) + "\n") if tp else "Student's interests: none configured.\n"
    known = open_items_brief(db)
    # checker S2-2: a count of 0 is not rendered. The counter sees no interests list and no ALWAYS-AN-ITEM rules, so its
    # 0 ("chatter") must not override them (interest posts, no-date notices, attachments): N=0 gets no tag and no rule.
    counts = {k: v for k, v in count_tasks(llm, msgs).items() if v > 0}
    rule = ("Some messages show [tasks=N], the number of separate tasks it holds. N>1: return exactly N items for that message, one per task, each with its own date.\n"
            "N=1: return exactly ONE item for it (the one-at-a-time rule).\n") if counts else ""
    raw = json.loads(llm(head + known + rule + render(msgs, tz, counts)))
    out = (raw.get("items", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []); n = 0     # models sometimes answer with a bare list
    # one-at-a-time rule, enforced: a message the counter put at exactly 1 task yields ONE item. Extra items sourced only
    # from that message are absorbed into the first one (kept in its history, not shown as separate tasks).
    # checker S2-3: absorption happens AFTER validation, inside the loop below. Doing it up front absorbed a valid item into
    # one whose quote was then rejected (the whole message was lost), and lost the 'absorbed' row when the first item merged.
    owner = {}                                                  # msg id -> (item id, its date) of the first VALID item from it
    for it in out:
        if not isinstance(it, dict): continue
        ids = list(dict.fromkeys(i for i in (it.get("msg_ids") or []) if i in by_id))   # checker S2-2: dedupe (a repeated id made the reply drop empty the sources)
        if not ids or it.get("type") == "noise": continue
        akey = ids[0] if len(ids) == 1 and counts.get(ids[0]) == 1 and it.get("type") not in ("spam", "interest") else None
        if it.get("type") == "spam":
            db.executemany("UPDATE msg SET spam=1, spam_reason=? WHERE id=?", [("LLM: " + str(it.get("title") or "spam")[:80], i) for i in ids]); continue
        src = [by_id[i] for i in ids]; text = "\n".join(s["text"] for s in src)
        quote = it.get("quote") or ""
        it["title"] = fix_node(it.get("title"), quote)
        if quote not in text: continue                        # quote must be verbatim
        flag = 0
        for k in ("start", "end", "due"):
            if not _has_time_evidence(it.get(k), text): it[k] = None; flag = 1
        if akey in owner:                                       # one-at-a-time: counter said this message holds 1 task
            d = it.get("start") or it.get("due")
            if d and _exact_day(d, text) and d[:10] != (owner[akey][1] or "")[:10]:
                flag = 1                                        # checker S2-3: its own exact date = a separate task by COUNT_SYSTEM's own rule; keep it, ask
            else:
                db.execute("INSERT INTO item_history(item_id,ts,field,old,new,msg_id,kind) VALUES(?,strftime('%s','now'),'absorbed',NULL,?,?,'one-at-a-time')",
                           (owner[akey][0], ((it.get("title") or "") + " | " + quote)[:300], akey)); continue
        it.update(sender=src[0]["user_id"], group_id=src[0]["group_id"], priority=max(s["priority"] for s in src),
                  needs_confirm=0 if it.get("type") == "interest" else (flag or int(not any(it.get(k) for k in ("start", "due")))),
                  requirements=json.dumps(it["requirements"], ensure_ascii=False) if isinstance(it.get("requirements"), (list, dict)) else it.get("requirements"),
                  extra=json.dumps(it.get("contest"), ensure_ascii=False) if it.get("contest") else None)
        if it.get("type") != "interest" and any("[image text (OCR, conf" in (s["text"] or "") for s in src): it["needs_confirm"] = 1; it["confidence"] = min(float(it.get("confidence") or 0.5), 0.5)   # S8: OCR text is lower confidence
        tps = sorted({*(t for t in (it.get("topics") or []) if t in {x["name"] for x in tp}), *(t for s in src for t in json.loads(s["topics"] or "[]"))})
        it["topics"] = json.dumps(tps, ensure_ascii=False) if tps else None
        authoritative = any(s["priority"] or (s["role"] in ("owner", "admin")) for s in src)
        change = is_change(text) and authoritative            # a member's "改为..." never supersedes an item (PLAN: tracked senders decide)
        if _is_reply_only(src, by_id) and not it.get("same_as"):             # a bare reply adds detail; it is not an event of its own,
            last = max(src, key=lambda s: s["ts"]); rest = [s for s in src if s["id"] != last["id"]]    # but the earlier message still is: keep that item
            rtext = "\n".join(s["text"] for s in rest)
            if rest and quote in rtext:                         # checker S2-2: never leave an item whose quote is only in the dropped reply
                ids = [s["id"] for s in rest]; src = rest; text = rtext
        hit = find_same(db, it, it.get("same_as"), change=change, text=text)
        if hit:                                                 # same event again, or a change to it: merge, keep history
            D.merge_into(db, hit, it, ids, change)
            if it.get("type") != "interest" and any("[image text (OCR, conf" in (s["text"] or "") for s in src): db.execute("UPDATE items SET needs_confirm=1 WHERE id=?", (hit["id"],))   # S8
            if akey and akey not in owner: owner[akey] = (hit["id"], hit["start"] or hit["due"])
            continue
        if change and it.get("type") != "interest" and _ambiguous: it["needs_confirm"] = 1   # looked like a change but no safe target
        if is_change(text) and not authoritative and it.get("type") != "interest": it["needs_confirm"] = 1   # checker S2-2: a member's "改到..." is hearsay; never a confident second item
        if change and re.search(r"取消(?!.{0,4}资格)|cancel+ed", text, re.I) and not re.search(r"取消|cancel", it.get("title") or "", re.I):
            it["needs_confirm"] = 1                              # checker S2-2: a new item whose source says cancelled but whose title does not
        ot = (it.get("origin_title") or "").strip() if isinstance(it.get("origin_title"), str) else ""
        if ot and ot not in text: ot = ""                           # the origin name must come from the message itself
        it["origin_title"] = ot or None
        iid = D.add_item(db, it, ids); n += 1
        if akey and akey not in owner: owner[akey] = (iid, it.get("start") or it.get("due"))
        if ot:
            db.execute("UPDATE items SET origin_title=? WHERE id=?", (ot, iid))
            o = D.find_origin(db, ot, iid, it.get("group_id"))
            if o: D.link_origin(db, iid, o)
            else: db.execute("UPDATE items SET needs_confirm=1 WHERE id=?", (iid,))   # origin not found yet: shown as unresolved, never invented
    n += homework_net(db, by_id)
    D.relink_origins(db)
    db.executemany("UPDATE msg SET processed=1 WHERE id=?", [(i,) for i in by_id]); db.commit(); return n

_REPLY = re.compile(r"^\s*(回复|回复上面|re[:：]|reply)", re.I)
def _is_reply_only(src, by_id):
    """True when the item's LAST source is an admin/owner short reply with no date or time of its own and
    an earlier message in the same item carries the date: the item would copy the earlier time onto the reply."""
    last = max(src, key=lambda s: s["ts"])
    if len(src) < 2 or not _REPLY.match(last["text"] or ""): return False
    if is_change(last["text"]) or _REL_WORDS.search(last["text"] or ""): return False   # checker S2-2: "回复上面：取消", "Reply: moved to Friday" carry the news
    return not re.search(r"\d|[周星期明后今]", last["text"] or "")

_NODE = re.compile(r"节\s*点\s*([0-9一二三四五六七八九十]+)")
def fix_node(title, quote):
    """Code guard for the 节点 rule: the quote says 节点N, so the title must carry 节点N, written together.
    Repairs 节 点 / 节点 N spacing and re-adds a dropped 节点 instead of trusting the model."""
    t = re.sub(r"节\s*点", "节点", title or "")
    t = re.sub(r"节点\s+(?=[0-9一二三四五六七八九十])", "节点", t)
    m = _NODE.search(quote or "")
    if m and not re.search(r"节点\s*[0-9一二三四五六七八九十]", t):
        t = re.sub(r"(提交|截止|deadline)$", "", t).rstrip("的 ") + f"节点{m.group(1)}"
    return t

def open_items_brief(db, n=40):
    """Hand the model what is already on the calendar so it can say 'same_as' instead of duplicating."""
    rows = db.execute("SELECT id,title,start,due,location FROM items WHERE status IN ('new','confirmed') ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows: return ""
    return "ALREADY ON THE CALENDAR (id | title | time | place):\n" + "\n".join(
        f"#{r['id']} | {r['title']} | {r['start'] or r['due'] or '-'} | {r['location'] or '-'}" for r in rows) + "\nMESSAGES:\n"

# checker S2: bare 提前/调整/取消/change are everyday words ("请提前10分钟到场", "调整好状态", "取消考试资格", "change your
# seat") and turned ordinary new notices into change notices that rewrote an existing item. Only their change forms count.
_CHANGE = re.compile(r"改为|改到|改在|改成|变更|更改|调整[为到至成]|[时日地]间调整|地点调整|推迟|延期|延后|提前[到至为]|顺延|更正|"
                     r"取消(?!.{0,4}资格)|换到|换成|由.{1,14}改|"
                     r"\b(moved|changed|rescheduled|postponed|cancel+ed|delayed|brought forward|correction)\b|instead of|no longer", re.I)
def is_change(text): return bool(_CHANGE.search(text or ""))

_ambiguous = False
def _norm(t): return re.sub(r"[\s\W_]+|变更|更改|调整|取消|推迟|延期|通知|更新|提醒|change[ds]?|cancel+ed|reminder", "", (t or "").lower())
def _overlap(a, b):
    """Longest common substring length of two normalised titles."""
    a, b = _norm(a), _norm(b); best = 0
    for i in range(len(a)):
        for j in range(best + 1, len(a) - i + 1):
            if a[i:i + j] in b: best = j
            else: break
    return best

def find_same(db, it, same_as=None, change=False, text=""):
    """Code-level duplicate/update guard (the model is asked to merge, but is not trusted to). Plain repeat: same
    type, same moment, and either one title contains the other or the same place. CHANGE notice ("地点由B201改为B305",
    "moved to Friday"): the moment or place may differ, so a target is scored from evidence in the notice itself:
    the model's same_as (valid, open row), the OLD place appearing in the text, same day, a shared run of title
    characters, same group. Needs a clear unique winner; a tie or weak match returns None (never guess, a new
    to-confirm item is safer than rewriting the wrong one). Only open items (new/confirmed) are ever touched."""
    global _ambiguous; _ambiguous = False
    t = it.get("start") or it.get("due")
    ids = it.get("msg_ids") or []
    if ids:                                                     # checker S2: replay of a message that already made this item (also untimed items)
        q = f"SELECT i.* FROM items i JOIN item_sources s ON s.item_id=i.id WHERE s.msg_id IN ({','.join('?' * len(ids))}) AND i.type=?"
        for r in db.execute(q, (*ids, it.get("type"))).fetchall():
            if _norm(r["title"]) == _norm(it.get("title")) or r["quote"] == it.get("quote"): return r
    sa = None
    if same_as:
        sa = db.execute("SELECT * FROM items WHERE id=? AND status IN ('new','confirmed')", (same_as,)).fetchone()
        if sa and not change and (not t or t == (sa["start"] or sa["due"])): return sa
    if not change:
        if not t: return None
        for r in db.execute("SELECT * FROM items WHERE status IN ('new','confirmed') AND COALESCE(start,due)=? AND type=?", (t, it.get("type"))).fetchall():
            a, b = it.get("title") or "", r["title"] or ""
            if a == b or (a and b and (a in b or b in a)): return r
            if it.get("location") and it.get("location") == r["location"]: return r
        return None
    scored = []
    for r in db.execute("SELECT * FROM items WHERE status IN ('new','confirmed') AND type!='interest'").fetchall():
        sc = 0
        if sa and r["id"] == sa["id"]: sc += 3
        if r["location"] and len(r["location"]) >= 2 and r["location"] in text and r["location"] != it.get("location"): sc += 4
        elif r["location"] and r["location"] == it.get("location"): sc += 2
        rt = r["start"] or r["due"]
        if t and rt and t[:10] == rt[:10]: sc += 2
        # checker S2: title evidence = how much of the EXISTING item's name the notice (title or text) repeats. A shared
        # run like 期中考试/考试 is not identity (高数期中考试 vs 线代期中考试); >=75% of the old name is.
        rn = _norm(r["title"]); ov = max(_overlap(r["title"], it.get("title")), _overlap(r["title"], text))
        strong = ov >= 2 and rn and ov >= 0.75 * len(rn)
        quoted = bool(r["location"] and len(r["location"]) >= 2 and r["location"] in text and r["location"] != it.get("location"))
        if strong: sc += 3
        elif ov >= 2: sc += 1
        if it.get("type") and it["type"] == r["type"]: sc += 1
        if it.get("group_id") and it["group_id"] == r["group_id"]: sc += 1
        if not (strong or quoted): sc = min(sc, 4)   # same day/type/group or the model's same_as alone never pick a target
        scored.append((sc, r))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 5 and (len(scored) == 1 or scored[0][0] > scored[1][0]): return scored[0][1]
    _ambiguous = bool(scored and scored[0][0] >= 3)
    return None

HW = re.compile(r"作业|homework", re.I)
def homework_net(db, by_id):
    """Safety net, in code and not trusted to the model: a message that mentions 作业/homework and ended up with no
    item at all gets a plain one (times unknown, needs confirm, verbatim quote). Better a visible item the student
    can dismiss than a homework correction nobody ever sees."""
    n = 0
    for mid, m in by_id.items():
        t = m["text"] or ""
        if not HW.search(t) or m["spam"]: continue
        if db.execute("SELECT 1 FROM item_sources WHERE msg_id=?", (mid,)).fetchone(): continue
        if len(re.sub(r"\[[a-z_]+\]|\s|[^\w\u4e00-\u9fff]", "", t)) < 6: continue
        it = dict(type="deadline", title="作业相关通知", start=None, end=None, due=None, location=None,
                  action="查看原消息，确认作业要求", confidence=0.4, quote=t, sender=m["user_id"], group_id=m["group_id"],
                  priority=m["priority"], needs_confirm=1, requirements=None, extra=None, topics=None)
        D.add_item(db, it, [mid]); n += 1
    return n
