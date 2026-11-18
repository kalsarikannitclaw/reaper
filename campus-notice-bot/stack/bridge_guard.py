"""Bridge guard: the ONLY thing allowed to start the NapCat container.

Why this exists (incident 2026-11-10/11, see PLAN backlog 12):
Docker's `restart: unless-stopped` plus NapCat's own entrypoint retry meant that when
the QQ process died, the account re-logged in roughly once a minute. The log shows
~100 login handshakes in two hours, after which Tencent invalidated the session:
  "你的用户身份已失效，为保证账号安全，请你重新登录"
That is the "account safety warning" the account owner saw. The account was not banned;
the login storm was the thing that looked abusive. So: restarting is the hazard, not the cure.

Rules enforced here:
 1 RATE CAP: at most MAX_LOGINS_24H login attempts per rolling 24h. At the cap the guard
   stops trying and asks for a human, rather than hammering the account.
 2 BACKOFF: 10m, 20m, 40m, 80m, 160m, 6h between restarts; reset only after a spell of
   verified-online time (STABLE_SECS).
 3 FATAL STATES ARE NOT RETRIED: when the log says the identity expired or mobile-QQ
   verification is needed, only the account owner scanning a QR can fix it. The guard stops,
   says so, and keeps the container up so the owner can scan in the WebUI.
 4 VISIBLE: every state goes to capture/bridge_status.json, which the report pages show as a
   banner, so a dead bridge never looks like "a quiet week" in the data.

Backends (one guard, one set of rules): "docker" (Linux: the NapCat container, behaviour unchanged) and "native"
(Windows: one NapCat process set up by install.py, see stack/native_win.py). Chosen by CNB_BACKEND, else
service.json {"backend": ...}, else docker.

Run: python3 stack/bridge_guard.py        (systemd user service campus-bridge-guard / Windows task BridgeGuard)
     python3 stack/bridge_guard.py --once --dry-run     (check only, change nothing)
     python3 stack/bridge_guard.py --reset-cap          (owner rescanned: clear the ledger)
"""
import argparse, asyncio, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATUS = os.path.join(ROOT, "capture", "bridge_status.json")
STATE = os.path.join(HERE, "guard_state.json")
ENV = os.path.join(HERE, ".env")
def _svc_container():      # package: install.py --name writes service.json at the package root
    try: return json.load(open(os.path.join(ROOT, "service.json"), encoding="utf-8")).get("container")
    except (OSError, ValueError): return None
CONTAINER = os.environ.get("CNB_CONTAINER") or _svc_container() or "stack-napcat-1"
WS = os.environ.get("CNB_WS", "ws://127.0.0.1:3001/")

MAX_LOGINS_24H = 6          # a human logs in a few times a day; a storm does it 100 times
BACKOFF = [600, 1200, 2400, 4800, 9600, 21600]
STABLE_SECS = 1800          # online this long -> the restart worked, reset the backoff
QR_GRACE = 7200             # unscanned QR: stop the container rather than re-ask QQ for days
POLL = 60
CHECK_TIMEOUT = 15

# Why a login failed. Reported as the detail; none of these is fixable by restarting.
CAUSES = [
    (r"你的用户身份已失效", "QQ invalidated the session for account safety"),
    (r"登录需要手Q验证", "QQ wants verification in the mobile app"),
    (r"账号被(冻结|限制)|冻结", "account frozen or restricted by QQ"),
    (r"密码错误|验证码错误", "QQ asked for a password or verification code"),
]
# The live signal that a human is needed: NapCat is sitting on the QR screen, or the QR
# keeps expiring unscanned (ErrType 1 ErrCode 3). True whatever the original cause was.
QR_WAIT = re.compile(r"请扫描下面的二维码|二维码已保存到|Login Error,ErrType")
# Printed only once a session is actually logged in (verified against this bridge's own log,
# 2026-11-10 12:30:46, the one login that succeeded). Used only to date the QR markers above;
# the authority on "online" is the OneBot probe, never the log.
LOGIN_OK = re.compile(r"已启用数据库辅助支持能力|本账号数据/缓存目录|登录成功|Login Success")
# Noise that would otherwise fill any tail: the QR block art, the hash dump printed with it,
# and the embedded Chromium's startup complaints (dbus/EGL/GPU) which say nothing about QQ.
NOISE = re.compile(r"^(?:[0-9a-f]{64}|[\s▀-▟░-▓█▀▄▌▐]+)$|"
                   r"ERROR:(?:dbus/|ui/gl/|gpu/|components/viz/)|^\[[IWE]\] <|MMKV")


def env(name):
    if os.environ.get(name):
        return os.environ[name]
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return None


