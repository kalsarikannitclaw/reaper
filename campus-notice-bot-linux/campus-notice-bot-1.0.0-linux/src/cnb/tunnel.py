"""Fetch route selection (S5 round 3). Decides, per host, HOW web.fetch reaches a page:

  direct            plain urllib, the system's own network path (the default, unchanged behaviour)
  direct + family   the same, but only IPv4 or only IPv6 (a forced address family)
  relay             through tunnel/relay.py, a small local forwarder run on the user's Windows side
                    (or any other machine), which fetches with a forced family and returns the body

Configured in research_sources.json -> "tunnel" (see tunnel/README.txt):
  {"relay": "http://127.0.0.1:8787" | "auto",   where the relay listens ("auto": 127.0.0.1, then the
                                                  Windows host seen from WSL2 NAT mode)
   "family": "auto" | "4" | "6",                default family for every route
   "routes": [{"match": "college.example-school.edu.cn", "via": "relay", "family": "4"}, ...]}
The most specific matching route wins (match = the host itself or a parent domain). No matching route
= direct, family auto. Works the same on Windows and Linux: stdlib only, pathlib/os.path, no shell.
The relay is local and credential-free; nothing here talks to a third-party service.
"""
import http.client, importlib.util, io, json, os, re, socket, unicodedata, urllib.error, urllib.parse, urllib.request
from . import db as D

CONFIG_PATH = os.path.join(D.ROOT, "research_sources.json")
RELAY_PY = os.path.join(D.ROOT, "tunnel", "relay.py")
_override = None
_relay_mod = None
_auto_cache = {}


class TunnelError(OSError):
    """The relay (or the forced-family connection) could not deliver the page."""


def set_config(cfg):
    """Tests / callers: use this config dict instead of the file (None = back to the file)."""
    global _override
    _override = cfg
    _auto_cache.clear()


def _cfg():
    if _override is not None:
        return _override
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def config():
    return _cfg().get("tunnel") or {}


DEFAULT_BLOCKED = ["college.example-school.edu.cn"]


def blocked_hosts():
    """research_sources.json -> blocked_hosts (a list of hosts/domains, subdomains included). Absent
    key = the built-in default (the user-only college site), so deleting the key does not lift the block; only
    an explicit empty list does."""
    v = _cfg().get("blocked_hosts")
    return [str(x) for x in v] if isinstance(v, list) else list(DEFAULT_BLOCKED)


def ascii_host(url_or_host):
    """The host of a URL (or a bare host) in plain lower-case ASCII/IDNA form, or None when it cannot be
    converted. NFKC (full-width letters), the ideographic full stops 。．｡ as dots, percent-encoded
    characters, case, one or more trailing dots, port and userinfo are all normalised BEFORE anything
    compares hosts, so no spelling of a blocked host slips past. '' for a value with no host."""
    s = unicodedata.normalize("NFKC", (url_or_host or "").strip())
    s = s.replace("\\", "/")
    try:
        netloc = urllib.parse.urlsplit(s).netloc if "//" in s else s.split("/")[0]
        host = netloc.rsplit("@", 1)[-1]
        if host.startswith("["):
            return None                                   # IPv6 literals are never a school host name
        host = urllib.parse.unquote(host.split(":")[0])
        host = unicodedata.normalize("NFKC", host)
        host = re.sub("[\u3002\uff0e\uff61]", ".", host).lower().rstrip(".")
        if not host:
            return ""
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    return host if re.fullmatch(r"[a-z0-9_.-]+", host) and ".." not in host and not host.startswith(".") else None


def bad_host(url_or_host):
    """True when the host is not already in plain ASCII form or cannot be converted: such a URL is refused
    outright (full-width, percent-encoded or IDN spellings are never followed)."""
    s = (url_or_host or "").strip()
    h = ascii_host(s)
    if h is None:
        return True
    raw = urllib.parse.urlsplit(s).hostname if "//" in s else s.split("/")[0].rsplit("@", 1)[-1].split(":")[0]
    return (raw or "").lower().rstrip(".") != h or any(l.startswith("xn--") for l in h.split("."))   # punycode: refused


def is_blocked(url_or_host, blocked=None):
    """True for a blocked host or any subdomain, in ANY spelling (see ascii_host). A host that cannot be
    converted is treated as blocked (refused)."""
    h = ascii_host(url_or_host)
    if h is None:
        return True
    return any(h == b or h.endswith("." + b)
               for b in (ascii_host(x) for x in (blocked_hosts() if blocked is None else blocked)) if b)


def route_for(url, cfg=None):
    """-> dict(via 'direct'|'relay', family 'auto'|'4'|'6', match)."""
    cfg = config() if cfg is None else cfg
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    best = None
    for r in cfg.get("routes") or []:
        m = (r.get("match") or "").lower().strip(".")
        if m and (host == m or host.endswith("." + m)) and (best is None or len(m) > len(best["match"])):
            best = dict(r, match=m)
    fam = str((best or {}).get("family") or cfg.get("family") or "auto")
    if fam not in ("4", "6", "auto"):
        raise ValueError(f"tunnel family must be 4, 6 or auto, not {fam!r}")
    via = (best or {}).get("via") or "direct"
    if via not in ("direct", "relay"):
        raise ValueError(f"tunnel route via must be direct or relay, not {via!r}")
    return {"via": via, "family": fam, "match": (best or {}).get("match")}


