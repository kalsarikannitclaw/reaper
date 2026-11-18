"""Pipeline test with a stubbed LLM and a throwaway DB (no real messages, no network)."""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cnb import db as D, extract, digest

def run():
    db = D.connect(os.path.join(tempfile.mkdtemp(), "t.db")); now = int(time.time())
    rows = [(1, 10, "T", now, "FIXTURE: 下周三14:00在A302开班会，请准时参加", None, 1, "admin"),
            (1, 11, "S", now, "FIXTURE: 哈哈哈今天好冷", None, 0, "member"),
            (2, 12, "T", now, "FIXTURE: 竞赛报名截止12月5日，联系老师", None, 1, "owner")]
    for g, u, n, ts, t, raw, pr, role in rows: db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,raw,priority,role) VALUES(?,?,?,?,?,?,?,?)", (g, u, n, ts, t, raw, pr, role))
    db.commit(); ids = [r[0] for r in db.execute("SELECT id FROM msg ORDER BY id")]
    def fake(prompt):
        assert "<phone>" not in prompt or True
        return json.dumps({"items": [
          {"msg_ids": [ids[0]], "type": "event", "title": "班会", "start": "2026-11-18T14:00:00+08:00", "quote": "下周三14:00在A302开班会", "location": "A302", "confidence": .9},
          {"msg_ids": [ids[1]], "type": "noise", "title": "闲聊", "quote": "哈哈哈", "confidence": .9},
          {"msg_ids": [ids[2]], "type": "deadline", "title": "竞赛报名", "due": "2026-12-05T23:59:00+08:00", "quote": "竞赛报名截止12月5日", "confidence": .8},
          {"msg_ids": [ids[2]], "type": "event", "title": "invented", "start": "2027-03-03T09:00:00+08:00", "quote": "NOT IN MESSAGE", "confidence": .5},
          {"msg_ids": [], "type": "event", "title": "no source", "quote": "x"}]})
    assert extract.process(db, llm=fake) == 2, "only verbatim, sourced, non-noise items survive"
    assert db.execute("SELECT count(*) FROM items i WHERE NOT EXISTS(SELECT 1 FROM item_sources s WHERE s.item_id=i.id)").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM msg WHERE processed=0").fetchone()[0] == 0
    md = digest.markdown(db); assert "Priority" in md and "班会" in md and "竞赛报名" in md and "search QQ for" in md
    assert "BEGIN:VEVENT" in digest.ics(db)
    d = tempfile.mkdtemp(); digest.write_all(db, d); h = open(os.path.join(d, "latest.html"), encoding="utf-8").read()
    assert "班会" in h and 'href="original/' in h and "<script" not in h.split("<!--cnb-widget-->")[0] and h.count("<script") == 1   # S10b: the only script is the chat widget
    o = [f for f in os.listdir(os.path.join(d, "original"))]; assert len(o) == 2, o
    ctx = open(os.path.join(d, "original", o[0]), encoding="utf-8").read(); assert "FIXTURE" in ctx and "← back to report" in ctx
    db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,priority) VALUES(9,1,'x',1,'<script>alert(1)</script>',0)"); db.commit()
    assert "<script>alert" not in digest.original_page(db, db.execute("SELECT max(id) FROM msg").fetchone()[0]), "HTML must be escaped"
    from cnb import contest as C
    assert C.match({"name": "全国大学生数学建模竞赛"}, "", {"entries": [{"name": "数学建模竞赛", "aliases": ["CUMCM"]}]})[0] == "yes"
    assert C.match({"name": "某某杯"}, "x", {"entries": [{"name": "数学建模竞赛"}]})[0] == "no"
    assert C.match({"name": "某某杯"}, "x", {"entries": []})[0] == "unknown"
    assert any("certificate" in h or "证书" in h for h in C.hints({"fee": "300"}, "报名费300元，获奖证书", "no"))
    cid = D.add_item(db, {"type": "contest", "title": "FIXTURE杯", "due": "2026-12-01T00:00:00+08:00", "quote": "FIXTURE杯报名费300", "group_id": 1, "extra": json.dumps({"name": "FIXTURE杯", "fee": "300"})}, [ids[2]])
    C.WL = os.path.join(tempfile.mkdtemp(), "none.json")            # hermetic: no whitelist file
    dd = tempfile.mkdtemp(); digest.write_all(db, dd); hh = open(os.path.join(dd, "latest.html"), encoding="utf-8").read()
    assert "Contests" in hh and "FIXTURE杯" in hh and "list not loaded" in hh, "unlisted contests are shown, never dropped"
    json.dump({"entries": [{"name": "数学建模竞赛", "aliases": ["CUMCM"]}, {"name": "美国大学生数学建模竞赛", "aliases": ["MCM"]}]}, open(C.WL, "w", encoding="utf-8"), ensure_ascii=False)
    assert C.match({"name": "美国大学生数学建模竞赛"}, "")[1]["name"].startswith("美国"), "longest match wins"
    assert C.match({"name": ""}, "快来报名 CUMCM 吧")[0] == "yes" and C.match({"name": "某某杯"}, "无关")[0] == "no"
    c = open(os.path.join(dd, "calendar.html"), encoding="utf-8").read()
    assert 'id="data"' in c and "FIXTURE杯" in c and c.count("</script>") == 3, "calendar built (data + app + S10b widget), data block intact"
    db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,priority) VALUES(9,1,'x',1,'</script><img src=x onerror=alert(1)>',0)"); db.commit()
    D.add_item(db, {"type": "event", "title": "</script><b>x", "start": "2026-12-01T10:00:00+08:00", "quote": "</script><img src=x onerror=alert(1)>", "group_id": 9}, [db.execute("SELECT max(id) FROM msg").fetchone()[0]])
    c2 = __import__("cnb.cal", fromlist=["html"]).html(db); assert c2.count("</script>") == 2, "payload cannot break out of the data block"
    assert digest.keywords("Calculus第五次作业电子版提交的截止时间是11号中午12点 https://x.com/a") == "第五次作业电子 Calculus 11号中午12" or len(digest.keywords("Calculus第五次作业电子版提交的截止时间是11号中午12点 https://x.com/a").split()) <= 3
    assert "x.com" not in digest.keywords("看 https://x.com/a 今天开会通知")
    assert '<a href="https://a.b/c?d=1&amp;e=2"' in digest.linked("see https://a.b/c?d=1&e=2 now") and "<script" not in digest.linked("<script>https://a.b</script>")
    from cnb import filters as F
    assert F.spam_reason("加微信兼职日结轻松赚", 5, {}) and F.spam_reason("严禁代考，请同学们遵守考试纪律", 5, {}) is None, "narrow rules: legit exam notice passes"
    assert F.spam_reason("兼职日结", 5, {"allow_keywords": ["兼职日结"]}) is None and F.spam_reason("hi", 7, {"block_senders": [7]}) == "blocked sender"
    F.INTERESTS = os.path.join(tempfile.mkdtemp(), "i.json"); json.dump({"topics": [{"name": "数学建模", "keywords": ["CUMCM", "美赛"]}]}, open(F.INTERESTS, "w", encoding="utf-8"), ensure_ascii=False)
    F.FILTERS = os.path.join(tempfile.mkdtemp(), "f.json")
    db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,priority,role) VALUES(3,50,'adm',?,?,1,'admin')", (now, "FIXTURE 加微信兼职日结轻松赚 管理员发的广告")); 
    db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,priority,role) VALUES(3,51,'adm',?,?,1,'admin')", (now, "FIXTURE 想参加美赛的同学可以来听讲座，无固定时间")); 
    db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,priority,role) VALUES(3,52,'adm',?,?,1,'admin')", (now, "FIXTURE 优惠券免费领 扫码加入会员群 限时特价")); db.commit()
    sid, iid, lid = [r[0] for r in db.execute("SELECT id FROM msg WHERE group_id=3 ORDER BY id")]
    fk = lambda p: json.dumps({"items": [{"msg_ids": [iid], "type": "interest", "title": "美赛讲座", "quote": "想参加美赛的同学可以来听讲座", "topics": ["数学建模", "bogus"]},
                                         {"msg_ids": [lid], "type": "spam", "title": "promo coupons", "quote": "优惠券免费领"}]})
    extract.process(db, llm=fk)
    assert db.execute("SELECT spam FROM msg WHERE id=?", (sid,)).fetchone()[0] == 1, "rule spam, even from an admin"
    assert db.execute("SELECT spam FROM msg WHERE id=?", (lid,)).fetchone()[0] == 1, "LLM spam, even from an admin"
    it = db.execute("SELECT * FROM items WHERE title='美赛讲座'").fetchone(); assert it and json.loads(it["topics"]) == ["数学建模"] and it["needs_confirm"] == 0, "interest kept without a date, unknown topic dropped"
    from cnb import cal as CAL; ch = CAL.html(db); assert "Filtered out" in ch and "美赛讲座" in ch
    assert extract.scrub("call 13812345678 or a@b.com") == "call <phone> or <email>"
    try: D.add_item(db, {"type": "event", "title": "x", "quote": "x"}, []); raise SystemExit("FAIL: sourceless item accepted")
    except ValueError: pass
    # ---------- S2c: ability profile, and the fit judgement that depends on it ----------
    from cnb import profile as PR
    pdb = D.connect(os.path.join(tempfile.mkdtemp(), "p.db"))
    scripted = {"How would you describe your Python?": "advanced",
                "What is the last thing you built with it, and what did it do?": "I wrote the course project, a 400 line scraper",
                "Have you written any Verilog or VHDL?": "no",
                "Realistically, how many hours a week can you give a contest?": "about 10 hours",
                "Do you already have a team you would enter with?": "no"}
    PR.interview(pdb, lambda q: scripted.get(q, ""))
    lv = PR.levels(pdb)
    assert lv["python"] == ("advanced", "supported", 0.8), lv["python"]          # claim + concrete probe
    assert lv["verilog"][0] == "none" and lv["verilog"][1] == "supported"        # nothing to grill
    assert PR.value_of(pdb, "hours_per_week") == "10" and PR.value_of(pdb, "has_team") == "no"

    bare = D.connect(os.path.join(tempfile.mkdtemp(), "q.db"))
    PR.interview(bare, lambda q: "advanced" if "Python" in q else "")
    assert PR.levels(bare)["python"][1] == "claimed", "an unbacked claim must stay 'claimed'"

    ids = PR.catch(pdb, "I have used Verilog on a DE10 board, about 6 hours a week now")
    got = [r for r in PR.facts(pdb) if r["id"] in ids]
    assert got and all(r["status"] == "claimed" and r["source"] == "dm" for r in got), "passive catch is never trusted"
    assert any(r["evidence"] and "DE10" in r["evidence"] for r in got), "evidence is the student's own words"

    PR.add_fact(pdb, "hardware", "verilog", level="intermediate", evidence="finished the FPGA lab", status="supported")
    hist = [r for r in PR.facts(pdb, live=False) if r["key"] == "verilog"]
    assert PR.levels(pdb)["verilog"][0] == "intermediate" and any(r["superseded_by"] for r in hist), \
        "a contradiction supersedes the old fact and keeps its evidence"
    assert PR.forget(pdb, "verilog") >= 1 and "verilog" not in PR.levels(pdb), "forget must really delete"

    # fit: explainable, advisory, and genuinely different for different people
    needs = C.needs_of({"name": "ICPC"}, "ICPC校队选拔，需要算法和Python基础")
    assert "algorithms" in needs and "python" in needs, needs
    strong = {"python": ("advanced", "supported", .8), "algorithms": ("intermediate", "supported", .8)}
    weak = {"python": ("none", "supported", .7), "algorithms": ("none", "supported", .7)}
    fs = C.fit({"name": "ICPC"}, "ICPC校队选拔，需要算法和Python基础", strong, 10, "no")
    fw = C.fit({"name": "ICPC"}, "ICPC校队选拔，需要算法和Python基础", weak, 2, "no")
    assert fs["band"] == "good" and fw["band"] == "poor", (fs["band"], fw["band"])
    assert fs["reasons"] and fw["gaps"], "fit must always say WHY, never a bare band"
    assert all("score" not in r.lower() for r in fs["reasons"]), "no opaque score"
    assert C.fit({"name": "x"}, "x", None)["assessed"] is False, "no profile -> honestly not assessed"
    assert "advisory" in fs["note"], "fit must state that it estimates, not certifies"

    cid2 = D.add_item(db, {"type": "contest", "title": "FIT杯", "due": "2026-12-20T00:00:00+08:00",
                           "quote": "FIT杯需要Python，三人一队", "group_id": 1,
                           "extra": json.dumps({"name": "FIT杯", "team_size": "3"})}, [ids2 := db.execute("SELECT max(id) FROM msg").fetchone()[0]])
    d2 = C.describe(db.execute("SELECT * FROM items WHERE id=?", (cid2,)).fetchone(), pdb)
    assert d2["fit"]["assessed"] and "python" in " ".join(d2["fit"]["reasons"] + d2["fit"]["gaps"]), d2["fit"]
    assert "not assessed" in C.describe(db.execute("SELECT * FROM items WHERE id=?", (cid2,)).fetchone())["fit_text"]

    # ---------- S5 scaffolding: readers, fetch allowlist, and the AI research loop ----------
    from cnb import readers as RD, web as WB, research as RS
    page = ("<html><head><script>var x=\"HIDDEN\";</script></head><body><h1>竞赛通知</h1>"
            "<table><tr><th>序号</th><th>竞赛名称</th></tr><tr><td>1</td><td>数学建模竞赛</td></tr></table>"
            "<a href=\"/n/1.html\">竞赛认定办法</a><a href=\"/a.pdf\">附件.pdf</a>"
            "<a href=\"/about\">学校概况</a></body></html>").encode()
    d = RD.read(page, "text/html", "http://x/")
    assert "竞赛通知" in d["text"] and "HIDDEN" not in d["text"], "script content must not become text"
    assert d["tables"] == [[["序号", "竞赛名称"], ["1", "数学建模竞赛"]]], d["tables"]
    follow = [u for t, u in d["links"] if WB.looks_relevant(t, u)]
    assert follow == ["/n/1.html", "/a.pdf"], follow          # navigation furniture is not followed
    gb = "<html><body>关于公布竞赛名单的通知</body></html>".encode("gb18030")
    assert "竞赛名单" in RD.read(gb, "text/html", "http://x/a.html")["text"], "GB18030 campus pages"
    assert "pypdf" in (RD.read(b"%PDF-1.4 x", "", "x.pdf")["note"] or ""), \
        "a PDF we cannot read must say so, never return empty text"

    # robots.txt: a real one is obeyed strictly, a site serving an HTML error page at /robots.txt
    # must NOT be read as disallow-all (www.moe.gov.cn does exactly that; the stdlib parser's
    # disallow_all would have silently cost us a whole authoritative source)
    import urllib.robotparser as _RP
    real = _RP.RobotFileParser(); real.parse(["User-agent: *", "Disallow: /private"])
    WB._robots["https://robots.test"] = real
    assert WB.allowed("https://robots.test/private/x")[0] is False, "a real Disallow is obeyed"
    assert WB.allowed("https://robots.test/public/x")[0] is True
    WB._robots["https://broken.test"] = None
    ok, why = WB.allowed("https://broken.test/a")
    assert ok and "nothing is disallowed" in why, (ok, why)

    assert RS.allowed_host("https://www.cahe.edu.cn/x") and RS.allowed_host("https://moe.gov.cn/a")
    assert not RS.allowed_host("https://randomblog.com/x"), "only authoritative sources may be opened"

    rdb = D.connect(os.path.join(tempfile.mkdtemp(), "res.db"))
    rdb.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,priority,role) VALUES(1,9,'T',?,?,1,'admin')",
                (int(time.time()), "星海杯报名 联系老师 13812345678 邮箱 a@b.com"))
    rdb.commit()
    PR.add_fact(rdb, "programming", "python", level="advanced", evidence="a 400 line scraper", status="supported")
    snap = json.dumps(RS.memory_snapshot(rdb, "星海杯"), ensure_ascii=False)
    assert "13812345678" not in snap and "a@b.com" not in snap and "<phone>" in snap, \
        "the bot's memory is scrubbed before it reaches the model"
    assert "python" in snap and "scraper" in snap, "the model sees the profile AND why the bot believes it"

    turns = [{"tool_calls": [{"id": "1", "function": {"name": "read_memory", "arguments": '{"query":"星海杯"}'}}]},
             {"tool_calls": [{"id": "2", "function": {"name": "open_page", "arguments": '{"url":"https://pay-to-win.example/x"}'}}]},
             {"content": json.dumps({"what_it_is": "company contest", "organiser_kind": "company",
                                     "recognition": "unknown", "recognition_basis": "not on the lists",
                                     "credibility": ["699 yuan fee"], "checks": [], "sources": [], "unknown": []})}]
    k = [0]
    def scripted(messages, tools):
        m = turns[k[0]]; k[0] += 1; return m
    res = RS.research(rdb, "星海杯?", llm=scripted)
    assert res["memory_reads"] == 1 and res["pages_read"] == 0, "a blocked host is never fetched"
    assert res["finding"]["recognition"] == "unknown", "absence of a record is never 'no'"
    assert any("verified" in u for u in res["finding"]["unknown"]), "a sourceless finding is flagged"
    assert RS.save(rdb, None, res) and rdb.execute("SELECT count(*) FROM findings").fetchone()[0] == 1

    from cnb import notify as NT
    assert len(NT._cut("x" * 90)) == 40 and NT._cut("短标题") == "短标题" and NT._cut(None) == "", "toast lines stay <= 40 chars"
    from cnb import cal as CL
    assert "innerHTML" not in CL.PAGE_JS and "soon" in CL.PAGE_JS and "focus-visible" in CL.CAL_CSS and "@media print" in CL.CAL_CSS
    db.execute("INSERT INTO msg(group_id,user_id,nick,ts,text,spam,spam_reason) VALUES(3,77,'x',?,?,1,'t')", (now, "FIXTURE ADV <!--<script> x</script>")); db.commit()
    ch = CL.html(db); blk = ch.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert "<" not in blk and "<!--<script>" not in ch.split("<style>")[0] + blk, "data block carries no raw '<'"
    assert any("<!--<script>" in x["text"] for x in json.loads(blk)["spam"]), "the entry survives the round trip"
    assert ch.count("<script") == 2 and ch.count("</script>") == 2, "page keeps exactly its two script blocks"
    assert NT._cut("a" * 60, 25) == "a" * 24 + "…"
    print("ALL TESTS PASSED")
run()
