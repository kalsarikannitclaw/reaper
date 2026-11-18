#!/usr/bin/env python3
"""Fetch relay ("tunnel") for the Campus Notice Bot. Standard library only; runs on Windows, Linux
and macOS, with any Python 3.8+.

WHAT IT IS
  A tiny local HTTP server. The bot asks it "GET /fetch?url=<page>&family=4|6|auto"; the relay opens
  that page from the machine it runs on, forcing IPv4 or IPv6 as asked, and hands back the body.
  Run it on the Windows side when WSL cannot reach a school site (or reaches it only over one IP
  family); run it in WSL/Linux for the same purpose on Linux.

WHAT IT IS NOT
  No third-party service, no login, no cookies, no credentials, no proxying of arbitrary sites:
   - only hosts in --allow (default: example-school.edu.cn and its subdomains) are fetched, also after every
     redirect hop;
   - only http/https, only GET, IP-address hosts refused, and hosts that resolve to loopback/private/
     link-local addresses refused (so it cannot be used to poke at your own network; --allow-private
     exists for the unit test with a fake local server);
   - listens on 127.0.0.1 only unless you pass --bind;
   - it never solves a CAPTCHA and never fakes a browser: it forwards the bot's own User-Agent.
  robots.txt, rate limit and cache are the BOT's job (src/cnb/web.py); the relay adds its own
  safety-net gap per site (--gap, default 2 s) so that even a misbehaving client cannot hammer a school.

RUN
    python tunnel/relay.py                      # 127.0.0.1:8787, allow example-school.edu.cn
    python tunnel/relay.py --port 9000 --allow example-school.edu.cn --allow college.example-school.edu.cn
  Windows: double-click tunnel\\start_relay.cmd    Linux/macOS: tunnel/start_relay.sh
  Check: open http://127.0.0.1:8787/health in a browser.
"""
import argparse, collections, http.client, ipaddress, json, os, re, socket, ssl, sys, threading, time, unicodedata, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1"
MAX_BYTES = 20 * 1024 * 1024
FORWARD = ("User-Agent", "Accept", "Accept-Language")       # the only client headers passed on
DEFAULT_UA = "campus-notice-bot/0.1 (student notice reader)"


DEFAULT_BLOCKED = ["college.example-school.edu.cn"]     # the user-only college site: blocked until they remove the block in config


def norm_host(h):
    """Plain ASCII/IDNA lower-case host: NFKC (full-width), 。 as dot, percent-decoding, trailing dots.
    None when it cannot be converted (callers refuse such a host)."""
    try:
        h = unicodedata.normalize("NFKC", urllib.parse.unquote((h or "").strip()))
        h = re.sub("[\u3002\uff0e\uff61]", ".", h).lower().rstrip(".")
        if not h:
            return ""
        h = h.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    return h if re.fullmatch(r"[a-z0-9_.-]+|\[[0-9a-f:.]+\]", h) else None


def host_blocked(host, blocked):
    h = norm_host(host)
    if h is None:
        return True                                        # unconvertible = refused
    return any(h == b or h.endswith("." + b) for b in (norm_host(x) for x in blocked) if b)


