"""HTML calendar (replaces QQ delivery, v1.1): month grid + agenda. Per entry: DDL, what to do, requirements, original link.
Self-contained single file (inline CSS+JS, no network). All text is rendered with textContent, never as HTML."""
import json
from . import contest as C
from .digest import is_manual, items, fmt, keywords, CSS, bridge_banner, by_state, STATES, state, notes, note_text, origin_of, notified, tz, tz_name, model_name
from . import scope as S
import datetime as dt, html as _html
_e = lambda x: _html.escape(str(x))

def _date(t): return (t or "")[:10] if t and t[:4].isdigit() else None
def _time(t): return t[11:16] if t and len(t) >= 16 and t[11:16] != "00:00" else ""

def entries(db):
    """Calendar entries for open, expired, done and cancelled items (S6b). Each carries state (+ word and symbol), scope,
    origin, history notes, so a cancelled event is never drawn as a normal one."""
    out = []; st = by_state(db); cfg = S.load(); tags = S.audience_tags(cfg=cfg); nt = notified(db)
    rows = [(i, k) for k in ("open", "expired", "past", "done", "cancelled") for i in st[k]]
    for i, stt in rows:
        srcs = db.execute("SELECT m.* FROM item_sources s JOIN msg m ON m.id=s.msg_id WHERE s.item_id=? ORDER BY m.id", (i["id"],)).fetchall()
        who = None
        if db.execute("SELECT name FROM sqlite_master WHERE name='admins'").fetchone():
            r = db.execute("SELECT nick FROM admins WHERE group_id=? AND user_id=?", (i["group_id"], i["sender"])).fetchone(); who = r and r["nick"]
        req = i["requirements"]
        try:
            j = json.loads(req) if req and req[:1] in "[{" else req
            req = "; ".join(f"{k}: {v}" for k, v in j.items()) if isinstance(j, dict) else ("; ".join(map(str, j)) if isinstance(j, list) else j)
        except ValueError: pass
        o = origin_of(db, i); sc = S.of_item(db, i, cfg, tags)
        extra = dict(state=stt, state_label=STATES[stt][0], state_icon=STATES[stt][1], scope=sc, scope_label=S.label(sc, cfg), alerted=bool(nt.get(i["id"])) and stt in ("open", "expired"),
                     origin=o and dict(id=o[0], title=o[1]), notes=[dict(kind=n["kind"], counts=n["counts"], text=note_text(n)) for n in notes(db, i, cfg, tags)])
        base = dict(**extra, topics=json.loads(i["topics"] or "[]"), id=i["id"], type=i["type"], title=PF(i["title"]), loc=i["location"], action=i["action"], req=req, pri=bool(i["priority"]), confirm=bool(i["needs_confirm"]),
                    quote=PF(i["quote"]), group=i["group_id"], who=who or i["sender"], manual=is_manual(i), at=fmt(srcs[0]["ts"]) if srcs else "", kw=keywords(i["quote"]), orig=[f"original/{s['id']}.html" for s in srcs], iid=i["id"])
        ddl = bool(i["due"]) and not i["start"] or i["type"] == "deadline"
        if i["type"] == "contest":
            d = C.describe(i, db); x = d["extra"]
            base.update(contest=dict(name=x.get("name"), rec=d["recognition"], org=x.get("organiser"), team=x.get("team_size"), fee=x.get("fee"), no=(d["entry"] or {}).get("no"), hints=d["hints"], fit=d["fit"]))
            rd = x.get("reg_deadline") or i["due"]
            out.append({**base, "kind": "ddl", "label": "Registration deadline", "date": _date(rd), "time": _time(rd)})
            if i["start"]: out.append({**base, "kind": "event", "label": "Contest", "date": _date(i["start"]), "time": _time(i["start"])})
        else:
            t = i["start"] or i["due"]
            e = i["end"]; et = _time(e) if e and _date(e) == _date(t) and _time(e) != _time(t) else ""
            out.append({**base, "kind": "ddl" if ddl else "event", "label": "Deadline" if ddl else "Event", "date": _date(t), "time": _time(t), "etime": et})
    return out

