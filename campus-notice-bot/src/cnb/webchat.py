"""Local chat page (stage S10): a chat box + a questionnaire panel on 127.0.0.1, stdlib only, no external assets.

Bound to the loopback address only (make_server refuses anything else). Defences for a page that holds a student's
profile and may call the LLM with their key:
 - Host header must be 127.0.0.1/localhost[:port] (blocks DNS rebinding);
 - POST needs Content-Type application/json (a cross-site <form> cannot send that) and, when an Origin header is
   present, it must be this same origin;
 - request bodies are capped; all user text is shown with textContent, never as HTML.
One user, one session. `db` is a DB PATH: each request opens (and closes) its own connection, so the server thread
and the main thread never share one, and the live capture DB is held only for the length of a request.
"""
import json, os, mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from . import converse as CV, profile as P, db as D, widget as WG, digest as DG, manual as MN

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8790
MAX_BODY = 16 * 1024
MAX_PROVIDE = 3 * 1024 * 1024        # /api/page/provide only: a 2 MB file as base64 JSON (MN.MAX_FILE is checked again after decoding)


def bind_ok(host):
    return host == LOOPBACK


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Campus Notice Bot - chat</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--card:#fff;--line:#d9dde5;--me:#dbeafe;--bot:#fff;--acc:#2456b8}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e8ebf0;--card:#1d222b;--line:#323947;--me:#1e3a66;--bot:#232936;--acc:#7aa7ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Segoe UI","Microsoft YaHei",sans-serif}
main{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);gap:12px;max-width:1100px;margin:0 auto;padding:12px}
@media (max-width:760px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
h1{font-size:16px;margin:0 0 8px}#log{height:60vh;overflow:auto;display:flex;flex-direction:column;gap:8px}
.m{padding:8px 10px;border-radius:8px;white-space:pre-wrap;border:1px solid var(--line);max-width:92%}
.me{background:var(--me);align-self:flex-end}.bot{background:var(--bot)}
form.row{display:flex;gap:8px;margin-top:8px}input[type=text],select{flex:1;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);min-width:0}
button{padding:8px 12px;border:1px solid var(--acc);background:var(--acc);color:#fff;border-radius:6px;cursor:pointer}
button.alt{background:transparent;color:var(--acc)}.opts{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.cnbp-card{border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0}.cnbp-url{word-break:break-all;font-family:ui-monospace,Consolas,monospace;font-size:12px}.cnbp-why,.cnbp-note,.cnbp-msg{font-size:12px;opacity:.85;margin:4px 0}
.cnbp-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:4px 0}.cnbp-btn{display:inline-block;padding:4px 10px;border:1px solid var(--acc);background:transparent;color:var(--acc);border-radius:6px;cursor:pointer;font:inherit;font-size:13px;text-decoration:none}.cnbp-ta{width:100%;box-sizing:border-box;font:inherit}
.q{border-top:1px solid var(--line);padding:8px 0}.q small{opacity:.7}.note{font-size:12px;opacity:.75;margin-top:8px}
</style></head><body><main>
<section><h1>Chat</h1><div id="log" aria-live="polite"></div>
<div id="opts" class="opts"></div>
<form class="row" id="f"><input type="text" id="t" autocomplete="off" maxlength="2000" placeholder="Ask about a notice, a plan, your level... (/help)"><button>Send</button></form>
<div class="note">Local page: only this computer can open it. A chat estimates your level, it cannot certify it.</div></section>
<section><h1>Pages I could not open</h1><div id="pages"></div></section>
<section><h1>Questionnaire</h1><div id="form"></div><button class="alt" id="rf" type="button">Refresh</button></section>
</main><script>
const log=document.getElementById('log'),opts=document.getElementById('opts');
function add(c,t){const d=document.createElement('div');d.className='m '+c;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight}
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json()}
function showQ(q){opts.textContent='';if(!q||!q.options||!q.options.length)return;
 q.options.forEach(o=>{const b=document.createElement('button');b.type='button';b.className='alt';b.textContent=o;b.onclick=()=>send(o);opts.appendChild(b)});
 const s=document.createElement('button');s.type='button';s.className='alt';s.textContent='skip';s.onclick=()=>send('/skip');opts.appendChild(s)}