def config_blocked(path=None):
    """blocked_hosts from research_sources.json (next to tunnel/), else the built-in default. The block
    holds whatever the allowlist says; the user lifts it only by editing blocked_hosts in that file."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "research_sources.json")
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f).get("blocked_hosts")
        if isinstance(v, list):
            return [str(x) for x in v]
    except (OSError, ValueError):
        pass
    return list(DEFAULT_BLOCKED)


def host_allowed(host, allow):
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == a or host.endswith("." + a) for a in allow)


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _public(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def _has_v6():
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


def _fam(code):
    return {"4": socket.AF_INET, "6": socket.AF_INET6}.get(str(code), socket.AF_UNSPEC)


class RelayError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _connect(host, port, family, allow_private, timeout):
    """Open a TCP socket to host:port using ONLY the asked family. Tries every address of that
    family in order. -> (socket, 4|6, address)."""
    try:
        infos = socket.getaddrinfo(host, port, _fam(family), socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise RelayError(502, f"no IPv{family} address for {host}: {e}" if family in ("4", "6")
                         else f"DNS failed for {host}: {e}")
    if not infos:
        raise RelayError(502, f"no IPv{family} address for {host}")
    errs = []
    for af, st, proto, _, sa in infos:
        if not allow_private and not _public(sa[0]):
            errs.append(f"{sa[0]} refused (not a public address)")
            continue
        try:
            s = socket.socket(af, st, proto)        # raises where the OS has no IPv6 stack at all
        except OSError as e:
            errs.append(f"{sa[0]}: {e}")
            continue
        s.settimeout(timeout)
        try:
            s.connect(sa)
            return s, (6 if af == socket.AF_INET6 else 4), sa[0]
        except OSError as e:
            s.close()
            errs.append(f"{sa[0]}: {e}")
    raise RelayError(502, f"cannot connect over IPv{family}: " + "; ".join(errs[:3]))


class Relay:
    """The fetch logic, separate from the HTTP front so tests and other code can call it directly."""

    def __init__(self, allow=("example-school.edu.cn",), allow_private=False, gap=2.0, timeout=30, max_redirects=5, blocked=None):
        self.allow = [a.lower().strip(".") for a in allow]
        self.allow_private, self.gap, self.timeout, self.max_redirects = allow_private, gap, timeout, max_redirects
        self.blocked = list(DEFAULT_BLOCKED if blocked is None else blocked)
        self._last, self._lock = {}, threading.Lock()
        self.log = collections.deque(maxlen=200)                                       # (time, family asked, url, outcome)

    def _site(self, host):
        # the allow entry that matched is the "site": one gap per entry
        for a in self.allow:
            if host == a or host.endswith("." + a):
                return a
        return host

    def _pause(self, host):
        with self._lock:
            key = self._site(host)
            wait = self.gap - (time.time() - self._last.get(key, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[key] = time.time()

    def check_url(self, url):
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            raise RelayError(403, f"scheme {p.scheme!r} not allowed")
        raw = (p.hostname or "").lower()
        host = norm_host(p.hostname)
        if host is None or host != raw.rstrip(".") or any(l.startswith("xn--") for l in host.split(".")):        # full-width, percent-encoded, IDN or garbage hosts
            raise RelayError(403, f"host {raw!r} is not in plain ASCII form: refused")
        if host_blocked(host, self.blocked):               # before the allowlist, before any DNS
            raise RelayError(403, f"host {host!r} is blocked (blocked_hosts)")
        if _is_ip_literal(host) and not self.allow_private:
            raise RelayError(403, "IP-address hosts are not fetched")
        if not host_allowed(host, self.allow):
            raise RelayError(403, f"host {host!r} is not in the relay allowlist {self.allow}")
        return p

    def fetch(self, url, family="auto", headers=None):
        """-> dict(status, ctype, body, final_url, family_used, addr). Raises RelayError."""
        family = str(family or "auto")
        if family not in ("4", "6", "auto"):
            raise RelayError(400, "family must be 4, 6 or auto")
        low = {str(k).lower(): v for k, v in (headers or {}).items()}
        hdr = {k: low[k.lower()] for k in FORWARD if low.get(k.lower())}
        hdr.setdefault("User-Agent", DEFAULT_UA)
        hdr["Accept-Encoding"] = "identity"
        cur = url
        for hop in range(self.max_redirects + 1):
            p = self.check_url(cur)
            port = p.port or (443 if p.scheme == "https" else 80)
            self._pause(p.hostname.lower())
            sock, used, addr = self._open(p, port, family)
            try:
                if p.scheme == "https":
                    ctx = ssl.create_default_context()
                    sock = ctx.wrap_socket(sock, server_hostname=p.hostname)
                conn = http.client.HTTPConnection(p.hostname, port, timeout=self.timeout)
                conn.sock = sock
                path = (p.path or "/") + (("?" + p.query) if p.query else "")
                conn.request("GET", path, headers=dict(hdr, Host=p.netloc.rsplit("@", 1)[-1]))
                r = conn.getresponse()
                if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
                    cur = urllib.parse.urljoin(cur, r.getheader("Location"))
                    r.read(0)
                    continue
                body = r.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise RelayError(502, f"larger than {MAX_BYTES} bytes")
                self.log.append((time.time(), family, url, f"{r.status} via IPv{used}"))
                return dict(status=r.status, reason=r.reason, ctype=r.getheader("Content-Type") or "",
                            body=body, final_url=cur, family_used=used, addr=addr)
            except (OSError, http.client.HTTPException, ssl.SSLError) as e:
                raise RelayError(502, f"{type(e).__name__}: {e}")
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        raise RelayError(502, f"more than {self.max_redirects} redirects")

    def _open(self, p, port, family):
        if family != "auto":
            return _connect(p.hostname, port, family, self.allow_private, self.timeout)
        last = None
        for f in ("6", "4"):                                  # auto: prefer IPv6 like a normal client, fall back
            try:
                return _connect(p.hostname, port, f, self.allow_private, self.timeout)
            except RelayError as e:
                last = e
        raise last


def _hval(v):
    """A header value that can never break the response: control characters (CR, LF, ...) become
    spaces, anything outside ASCII is backslash-escaped (latin-1 only headers would raise)."""
    t = re.sub(r"[\x00-\x1f\x7f]", " ", str(v))
    return t.encode("ascii", "backslashreplace").decode("ascii")[:500]


def make_handler(relay):
    class H(BaseHTTPRequestHandler):
        server_version = "cnb-relay/" + VERSION
        protocol_version = "HTTP/1.0"

        def _send(self, status, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, _hval(v))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/health":
                d = json.dumps({"relay": "cnb", "version": VERSION, "allow": relay.allow,
                                "python": sys.version.split()[0], "platform": sys.platform,
                                "ipv6_stack": _has_v6()}).encode()
                return self._send(200, d, "application/json")
            if u.path != "/fetch" or not q.get("url"):
                return self._send(404, b"use /fetch?url=...&family=4|6|auto or /health", extra={"X-Tunnel-Error": "bad request"})
            hdr = {k: self.headers.get(k) for k in FORWARD}
            try:
                r = relay.fetch(q["url"][0], (q.get("family") or ["auto"])[0], hdr)
            except RelayError as e:
                return self._send(e.status, e.msg.encode("utf-8", "replace"), extra={"X-Tunnel-Error": e.msg})
            except Exception as e:                  # noqa: BLE001 - a bug must not look like a dead relay
                m = f"relay internal error: {type(e).__name__}: {e}"
                return self._send(500, m.encode("utf-8", "replace"), extra={"X-Tunnel-Error": m})
            self._send(r["status"], r["body"], r["ctype"] or "application/octet-stream",
                       {"X-Tunnel-Final-Url": r["final_url"], "X-Tunnel-Family": r["family_used"],
                        "X-Tunnel-Addr": r["addr"], "X-Tunnel-Reason": r["reason"]})

        def log_message(self, fmt, *a):
            pass
    return H


class _Server(ThreadingHTTPServer):
    # Windows lets a second process bind a port that is already in use when SO_REUSEADDR is set,
    # so a double-clicked second relay would silently share 8787; keep it only where it is safe.
    allow_reuse_address = os.name != "nt"


def serve(bind="127.0.0.1", port=8787, relay=None):
    relay = relay or Relay(blocked=config_blocked())
    srv = _Server((bind, port), make_handler(relay))
    srv.daemon_threads = True
    return srv, relay


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1 only)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow", action="append", help="host or domain the relay may fetch (repeatable); default example-school.edu.cn")
    ap.add_argument("--gap", type=float, default=2.0, help="minimum seconds between requests to one site")
    ap.add_argument("--allow-private", action="store_true", help="TEST ONLY: allow loopback/private targets")
    a = ap.parse_args(argv)
    relay = Relay(allow=a.allow or ["example-school.edu.cn"], allow_private=a.allow_private, gap=a.gap, blocked=config_blocked())
    srv, _ = serve(a.bind, a.port, relay)
    print(f"cnb relay {VERSION} on http://{a.bind}:{a.port}  allow={relay.allow}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
