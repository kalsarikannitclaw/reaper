"""Coverage watermark: 'every message up to this time is in the database' (S3b).

Why: the old sweep started from the newest STORED message minus 1 h. After an outage the live listener stores a new
message first, the newest stored time jumps past the gap, and the next sweep never looks back at it. The watermark only
moves forward when the gap is provably closed: a complete history sweep (every group reached its window start or ran out
of history, no query failed), or the live socket has been connected continuously since the last such sweep.
Stored next to the database (capture/coverage.json); contains only a time, no ids or text.
"""
import json, os, time


def path_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "coverage.json")


def load(db_path):
    """-> epoch seconds or None (no watermark yet)."""
    try:
        v = json.load(open(path_for(db_path), encoding="utf-8")).get("covered_until")
        return int(v) if v else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def advance(db_path, ts):
    """Move the watermark forward (never back). Atomic write; returns the value now stored."""
    ts = int(ts)
    cur = load(db_path)
    if cur is not None and cur >= ts:
        return cur
    p = path_for(db_path)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"covered_until": ts}, f)
    os.replace(tmp, p)
    return ts


def start_from(db_path, group_last, now=None, default_days=7, overlap=3600):
    """Where a sweep for one group should start: the watermark if there is one, else the group's newest stored message,
    else default_days back; minus an overlap (duplicates are ignored by the UNIQUE key)."""
    now = int(now or time.time())
    base = load(db_path) or group_last or (now - default_days * 86400)
    return base - overlap
