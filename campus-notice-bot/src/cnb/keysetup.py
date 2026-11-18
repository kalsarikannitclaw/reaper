"""First-run API-key onboarding (S2a). The key lives ONLY in the env file extract.ENV (stack/.env) under the variable
named by config.json key_env. It is read with getpass / stdin (never argv, so not in shell history or `ps`), never echoed,
never logged; every display is masked and every error text is redacted. Validation is one cheap GET /models call."""
import getpass, json, os, sys, tempfile, urllib.request, urllib.error
from . import extract
TIMEOUT = 15                                      # seconds for the one validation call

def _cfg():
    try: c = json.load(open(extract.CFG)) if os.path.exists(extract.CFG) else {}
    except ValueError: return {}
    return c if isinstance(c, dict) else {}       # checker fix: a non-object config.json crashed `cnb key`

def _assist(c):
    v = c.get("manual_page_assist", True)
    if isinstance(v, str): return v.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(v) if isinstance(v, (bool, int)) else True        # null/other = default (on), same as manual.assist_enabled

def key_name(): return _cfg().get("key_env", "OPENAI_API_KEY")

def mask(key):
    """Never reveals the key body: only its length and a 2-char tail for long keys."""
    if not key: return "(none)"
    return f"set ({len(key)} chars, ends ...{key[-2:]})" if len(key) >= 16 else f"set ({len(key)} chars)"

def redact(text, key):
    return str(text).replace(key, "<key>") if key else str(text)

def _lines(path):
    """Lines split on real newlines only. (str.splitlines also splits on \\x0c, \\x85, U+2028..., which broke other
    lines apart on rewrite; utf-8-sig so a Windows BOM does not hide the first line.)"""
    if not os.path.exists(path): return []
    l = open(path, encoding="utf-8-sig").read().split("\n")
    return l[:-1] if l and l[-1] == "" else l

def _write(path, lines):
    """Atomic rewrite (temp file in the same folder, then replace); mode 600; a symlinked env file keeps its link."""
    path = os.path.realpath(path); d = os.path.dirname(path); os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".env.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f: f.write("".join(l + "\n" for l in lines))
        os.chmod(tmp, 0o600); os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

def perm_note(path=None):
    """Honest note when the file system ignores chmod (e.g. a Windows drive under WSL shows 777)."""
    path = path or extract.ENV
    try: loose = os.name == "posix" and os.stat(path).st_mode & 0o077
    except OSError: return None
    return "note: this drive ignores file permissions (the env file is not 600 here); it is protected only by your Windows account." if loose else None

def read_key(path=None, name=None):
    path, name = path or extract.ENV, name or key_name()
    v = os.environ.get(name)
    if v: return v.strip()
    for l in _lines(path):
        if l.startswith(name + "="): return l.split("=", 1)[1].strip().strip("'\"") or None
    return None

def write_key(key, path=None, name=None):
    """Atomic replace of the one KEY=... line; other lines kept; file mode 600."""
    path, name = path or extract.ENV, name or key_name()
    key = (key or "").strip()
    # printable ASCII only: a key with a form feed / U+2028 / NEL was stored and then read back as two lines
    if not key or not all("!" <= c <= "~" for c in key): raise ValueError("that does not look like an API key (empty, or contains spaces / non-ASCII / control characters)")
    _write(path, [l for l in _lines(path) if not l.startswith(name + "=")] + [f"{name}={key}"])

def remove_key(path=None, name=None):
    path, name = path or extract.ENV, name or key_name()
    old = _lines(path); new = [l for l in old if not l.startswith(name + "=")]
    if len(new) == len(old): return False
    _write(path, new)                             # checker fix: was a truncate-then-write, a crash could empty stack/.env
    return True

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None       # urllib would re-send the Authorization header to the new host

def ping(key, base):
    """One cheap authenticated call. Returns (ok, message); the message never contains the key. Redirects are not followed."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=TIMEOUT) as r: return True, f"provider accepted the key (HTTP {r.status})"
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400: return False, f"provider answered with a redirect (HTTP {e.code}); not followed, key not verified. Check base_url in config.json"
        return False, f"provider rejected the key (HTTP {e.code})" if e.code in (401, 403) else f"provider error (HTTP {e.code})"
    except Exception as e: return False, redact(f"could not reach the provider ({type(e).__name__}); key not verified", key)

def status(out=print, path=None):
    c = _cfg(); name = key_name(); k = read_key(path, name)
    out(f"model    : {c.get('model') or '(not set in config.json)'}")
    out(f"base_url : {c.get('base_url') or '(not set in config.json)'}")
    out(f"key      : {name} {mask(k)}")
    out(f"pages    : manual_page_assist {'on' if _assist(c) else 'OFF'} (config.json). On: if a page cannot be opened by the bot (a site that blocks it), "
        "the model may ask you to open it yourself and give the text; nothing is opened without your approval.")
    if not k: out("No key yet: capture keeps working, extraction waits. Run: cnb key set")
    return bool(k)

def setup(read_secret=getpass.getpass, out=print, path=None, validate=ping, interactive_note=True):
    """Prompt for the key (hidden), validate, store. Returns True when a key is stored."""
    c = _cfg(); name = key_name(); path = path or extract.ENV
    if interactive_note:
        out("API key setup. Paste your key when asked; it is not shown and is stored only in the local env file.")
        out("Never paste the key into a QQ group. If you ever pasted it in any chat, delete that message and rotate the key.")
    try: key = (read_secret(f"{name}: ") or "").strip()
    except (EOFError, KeyboardInterrupt): key = ""    # no terminal, closed stdin or Ctrl-C: no traceback
    if not key: out("nothing entered; no change."); return False
    try: write_key(key, path, name)
    except ValueError as e: out(str(e)); return False
    ok, msg = validate(key, c.get("base_url") or "https://api.openai.com/v1")
    out(f"stored as {name} ({mask(key)}). " + redact(msg, key))
    if not ok: out("The key is saved anyway; replace it with `cnb key set` or remove it with `cnb key remove`.")
    n = perm_note(path)
    if n: out(n)
    return True

def run(args, out=print):
    """args.action in status|set|remove|test; args.stdin reads the key from stdin instead of a hidden prompt."""
    if args.action == "status": status(out)
    elif args.action == "set":
        rs = (lambda _p: sys.stdin.readline()) if getattr(args, "stdin", False) else getpass.getpass
        setup(read_secret=rs, out=out)
    elif args.action == "remove": out("key removed" if remove_key() else "no key was stored")
    elif args.action == "test":
        k = read_key()
        if not k: out("no key set. Run: cnb key set"); return
        out(redact(ping(k, _cfg().get("base_url") or "https://api.openai.com/v1")[1], k))
