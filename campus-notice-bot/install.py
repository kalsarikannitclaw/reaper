#!/usr/bin/env python3
"""Campus Notice Bot installer. Two packages share this file: the NATIVE WINDOWS one (no Docker, no WSL) and the LINUX one
(Docker). The package says which it is in target.json; running the wrong one stops. Standard library only.

What it does:  venv (.venv) -> pinned requirements -> config files from templates -> asks for YOUR OWN chat-API key
(hidden prompt, never echoed, stored only in stack/.env) ->
  Linux:   creates the NapCat container WITHOUT starting it, registers systemd user units.
  Windows: sets up the NapCat folder you give it (--napcat-zip with a checked hash, or --napcat-dir), writes its loopback-only
           OneBot/WebUI config, registers Task Scheduler jobs. It never downloads NapCat or QQ by itself.
What it never does: ask for a QQ password or code, start NapCat (only bridge_guard does), log in, or send messages.
QQ login = you scan the QR with your own phone (see README-INSTALL.md, First run).

  python install.py                interactive
  python install.py --dry-run      print the plan, change nothing
  python install.py --no-services  skip systemd / Task Scheduler
  python install.py --uninstall    remove ONLY the services this installer created here (files and data stay)
  python install.py --name NAME    use unit/task/project names NAME-... (required when another install already exists)
  python install.py --enable-guard start the bridge guard on purpose (after the API key is set and you are ready to scan)
Windows only:
  --napcat-dir DIR      use a NapCat folder you already unpacked
  --napcat-zip FILE     unpack this NapCat zip into the napcat folder (its SHA-256 is shown; give --sha256 HASH to have it checked, or
                        confirm it by typing yes; with --yes and no --sha256 the zip is NOT used)
  --sha256 HASH         the hash the official source publishes for that zip
  --napcat-launch NAME  the start file inside the NapCat folder (e.g. napcat.bat); chosen from a list when omitted

Existing installs: if a unit/task named like ours, or a container <project>-napcat-1, already exists and was not made by
THIS folder's installer, the installer stops and changes nothing. Re-run with a different --name.
"""
import argparse, getpass, json, os, platform, re, secrets, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try: TARGET = json.loads((ROOT / "target.json").read_text(encoding="utf-8")).get("target")
except (OSError, ValueError): TARGET = None
TARGET = TARGET or ("windows" if os.name == "nt" else "linux")      # a dev checkout has no target.json: follow the OS
IS_WIN = TARGET == "windows"                                         # True = the native Windows package (no Docker, no WSL)
VENV = ROOT / ".venv"
VPY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
ENVF = ROOT / "stack" / ".env"
MARK = ROOT / ".installed.json"          # what THIS installer created here (units/tasks/container): uninstall touches only this
DRY = False
NAME = ""                                # --name suffix; "" = default names


def prefix(): return "campus" if not NAME else f"campus-{NAME}"
def project(): return "stack" if not NAME else f"stack-{NAME}"
def container(): return f"{project()}-napcat-1"
def units(): return [f"{prefix()}-{k}.service" for k in ("bridge-guard", "capture", "web", "daily")] + [f"{prefix()}-daily.timer"]
def tasks():
    b = "CampusNoticeBot" + (f"-{NAME}" if NAME else "")
    return [f"{b}-BridgeGuard", f"{b}-Capture", f"{b}-Web", f"{b}-Daily"]
def unit_dir(): return Path.home() / ".config/systemd/user"


def die(m): raise SystemExit("\nSTOPPED: " + m)


def pre_check_target(dry):
    """The two packages are not interchangeable: the Windows one has no Docker parts, the Linux one has no Task Scheduler parts."""
    if IS_WIN != (os.name == "nt"):
        msg = f"this is the {'WINDOWS' if IS_WIN else 'LINUX'} package but this computer runs {'Windows' if os.name == 'nt' else 'Linux/macOS'}. Use the other zip."
        if not dry: die(msg)
        say("  NOTE (dry-run only): " + msg)


def say(m=""): print(m, flush=True)


def run(cmd, must=False, **kw):
    say("  $ " + " ".join(str(c) for c in cmd))
    if DRY: return 0
    try: rc = subprocess.run([str(c) for c in cmd], **kw).returncode
    except OSError as e: rc = 127; say(f"  could not run: {e}")
    if rc and must: die(f"command failed (exit {rc}): {' '.join(str(c) for c in cmd[:4])} ... nothing further was done.")
    return rc