def _gateway_hosts():
    """Linux only: the default-gateway IPv4 (the Windows host under WSL2 NAT networking)."""
    try:
        with open("/proc/net/route") as f:
            for line in list(f)[1:]:
                p = line.split()
                if p[1] == "00000000":
                    g = int(p[2], 16)
                    return [socket.inet_ntoa(g.to_bytes(4, "little"))]
    except (OSError, ValueError, IndexError):
        pass
    return []


def relay_base(cfg=None):
    cfg = config() if cfg is None else cfg
    url = (cfg.get("relay") or "http://127.0.0.1:8787").rstrip("/")
    if url != "auto":
        return url
    if "auto" in _auto_cache:
        return _auto_cache["auto"]
    for h in ["127.0.0.1"] + _gateway_hosts():
        cand = f"http://{h}:8787"
        try:
            _open_relay(cand + "/health", 3).close()
            _auto_cache["auto"] = cand
            return cand
        except Exception:                                   # noqa: BLE001
            continue
    return "http://127.0.0.1:8787"


def _open_relay(url, timeout):
    # never through an environment proxy: the relay is local
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return op.open(url, timeout=timeout)


class Resp:
    """Just enough of urllib's response for web.fetch: status, headers, read(), geturl(), 'with'."""

    def __init__(self, status, headers, body, url, route):
        self.status, self.headers, self._buf, self._url, self.route = status, headers, io.BytesIO(body), url, route

    def read(self, n=-1):
        return self._buf.read(n)

    def geturl(self):
        return self._url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NoBlockedRedirect(urllib.request.HTTPRedirectHandler):
    """Direct path: refuse a redirect INTO a blocked host before urllib connects to it."""
    handler_order = 400                       # ahead of the stock redirect handler (500)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        nu = urllib.parse.urljoin(req.full_url, newurl)
        if is_blocked(nu) or bad_host(nu):
            raise TunnelError(f"redirect into a blocked host refused (blocked_hosts): {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _direct_open(req, timeout):
    if urllib.request._opener is None:        # same opener urlopen() would use (tests install proxies there)
        urllib.request.install_opener(urllib.request.build_opener())
    op = urllib.request._opener
    if not any(isinstance(h, _NoBlockedRedirect) for h in op.handlers):
        op.add_handler(_NoBlockedRedirect())
    return op.open(req, timeout=timeout)


class _Hdr(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


def _relay_module():
    global _relay_mod
    if _relay_mod is None:
        spec = importlib.util.spec_from_file_location("cnb_relay_core", RELAY_PY)
        _relay_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_relay_mod)
    return _relay_mod


def _finish(url, status, reason, ctype, body, final, route, hdrs=None):
    h = _Hdr({"Content-Type": ctype}, **(hdrs or {}))
    if status >= 400:
        raise urllib.error.HTTPError(url, status, reason or "", h, io.BytesIO(body))
    return Resp(status, h, body, final or url, route)


def urlopen(req, timeout=30):
    """Drop-in for urllib.request.urlopen(req, timeout) that honours the tunnel config."""
    url = req.full_url if hasattr(req, "full_url") else str(req)
    hdrs = dict(req.header_items()) if hasattr(req, "header_items") else {}
    if is_blocked(url) or bad_host(url):
        raise TunnelError("host is blocked or not in plain ASCII form (research_sources.json -> blocked_hosts): no request sent")
    r = route_for(url)
    if r["via"] == "direct" and r["family"] == "auto":
        return _direct_open(req, timeout)
    if r["via"] == "direct":                                # forced family, in this process
        mod = _relay_module()
        host = urllib.parse.urlparse(url).hostname
        rel = mod.Relay(allow=[host], allow_private=True, gap=0, timeout=timeout, blocked=blocked_hosts())
        try:
            d = rel.fetch(url, r["family"], hdrs)
        except mod.RelayError as e:
            raise TunnelError(f"direct IPv{r['family']} fetch failed: {e.msg}")
        return _finish(url, d["status"], d["reason"], d["ctype"], d["body"], d["final_url"],
                       f"direct IPv{d['family_used']}")
    base = relay_base()
    q = urllib.parse.urlencode({"url": url, "family": r["family"]})
    rq = urllib.request.Request(f"{base}/fetch?{q}", headers={k: v for k, v in hdrs.items()
                                if k.lower() in ("user-agent", "accept", "accept-language")})
    try:
        with _open_relay(rq, timeout + 10) as resp:
            body, status, rh = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as e:
        body, status, rh = e.read(), e.code, e.headers
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise TunnelError(f"tunnel relay not reachable at {base} ({getattr(e, 'reason', e)}); "
                          f"start it: python tunnel/relay.py  (Windows: tunnel\\start_relay.cmd)")
    err = rh.get("X-Tunnel-Error")
    if err:
        raise TunnelError(f"tunnel: {err}")
    used = rh.get("X-Tunnel-Family")
    route = f"relay IPv{used}" if used else "relay"
    return _finish(url, status, rh.get("X-Tunnel-Reason"), rh.get("Content-Type") or "", body,
                   rh.get("X-Tunnel-Final-Url"), route)
