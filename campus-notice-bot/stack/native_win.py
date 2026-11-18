"""Native Windows NapCat backend for stack/bridge_guard.py (native Windows). Standard library only.

bridge_guard.py stays the ONLY starter of NapCat. This module is the "native" backend it can load instead of the
Linux one: it starts, stops and inspects ONE NapCat process that install.py set up (native.json at the package root).
All the safety logic (login cap 6 per 24 h, backoff, never-retried fatal states, OneBot get_login_info as proof of
"online", capture/bridge_status.json, QR stop rule) lives in bridge_guard.tick() and is shared by both backends.

Rules kept here (belt and braces on top of the guard's ledger):
 * start() is one logged call. It never loops, never retries. Every call is appended to logs/napcat_launch.log.
 * start() refuses when the previous start was less than MIN_START_GAP seconds ago, whatever the caller asks.
 * start() refuses when NapCat is already running (a second QQ process on the same account is a second login).
 * Nothing here is registered with Task Scheduler: NapCat is never a task, so Windows cannot restart it by itself.

Every OS call goes through a Runner object, so the whole state machine is tested on Linux with a fake one.

Windows/NapCat facts: see UNVERIFIED notes in the docstrings below and in COMPONENTS.md. On 2026-11-15 the official
NapCatQQ repository (github.com/NapNeko/NapCatQQ) had no releases and a README reading only "Ending", so the file names,
the launch command and the config shapes used here could NOT be checked against official documentation.

  python stack/native_win.py --check       read-only report (used by tools/wincheck.cmd)
  python stack/native_win.py --selftest    state machine against a fake runner (changes nothing real)
"""
import csv, io, json, os, re, shutil, socket, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = ROOT / "native.json"                 # written by install.py: {"napcat_dir", "launch": [...], "qq_dir"}
PIDF = HERE / "napcat_proc.json"           # {"pid", "image", "started", "stopped"}
LOGS = ROOT / "logs"
OUT_LOG = LOGS / "napcat.log"              # NapCat's stdout/stderr (the guard reads QR / expiry markers from it)
LAUNCH_LOG = LOGS / "napcat_launch.log"    # one line per start/stop call
MIN_START_GAP = 120                        # seconds between two process starts, no matter who asks
STOP_WAIT = 20
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# UNVERIFIED guesses of launcher file names inside a NapCat Shell folder, only used to OFFER choices to the user.
# The installer never runs one of them without the user choosing it (or passing --napcat-launch).
LAUNCH_CANDIDATES = ["napcat.bat", "launcher.bat", "launcher-user.bat", "napcat.quick.bat", "NapCatWinBootMain.exe"]


# ------------------------------------------------------------------ OS calls (replaced by a fake in tests)
class Runner:
    """The only place that touches Windows. Methods are small so a fake can replace them one by one."""

    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def image_of(self, pid):
        """-> image name (e.g. 'QQ.exe') of a live process, else None. Uses `tasklist` (os.kill(pid, 0) would KILL on Windows)."""
        try:
            p = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True,
                               text=True, timeout=20, encoding="mbcs" if os.name == "nt" else "utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        for row in csv.reader(io.StringIO(p.stdout or "")):
            if len(row) >= 2 and row[1].strip() == str(int(pid)):
                return row[0]
        return None

    def port_busy(self, port):
        """True when something already listens on this port (any interface). Bind test only: connecting to a live NapCat port is not free of side effects."""
        return not port_free(port)

    def spawn(self, argv, cwd, logfile):
        """Start ONE process, output to logfile, no console window. -> pid. Raises OSError."""
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        try:
            if Path(logfile).stat().st_size > 5_000_000:          # keep one old file; the log must not grow forever
                os.replace(logfile, str(logfile) + ".1")
        except OSError:
            pass
        flags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        with open(logfile, "ab") as lf:
            p = subprocess.Popen([str(a) for a in argv], cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=lf,
                                 stderr=subprocess.STDOUT, creationflags=flags)
        return p.pid

    def terminate(self, pid, force=False):
        """taskkill the process tree (QQ runs as child processes of the launcher). -> return code."""
        cmd = ["taskkill", "/PID", str(int(pid)), "/T"] + (["/F"] if force else [])
        try:
            return subprocess.run(cmd, capture_output=True, timeout=30).returncode
        except (OSError, subprocess.SubprocessError):
            return 1