def docker(*args, timeout=60, merge=False):
    """merge=True keeps the two streams in the order the daemon emitted them. NapCat writes
    its own lines to stdout and the embedded Chromium writes to stderr, so capturing them
    separately and concatenating would reorder the log and hide the newest lines."""
    try:
        p = subprocess.run(["docker", *args], text=True, timeout=timeout,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT if merge else subprocess.PIPE)
        return p.returncode, p.stdout, ("" if merge else p.stderr)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", f"{type(e).__name__}: {e}"


class DockerBackend:
    """Linux: the NapCat container. These are exactly the calls the guard always made."""
    name = "docker"
    persist_before_start = False
    probe_always = False

    def state(self):
        rc, out, _ = docker("inspect", CONTAINER, "--format", "{{.State.Status}}")
        return out.strip() if rc == 0 else "absent"

    def raw_log(self, lines=4000):
        return docker("logs", "--tail", str(lines), CONTAINER, merge=True)[1]

    def start(self):
        return docker("start", CONTAINER)

    def stop(self):
        return docker("stop", "-t", "20", CONTAINER)

    def restart(self):
        self.stop()
        rc, _, err = self.start()
        return rc, err

    def fetch_qr(self, out):
        docker("cp", f"{CONTAINER}:/app/napcat/cache/qrcode.png", out)
        return os.path.exists(out)


_BK = None


def backend():
    global _BK
    if _BK is None:
        name = os.environ.get("CNB_BACKEND")
        if not name:
            try:
                name = json.load(open(os.path.join(ROOT, "service.json"), encoding="utf-8")).get("backend")
            except (OSError, ValueError):
                name = None
        if (name or "docker") == "native":
            sys.path.insert(0, HERE)
            import native_win
            _BK = native_win.NativeBackend()
        else:
            _BK = DockerBackend()
    return _BK


def container_state():
    return backend().state()


def recent_log(lines=4000, keep=300):
    """Last `keep` meaningful lines. A big raw tail is needed because one QR screen alone
    prints ~80 lines of block art and hashes, which would otherwise be the whole window."""
    out = backend().raw_log(lines)
    clean = [l for l in re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines()
             if l.strip() and not NOISE.search(l.strip())]
    return "\n".join(clean[-keep:])


def needs_human(text):
    """-> reason, when only the account owner (scanning a QR) can get the bridge back.
    Judged on what happened AFTER the newest successful login, so a stale expiry in the
    ring buffer cannot pin the guard in 'needs QR' forever."""
    ok = [m.end() for m in LOGIN_OK.finditer(text)]
    tail = text[max(ok):] if ok else text
    if not QR_WAIT.search(tail):
        return None
    for pat, why in CAUSES:
        if re.search(pat, tail) or re.search(pat, text):
            return why
    return "the bridge is waiting at the QR login screen"


async def _probe():
    """Online = the OneBot socket answers get_login_info with a uin. A socket that merely
    accepts the connection is not proof: NapCat serves it while sitting on the QR screen."""
    try:
        import websockets
    except ImportError:
        return None, "websockets not installed in this interpreter"
    tok = env("CAPTURE_WS_TOKEN")
    try:
        async with websockets.connect(WS, additional_headers={"Authorization": f"Bearer {tok}"},
                                      open_timeout=CHECK_TIMEOUT, close_timeout=5) as ws:
            await ws.send(json.dumps({"action": "get_login_info", "echo": "guard"}))
            deadline = time.time() + CHECK_TIMEOUT
            while time.time() < deadline:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time())))
                if ev.get("echo") == "guard":
                    uin = (ev.get("data") or {}).get("user_id")
                    return (True, f"logged in as {uin}") if uin else (False, f"not logged in: {ev.get('message') or ev.get('status')}")
            return False, "no answer to get_login_info"
    except Exception as e:                      # noqa: BLE001 - any failure means "not usable"
        return False, f"{type(e).__name__}"


def probe():
    try:
        return asyncio.run(_probe())
    except Exception as e:                      # noqa: BLE001
        return False, f"{type(e).__name__}"


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else dict(default)
    except ValueError:
        return dict(default)


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def seed_last_ok(st):
    """First run of the guard on an existing install: it has no history, but the capture DB does.
    The newest captured message is the last moment the bridge is KNOWN to have been working, so
    use it rather than telling the student "not yet seen online", which reads as "never worked"."""
    if st.get("last_ok"):
        return st["last_ok"]
    path = os.path.join(ROOT, "capture", "messages.db")
    if not os.path.exists(path):             # connecting would create an empty DB
        return None
    try:
        import sqlite3
        # Opened by plain path, not a file: URI: this install lives under a directory whose name
        # contains '#', which a URI reads as the start of a fragment, so the read silently failed.
        db = sqlite3.connect(path)
        ts = db.execute("SELECT max(ts) FROM msg").fetchone()[0]
        db.close()
        if ts:
            st["last_ok"] = int(ts)
    except (OSError, ValueError, ImportError):
        pass
    except Exception:                      # noqa: BLE001 - a missing/odd DB must not stop the guard
        pass
    return st.get("last_ok")


