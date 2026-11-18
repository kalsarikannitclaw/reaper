"""Service launcher for the NATIVE WINDOWS package (Task Scheduler runs it). Standard library only.
  pythonw svc_launch.py guard|capture|web|daily
Why: pythonw has no stdout/stderr (a local web server's console output would fail), console programs started from
it (tesseract, taskkill) would flash a window each time, and a Task Scheduler action cannot set environment variables.
So: output goes to logs/<kind>.log, child processes get CREATE_NO_WINDOW, UTF-8 mode is switched on (Windows' legacy code
page would break Chinese text in config files), and service.json (written by install.py) names the backend ("native").
It starts nothing by itself except the one script named. NapCat is never started here: only stack/bridge_guard.py does that."""
import json, os, runpy, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
kind = sys.argv[1] if len(sys.argv) > 1 else ""
cfg = {}
try: cfg = json.loads((ROOT / "service.json").read_text(encoding="utf-8"))
except (OSError, ValueError): pass
(ROOT / "logs").mkdir(exist_ok=True)
_lp = ROOT / "logs" / f"{kind or 'svc'}.log"
try:
    if _lp.exists() and _lp.stat().st_size > 1_000_000:      # simple rotation: keep one old file (about 2 MB per job at most)
        _lp.replace(_lp.with_suffix(".log.1"))
except OSError: pass
log = open(_lp, "a", buffering=1, encoding="utf-8")
sys.stdout = sys.stderr = log
if os.name == "nt" and not sys.flags.utf8_mode:      # the task is registered with `-X utf8`; if it was started some other way, say so
    print("warning: Python is not in UTF-8 mode (start with: pythonw -X utf8 svc_launch.py <kind>); Chinese text in files may fail")
if os.name == "nt":
    _init = subprocess.Popen.__init__
    def _quiet(self, *a, **k):
        k["creationflags"] = k.get("creationflags", 0) | 0x08000000      # CREATE_NO_WINDOW
        _init(self, *a, **k)
    subprocess.Popen.__init__ = _quiet
sys.path.insert(0, str(ROOT / "src"))
os.environ["PYTHONPATH"] = str(ROOT / "src"); os.environ["PYTHONUNBUFFERED"] = "1"; os.environ["PYTHONUTF8"] = "1"; os.environ["PYTHONIOENCODING"] = "utf-8"; os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["CNB_BACKEND"] = cfg.get("backend") or "native"
os.chdir(ROOT)
if kind == "guard":
    sys.argv = [str(ROOT / "stack" / "bridge_guard.py")]; runpy.run_path(sys.argv[0], run_name="__main__")
elif kind == "capture":
    sys.argv = [str(ROOT / "capture" / "listener.py")]; runpy.run_path(sys.argv[0], run_name="__main__")
elif kind == "web":
    sys.argv = [str(ROOT / "tools" / "talk.py")]; runpy.run_path(sys.argv[0], run_name="__main__")      # one program: calendar + report + chat on 127.0.0.1:8790
elif kind == "daily":
    from cnb import cli
    sys.exit(cli.main(["daily"]))
else:
    print("usage: svc_launch.py guard|capture|web|daily"); sys.exit(2)