# ------------------------------------------------------------------ small file helpers
def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else dict(default)
    except (OSError, ValueError):
        return dict(default)


def _save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_config(path=None):
    return _load(path or CFG, {})


def launch_argv(cfg):
    """The one command line the guard may run, from native.json. None when the installer has not set it."""
    d, launch = cfg.get("napcat_dir"), cfg.get("launch")
    if not d or not launch:
        return None, None, None
    d = Path(d)
    target = d / launch[0]
    if target.suffix.lower() in (".bat", ".cmd"):
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(target), *launch[1:]]
    else:
        argv = [str(target), *launch[1:]]
    return argv, d, target


# ------------------------------------------------------------------ the backend
class NativeBackend:
    name = "native"
    persist_before_start = True      # the guard writes its ledger to disk BEFORE the process is started
    probe_always = True              # a launcher .bat may exit while QQ lives on: ask OneBot whatever the pid says
    PORTS = (3001, 6099)

    def __init__(self, cfg=None, runner=None, pidfile=None, outlog=None, launchlog=None):
        self.cfg = load_config() if cfg is None else cfg
        self.run = runner or Runner()
        self.pidf = Path(pidfile or PIDF)
        self.outlog = Path(outlog or OUT_LOG)
        self.launchlog = Path(launchlog or LAUNCH_LOG)

    # -- bookkeeping
    def _note(self, text):
        try:
            self.launchlog.parent.mkdir(parents=True, exist_ok=True)
            with open(self.launchlog, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.run.now())) + " " + text + "\n")
        except OSError:
            pass

    def _proc(self):
        return _load(self.pidf, {})

    # -- the guard's view
    def state(self):
        """'running' | 'exited' | 'absent' (same words as the Linux backend)."""
        pr = self._proc()
        if not pr.get("pid"):
            return "absent"
        if pr.get("stopped"):
            return "exited"
        img = self.run.image_of(pr["pid"])
        if img is None:
            return "exited"
        if pr.get("image") and img.lower() != str(pr["image"]).lower():
            return "exited"           # the pid was reused by another program: ours is gone
        return "running"

    def raw_log(self, lines=4000):
        """Last `lines` lines of NapCat's own output file (the guard filters and judges them)."""
        try:
            size = self.outlog.stat().st_size
            with open(self.outlog, "rb") as f:
                f.seek(max(0, size - 600_000))
                data = f.read()
        except OSError:
            return ""
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])

    def start(self):
        """ONE start. -> (rc, out, err). Never loops, never retries; refuses rather than racing the ledger."""
        argv, cwd, target = launch_argv(self.cfg)
        if not argv:
            return 1, "", "NapCat is not set up here: run install.py with --napcat-dir (or --napcat-zip), see README"
        if not target.exists():
            return 1, "", "the NapCat launch file named in native.json does not exist"
        pr = self._proc()
        now = self.run.now()
        if self.state() == "running":
            self._note("start refused: already running")
            return 0, "already running", ""
        busy = [p for p in self.PORTS if self.run.port_busy(p)]
        if busy:      # NapCat (or something else) already holds our ports: a second instance would be a second login
            self._note(f"start refused: port(s) {busy} already in use")
            return 2, "", f"start refused: 127.0.0.1 port(s) {busy} already in use (NapCat may already be running; not starting a second one)"
        if pr.get("started") and now - pr["started"] < MIN_START_GAP:
            self._note("start refused: previous start %ds ago (min gap %ds)" % (now - pr["started"], MIN_START_GAP))
            return 1, "", "start refused: previous start was under %d s ago" % MIN_START_GAP
        self._note("START " + " ".join(Path(a).name for a in argv))
        # marker BEFORE the spawn: a crash between spawn and the pid being saved still leaves the gap rule and the start time on disk
        _save(self.pidf, {"pid": None, "started": now, "stopped": False, "starting": True})
        try:
            pid = self.run.spawn(argv, cwd, self.outlog)
        except OSError as e:
            self._note(f"start failed: {type(e).__name__}")
            _save(self.pidf, {"pid": None, "started": now, "stopped": True})
            return 1, "", f"{type(e).__name__}: {e}"
        # the image name is looked up once, so a recycled pid is never mistaken for NapCat later
        _save(self.pidf, {"pid": pid, "image": self.run.image_of(pid), "started": now, "stopped": False})
        return 0, str(pid), ""

    def stop(self):
        """Polite taskkill of the tree, then forced after STOP_WAIT s. Keeps the start time (so the gap rule survives)."""
        pr = self._proc()
        pid = pr.get("pid")
        if not pid:
            return 0, "", ""
        self._note(f"STOP pid {pid}")
        if self.state() == "running":
            self.run.terminate(pid)
            for _ in range(STOP_WAIT):
                if self.state() != "running":
                    break
                self.run.sleep(1)
            if self.state() == "running":
                self.run.terminate(pid, force=True)
        if self.state() == "running":      # still there after the forced kill: do NOT claim it stopped
            self._note(f"STOP FAILED pid {pid}: still running")
            return 1, "", "could not stop NapCat (still running after taskkill /F)"
        pr["stopped"] = True
        _save(self.pidf, pr)
        return 0, "", ""

    def restart(self):
        """What the guard calls for 'try again': stop if alive, then ONE start. If the stop failed, NOTHING is started. -> (rc, err)."""
        if self.state() == "running":
            rc, _, err = self.stop()
            if rc != 0:
                return rc, err
        rc, _, err = self.start()
        return rc, err

    def fetch_qr(self, out):
        """Copy NapCat's newest qrcode.png (searched under the NapCat folder; location UNVERIFIED) to `out`."""
        d = self.cfg.get("napcat_dir")
        if not d:
            return False
        best = None
        for p in Path(d).rglob("qrcode.png"):
            if best is None or p.stat().st_mtime > best.stat().st_mtime:
                best = p
        if best:
            shutil.copyfile(best, out)
            return True
        return False


