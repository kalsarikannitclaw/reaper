"""Incremental alerts (PLAN S4/S5): a Windows toast from WSL when a NEW item needs the student.

Why a toast and not a QQ message: the bridge is listen-only on a main account, and sending from it is
exactly the kind of traffic that risks the account. A toast needs no network and no login.

What alerts: items created since the last alert that are (a) from a priority sender, or (b) due within 48h.
Everything else waits for the report. At most 3 individual toasts, then one summary, so a backlog never
becomes a notification storm. Each item alerts once (table `notified`). Past items never alert.
"""
import base64, datetime as dt, os, subprocess
from . import digest

PS = "powershell.exe"
APP = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"   # a registered AppId, so Windows will show the toast
MAX_INDIVIDUAL = 3

def _esc(s): return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")

def _cut(s, n=40):
    """Toast lines stay short: <= n characters, ellipsis when cut."""
    s = " ".join((s or "").split()); return s if len(s) <= n else s[:n - 1] + "…"

def toast(title, body, line2=""):
    """Show one toast. Returns True when PowerShell ran without error. Text goes through -EncodedCommand
    (UTF-16LE base64), so Chinese and quotes survive the WSL -> Windows hop."""
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{_esc(title)}</text><text>{_esc(body)}</text>"
           + (f"<text>{_esc(line2)}</text>" if line2 else "") + "</binding></visual></toast>")
    script = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
              "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
              f"$x=New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('{xml}');"
              f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP}').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        r = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def _ensure(db):
    db.execute("CREATE TABLE IF NOT EXISTS notified(item_id INTEGER PRIMARY KEY, at INTEGER, why TEXT)"); db.commit()

def pending(db, now=None):
    """New, un-alerted, not-past items that deserve an interruption, with the reason."""
    _ensure(db); now = now or dt.datetime.now(digest.Z); out = []
    for i in digest.items(db):
        if db.execute("SELECT 1 FROM notified WHERE item_id=?", (i["id"],)).fetchone(): continue
        d = digest._t(i["due"] or i["start"]); soon = d is not None and now <= d <= now + dt.timedelta(hours=48)
        if i["priority"] or soon: out.append((i, "priority sender" if i["priority"] else "within 48h"))
    return out

def alerts(db, send=toast, now=None, first_run_quiet=True):
    """Alert on pending items; returns how many items were covered. On the very first run ever, mark what
    already exists as seen instead of toasting the whole backlog."""
    _ensure(db)
    if first_run_quiet and not db.execute("SELECT 1 FROM notified LIMIT 1").fetchone():
        for i in digest.items(db): db.execute("INSERT OR IGNORE INTO notified VALUES(?,?,?)", (i["id"], 0, "baseline"))
        db.commit(); return 0
    p = pending(db, now); ts = int((now or dt.datetime.now(digest.Z)).timestamp())
    if not p: return 0
    ok = True
    for i, why in p[:MAX_INDIVIDUAL]:
        line = f"{digest.when_short(i)} · {i['title']} · {digest.where_short(i)}"
        _log(line); ok = send(_cut(digest.when_short(i)), _cut(i["title"]), (_cut(digest.where_short(i), 40 - len(" · confirm time")) + " · confirm time" if i["needs_confirm"] else _cut(digest.where_short(i)))) and ok
    if len(p) > MAX_INDIVIDUAL:
        _log(f"+{len(p) - MAX_INDIVIDUAL} more"); ok = send("Campus Notice Bot", f"+{len(p) - MAX_INDIVIDUAL} more new item(s)", "open calendar.html") and ok
    if not ok: return 0            # Windows interop unavailable: leave them pending so the next run retries; the log still has them
    db.executemany("INSERT OR IGNORE INTO notified VALUES(?,?,?)", [(i["id"], ts, why) for i, why in p]); db.commit()
    return len(p)

def _log(line):
    """Plain-text trail of every alert, so an alert is never lost when the toast cannot show."""
    try:
        with open(os.path.join(digest.ROOT_DIR, "reports", "alerts.log"), "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now(digest.Z):%Y-%m-%d %H:%M}  {line}\n")
    except OSError: pass