def reread(st):
    """Take the on-disk ledger as the truth before each tick. `--qr` (and `--reset-cap`) write guard_state.json while this loop
    runs; without this the loop's next save would erase their entries and the 6/24 h cap could be exceeded."""
    try:
        disk = load(STATE, {}) if os.path.exists(STATE) else {}
    except OSError:                      # sharing violation / unreadable: keep what the guard has in memory and carry on
        return st
    if isinstance(disk, dict) and disk:
        mine = st.get("logins")
        st.clear()
        st.update(disk)
        if "logins" not in disk and mine is not None:      # a file without a ledger must not reset the count to 0
            st["logins"] = mine
    return st


def logins_24h(st):
    cut = time.time() - 86400
    st["logins"] = [t for t in st.get("logins", []) if t > cut]
    return len(st["logins"])


def write_status(state, detail, st, next_try=None):
    seed_last_ok(st)
    save(STATUS, {
        "state": state,                                  # online | offline | waiting | needs_qr | capped
        "detail": detail,
        "checked": int(time.time()),
        "last_online": st.get("last_ok"),
        "logins_24h": logins_24h(st),
        "login_cap": MAX_LOGINS_24H,
        "next_try": next_try,
        "webui": "http://127.0.0.1:6099/webui",
        "backend": backend().name,
    })


def _outside_control(cs, rc_stop):
    """Native only: NapCat is alive but not as the process the guard started (its launcher .bat exited, or taskkill failed), so the
    guard's stop cannot reach it. Docker: always False (behaviour unchanged)."""
    bk = backend()
    if not bk.probe_always:
        return False
    if cs == "running":
        return bool(rc_stop) and rc_stop[0] != 0
    return any(bk.run.port_busy(p) for p in bk.PORTS)


def tick(st, dry=False):
    """One decision. Returns (state, detail)."""
    now = int(time.time())
    cs = container_state()
    online, why = probe() if (cs == "running" or backend().probe_always) else (False, f"container {cs}")

    if online:
        st["offline_since"] = None
        st["last_ok"] = now
        if st.get("stable_from") is None:
            st["stable_from"] = now
        if now - st["stable_from"] >= STABLE_SECS and st.get("backoff_idx"):
            st["backoff_idx"] = 0                        # it held: forgive the backoff
        write_status("online", why, st)
        return "online", why

    st["stable_from"] = None
    st.setdefault("offline_since", now)
    st["offline_since"] = st["offline_since"] or now

    fatal = needs_human(recent_log()) if cs in ("running", "exited") else None
    if fatal:
        # Restarting cannot help, and each attempt is another login. Ask the owner instead.
        # While waiting, NapCat re-issues a QR every ~2 min; that is its normal login flow,
        # but it is still traffic on a flagged account, so after QR_GRACE we shut it down and
        # wait for the owner to say they are ready (`--qr`) rather than knocking for days.
        waited = now - (st.get("offline_since") or now)
        if waited > QR_GRACE:
            if cs == "running" and not dry:
                rc_stop = backend().stop()
            else:
                rc_stop = None
            if _outside_control(cs, rc_stop):
                write_status("needs_qr", f"{fatal}. NapCat is STILL RUNNING outside the guard's control after {waited // 3600}h of unscanned QR "
                                         "prompts (the guard cannot stop it): close QQ/NapCat yourself, then run bridge_guard.py --qr when ready", st)
            else:
                write_status("needs_qr", f"{fatal}. Bridge stopped after {waited // 3600}h of unscanned QR "
                                         "prompts, to keep traffic off a flagged account. When the account "
                                         "owner has their phone ready, run: bridge_guard.py --qr", st)
        else:
            write_status("needs_qr", f"{fatal}. The account owner must scan the QR once: "
                                     f"http://127.0.0.1:6099/webui (QR also at stack/qrcode.png).", st)
        return "needs_qr", fatal

    n = logins_24h(st)
    if n >= MAX_LOGINS_24H:
        rc_stop = None
        if cs == "running" and not dry:
            rc_stop = backend().stop()                    # stop the retry loop at the source
        if _outside_control(cs, rc_stop):
            write_status("capped", f"{n} login attempts in 24h. NapCat is STILL RUNNING outside the guard's control (the guard cannot stop it): "
                                   "close QQ/NapCat yourself, check QQ, then rerun with --reset-cap.", st)
        else:
            write_status("capped", f"{n} login attempts in 24h: stopped to protect the account. "
                                   "Owner: check QQ, then rerun with --reset-cap.", st)
        return "capped", f"{n} logins/24h"

    wait = BACKOFF[min(st.get("backoff_idx", 0), len(BACKOFF) - 1)]
    since = now - (st.get("last_attempt") or 0)
    if st.get("last_attempt") and since < wait:
        nxt = st["last_attempt"] + wait
        write_status("waiting", f"offline ({why}); next restart in {(nxt - now) // 60} min "
                                f"(attempt {n + 1}/{MAX_LOGINS_24H} today)", st, next_try=nxt)
        return "waiting", why

    if dry:
        write_status("offline", f"offline ({why}); would restart now [dry-run]", st)
        return "offline", why

    st["last_attempt"] = now
    st["logins"] = st.get("logins", []) + [now]
    st["backoff_idx"] = min(st.get("backoff_idx", 0) + 1, len(BACKOFF) - 1)
    if backend().persist_before_start:
        save(STATE, st)            # native: the attempt is on disk BEFORE the process exists, so a crash cannot lose it
    rc, err = backend().restart()
    detail = f"restarted the bridge ({why})" if rc == 0 else f"restart failed: {err.strip()[:120]}"
    write_status("offline", detail, st)
    return "offline", detail


