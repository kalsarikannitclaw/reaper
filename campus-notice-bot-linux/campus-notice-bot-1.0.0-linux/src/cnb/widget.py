"""Chat widget that lives INSIDE the report pages (stage S10b). One self-contained snippet: no external assets.

inject(html) puts a floating "Ask the bot" button + chat panel before </body>. It is added when the report is written
(so latest.html / calendar.html / weekly pages carry it) and again, if missing, when the local chat server serves a page.

Where it works: when the page is opened through the local chat server (python tools/talk.py, then
http://127.0.0.1:8790/r/latest.html) it talks to /api/chat on the same origin. Opened straight from disk (file://) it
cannot reach the server on its own (the server refuses other origins on purpose), so the panel says so and gives the
address to open instead. All text is shown with textContent, never as HTML. Nothing leaves this computer except the
AI call the server makes with the user's own key.
"""
PAGES_JS = r"""window.cnbPages=function(box,onCount){
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function post(p,b){return fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(function(r){return r.json()})}
function b64(buf){var s='',a=new Uint8Array(buf),i;for(i=0;i<a.length;i+=8192)s+=String.fromCharCode.apply(null,a.subarray(i,i+8192));return btoa(s)}
function day(t){return new Date(t*1000).toISOString().slice(0,10)}
function load(){return fetch('/api/pages').then(function(r){return r.json()}).then(render).catch(function(){box.textContent='(no answer from the local program: open this page through http://127.0.0.1:8790/)'})}
function render(d){box.textContent='';var rows=d.pages||[];if(onCount)onCount(rows.length);
 box.appendChild(el('div','cnbp-note','Pages the bot could not open itself. The bot never opens them: you decide whether and when a page is shown. What you give back is kept as a copy you supplied; the bot cannot know it is genuine.'));
 if(!rows.length)box.appendChild(el('div','cnbp-note','Nothing is waiting.'));
 rows.forEach(function(r){var c=el('div','cnbp-card');
  c.appendChild(el('div','cnbp-url',r.url));if(r.why)c.appendChild(el('div','cnbp-why','Why: '+r.why));
  var row=el('div','cnbp-row'),msg=el('div','cnbp-msg','');
  if(r.status==='pending'){
   var a=el('a','cnbp-btn','Approve: I will open it myself');
   if(/^https?:\/\//.test(r.url)){a.setAttribute('href',r.url);a.setAttribute('target','_blank');a.setAttribute('rel','noopener noreferrer')}
   a.onclick=function(){post('/api/page/approve',{id:r.id}).then(load)};row.appendChild(a);
   var s=el('button','cnbp-btn','Skip');s.type='button';s.onclick=function(){post('/api/page/skip',{id:r.id}).then(load)};row.appendChild(s);
   c.appendChild(row);
  }else{
   c.appendChild(el('div','cnbp-why','Approved. Open the page in your own browser, then give me what you see:'));
   if(/^https?:\/\//.test(r.url)){var a2=el('a','cnbp-btn','Open the address again');a2.setAttribute('href',r.url);a2.setAttribute('target','_blank');a2.setAttribute('rel','noopener noreferrer');c.appendChild(a2)}
   var ta=el('textarea','cnbp-ta');ta.rows=4;ta.placeholder='Paste the page text here';ta.maxLength=400000;c.appendChild(ta);
   var g=el('button','cnbp-btn','Give me this text');g.type='button';
   g.onclick=function(){if(!ta.value.trim()){msg.textContent='Paste some text first.';return}msg.textContent='Sending...';
    post('/api/page/provide',{id:r.id,text:ta.value}).then(function(x){if(x.ok){load()}else msg.textContent='Not stored: '+(x.reason||x.error||'unknown')}).catch(function(){msg.textContent='Not stored: no answer.'})};
   row.appendChild(g);
   var fi=el('input');fi.type='file';fi.accept='.html,.htm,.txt,.pdf';row.appendChild(fi);
   var fb=el('button','cnbp-btn','Give me this file (max 2 MB)');fb.type='button';
   fb.onclick=function(){var f=fi.files&&fi.files[0];if(!f){msg.textContent='Pick a saved file first (.html, .htm, .txt, .pdf).';return}
    if(f.size>2097152){msg.textContent='That file is over 2 MB.';return}
    if(!/\.(html?|txt|pdf)$/i.test(f.name)){msg.textContent='Only .html, .htm, .txt and .pdf files.';return}
    var rd=new FileReader();rd.onload=function(){msg.textContent='Sending...';post('/api/page/provide',{id:r.id,filename:f.name,b64:b64(rd.result)}).then(function(x){if(x.ok){load()}else msg.textContent='Not stored: '+(x.reason||x.error||'unknown')}).catch(function(){msg.textContent='Not stored: no answer.'})};rd.readAsArrayBuffer(f)};
   row.appendChild(fb);if(d.pdf===false)c.appendChild(el('div','cnbp-why','PDF files need the optional package pypdf, which is not installed here: paste the text instead.'));
   c.appendChild(row);var s2=el('button','cnbp-btn','Skip for 30 days');s2.type='button';s2.onclick=function(){post('/api/page/skip',{id:r.id}).then(load)};c.appendChild(s2);
  }
  c.appendChild(msg);box.appendChild(c)})}
load();return load};
"""