async function send(text){if(!text)return;add('me',text);const r=await api('/api/chat',{text});
 add('bot',r.reply||(r.refused?('(refused: '+r.reason+')'):'(no reply)'));showQ(r.question);loadForm()}
document.getElementById('f').onsubmit=e=>{e.preventDefault();const t=document.getElementById('t');const v=t.value.trim();t.value='';send(v)};
async function loadForm(){const qs=await api('/api/form'),box=document.getElementById('form');box.textContent='';
 qs.forEach(q=>{const d=document.createElement('div');d.className='q';
  const l=document.createElement('div');l.textContent=q.ask;d.appendChild(l);
  const cur=document.createElement('small');cur.textContent=q.current?('now: '+(q.current.level||q.current.value)+' ('+q.current.status+(q.current.pending?', proposed':'')+')'):'not answered';d.appendChild(cur);
  const row=document.createElement('div');row.className='row';row.style.display='flex';row.style.gap='6px';
  let inp;if(q.options.length){inp=document.createElement('select');q.options.forEach(o=>{const x=document.createElement('option');x.textContent=o;inp.appendChild(x)})}
  else{inp=document.createElement('input');inp.type='text'}
  row.appendChild(inp);let pr=null;
  if(q.kind==='level'&&q.probe){pr=document.createElement('input');pr.type='text';pr.placeholder=q.probe;pr.style.marginTop='4px'}
  const b=document.createElement('button');b.type='button';b.textContent='Save';
  b.onclick=async()=>{const r=await api('/api/answer',{key:q.key,answer:inp.value,probe:pr?pr.value:null});add('bot',r.message);loadForm()};
  row.appendChild(b);d.appendChild(row);if(pr)d.appendChild(pr);box.appendChild(d)})}