def qr(wait=180):
    """Owner-initiated login: start the bridge, fetch the QR it prints, wait for the scan.
    Done this way round so the QQ account is only asked to log in while a human is watching."""
    if container_state() != "running":
        if backend().persist_before_start:        # native: an owner-started login still counts against the cap
            st0 = load(STATE, {})
            if logins_24h(st0) >= MAX_LOGINS_24H:
                print(f"{MAX_LOGINS_24H} logins in 24h already: not starting. Check QQ on your phone, then --reset-cap.")
                return 1
            st0["logins"] = st0.get("logins", []) + [int(time.time())]
            save(STATE, st0)
        rc, _, err = backend().start()
        if rc == 2 and backend().name == "native":          # native: NapCat already holds its ports (started elsewhere / launcher exited): do not start another
            print("NapCat already seems to be running (its ports are in use): not starting a second one. Waiting for its QR / login.")
        elif rc != 0:
            print("could not start the bridge:", err.strip()[:200])
            return 1
    print("bridge starting; waiting for its QR...")
    url, out = None, os.path.join(HERE, "qrcode.png")
    for _ in range(wait // 5):
        m = re.findall(r"二维码解码URL:\s*(\S+)", recent_log())
        if m:
            url = m[-1]
            break
        time.sleep(5)
    backend().fetch_qr(out)
    print("\nScan this with the QQ app on the account owner's phone, then authorise:")
    print(f"  QR image : {out if os.path.exists(out) else '(not written)'}")
    print(f"  WebUI    : http://127.0.0.1:6099/webui")
    print(f"  QR URL   : {url or '(not seen in the log yet; open the WebUI)'}")
    print("\nWaiting for the login to complete (up to 5 min)...")
    for _ in range(60):
        ok, why = probe()
        if ok:
            st = load(STATE, {})
            st.update(logins=[], backoff_idx=0, last_attempt=None, last_ok=int(time.time()),
                      offline_since=None, stable_from=int(time.time()))
            save(STATE, st)
            write_status("online", why, st)
            print(f"online: {why}. Login ledger cleared.")
            return 0
        time.sleep(5)
    print("not logged in yet. Leave the guard running; it will see the login when it happens.")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bridge_guard")
    ap.add_argument("--once", action="store_true", help="one check, then exit")
    ap.add_argument("--dry-run", action="store_true", help="decide and report, never touch the container")
    ap.add_argument("--reset-cap", action="store_true", help="clear the login ledger after the owner rescanned")
    ap.add_argument("--status", action="store_true", help="print the current status file")
    ap.add_argument("--qr", action="store_true", help="owner is ready: start the bridge and show the login QR")
    a = ap.parse_args(argv)

    if a.status:
        print(json.dumps(load(STATUS, {"state": "unknown"}), ensure_ascii=False, indent=2))
        return 0
    if a.qr:
        return qr()
    st = load(STATE, {"logins": [], "backoff_idx": 0, "last_attempt": None, "last_ok": None,
                      "stable_from": None, "offline_since": None})
    if a.reset_cap:
        st.update(logins=[], backoff_idx=0, last_attempt=None)
        save(STATE, st)
        print("login ledger cleared; backoff reset")
        return 0
    while True:
        reread(st)
        state, detail = tick(st, dry=a.dry_run)
        save(STATE, st)
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {state}: {detail}", flush=True)
        if a.once:
            return 0 if state == "online" else 1
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