# ------------------------------------------------------------------ installer helpers (config NapCat must have)
def onebot_config(ws_token):
    """OneBot v11 settings: forward WebSocket on loopback 3001 with our token. Same shape the Linux install writes
    (that shape was working with NapCat.Core 4.18.42 in the Linux package); the HOST is 127.0.0.1 because there is no container
    boundary here. UNVERIFIED that the native Windows build reads exactly this file name/shape (see COMPONENTS.md)."""
    return {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketClients": [],
                        "websocketServers": [{"name": "ws", "enable": True, "host": "127.0.0.1", "port": 3001,
                                              "messagePostFormat": "array", "reportSelfMessage": False,
                                              "token": ws_token, "enableForcePushEvent": True, "debug": False,
                                              "heartInterval": 30000}]},
            "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}


def webui_config(webui_token):
    """WebUI on loopback 6099. UNVERIFIED shape (key names host/port/token taken from memory of NapCat's webui.json)."""
    return {"host": "127.0.0.1", "port": 6099, "token": webui_token, "loginRate": 3}


def write_napcat_config(napcat_dir, ws_token, webui_token, uin="", say=print):
    """Write onebot11[_uin].json and webui.json under <napcat_dir>/config, NEVER overwriting an existing file.
    -> list of files written. Existing files are reported so the user can compare host/port/token by hand."""
    cdir = Path(napcat_dir) / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    wrote = []
    plan = [("onebot11.json", onebot_config(ws_token))] + ([(f"onebot11_{uin}.json", onebot_config(ws_token))] if uin else []) \
        + [("webui.json", webui_config(webui_token))]
    for fn, body in plan:
        p = cdir / fn
        if p.exists():
            say(f"  keep existing {fn}: check it binds 127.0.0.1 and uses the tokens from stack/.env")
            continue
        p.write_text(json.dumps(body, indent=1), encoding="utf-8")
        wrote.append(fn)
    return wrote


def find_launchers(napcat_dir):
    d = Path(napcat_dir)
    return [n for n in LAUNCH_CANDIDATES if (d / n).is_file()]


