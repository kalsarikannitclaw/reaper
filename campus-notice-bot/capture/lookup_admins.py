"""One-time (re-runnable) lookup: owners/admins of whitelisted groups -> groups.json priority_by_group + admins table.
Read-only OneBot action get_group_member_list; only role owner/admin rows are kept, the rest of the list is discarded."""
import asyncio, json, os, sqlite3, time
import sys
import websockets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # python -I drops the script dir
import dyn
HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS, DB = os.path.join(HERE, "groups.json"), os.path.join(HERE, "messages.db")
tok = [l.split("=",1)[1].strip() for l in open(os.path.join(HERE,"..","stack",".env")) if l.startswith("CAPTURE_WS_TOKEN=")][0]

async def main():
    cfg = json.load(open(GROUPS, encoding="utf-8")); found = {}
    async with websockets.connect("ws://127.0.0.1:3001/", additional_headers={"Authorization": f"Bearer {tok}"}) as ws:
        for g in cfg["groups"]:
            echo = f"lookup-{g}-{time.time()}"
            await ws.send(json.dumps({"action": "get_group_member_list", "params": {"group_id": g, "no_cache": True}, "echo": echo}))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), 60))
                if r.get("echo") == echo: break
            if r.get("status") != "ok":
                print(g, "FAILED:", r.get("message") or r.get("wording")); continue
            found[g] = [(m["user_id"], m.get("card") or m.get("nickname"), m["role"]) for m in r["data"] if m.get("role") in ("owner", "admin")]
            print(g, "members:", len(r["data"]), "owners/admins:", len(found[g]))
            await asyncio.sleep(2)   # be gentle
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS admins(group_id INTEGER, user_id INTEGER, nick TEXT, role TEXT, checked_at INTEGER, PRIMARY KEY(group_id,user_id))")
    pbg = {int(k): set(v) for k, v in cfg.get("priority_by_group", {}).items()}
    for g, rows in found.items():
        db.execute("DELETE FROM admins WHERE group_id=?", (g,))
        for u, n, role in rows:
            if u in {int(x) for x in cfg.get("ignore_users", [])} or str(u) in cfg.get("demoted", {}).get(str(g), {}): continue
            db.execute("INSERT INTO admins VALUES(?,?,?,?,?)", (g, u, n, role, int(time.time())))
            pbg.setdefault(g, set()).add(u)
    db.commit()
    cfg["priority_by_group"] = {str(g): sorted(us) for g, us in pbg.items()}
    dyn.save_groups(cfg)   # atomic, unique temp name (shared with dyn.demote)
    for g, rows in found.items():
        for u, n, role in rows: print(f"  {g}  {role:5}  {u}  {n}")
asyncio.run(main())