def sh_out(cmd):
    try: return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError): return None


def load_mark():
    try: return json.loads(MARK.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}


def save_mark(d):
    if not DRY: MARK.write_text(json.dumps(d, indent=1), encoding="utf-8")


def check_existing(have_docker, probe_ports=False):
    """M1: refuse when something with our names exists and this folder did not create it."""
    say("\n[1b] Looking for an existing install (so it is never overwritten)")
    mine = load_mark(); bad = []
    if IS_WIN:
        for t in tasks():
            r = sh_out(["schtasks", "/Query", "/TN", t])
            if r is not None and r.returncode == 0 and t not in mine.get("tasks", []): bad.append(f"scheduled task {t}")
    else:
        for u in units():
            p = unit_dir() / u
            if p.exists():
                txt = p.read_text(encoding="utf-8", errors="replace")
                if u not in mine.get("units", []) or str(ROOT) not in txt: bad.append(f"systemd unit {u} ({'points elsewhere' if str(ROOT) not in txt else 'not created by this installer'})")
    if have_docker:
        r = sh_out(["docker", "inspect", container(), "--format", "{{.Name}}"])
        if r is not None and r.returncode == 0 and container() not in mine.get("containers", []): bad.append(f"container {container()}")
    if probe_ports:                       # opt-in only (--check-ports): a TCP connect to a live NapCat port is not free of side effects
        import socket
        for port in (3001, 6099, 8790):
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) == 0 and not mine: say(f"  note: something already listens on 127.0.0.1:{port} (another install?).")
    if bad:
        die("an existing install was found: " + "; ".join(bad) + ".\n  This installer will not touch it (it could drop a live QQ login).\n"
            "  Use a different explicit name:  python install.py --name second   (units, tasks and container get that name).\n"
            "  Note the NapCat ports 3001/6099/8790 are fixed: two installs cannot run side by side, stop the other one first.")
    say("  none found")


def write(path, text, mode=None):
    if DRY: say(f"  (dry-run) would write {path}"); return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if mode and not IS_WIN: os.chmod(path, mode)


def copy_if_missing(src, dst):
    if dst.exists(): say(f"  keep existing {dst.relative_to(ROOT)}"); return
    if DRY: say(f"  (dry-run) would create {dst.relative_to(ROOT)}"); return
    dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dst); say(f"  created {dst.relative_to(ROOT)}")


# ------------------------------------------------------------------ env file (secrets stay here, never printed)
def read_env():
    d = {}
    if ENVF.exists():
        for l in ENVF.read_text(encoding="utf-8").splitlines():
            if "=" in l and not l.lstrip().startswith("#"):
                k, v = l.split("=", 1); d[k.strip()] = v.strip()
    return d


def write_env(d):
    keep = [l for l in (ENVF.read_text(encoding="utf-8").splitlines() if ENVF.exists() else [])
            if l.lstrip().startswith("#") or not l.strip()]
    if not keep: keep = ["# Campus Notice Bot local secrets. NEVER share this file."]
    body = "\n".join(keep + [f"{k}={v}" for k, v in d.items()]) + "\n"
    write(ENVF, body, 0o600)