PAGE_JS = r"""
const D=JSON.parse(document.getElementById('data').textContent);
const $=(s,p=document)=>p.querySelector(s), el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
const linkify=(p,t)=>{String(t).split(/(https?:\/\/[^\s<>"']+)/).forEach((x,i)=>{if(i%2){const a=el('a','url',x);a.href=x;a.target='_blank';a.rel='noopener noreferrer';p.append(a)}else if(x)p.append(x)})};
const pad=n=>String(n).padStart(2,'0'), ymd=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
const today=D.now.slice(0,10);   // the tenant's today (server side), not the browser's
const zh=(n,t)=>{if(/[\u3400-\u9fff]/.test(String(t)))n.lang='zh-CN';return n};
const soonL=e=>{if(!e.date)return '';if(e.date===today)return 'today';const n=new Date(D.now),t=new Date(e.date+'T'+(e.time||'00:00')+':00'),h=(t-n)/36e5;return h>=0&&h<=48?'next 48h':''};
let topic=null;const vis=()=>D.entries.filter(e=>!topic||(e.topics||[]).includes(topic));
let dated=[],undated=[];const split=()=>{dated=vis().filter(e=>e.date);undated=vis().filter(e=>!e.date)};split();
const next=dated.map(e=>e.date).filter(d=>d>=today).sort()[0]||today; let sel=dated.some(e=>e.date===today)?today:next; let cur=new Date(sel+'T00:00:00'); cur.setDate(1);
function cnbBtn(e,c){if(e.state==='cancelled')return;const d=e.state==='done',b=el('button','cnb-done',d?'↩ Not done after all':'✔ Mark done');b.type='button';b.dataset.id=e.id;b.dataset.act=d?'undo':'done';c.append(b)}
function card(e,anchor){const sl=e.state==='open'?soonL(e):'',c=el('div','card'+(e.kind==='ddl'?' ddl':'')+(sl?' soon':'')+(sl==='today'?' istoday':'')+' st-'+e.state);if(anchor)c.id='item-'+e.id+(e.kind==='ddl'&&e.contest?'-ddl':'');
 const md=e.date?(+e.date.slice(5,7))+'.'+(+e.date.slice(8,10)):'date ?';
 const row=el('div','row');row.append(el('span','c when',md+(e.time?' '+e.time+(e.etime?'–'+e.etime:''):'')));
 const wt=el('span','c what');if(e.state!=='open')wt.append(el('span','tag st st-'+e.state,e.state_icon+' '+e.state_label));wt.append(el('span','tag '+e.kind,e.label));if(sl)wt.append(el('span','tag soon',sl));wt.append(' ');const tt=el('span','tt',e.contest&&e.contest.name||e.title);zh(tt,tt.textContent);wt.append(tt);row.append(wt);
 const wr=el('span','c where');if(e.loc){linkify(wr,e.loc);zh(wr,e.loc)}else wr.textContent='—';row.append(wr);c.append(row);
 const h=el('div','t');(e.topics||[]).forEach(t=>h.append(el('span','tag top','# '+t)));if(e.pri)h.append(el('span','tag pri','priority'));h.append(el('span','tag sc sc-'+e.scope,e.scope_label));if(e.alerted)h.append(el('span','alerted','alerted, not done yet'));if(e.confirm)h.append(el('span','warn','confirm time'));if(h.childNodes.length)c.append(h);
 if(e.origin){const o=el('div','orig','Part of: ');if(e.origin.id&&D.entries.some(x=>x.id===e.origin.id)){const a=el('a',null,e.origin.title);a.href='#item-'+e.origin.id;o.append(a)}else o.append(e.origin.title);zh(o,e.origin.title);c.append(o)}
 (e.notes||[]).forEach(n=>c.append(zh(el('div','note '+(n.kind==='change'&&n.counts?'chg':'off'),(n.kind==='change'?'↻ ':'· ')+n.text),n.text)));
 if(e.action){const a=el('div','act');a.append(el('b',null,'To do: '));a.append(e.action);c.append(a)}
 if(e.req){const a=el('div','req');a.append(el('b',null,'Requirements: '));a.append(e.req);c.append(a)}
 if(e.contest){const k=e.contest,b=[];b.push(k.rec==='yes'?'on school list'+(k.no?' (#'+k.no+')':''):k.rec==='no'?'NOT on school list':'school list not loaded');if(k.org)b.push('organiser: '+k.org);if(k.team)b.push('team: '+k.team);if(k.fee)b.push('fee: '+k.fee);c.append(el('div','meta',b.join(' · ')));k.hints.forEach(x=>c.append(el('div','meta','⚠ '+x)));
 const F=k.fit||{};if(F.assessed){c.append(el('div','fitband','fit: '+F.band));(F.reasons||[]).forEach(x=>c.append(el('div','meta ok','✓ '+x)));(F.gaps||[]).forEach(x=>c.append(el('div','meta gap','✗ '+x)))}
 if(F.note)c.append(el('div','meta note',F.note))}
 const q=el('div','q');linkify(q,e.quote);zh(q,e.quote);c.append(q);
 const m=el('div','meta',e.manual?'From a page you gave me (not verified by the bot) · '+e.at:'group '+e.group+' · '+e.at+' · '+e.who+' · QQ search: ');if(!e.manual)m.append(el('code',null,e.kw));e.orig.forEach((o,i)=>{const a=el('a',null,e.orig.length>1?'original '+(i+1):'original');a.href=o;m.append(' · ');m.append(a)});c.append(m);cnbBtn(e,c);return c}
function agenda(){const box=$('#agenda');box.textContent='';const hd=el('div','row hd');['When','What','Where'].forEach(x=>hd.append(el('span','c'+(x==='Where'?' where':''),x)));box.append(hd);
 const up=dated.filter(e=>e.date>=today).sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time));
 const late=dated.filter(e=>e.date<today).sort((a,b)=>b.date.localeCompare(a.date));
 let last='';if(!up.length)box.append(el('p','sub empty','Nothing coming up. Dated items will appear here.'));
 up.forEach(e=>{if(e.date!==last){last=e.date;const d=Math.round((new Date(e.date)-new Date(today))/864e5);box.append(el('h3',d<=1?'dh soon':'dh',e.date+'  ·  '+(d===0?'today':d===1?'tomorrow':'in '+d+' days')))}box.append(card(e,1))});
 if(late.length){box.append(el('h2',null,'Past dates (expired tasks crossed out, past events and done items grey)'));late.forEach(e=>box.append(card(e,1)))}
 const live=e=>e.state==='open'||e.state==='expired',follow=undated.filter(e=>e.type==='interest'&&live(e)),conf=undated.filter(e=>e.type!=='interest'&&live(e)),closed=undated.filter(e=>!live(e));
 if(follow.length){box.append(el('h2',null,'Topics you follow'));follow.forEach(e=>box.append(card(e,1)))}
 if(conf.length){box.append(el('h2',null,'No date yet: please confirm'));conf.forEach(e=>box.append(card(e,1)))}
 if(closed.length){box.append(el('h2',null,'Done or cancelled, no date'));closed.forEach(e=>box.append(card(e,1)))}
 if(!dated.length&&!undated.length)box.append(el('p','sub empty',topic?'No items under this topic.':'No open items yet.'));
 if(D.spam.length){const d=el('details');d.append(el('summary',null,'Filtered out as spam ('+D.spam.length+'): check nothing real is here'));D.spam.forEach(s=>{const c=el('div','card');c.append(el('div','meta',s.at+' · group '+s.group+' · '+s.who+(s.role&&s.role!=='member'?' ('+s.role+')':'')+' · '+s.reason));c.append(el('div','q',s.text));d.append(c)});box.append(d)}}
function chips(){const b=$('#chips');b.textContent='';if(!D.topics.length)return;[null,...D.topics].forEach(t=>{const x=el('button','chip'+(t===topic?' on':''),t||'All topics');x.setAttribute('aria-pressed',t===topic?'true':'false');x.onclick=()=>{topic=t;split();chips();month();day();agenda();const o=$('#chips .chip.on');if(o)o.focus()};b.append(x)})}
function month(){const g=$('#grid');g.textContent='';$('#mt').textContent=cur.toLocaleString('en',{month:'long',year:'numeric'});
 ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach(d=>g.append(el('div','dow',d)));
 const first=new Date(cur.getFullYear(),cur.getMonth(),1),off=(first.getDay()+6)%7,n=new Date(cur.getFullYear(),cur.getMonth()+1,0).getDate();
 for(let i=0;i<off;i++)g.append(el('div','day empty'));
 for(let d=1;d<=n;d++){const k=cur.getFullYear()+'-'+pad(cur.getMonth()+1)+'-'+pad(d),es=dated.filter(e=>e.date===k);
  const c=el('button','day'+(k===today?' today':'')+(k===sel?' sel':''),null);c.append(el('span','n',d));
  if(es.length){const s=el('span','dots');es.slice(0,4).forEach(e=>s.append(el('i',e.kind+' '+e.state)));if(es.length>4)s.append(el('em',null,'+'));c.append(s)}
  c.setAttribute('aria-label',k+(es.length?', '+es.length+' item(s)':', nothing'));if(k===sel)c.setAttribute('aria-current','date');c.onclick=()=>{sel=k;month();day();const o=$('#grid .day.sel');if(o)o.focus()};g.append(c)}}
function day(){const b=$('#day');b.textContent='';b.append(el('h3',sel===today?'dh soon':'dh',sel+(sel===today?'  ·  today':'')));const es=dated.filter(e=>e.date===sel);if(!es.length)b.append(el('p','sub empty','Nothing on this day.'));es.forEach(e=>b.append(card(e)))}
$('#prev').onclick=()=>{cur.setMonth(cur.getMonth()-1);month()};$('#next').onclick=()=>{cur.setMonth(cur.getMonth()+1);month()};$('#prev').setAttribute('aria-label','Previous month');$('#next').setAttribute('aria-label','Next month');$('#tod').onclick=()=>{cur=new Date(today+'T00:00:00');cur.setDate(1);sel=today;month();day()};
chips();month();day();agenda();
"""
CAL_CSS = """.row{display:grid;grid-template-columns:7.5rem minmax(0,1fr) 10rem;gap:12px;align-items:baseline}.row .when{font-weight:700;font-variant-numeric:tabular-nums}.row .where{color:var(--mut)}.row.hd{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);border-bottom:1px solid var(--line);padding-bottom:4px;margin-bottom:8px}@media(max-width:640px){.row{grid-template-columns:7.5rem 1fr}.row .where{grid-column:2}.row.hd .where{display:none}}@font-face{font-family:"CalCN";src:local("Microsoft YaHei"),local("微软雅黑"),local("PingFang SC"),local("Noto Sans CJK SC");unicode-range:U+2E80-9FFF,U+3000-303F,U+FF00-FFEF}body{font-family:"CalCN",system-ui,sans-serif;font-size:18px}main{max-width:1240px;padding:28px 28px 80px}h1{font-size:1.9rem}h2{font-size:1.25rem}h3{font-size:1rem}.card{padding:16px 18px;margin:12px 0}.cols{display:block}
@media(min-width:960px){.cols{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:40px;align-items:start}.cols>section:first-child{position:sticky;top:16px}.cols>section:last-child>h2:first-child{margin-top:12px}}
button{font-size:1rem;padding:6px 16px}.nav b{font-size:1.35rem}.dow{font-size:.9rem}.day{font-size:1.1rem}.dots i{width:10px;height:10px}.tag{font-size:.8rem}.meta,.sub{font-size:.95rem}.nav{display:flex;gap:8px;align-items:center;margin:12px 0}.nav b{flex:1;font-size:1.1rem}button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 12px;cursor:pointer}
#grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}.dow{color:var(--mut);font-size:.75rem;text-align:center}.day{height:78px;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:4px 2px;gap:4px}
.day.empty{border:0;background:none;cursor:default;visibility:hidden}.day.today .n{background:var(--acc);color:var(--bg);border-radius:50%;padding:0 6px}.day.sel{outline:2px solid var(--acc)}.dots{display:flex;gap:3px;flex-wrap:wrap;justify-content:center}
.dots i{width:8px;height:8px;border-radius:50%;background:var(--mut)}.dots i.ddl{background:#dc2626}.dots i.event{background:#2563eb}.dots em{font-size:.7rem;color:var(--mut);font-style:normal}
.tag{font-size:.72rem;border-radius:4px;padding:1px 6px;margin-right:6px;background:var(--line);color:var(--fg)}.tag.ddl{background:#dc2626;color:#fff}.tag.event{background:#2563eb;color:#fff}.tag.pri{background:var(--hi);color:var(--fg);margin-left:6px}
.card.ddl{border-left:4px solid #dc2626}.act,.req{margin-top:4px}.card a.url{word-break:break-all}.tag.top{background:var(--hi);color:var(--fg)}#chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.chip.on{background:var(--acc);color:var(--bg)}.fitband{margin-top:6px;font-weight:600}.meta.ok{color:#15803d}.meta.gap{color:#b45309}@media(prefers-color-scheme:dark){.meta.ok{color:#4ade80}.meta.gap{color:#fbbf24}}.meta.note{font-style:italic;opacity:.8}details{margin-top:24px}summary{cursor:pointer;color:var(--mut)}.sub{color:var(--mut)}code{background:var(--line);padding:0 4px;border-radius:3px}
/* S6b states, scope, fonts, print */
:root{--grey:#57534e}@media(prefers-color-scheme:dark){:root{--grey:#a8a29e}}
body{font-family:"CalCN","Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei UI","Noto Sans CJK SC","Noto Sans SC","Source Han Sans SC","WenQuanYi Micro Hei",system-ui,sans-serif}
@font-face{font-family:"CalCN";src:local("Microsoft YaHei UI"),local("Hiragino Sans GB"),local("Source Han Sans SC"),local("WenQuanYi Micro Hei");unicode-range:U+2E80-9FFF,U+3000-303F,U+FF00-FFEF}
.tag.st{background:transparent;color:var(--fg);border:1px solid currentColor;font-weight:700}.tag.sc{background:transparent;color:var(--mut);border:1px solid var(--line)}.tag.sc-unknown{border-style:dashed}.tag.sc-institute,.tag.sc-school{color:var(--fg)}
.card.st-expired{border-left:5px solid var(--red)}.card.st-expired .tt,.card.st-cancelled .tt{text-decoration:line-through;text-decoration-thickness:2px;text-decoration-skip-ink:none}.card.st-expired .tt{text-decoration-color:var(--red)}.card.st-cancelled .tt{text-decoration-color:var(--fg)}.card.st-past,.card.st-past .when,.card.st-past .q,.card.st-past .meta{color:var(--grey)}.tag.st-past{border-style:dotted}.st-expired .tag.st{color:var(--red)}
.card.st-done{background:transparent}.card.st-done,.card.st-done .when,.card.st-done .q,.card.st-done .meta{color:var(--grey)}
.card.st-cancelled{border:2px dashed var(--grey);background:transparent;box-shadow:none}.card.st-cancelled,.card.st-cancelled .when,.card.st-cancelled .q{color:var(--grey)}
.dots i.cancelled,.dots i.done,.dots i.past{background:transparent;border:2px solid var(--grey);box-sizing:border-box}.dots i.expired{box-shadow:0 0 0 2px var(--bg),0 0 0 3px var(--red)}
.alerted{color:var(--red);font-size:.85rem;font-weight:600;margin-left:6px}.note{font-size:.92rem;margin-top:4px}.note.chg{font-weight:600}.note.off{color:var(--mut)}.orig{font-size:.92rem;margin-top:4px}
/* S6 aesthetics */
:root{color-scheme:light dark;--red:#b91c1c;--blue:#1d4ed8;--soon:#fff7f5;--cp:19px}
@media(prefers-color-scheme:dark){:root{--red:#ef4444;--blue:#60a5fa;--soon:#2a1c1a}}
body{font-family:"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC",system-ui,sans-serif;line-height:1.6;font-size:17px}
main{padding:24px 16px 80px}@media(min-width:700px){main{padding:28px 28px 80px}}
.card{border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.05)}.card .row{margin-bottom:2px}.row .what{font-weight:600;line-height:1.4}.row .when{color:var(--fg);font-size:1.05rem;white-space:nowrap}.row.hd{padding:0 var(--cp)}.meta code{color:#57534e}.bad code{background:rgba(0,0,0,.3);color:#fff}.dots em{color:var(--fg)}.meta.note{opacity:1}.day:focus-visible{outline:3px solid var(--acc);outline-offset:2px}@media(prefers-color-scheme:dark){.meta code{color:var(--fg)}}
.card .t{margin-top:6px;font-weight:400}.card .q{font-size:.95rem}.card .act,.card .req{font-size:.97rem}
.tag.ddl,.dots i.ddl{background:var(--red)}.tag.event,.dots i.event{background:var(--blue)}.card.ddl{border-left-color:var(--red)}
@media(prefers-color-scheme:dark){.tag.ddl,.tag.event{color:#0b0b0b}}
.tag.soon{background:var(--acc);color:var(--bg);margin-left:0}
.card.soon{background:var(--soon);box-shadow:0 0 0 1px var(--acc)}.card.istoday{box-shadow:0 0 0 2px var(--acc)}
h3.dh{border-bottom:1px solid var(--line);padding-bottom:4px}h3.dh.soon{color:var(--acc);font-weight:700}
.sub.empty{padding:14px 16px;border:1px dashed var(--line);border-radius:10px;text-align:center}
.day{min-height:64px;height:auto;aspect-ratio:1/1.05;border-radius:8px}.day.today{border-color:var(--acc)}.day:hover{background:var(--hi)}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
button{min-height:40px}.chip{border-radius:999px}
@media(max-width:640px){body{font-size:16px}h1{font-size:1.5rem}.day{min-height:48px;aspect-ratio:auto;height:54px}.dots i{width:7px;height:7px}.nav b{font-size:1.05rem;text-align:center}.card{padding:12px 12px}:root{--cp:13px}}
@media print{:root{--red:#000;--grey:#333}.card .tt{text-decoration-color:#000!important}:root{--bg:#fff;--fg:#000;--mut:#444;--card:#fff;--line:#bbb;--acc:#000;--hi:#eee;--soon:#fff}body{font-size:12pt}main{max-width:none;padding:0}.nav,#chips,#grid,.dots,summary,button,#day,.tag.soon{display:none!important}.bad{background:none;color:#000;border:2px solid #000}.card .q{max-height:none!important;overflow:visible!important}.cols{display:block!important}.cols>section:first-child{position:static}.card{break-inside:avoid;box-shadow:none!important}details:not([open])>*:not(summary){display:none}.card a.url::after{content:""}}
"""