def safe_extract(zip_path, dest):
    """Extract a zip refusing absolute paths and '..' (zip-slip). -> number of files."""
    import zipfile
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if dest != target and dest not in target.parents:
                raise ValueError(f"unsafe path in zip: {info.filename!r}")
        for info in z.infolist():
            z.extract(info, dest)
            n += 0 if info.is_dir() else 1
    return n


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ read-only check (wincheck.cmd)
def port_free(port, host="127.0.0.1"):
    """True when nothing listens. Uses bind (a connect to a live NapCat port is not free of side effects)."""
    for h in dict.fromkeys((host, "0.0.0.0")):      # a listener on 0.0.0.0 would not block a 127.0.0.1 bind on Windows
        s = socket.socket()
        try:
            s.bind((h, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def check(root=ROOT, out=print):
    root = Path(root)
    ok = True
    cfg = load_config(root / "native.json")
    if not cfg:
        out("native.json: MISSING (install.py has not set up NapCat here; run it with --napcat-dir or --napcat-zip)")
        return False
    d = Path(cfg.get("napcat_dir") or "")
    out(f"napcat_dir: {'ok' if d.is_dir() else 'MISSING'}  (folder name only: {d.name})")
    ok &= d.is_dir()
    lf = (cfg.get("launch") or [None])[0]
    out(f"launch file: {lf or 'NOT CHOSEN'}  {'ok' if lf and (d / lf).is_file() else 'MISSING'}")
    ok &= bool(lf and (d / lf).is_file())
    extra = sorted(p.name for p in (d / "config").glob("onebot11_*.json")) if (d / "config").is_dir() else []
    for fn in ("onebot11.json", "webui.json", *extra):
        p = d / "config" / fn
        if not p.is_file():
            out(f"config\\{fn}: MISSING"); ok = False; continue
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            out(f"config\\{fn}: not valid JSON"); ok = False; continue
        hosts = [s.get("host") for s in (body.get("network", {}).get("websocketServers") or [])] if fn.startswith("onebot") else [body.get("host")]
        loop = bool(hosts) and all(h in ("127.0.0.1", "localhost", "::1") for h in hosts)
        out(f"config\\{fn}: present, {'loopback only' if loop else 'NOT LOOPBACK (host must be 127.0.0.1)'}")
        ok &= loop
    for port, what in ((3001, "OneBot WebSocket"), (6099, "WebUI"), (8790, "report/chat page")):
        out(f"port {port} ({what}): {'free' if port_free(port) else 'in use (fine if the bot/NapCat is running)'}")
    out("launcher rule: NapCat is started only by bridge_guard; no scheduled task starts it.")
    return bool(ok)


# ------------------------------------------------------------------ self-test with a fake runner
class FakeRunner:
    def __init__(self):
        self.t = 1_000_000.0; self.alive = {}; self.next_pid = 4000; self.spawned = []; self.killed = []
        self.ignore_polite = False; self.busy = set(); self.refuse_kill = False

    def now(self): return self.t
    def sleep(self, s): self.t += s
    def image_of(self, pid): return self.alive.get(pid)

    def spawn(self, argv, cwd, logfile):
        self.next_pid += 1; self.alive[self.next_pid] = "QQ.exe"; self.spawned.append(list(argv)); return self.next_pid

    def port_busy(self, port): return port in self.busy

    def terminate(self, pid, force=False):
        self.killed.append((pid, force))
        if self.refuse_kill: return 1
        if force or not self.ignore_polite: self.alive.pop(pid, None)
        return 0


def selftest(out=print):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td); (td / "napcat").mkdir(); (td / "napcat" / "napcat.bat").write_text("@echo off\n", encoding="utf-8")
        fr = FakeRunner()
        b = NativeBackend({"napcat_dir": str(td / "napcat"), "launch": ["napcat.bat"]}, fr, td / "p.json", td / "o.log", td / "l.log")
        assert b.state() == "absent"
        rc, pid, _ = b.start(); assert rc == 0 and b.state() == "running" and len(fr.spawned) == 1
        assert b.start()[1] == "already running" and len(fr.spawned) == 1
        b.stop(); assert b.state() == "exited"
        rc, _, err = b.start(); assert rc == 1 and len(fr.spawned) == 1, "gap rule"
        fr.t += MIN_START_GAP + 1
        assert b.start()[0] == 0 and len(fr.spawned) == 2
        fr.alive.clear(); assert b.state() == "exited"
        assert "START" in (td / "l.log").read_text(encoding="utf-8")
    out("native backend selftest: OK (fake runner; no real process was started)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    print(__doc__)