def step_env(args):
    say("\n[3/7] Local settings and your chat-API key")
    d = read_env()
    for k in ("WEBUI_TOKEN", "CAPTURE_WS_TOKEN"):
        if not d.get(k): d[k] = secrets.token_hex(16) if not DRY else "<generated>"
    d.setdefault("ACCOUNT", ""); d.setdefault("DEEPSEEK_API_KEY", "")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}
    name = cfg.get("key_env", "DEEPSEEK_API_KEY"); d.setdefault(name, "")
    if d.get(name):
        say(f"  {name} already set ({'*' * 6}); keep it. Replace later with: python -m cnb.cli key set")
    elif args.yes or DRY or not sys.stdin.isatty():
        say(f"  No key entered. Add it later:  <venv python> -m cnb.cli key set   (capture works without it)")
    else:
        say(f"  Paste YOUR OWN {cfg.get('model', 'chat')} API key for {cfg.get('base_url', 'the provider')}.")
        say("  Typing is hidden. Press Enter to skip (you can add it later). It is stored only in stack/.env.")
        k = getpass.getpass("  API key: ").strip()
        if k: d[name] = k
    qq = d.get("ACCOUNT", "")
    if not qq and not (args.yes or DRY or not sys.stdin.isatty()):
        qq = input("  Your OWN QQ number (optional, Enter to skip; no password is ever asked): ").strip()
        if qq and not re.fullmatch(r"\d{5,12}", qq): say("  not a QQ number, ignored"); qq = ""
        d["ACCOUNT"] = qq
    write_env(d)
    # NapCat OneBot network config: ws server on 3001 with the local token. UNVERIFIED against every NapCat build:
    # see README "Troubleshooting" if the listener cannot connect.
    if IS_WIN:
        say("  (NapCat's own config is written in the NapCat step, into the NapCat folder)")
    elif not DRY:
        cdir = ROOT / "stack" / "data" / "config"; cdir.mkdir(parents=True, exist_ok=True)
        for fn in ["onebot11.json"] + ([f"onebot11_{qq}.json"] if qq else []):
            p = cdir / fn
            if not p.exists():
                p.write_text(json.dumps({"network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketClients": [],
                    "websocketServers": [{"name": "ws", "enable": True, "host": "0.0.0.0", "port": 3001, "messagePostFormat": "array",
                                          "reportSelfMessage": False, "token": d["CAPTURE_WS_TOKEN"], "enableForcePushEvent": True,
                                          "debug": False, "heartInterval": 30000}]},
                    "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}, indent=1), encoding="utf-8")
                if not IS_WIN: os.chmod(p, 0o600)
    else:
        say("  (dry-run) would write stack/data/config/onebot11.json (local ws token)")


# ------------------------------------------------------------------ steps
def step_checks():
    say("[1/7] Checking this computer")
    say(f"  OS: {platform.system()} {platform.release()}   Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 9): die("Python 3.9 or newer is required.")
    if sys.version_info < (3, 14):
        say("  WARNING: only Python 3.14.9 was ever tested with the pinned packages (websockets 17.4, pillow 13.0.0). On older Pythons "
            "pip may refuse them: if so install Python 3.14 or loosen requirements.txt yourself.")
    if IS_WIN:
        say("  mode: native Windows (no Docker, no WSL needed)")
        tess = shutil.which("tesseract") or next((str(Path(os.environ[v]) / "Tesseract-OCR" / "tesseract.exe") for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                                                  if os.environ.get(v) and (Path(os.environ[v]) / "Tesseract-OCR" / "tesseract.exe").is_file()), None)
        say("  tesseract: " + (tess or "not found (optional: picture text OCR is off until installed; see COMPONENTS.md)"))
        return False
    docker = shutil.which("docker")
    say("  docker: " + (docker or "NOT FOUND. Install Docker Engine before the QQ bridge can run."))
    tess = shutil.which("tesseract")
    say("  tesseract: " + (tess or "not found (optional: picture text OCR is off until installed; see COMPONENTS.md)"))
    return bool(docker)


def step_venv():
    say("\n[2/7] Python environment (.venv) and pinned packages")
    if not VPY.exists(): run([sys.executable, "-m", "venv", VENV], must=True)
    run([VPY, "-m", "pip", "install", "--disable-pip-version-check", "-r", ROOT / "requirements.txt"], must=True)


def step_files():
    say("\n[3/7] Configuration files (never overwritten if they exist)")
    t = ROOT / "templates"
    for src, dst in [("config.template.json", "config.json"), ("groups.sample.json", "capture/groups.json"),
                     ("interests.template.json", "interests.json"), ("filters.template.json", "filters.json"),
                     ("contest_whitelist.template.json", "contest_whitelist.json"),
                     ("research_sources.template.json", "research_sources.json")] + ([] if IS_WIN else [("compose.template.yaml", "stack/compose.yaml")]):
        copy_if_missing(t / src, ROOT / dst)
    if not DRY:
        for d in ("reports", "logs", "capture") + (() if IS_WIN else ("stack/data/qq", "stack/data/plugins")): (ROOT / d).mkdir(parents=True, exist_ok=True)
        write(ROOT / "service.json", json.dumps({"backend": "native", "name": NAME} if IS_WIN else {"backend": "docker", "container": container(), "project": project(), "name": NAME}))
    say("  capture/groups.json holds FAKE sample ids: put your own groups in it (README, First run step 3).")


def step_container(have_docker):
    say("\n[5/7] NapCat container (created, NOT started)")
    if not have_docker: say("  skipped: no docker. Install Docker, then run:  docker compose -p " + project() + " up --no-start  (in stack/)"); return False
    say("  Only bridge_guard may start it. Login cap 6 per 24 h, restart policy \"no\". Creating it does not log in to QQ.")
    r = None if DRY else sh_out(["docker", "inspect", container(), "--format", "{{.State.Status}}"])
    if r is not None and r.returncode == 0:
        say(f"  container {container()} already exists ({r.stdout.strip()}): left exactly as it is (no compose up, no recreate, no restart).")
        save_mark({**load_mark(), "containers": sorted(set(load_mark().get("containers", [])) | {container()})})
        return True
    rc = run(["docker", "compose", "-p", project(), "--env-file", ".env", "up", "--no-start"], cwd=ROOT / "stack")
    if rc: say("  WARNING: container creation failed (is Docker running?). Services that need it are NOT enabled; fix Docker and re-run."); return False
    save_mark({**load_mark(), "containers": sorted(set(load_mark().get("containers", [])) | {container()})})   # recorded at once
    return True


# ------------------------------------------------------------------ native Windows NapCat (no Docker)
def _native():
    sys.path.insert(0, str(ROOT / "stack"))
    try:
        import native_win
        return native_win
    finally: sys.path.pop(0)


def step_napcat_native(args):
    """Set up the NapCat folder the user supplies. Never downloads, never runs NapCat, never runs an unverified zip.
    Why no download: on 2026-11-15 the official repository had no releases (README only "Ending"), so there is no official
    URL or published hash this installer could pin. UNVERIFIED facts are listed in COMPONENTS.md."""
    say("\n[5/7] NapCat for Windows (native, no Docker). The installer does NOT download it.")
    nw = _native()
    ndir = None
    if args.napcat_zip:
        z = Path(args.napcat_zip)
        if not z.is_file(): die(f"--napcat-zip {z} not found.")
        got = nw.sha256_file(z)
        say(f"  SHA-256 of {z.name}: {got}")
        if args.sha256:
            if args.sha256.strip().lower() != got: die("the zip's SHA-256 does not match --sha256. Not unpacked. Download it again from the official source.")
            say("  matches the hash you gave: ok")
        elif args.yes or DRY or not sys.stdin.isatty():
            say("  No --sha256 given and no one to confirm: the zip is NOT unpacked. Compare the hash above with the official release page, then re-run with --sha256 HASH.")
            return None
        elif input("  Compare it with the hash on the official release page. Type yes to trust this file: ").strip().lower() != "yes":
            say("  not confirmed: nothing unpacked."); return None
        ndir = ROOT / "napcat"
        if ndir.exists() and any(ndir.iterdir()): die(f"{ndir} already exists and is not empty: use --napcat-dir {ndir} instead.")
        if DRY: say(f"  (dry-run) would unpack into {ndir}")
        else:
            nw.safe_extract(z, ndir)
            subs = [p for p in ndir.iterdir() if p.is_dir()]
            if len(subs) == 1 and not nw.find_launchers(ndir) and not any(p.is_file() for p in ndir.iterdir()): ndir = subs[0]
            say(f"  unpacked into {ndir}")
    elif args.napcat_dir:
        ndir = Path(args.napcat_dir).resolve()
        if not ndir.is_dir() and not DRY: die(f"--napcat-dir {ndir} is not a folder.")
    else:
        say("  No NapCat given. The bot, report and chat work without it; only the QQ bridge needs it. To add it later:")
        say("    python install.py --napcat-dir C:\\path\\to\\NapCat     (a folder you unpacked yourself)")
        say("    python install.py --napcat-zip FILE --sha256 HASH     (HASH from the official release page)")
        say("  See README-INSTALL-WINDOWS.md, step 'NapCat'.")
        return None
    launch = args.napcat_launch
    if launch and (Path(launch).is_absolute() or ".." in Path(launch).parts or (not DRY and not (ndir / launch).is_file())):
        die("--napcat-launch must be the name of a file inside the NapCat folder.")
    if not launch:      # a re-run must not blank the start file chosen earlier (same NapCat folder)
        try:
            old = json.loads((ROOT / "native.json").read_text(encoding="utf-8"))
            if old.get("launch") and Path(old.get("napcat_dir", "")).resolve() == Path(ndir).resolve(): launch = old["launch"][0]; say(f"  keeping the start file chosen before: {launch}")
        except (OSError, ValueError, IndexError, TypeError): pass
    if not launch and not DRY and ndir.is_dir():
        cands = nw.find_launchers(ndir)
        if len(cands) >= 1 and not (args.yes or not sys.stdin.isatty()):
            for i, c in enumerate(cands, 1): say(f"    {i}. {c}")
            pick = input("  Which file starts NapCat (number, Enter = none yet): ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(cands): launch = cands[int(pick) - 1]
        elif cands: say("  candidate start files found: " + ", ".join(cands) + "  (choose one with --napcat-launch NAME)")
        else: say("  no known start file found in the NapCat folder; choose it with --napcat-launch NAME")
    if DRY: say(f"  (dry-run) would write native.json and NapCat's config into {ndir}\\config"); return ndir
    (ROOT / "native.json").write_text(json.dumps({"napcat_dir": str(ndir), "launch": [launch] if launch else None}, indent=1), encoding="utf-8")
    d = read_env()
    wrote = nw.write_napcat_config(ndir, d["CAPTURE_WS_TOKEN"], d["WEBUI_TOKEN"], d.get("ACCOUNT", ""), say)
    say("  wrote NapCat config (loopback only: ws 127.0.0.1:3001, WebUI 127.0.0.1:6099): " + (", ".join(wrote) or "nothing new"))
    say("  Whether this NapCat build reads exactly these files is UNVERIFIED: after the first login open http://127.0.0.1:6099 and `python stack/native_win.py --check`.")
    say("  If NapCat needs a separately installed QQ, its own README names the version: install that one yourself from the official QQ site; this installer never downloads QQ.")
    if not launch: say("  No start file chosen yet: the guard cannot start NapCat until native.json has one (re-run with --napcat-launch NAME).")
    save_mark({**load_mark(), "napcat_dir": str(ndir)})
    return ndir


# ------------------------------------------------------------------ services
def unit_text(name, ex, extra="", wd=True):
    return f"[Unit]\nDescription=Campus Notice Bot {name}\nAfter=docker.service network.target\n\n[Service]\n" \
           f"{'WorkingDirectory=' + str(ROOT) + chr(10) if wd else ''}Environment=PYTHONUNBUFFERED=1\nEnvironment=\"PYTHONPATH={ROOT / 'src'}\"\n" \
           f"Environment=NO_PROXY=localhost,127.0.0.1\nEnvironment=\"CNB_CONTAINER={container()}\"\n{extra}ExecStart={ex}\n"


def linux_services(tz, docker_ok):
    say(f"\n[6/7] systemd user units (~/.config/systemd/user), prefix {prefix()}-")
    ud = unit_dir(); py = f'"{VPY}" -I'; pf = prefix()
    q = lambda p: f'"{ROOT / p}"'
    files = {
        f"{pf}-bridge-guard.service": unit_text("bridge guard (only starter of NapCat)", f"{py} {q('stack/bridge_guard.py')}",
                                                 "Restart=always\nRestartSec=30\n") + "\n[Install]\nWantedBy=default.target\n",
        f"{pf}-capture.service": unit_text("capture listener", f"{py} {q('capture/listener.py')}", "Restart=always\nRestartSec=5\n")
                                  + "\n[Install]\nWantedBy=default.target\n",
        f"{pf}-web.service": unit_text("the bot's one program: calendar + report + chat (localhost only)",
                                        f'"{VPY}" {q("tools/talk.py")}', "Restart=always\n")
                              + "\n[Install]\nWantedBy=default.target\n",
        f"{pf}-daily.service": unit_text("daily run", f'"{VPY}" -m cnb.cli daily', "Type=oneshot\n"),
        f"{pf}-daily.timer": f"[Unit]\nDescription=Campus Notice Bot daily run at 21:00 {tz}\n\n[Timer]\nOnCalendar=*-*-* 21:00:00 {tz}\n"
                              f"Persistent=true\nUnit={pf}-daily.service\n\n[Install]\nWantedBy=timers.target\n",
    }
    for n, t in files.items(): write(ud / n, t)
    save_mark({**load_mark(), "units": list(files), })
    if not shutil.which("systemctl"):
        say("  systemctl not found (on WSL enable systemd: /etc/wsl.conf [boot] systemd=true). Units written; enable them later."); return
    run(["systemctl", "--user", "daemon-reload"])
    for n in (f"{pf}-capture.service", f"{pf}-web.service", f"{pf}-daily.timer"):
        run(["systemctl", "--user", "enable", "--now", n])
    run(["systemctl", "--user", "enable", f"{pf}-bridge-guard.service"])      # enabled for the next login, NOT started now
    say("  The bridge guard is enabled but NOT started: starting it would start NapCat and use one of your 6 daily QQ logins.")
    say(f"  When your API key is set and your phone is ready:  python install.py --enable-guard{' --name ' + NAME if NAME else ''}")
    say("  Optional, so jobs run while you are logged out:  loginctl enable-linger $USER")


def win_user():
    u = os.environ.get("USERNAME", "")
    d = os.environ.get("USERDOMAIN", "")
    return f"{d}\\{u}" if d and u else u


def task_xml(trigger, args, user, restart="<Interval>PT1M</Interval><Count>999</Count>"):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    exe = str(VENV / "Scripts/pythonw.exe")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers>{trigger}</Triggers>
<Principals><Principal id="A"><UserId>{esc(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
<RestartOnFailure>{restart}</RestartOnFailure></Settings>
<Actions Context="A"><Exec><Command>{esc(exe)}</Command><Arguments>{esc(args)}</Arguments><WorkingDirectory>{esc(str(ROOT))}</WorkingDirectory></Exec></Actions></Task>"""


def register_task(name, trig, kind, user, restart=None):
    xml = ROOT / f".task-{name}.xml"
    if DRY: say(f"  (dry-run) would register task {name}"); return 0
    xml.write_text(task_xml(trig, f'-X utf8 "{ROOT / "svc_launch.py"}" {kind}', user, **({"restart": restart} if restart else {})), encoding="utf-16")
    rc = run(["schtasks", "/Create", "/F", "/TN", name, "/XML", xml]); xml.unlink(missing_ok=True)
    return rc


DAILY_RESTART = "<Interval>PT30M</Interval><Count>2</Count>"          # a failing daily run retries twice, 30 min apart


def windows_services(tz, docker_ok):
    say("\n[6/7] Task Scheduler jobs (current user only, no admin rights)")
    user = win_user()
    logon = f"<LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>"
    logon_off = f"<LogonTrigger><Enabled>false</Enabled><UserId>{user}</UserId></LogonTrigger>"   # guard: off until --enable-guard
    daily = "<CalendarTrigger><StartBoundary>2026-01-01T21:00:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>"
    names = tasks()
    jobs = [(names[0], logon_off, "guard", None), (names[1], logon, "capture", None), (names[2], logon, "web", None), (names[3], daily, "daily", DAILY_RESTART)]
    done = []
    for name, trig, kind, rs in jobs:
        if register_task(name, trig, kind, user, rs) == 0: done.append(name)
    save_mark({**load_mark(), "tasks": done})
    say("  Output of each job goes to logs\\<job>.log (no console windows).")
    say(f"  The daily run is at 21:00 on THIS computer's clock (the Linux build uses {tz}). Change it in Task Scheduler if needed.")
    say("  Capture, report server and daily run start at your next logon; start them now:  schtasks /Run /TN " + names[1] + "  (and " + names[2] + ")")
    say("  The bridge guard task is registered DISABLED so that nothing logs in to QQ before you are ready:  python install.py --enable-guard")


def enable_guard():
    mine = load_mark()
    if IS_WIN:
        t = tasks()[0]
        if t not in mine.get("tasks", []): die(f"task {t} was not created by this installer; run install.py first.")
        # enabling the task object alone is not enough: the trigger itself was registered disabled, so re-register with it enabled
        user = win_user(); logon = f"<LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>"
        if register_task(t, logon, "guard", user) != 0: die("could not re-register the guard task with an enabled trigger.")
        say("  Guard task enabled. It starts at next logon; start it now with:  schtasks /Run /TN " + t)
    else:
        u = f"{prefix()}-bridge-guard.service"
        if u not in mine.get("units", []): die(f"unit {u} was not created by this installer; run install.py first.")
        run(["systemctl", "--user", "start", u], must=True)
    say("  The guard now watches NapCat and starts it within about a minute if it is not running (one QQ login). "
        "Do this only after the API key is set; then scan the QR (README, First run step 2).")


def uninstall():
    mine = load_mark()
    if not mine: die("nothing recorded: this folder's installer created no services, so nothing is removed.")
    if IS_WIN:
        for t in mine.get("tasks", []): run(["schtasks", "/Delete", "/F", "/TN", t])
    else:
        for u in mine.get("units", []):
            p = unit_dir() / u
            if p.exists() and str(ROOT) in p.read_text(encoding="utf-8", errors="replace"):
                run(["systemctl", "--user", "disable", "--now", u])
                if not DRY: p.unlink()
            else: say(f"  skipped {u}: not ours (missing or points elsewhere)")
        run(["systemctl", "--user", "daemon-reload"])
    if not DRY: MARK.unlink(missing_ok=True)
    say("Services removed. Your files, .env, NapCat and its login data were left alone.")


def main():
    global DRY, NAME
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--no-services", action="store_true")
    ap.add_argument("--yes", action="store_true", help="no prompts (key can be added later)"); ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--name", default="", help="suffix for unit/task/compose names (needed when another install exists)")
    ap.add_argument("--check-ports", action="store_true", help="also probe 127.0.0.1:3001/6099/8790 (off by default: connecting to a live bridge port is not free)")
    ap.add_argument("--enable-guard", action="store_true", help="start the bridge guard on purpose")
    ap.add_argument("--napcat-dir", help="Windows: use this NapCat folder"); ap.add_argument("--napcat-zip", help="Windows: unpack this NapCat zip (hash checked)")
    ap.add_argument("--sha256", help="Windows: expected SHA-256 of --napcat-zip"); ap.add_argument("--napcat-launch", help="Windows: start file inside the NapCat folder")
    a = ap.parse_args(); DRY = a.dry_run; NAME = re.sub(r"[^A-Za-z0-9]", "", a.name)
    if a.name and NAME != a.name: die("--name may contain only letters and digits")
    if DRY: say("DRY RUN: nothing will be changed.\n")
    if a.uninstall: return uninstall()
    if a.enable_guard: return enable_guard()
    pre_check_target(DRY)
    have_docker = step_checks()
    check_existing(have_docker, a.check_ports)
    step_venv(); step_files(); step_env(a)
    if IS_WIN: step_napcat_native(a); container_ok = True
    else: container_ok = step_container(have_docker)
    cfg = ROOT / "config.json"
    tz = (json.loads(cfg.read_text(encoding="utf-8")).get("tz") if cfg.exists() else None) or "Asia/Shanghai"
    if a.no_services: say("\n[6/7] services skipped (--no-services)")
    elif have_docker and not container_ok and not DRY: say("\n[6/7] services skipped because the container could not be created")
    elif IS_WIN: windows_services(tz, container_ok)
    else: linux_services(tz, container_ok)
    say("\n[7/7] Done. NEXT, the QQ login (you do this yourself; no password or code is ever requested):")
    py = r".venv\Scripts\python" if IS_WIN else ".venv/bin/python"
    say("  0. add your API key if you skipped it:  " + ("cnb key set" if IS_WIN else "./cnb.sh key set"))
    say(f"  1. {py} stack/bridge_guard.py --qr        (starts NapCat through the guard and saves the login QR)")
    say("  2. scan the QR with YOUR phone QQ (or open http://127.0.0.1:6099 and scan there)")
    say(f"  3. {py} stack/bridge_guard.py --status    -> state should become \"online\"")
    say("  4. python install.py --enable-guard" + (f" --name {NAME}" if NAME else "") + "   (lets the guard watch the bridge from now on)")
    say("  Never start/restart NapCat by hand" + (" (not the window, not the .bat)" if IS_WIN else " (container)") + ": 6 logins per 24 h is the cap. See " + ("README-INSTALL-WINDOWS.md." if IS_WIN else "README-INSTALL-LINUX.md."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