def filtered(db, n=40):
    return [dict(at=fmt(m["ts"]), group=m["group_id"], who=m["nick"] or m["user_id"], role=m["role"], reason=m["spam_reason"], text=(m["text"] or "")[:300], orig=None)
            for m in db.execute("SELECT * FROM msg WHERE spam=1 ORDER BY ts DESC LIMIT ?", (n,))]

def held_banner(db):
    from . import review
    try: n = review.count(db)
    except Exception: return ""
    return (f'<p class="bad"><b>{n} post(s) from priority members matched no keyword.</b> Open NapCat with <code>napcat-ui</code> '
            f'(or run <code>cnb review</code>) to choose which, if any, become tasks.</p>') if n else ""

from .periods import fix as PF      # display-only: 910节 -> 9-10节

def html(db):
    from . import filters as F
    data = json.dumps({"entries": entries(db), "spam": filtered(db), "topics": [t["name"] for t in F.load_interests()], "now": dt.datetime.now(tz()).strftime("%Y-%m-%dT%H:%M")}, ensure_ascii=False).replace("<", "\\u003c")
    n = sum(1 for _ in items(db))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Campus calendar</title><style>{CSS}{CAL_CSS}</style></head><body><main>'
            f'<h1>Campus calendar</h1><p class="sub">{n} open item(s) · red = deadline, blue = event · ! expired task, ◷ past event, ✔ done, ✖ cancelled (words on every card) · times in {_e(tz_name())} · model {_e(model_name())} · <a href="latest.html">list view</a></p>'
            + bridge_banner() + held_banner(db)
            + '<div id="chips"></div><div class="cols"><section><div class="nav"><button id="prev">◀</button><b id="mt"></b><button id="tod">Today</button><button id="next">▶</button></div><div id="grid"></div><div id="day"></div></section>'
            '<section><h2>Coming up</h2><div id="agenda"></div></section></div>'
            f'</main><script id="data" type="application/json">{data}</script><script>{PAGE_JS}</script></body></html>')