MARK = "cnb-widget"
PORT = 8790

SNIPPET = """<!--cnb-widget--><style>
#cnbw-b{position:fixed;right:16px;bottom:16px;z-index:9999;padding:10px 14px;border:0;border-radius:22px;background:#2456b8;color:#fff;font:600 14px system-ui,"Microsoft YaHei",sans-serif;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.3)}
#cnbw-p{position:fixed;right:16px;bottom:64px;z-index:9999;width:min(380px,calc(100vw - 32px));height:min(480px,70vh);display:none;flex-direction:column;background:#fff;color:#1d2330;border:1px solid #c9cfda;border-radius:10px;box-shadow:0 4px 18px rgba(0,0,0,.3);font:14px/1.45 system-ui,"Microsoft YaHei",sans-serif}
#cnbw-p.open{display:flex}
@media (prefers-color-scheme:dark){#cnbw-p{background:#1d222b;color:#e8ebf0;border-color:#323947}}
#cnbw-l{flex:1;overflow:auto;padding:8px;display:flex;flex-direction:column;gap:6px}
#cnbw-l div{padding:6px 9px;border-radius:8px;white-space:pre-wrap;border:1px solid #c9cfda;max-width:92%}
#cnbw-l .me{align-self:flex-end;background:#dbeafe;color:#1d2330}
#cnbw-f{display:flex;gap:6px;padding:8px;border-top:1px solid #c9cfda}
#cnbw-f input{flex:1;min-width:0;padding:7px;border:1px solid #c9cfda;border-radius:6px;font:inherit}
#cnbw-f button{padding:7px 11px;border:0;border-radius:6px;background:#2456b8;color:#fff;cursor:pointer}
#cnbw-o{display:flex;gap:5px;flex-wrap:wrap;padding:0 8px}#cnbw-o button{padding:3px 8px;border:1px solid #2456b8;background:transparent;color:#2456b8;border-radius:12px;cursor:pointer}
#cnbw-h{padding:6px 8px;font-size:12px;border-bottom:1px solid #c9cfda}#cnbw-h a{color:#2456b8}
html.cnb-live .cnb-done{display:inline-block}.cnb-done{display:none;margin:6px 0 0;padding:4px 10px;border:1px solid #2456b8;background:transparent;color:#2456b8;border-radius:6px;cursor:pointer;font:inherit;font-size:13px}.cnb-done:disabled{opacity:.5}
#cnbw-n{font-size:11px;opacity:.7;padding:0 8px 6px}
#cnbw-pg{display:none;flex:1;overflow:auto;padding:8px}#cnbw-p.pages #cnbw-pg{display:block}#cnbw-p.pages #cnbw-l,#cnbw-p.pages #cnbw-o,#cnbw-p.pages #cnbw-f,#cnbw-p.pages #cnbw-n{display:none}
.cnbp-card{border:1px solid #c9cfda;border-radius:8px;padding:8px;margin:6px 0}.cnbp-url{word-break:break-all;font-family:ui-monospace,Consolas,monospace;font-size:12px}.cnbp-why,.cnbp-note,.cnbp-msg{font-size:12px;opacity:.85;margin:4px 0}
.cnbp-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:4px 0}.cnbp-btn{display:inline-block;padding:4px 10px;border:1px solid #2456b8;background:transparent;color:#2456b8;border-radius:6px;cursor:pointer;font:inherit;font-size:13px;text-decoration:none}.cnbp-ta{width:100%;box-sizing:border-box;font:inherit}
</style>
<button id="cnbw-b" type="button" aria-expanded="false">Ask the bot</button>
<div id="cnbw-p" role="dialog" aria-label="Chat with the Campus Notice Bot"><div id="cnbw-h"><a href="/r/calendar.html">Calendar</a> · <a href="/r/latest.html">By chat group</a> · <a href="/chat">Questionnaire</a> · <a href="#" id="cnbw-r">Refresh report</a> · <a href="#" id="cnbw-pgl">Pages I could not open</a></div><div id="cnbw-pg" aria-live="polite"></div><div id="cnbw-l" aria-live="polite"></div><div id="cnbw-o"></div>
<form id="cnbw-f"><input id="cnbw-t" type="text" maxlength="2000" autocomplete="off" placeholder="Ask about a notice, a plan, your level..."><button>Send</button></form>
<div id="cnbw-n">Local only. A chat estimates your level, it cannot certify it. Check dates, places and fees in the original notice.</div></div>
<script>(function(){
var b=document.getElementById('cnbw-b'),p=document.getElementById('cnbw-p'),l=document.getElementById('cnbw-l'),o=document.getElementById('cnbw-o'),f=document.getElementById('cnbw-f'),t=document.getElementById('cnbw-t');
var live=/^https?:$/.test(location.protocol)&&/^(127\\.0\\.0\\.1|localhost)$/.test(location.hostname),started=false;
if(live)document.documentElement.classList.add('cnb-live');
%PAGESJS%
var pgl=document.getElementById('cnbw-pgl'),pgbox=document.getElementById('cnbw-pg'),pgload=null;
function pgcount(n){pgl.textContent=n?'Pages I could not open ('+n+')':'Pages I could not open';b.textContent=n?'Ask the bot ('+n+')':'Ask the bot'}
if(live)pgload=window.cnbPages(pgbox,pgcount);
pgl.onclick=function(e){e.preventDefault();if(!live){p.classList.add('open');p.classList.remove('pages');add('bot','Open this report through http://127.0.0.1:%PORT%/r/calendar.html to handle page requests.');return}
 var on=!p.classList.contains('pages');p.classList.toggle('pages',on);if(on)pgload()};
document.addEventListener('click',function(ev){var t=ev.target;if(!t||!t.classList||!t.classList.contains('cnb-done'))return;t.disabled=true;
 fetch('/api/item',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:+t.dataset.id,action:t.dataset.act})}).then(function(r){return r.json()}).then(function(r){if(r.ok)location.reload();else{t.disabled=false;alert('Not saved: '+(r.error||'unknown'))}}).catch(function(){t.disabled=false;alert('Not saved: no answer from the local program.')})});
function add(c,x){var d=document.createElement('div');d.className=c;d.textContent=x;l.appendChild(d);l.scrollTop=l.scrollHeight}
function opts(q){o.textContent='';if(!q||!q.options||!q.options.length)return;q.options.concat(['/skip']).forEach(function(x){var k=document.createElement('button');k.type='button';k.textContent=x;k.onclick=function(){send(x)};o.appendChild(k)})}
function send(x){if(!x)return;add('me',x);
 fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:x})}).then(function(r){return r.json()}).then(function(r){
  add('bot',r.reply||(r.refused?'(refused: '+r.reason+')':'(no reply)'));opts(r.question)}).catch(function(){add('bot','(the chat server did not answer. Is "python tools/talk.py" still running?)')})}
b.onclick=function(){var open=!p.classList.contains('open');p.classList.toggle('open',open);b.setAttribute('aria-expanded',open);
 if(open&&!started){started=true;add('bot',live?'Hello. Ask about a notice, what to do, or say /interview for questions. /help lists commands.':'This page was opened from a file, so it cannot reach the bot. Start it with: python tools/talk.py (or run Chat with the bot), then open http://127.0.0.1:%PORT%/r/calendar.html . The notices below still work without it.')}
 if(open)t.focus()};
document.getElementById('cnbw-r').onclick=function(e){e.preventDefault();if(!live){add('bot','Open this report through http://127.0.0.1:%PORT%/r/calendar.html to refresh it.');return}
 add('bot','Refreshing the report...');fetch('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(r){return r.json()}).then(function(r){if(r.ok)location.reload();else add('bot','Refresh failed ('+r.error+').')}).catch(function(){add('bot','Refresh failed: no answer from the server.')})};
f.onsubmit=function(e){e.preventDefault();var v=t.value.trim();t.value='';if(!live){add('bot','Not connected: open this report through http://127.0.0.1:%PORT%/r/calendar.html');return}send(v)};
})();</script><!--/cnb-widget-->"""


def snippet(port=PORT):
    return SNIPPET.replace("%PAGESJS%", PAGES_JS).replace("%PORT%", str(int(port)))


def inject(html, port=PORT):
    """Add the widget once, just before </body> (appended when there is no </body>). Idempotent."""
    if MARK in html:
        return html
    s = snippet(port)
    i = html.lower().rfind("</body>")
    return html + s if i < 0 else html[:i] + s + html[i:]