@@PAGESJS@@
cnbPages(document.getElementById('pages'));
document.getElementById('rf').onclick=loadForm;loadForm();add('bot','Hello. Ask me about a notice, what to do, a plan, or say /interview for questions. /help lists the commands.');
</script></body></html>"""


def make_handler(db_path, llm, session, activities_path=None, reports_dir=None, assist=None):
    root = os.path.realpath(reports_dir or os.path.join(DG.ROOT_DIR, "reports"))

    class H(BaseHTTPRequestHandler):
        server_version = "cnb-chat"
        timeout = 10                            # a stalled request (declared body never sent) cannot freeze the server

        def log_message(self, *a):              # no request log: bodies hold the student's words
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def do_GET(self):
            db = D.connect(db_path)
            try:
                self._get(db)
            finally:
                db.close()

        def do_POST(self):
            db = D.connect(db_path)
            try:
                self._post(db)
            finally:
                db.close()

        def _get(self, db):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):               # one program: the calendar is the front page, chat is inside it
                self.send_response(302); self.send_header("Location", "/r/calendar.html"); self.send_header("Content-Length", "0"); self.end_headers()
                return
            if path == "/chat":
                return self._send(200, PAGE.replace("@@PAGESJS@@", WG.PAGES_JS), "text/html; charset=utf-8")
            if path in ("/r", "/r/"):
                path = "/r/calendar.html"
            if path.startswith("/r/"):
                return self._report(path[3:])
            if path == "/api/form":
                return self._json(200, CV.form_state(db))
            if path == "/api/pages":                        # the requests waiting for the user (no page text, nothing is fetched)
                try:
                    import pypdf                            # noqa: F401
                    pdf = True
                except ImportError:
                    pdf = False
                return self._json(200, {"pages": MN.list_pages(db), "pdf": pdf})
            if path == "/api/profile":
                return self._json(200, P.snapshot_facts(db))
            self._json(404, {"error": "not found"})

        def _report(self, rel):
            """Serve a file of the reports folder (the report pages with the chat widget in them). Nothing outside it."""
            from urllib.parse import unquote
            rel = unquote(rel)
            full = os.path.realpath(os.path.join(root, rel))
            if "\x00" in rel or not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
                return self._json(404, {"error": "not found"})
            ext = os.path.splitext(full)[1].lower()
            if ext not in (".html", ".md", ".ics", ".txt"):
                return self._json(404, {"error": "not found"})
            data = open(full, "rb").read()
            if ext == ".html":
                data = WG.inject(data.decode("utf-8", "replace"), self.server.server_address[1]).encode("utf-8")
            ctype = "text/html; charset=utf-8" if ext == ".html" else "text/calendar; charset=utf-8" if ext == ".ics" else "text/plain; charset=utf-8"
            self._send(200, data, ctype)

        def _post(self, db):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1].split(":")[0].strip("[]").lower() not in ("127.0.0.1", "localhost"):
                return self._json(403, {"error": "bad origin"})
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._json(415, {"error": "json only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            cap = MAX_PROVIDE if self.path.split("?")[0] == "/api/page/provide" else MAX_BODY
            if n < 0 or n > cap:
                return self._json(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                return self._json(400, {"error": "bad json"})
            path = self.path.split("?")[0]
            if path == "/api/chat":
                r = CV.reply(db, body.get("text"), session, llm, channel="web", activities_path=activities_path, assist=assist)
                return self._json(200, {k: r.get(k) for k in ("reply", "kind", "refused", "reason", "question", "cites", "used_llm")})
            if path == "/api/item":                         # confirm a task: mark done / undo (writes only status + one history row)
                try:
                    iid, act = int(body.get("id")), str(body.get("action"))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad id"})
                if act not in ("done", "undo"):
                    return self._json(400, {"ok": False, "error": "bad action"})
                row = db.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()
                if not row or row["status"] in ("cancelled", "dismissed"):
                    return self._json(404, {"ok": False, "error": "no such open item"})
                new = "done" if act == "done" else "new"
                if row["status"] != new:
                    db.execute("UPDATE items SET status=? WHERE id=?", (new, iid))
                    db.execute("INSERT INTO item_history(item_id,ts,field,old,new,kind) VALUES(?,strftime('%s','now'),'status',?,?,'user')", (iid, row["status"], new))
                    db.commit()
                try:
                    DG.write_all(db, root)
                except Exception as e:                      # noqa: BLE001 - the change is saved; pages refresh on the next Refresh
                    return self._json(200, {"ok": True, "pages": type(e).__name__})
                return self._json(200, {"ok": True, "status": new})
            if path in ("/api/page/approve", "/api/page/skip", "/api/page/provide"):   # the USER decides; the server only records it
                try:
                    pid = int(body.get("id"))
                except (TypeError, ValueError, OverflowError):
                    return self._json(400, {"ok": False, "error": "bad id"})
                if isinstance(body.get("id"), bool) or not 0 < pid < 2**62:
                    return self._json(400, {"ok": False, "error": "bad id"})
                if path.endswith("approve"):
                    return self._json(200, MN.approve(db, pid))
                if path.endswith("skip"):
                    return self._json(200, MN.skip(db, pid))
                if isinstance(body.get("b64"), str):
                    r = MN.provide_b64(db, pid, body["b64"], str(body.get("filename") or ""))
                elif isinstance(body.get("text"), str):
                    r = MN.provide(db, pid, text=body["text"])
                else:
                    r = {"ok": False, "reason": "nothing was given"}
                return self._json(200, r)
            if path == "/api/refresh":                     # report generating, from inside the same program
                try:
                    DG.write_all(db, root)
                except Exception as e:                      # noqa: BLE001
                    return self._json(500, {"ok": False, "error": type(e).__name__})
                return self._json(200, {"ok": True})
            if path == "/api/answer":
                r = CV.submit_answer(db, str(body.get("key") or ""), body.get("answer"), body.get("probe"))
                if r["ok"]:
                    msg = f"Recorded {r['key']}: {r.get('level') or r.get('value')} ({r['status']}). A chat or form estimates a level, it does not certify it."
                else:
                    msg = f"Not recorded: {r.get('reason')}"
                return self._json(200, dict(r, message=msg))
            self._json(404, {"error": "not found"})
    return H


def make_server(db_path, llm=None, port=DEFAULT_PORT, host=LOOPBACK, session=None, activities_path=None, reports_dir=None, assist=None):
    """db_path: path of the SQLite file (None = the default tenant DB). HTTPServer bound to loopback only; port 0 = pick a free one. Raises ValueError for any other host."""
    if not bind_ok(host):
        raise ValueError("the chat page only binds to 127.0.0.1")
    return HTTPServer((host, port), make_handler(db_path, llm, session if session is not None else CV.new_session(), activities_path, reports_dir, assist))
