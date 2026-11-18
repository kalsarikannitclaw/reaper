"""OCR for pictures posted in whitelisted groups (S8, 2026-11-14).

Before S8 every picture became the placeholder "[image]", so an exam arrangement posted as a screenshot matched no
keyword and was dropped before storage. Now each picture of a whitelisted-group message is read with tesseract
(chi_sim+eng), the keyword gate sees caption + picture text, and a stored message carries the picture text INSIDE
msg.text as a clearly labelled machine reading:

    [image][image text (OCR, conf 81%, zh): 期末考试安排 ... ]
    [image][image text (OCR failed: expired)]

plus a JSON summary in the nullable column msg.ocr (per picture: file id, sha256, size, ok, reason, confidence).
OCR text is a MACHINE READING, never a fact: digits in particular (dates, times, rooms, seats) can be wrong, so
whatever is extracted from it must be confirmed against the original picture (see stages/S8/build.md, hook).

Rules, enforced here:
  * only https URLs on qq.com / qpic.cn / qq.com.cn hosts (and every redirect must stay on them);
  * size cap (CNB_OCR_MAX_BYTES, default 10 MB), download + tesseract timeouts, at most 2 OCR jobs at once;
  * nothing here raises: every failure comes back as {"ok": False, "reason": ...};
  * the picture itself is NOT kept (only sha256 + size) unless CNB_OCR_KEEP_IMAGES=1;
  * picture text reaches the disk only once the message is STORED or HELD (remember()); until the gate decision is
    persisted an ok reading is logged as reason 'pending:*' with no text, so a crash before store() makes the next
    delivery read the picture again instead of treating it as "read and dropped" (round 3, N3);
  * two tesseract passes (with and without Pillow preprocessing); dates/times/rooms/weekdays only the weaker reading
    has are appended as a separate labelled block of fragments (round 2 F1, round 3 N4);
  * a weekday with no calendar date in the picture gets a labelled, UNCONFIRMED date suggestion computed from the
    message's send time (round 3; never shown to the keyword gate, never presented as read from the picture);
  * a failed or skipped OCR is VISIBLE: ocr_log table (one row per picture), capture/ocr_status.json counters,
    a log line, and a "[image text (OCR failed: why)]" marker in any stored/held text;
  * images and OCR text stay on this machine; only scrubbed text may later reach DeepSeek (extract.scrub).
Env: CNB_OCR=0 turns OCR off (pictures fall back to plain "[image]", and that is counted as "disabled").

S8b (2026-11-14, user decisions):
  * ocr_log rows older than N days are pruned (prune(); default 30, CNB_OCR_LOG_DAYS or "ocr_log_days" in the
    project's config.json; 0 = keep forever). Stored messages keep their picture text in msg.text: only the cache goes.
  * a PRIORITY sender's photo-only post whose pictures hold no text is HELD (labelled PHOTO_LABEL), never dropped,
    so a human can glance at the photo in QQ. Ordinary members' text-free photos are still dropped.
"""
import asyncio, hashlib, json, os, re, shutil, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "ocr_status.json")
CONFIG = os.path.join(os.path.dirname(HERE), "config.json")     # project config (read only): optional "ocr_log_days"
IMG_DIR = os.path.join(HERE, "ocr_images")

ALLOWED_SUFFIXES = ("qq.com", "qpic.cn", "qq.com.cn")
MAX_BYTES = int(os.environ.get("CNB_OCR_MAX_BYTES", 10 * 1024 * 1024))
DL_TIMEOUT = 20             # per socket operation ...
DL_DEADLINE = 60            # ... and for the whole download (a slow drip cannot hold a worker forever)
MAX_PIXELS = 40_000_000     # a 14000x14000 PNG drove tesseract to 2.1 GB (checker S8 L3); QQ pictures are far smaller
OCR_TIMEOUT = 90
MAX_IMAGES_PER_MSG = 9
LANGS = "chi_sim+eng"
PSM = 3
MIN_CONF = 35                # below this mean word confidence the reading is noise (measured: QR codes ~20%)
DPI = None
RETRY_AFTER = 1800          # a failed picture is retried at most every 30 min ...
MAX_TRIES = 3               # ... and at most 3 times (an expired CDN link stays expired)
_SEM = threading.BoundedSemaphore(2)
MARKER = re.compile(r"\[image text \(OCR[^\]]*\]")       # OCR text never contains "[" or "]" (sanitised), so this is exact
READ_OK = "[image text (OCR, conf"                     # prefix of a SUCCESSFUL reading's marker
CJK = re.compile(r"[一-鿿]")


def enabled():
    return os.environ.get("CNB_OCR", "1") != "0"


def tesseract_bin():
    win = [str(Path(os.environ[v]) / "Tesseract-OCR" / "tesseract.exe")       # the usual Windows installer location
           for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(v)]
    for c in (os.environ.get("CNB_TESSERACT"), shutil.which("tesseract"), os.path.expanduser("~/.local/bin/tesseract"),
              r"C:\Program Files\Tesseract-OCR\tesseract.exe", *win):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _fail(reason, **kw):
    return dict(ok=False, text="", lang=None, mean_conf=None, reason=reason, **kw)


# ---------------------------------------------------------------- download (allowlisted hosts only)
_NETLOC = re.compile(r"^([a-z0-9-]+\.)*[a-z0-9-]+\.?(:443)?$")       # plain host, optional :443. No userinfo/%/\\/odd ports


def host_allowed(url):
    """https on qq.com / qpic.cn / qq.com.cn (or a subdomain), default port only. Anything odd is refused, so this
    check and urllib can never disagree about which host is meant (checker S8 L1)."""
    if not isinstance(url, str) or "\\" in url or any(ord(c) < 33 for c in url):
        return False
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    net = u.netloc.lower()
    if u.scheme != "https" or not _NETLOC.match(net):
        return False
    h = net.split(":")[0].rstrip(".")
    return bool(h) and any(h == s or h.endswith("." + s) for s in ALLOWED_SUFFIXES)


class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_allowed(newurl):
            raise urllib.error.URLError(f"redirect off allowlist")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, max_bytes=None, timeout=DL_TIMEOUT, opener=None):
    """-> (bytes, None) or (None, reason). Never raises."""
    max_bytes = max_bytes or MAX_BYTES
    if not host_allowed(url):
        return None, "host_not_allowed"
    try:
        op = opener or urllib.request.build_opener(_GuardRedirect)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (campus-notice-bot image reader)"})
        with op.open(req, timeout=timeout) as r:
            if not host_allowed(r.geturl()):
                return None, "redirect_off_allowlist"
            n = r.headers.get("Content-Length")
            if n and n.isdigit() and int(n) > max_bytes:
                return None, "too_large"
            chunks, got, t_end = [], 0, time.monotonic() + DL_DEADLINE
            while True:
                c = (getattr(r, "read1", None) or r.read)(65536)    # read1: whatever has arrived, so the deadline
                                                                      # is checked even on a 1-byte drip (round 3, N2)
                if not c:
                    break
                chunks.append(c); got += len(c)
                if got > max_bytes:
                    return None, "too_large"
                if time.monotonic() > t_end:
                    return None, "timeout"
            buf = b"".join(chunks)
            ctype = (r.headers.get("Content-Type") or "").lower()
        if not buf:
            return None, "empty_download"
        if not _looks_like_image(buf):
            return None, "not_image" + (" (" + ctype.split(";")[0] + ")" if ctype else "")
        return buf, None
    except urllib.error.HTTPError as e:
        # QQ NT CDN links carry a time-limited rkey; an old link answers 4xx. That is "expired", not "no text".
        return None, ("expired" if e.code in (400, 401, 403, 404, 410) else f"http_{e.code}")
    except urllib.error.URLError as e:
        r = str(getattr(e, "reason", e))
        return None, ("redirect_off_allowlist" if "allowlist" in r else "timeout" if "timed out" in r else "network")
    except TimeoutError:
        return None, "timeout"
    except Exception as e:                       # never raise into the capture loop
        return None, "download_error:" + type(e).__name__


def _dims(b):
    """(width, height) from the PNG / GIF / JPEG header without Pillow, else via Pillow (lazy, no decode), else None."""
    try:
        import struct
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", b[16:24])
        if b[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", b[6:10])
        if b[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(b):
                if b[i] != 0xFF:
                    i += 1; continue
                m = b[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", b[i + 5:i + 9]); return w, h
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2; continue
                i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
        from PIL import Image
        import io
        return Image.open(io.BytesIO(b)).size
    except Exception:
        return None


def _looks_like_image(b):
    return (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or b[:6] in (b"GIF87a", b"GIF89a")
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP") or b[:2] == b"BM" or b[:4] in (b"II*\x00", b"MM\x00*"))


# ---------------------------------------------------------------- OCR
def _preprocess(b):
    """Optional: Pillow is NOT in the service venv, so this silently returns the input there. With Pillow, small
    pictures are upscaled (tesseract reads ~30px glyphs far better than ~12px ones) and converted to grayscale."""
    try:
        from PIL import Image
        import io
    except Exception:
        return b, False
    try:
        im = Image.open(io.BytesIO(b))
        im.load()
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im = im.convert("L")
        w, h = im.size
        f = 2 if max(w, h) < 1400 else 1
        if f > 1:
            im = im.resize((w * f, h * f), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue(), True
    except Exception:
        return b, False


def _tidy(text):
    text = re.sub(r"(?<=[一-鿿＀-￯　-〿]) +(?=[一-鿿＀-￯　-〿])", "", text)
    text = re.sub(r"(?<=[一-鿿]) +(?=\d)|(?<=\d) +(?=[一-鿿])", "", text)
    text = text.replace("[", "(").replace("]", ")")       # keeps the [image text (...)] marker unambiguous
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


def _parse_tsv(tsv):
    lines, confs = {}, []
    for row in tsv.splitlines()[1:]:
        p = row.split("\t")
        if len(p) < 12 or p[0] != "5":
            continue
        word = p[11].strip()
        try:
            c = float(p[10])
        except ValueError:
            continue
        if not word or c < 0:
            continue
        lines.setdefault((int(p[2]), int(p[3]), int(p[4])), []).append(word)
        confs.append((c, len(word)))
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    mean = round(sum(c * n for c, n in confs) / sum(n for _, n in confs), 1) if confs else None
    return text, mean


def lang_of(text):
    cjk = len(CJK.findall(text)); lat = len(re.findall(r"[A-Za-z]", text))
    if not cjk and not lat: return None
    return "zh" if lat < cjk * 0.3 else "en" if cjk < lat * 0.1 else "mixed"


def have_pillow():
    if os.environ.get("CNB_OCR_NO_PIL") == "1":          # tests / eval: simulate the service venv (no Pillow)
        return False
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


# Measured on the S8 sample set (stages/S8/build.md): with Pillow, grayscale + 2x upscale + --psm 1 --dpi 300 is best;
# without Pillow (the service venv today), plain --psm 3 is best on real QQ pictures, and --psm 1 --dpi 300 rescues
# rotated / table pictures that --psm 3 reads as garbage. So: first pass, and a second pass only when the first is weak.
PASSES_PIL = (dict(psm=1, dpi=300, preprocess=True), dict(psm=3, dpi=None, preprocess=False))
PASSES_NOPIL = (dict(psm=3, dpi=None, preprocess=False), dict(psm=1, dpi=300, preprocess=False))


def _better(a, b):
    """The stronger of two readings: ok beats not ok, then 'ok' beats 'low_conf', then higher mean confidence."""
    rank = lambda r: (bool(r.get("ok")), r.get("reason") == "ok", r.get("mean_conf") or 0)
    return b if rank(b) > rank(a) else a


# date / time / room / weekday tokens: the facts a second reading must not lose (checker S8 F1)
FACT = re.compile(r"\d{1,2}[:：]\d{2}(?:\s*[-–~至到]\s*\d{1,2}[:：]\d{2})?|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
                  r"|\d{1,2}月\d{1,2}[日号]|(?<![A-Za-z0-9])[A-Z]-?\d{3}(?![\dA-Za-z])"      # rooms: CASE-SENSITIVE (N4)
                  r"|(?i:\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b)|周[一二三四五六日天]|星期[一二三四五六日天]")
MAX_EXTRA_LINES = 20
EXTRA_LABEL = ("(second reading with other settings - separate fragments, NOT table rows, "
               "each line on its own:")                # our words, not the picture's: never shown to the keyword gate


def _facts(text):
    out = {}
    for m in FACT.finditer(text or ""):
        k = re.sub(r"\s+", "", m.group(0)).lower(); out[k] = out.get(k, 0) + 1
    return out


def _extra_lines(primary, other):
    """Lines of the other reading that hold a date/time/room/weekday the primary reading has fewer of."""
    have, want, out = _facts(primary), {}, []
    for k, n in _facts(other).items():
        if n > have.get(k, 0):
            want[k] = n - have.get(k, 0)
    for line in (other or "").splitlines():
        ks = [k for k in _facts(line) if want.get(k, 0) > 0]
        if ks:
            out.append(line.strip())
            for k in ks: want[k] -= 1
        if len(out) >= MAX_EXTRA_LINES:
            break
    return out


def ocr_bytes(b, langs=LANGS, psm=None, timeout=OCR_TIMEOUT, preprocess=None, dpi=None):
    """-> {ok, text, lang, mean_conf, reason, sha256, file_size, preprocessed, passes, extra_lines}. Never raises.
    psm/dpi/preprocess given -> exactly that one pass (eval harness). Otherwise BOTH passes always run (round 2,
    checker S8 F1: with Pillow the Moodle picture lost '17:10-17:35' at 84 % confidence, which the plain pass reads).
    The stronger reading is the text; any line of the other reading holding a date/time/room/weekday the stronger one
    lacks is appended as a labelled '(second reading, other settings: ...)'."""
    if psm is not None or preprocess is not None or dpi is not None:
        return _ocr_once(b, langs, psm or PSM, timeout, bool(preprocess), dpi)
    passes = PASSES_PIL if have_pillow() else PASSES_NOPIL
    a = _ocr_once(b, langs, timeout=timeout, **passes[0])
    if a.get("reason") in ("empty_download", "too_large", "not_image", "no_tesseract", "too_many_pixels", "ocr_timeout"):
        return dict(a, passes=1, extra_lines=0)           # the second pass cannot do better
    c = _ocr_once(b, langs, timeout=timeout, **passes[1])
    best = _better(a, c); other = c if best is a else a
    best = dict(best, passes=2, extra_lines=0)
    if best.get("ok") and other.get("ok"):
        extra = _extra_lines(best["text"], other["text"])
        if extra:
            best["text"] += "\n" + EXTRA_LABEL + "".join("\n- " + x for x in extra) + "\n)"
            best["extra_lines"] = len(extra)
    return best


def _ocr_once(b, langs, psm, timeout, preprocess, dpi):
    meta = {}
    try:
        if not b:
            return _fail("empty_download")
        meta = dict(sha256=hashlib.sha256(b).hexdigest(), file_size=len(b))
        if len(b) > MAX_BYTES:
            return _fail("too_large", **meta)
        if not _looks_like_image(b):
            return _fail("not_image", **meta)
        wh = _dims(b)
        if wh and wh[0] * wh[1] > MAX_PIXELS:
            return _fail("too_many_pixels", **meta)
        exe = tesseract_bin()
        if not exe:
            return _fail("no_tesseract", **meta)
        data, pre = _preprocess(b) if (preprocess and have_pillow()) else (b, False)
        cmd = [exe, "stdin", "stdout", "-l", langs, "--psm", str(psm)] + (["--dpi", str(dpi)] if dpi else []) + ["tsv"]
        with _SEM:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            return _fail("tesseract_error", **meta)
        text, mean = _parse_tsv(r.stdout.decode("utf-8", "replace"))
        text = _tidy(text)
        if not text:
            return dict(ok=False, text="", lang=None, mean_conf=mean, reason="no_text", preprocessed=pre, **meta)
        if (mean or 0) < MIN_CONF:          # QR codes, photos: tesseract "reads" noise at ~20% confidence. Not text.
            return dict(ok=False, text="", lang=None, mean_conf=mean, reason="unreadable", preprocessed=pre, **meta)
        return dict(ok=True, text=text, lang=lang_of(text), mean_conf=mean,
                    reason="ok" if (mean or 0) >= 60 else "low_conf", preprocessed=pre, **meta)
    except subprocess.TimeoutExpired:
        return _fail("ocr_timeout", **meta)
    except Exception as e:
        return _fail("ocr_error:" + type(e).__name__, **meta)


def ocr_url(url, **kw):
    """Download (allowlisted host, size cap, timeout) then OCR. Never raises."""
    if not enabled():
        return _fail("disabled")
    if not isinstance(url, str):
        return _fail("bad_url")
    b, why = fetch(url)
    if b is None:
        return _fail(why)
    res = ocr_bytes(b, **kw)
    if os.environ.get("CNB_OCR_KEEP_IMAGES") == "1" and res.get("sha256"):
        try:
            os.makedirs(IMG_DIR, exist_ok=True)
            p = os.path.join(IMG_DIR, res["sha256"][:32])
            if not os.path.exists(p):
                open(p, "wb").write(b)
        except OSError:
            pass
    return res


# ---------------------------------------------------------------- message helpers (shared by listener + backfill)
def _pictures(message):
    """All readable pictures of a message, in order (malformed segments are skipped, never raise)."""
    if not isinstance(message, list):
        return []
    out = []
    for s in message:
        if not isinstance(s, dict) or s.get("type") != "image" or not isinstance(s.get("data"), dict):
            continue
        d = s["data"]
        if str(d.get("sub_type", "0")) == "1" or "表情" in str(d.get("summary") or ""):
            continue
        out.append(d)
    return out


def image_segments(message):
    """Pictures worth reading (at most MAX_IMAGES_PER_MSG). Stickers (sub_type 1 / '[动画表情]') are skipped."""
    return _pictures(message)[:MAX_IMAGES_PER_MSG]


def image_key(d):
    """Stable id of a picture: QQ's file name, else the fileid in its URL, else a hash. Never raises (checker S8 F2)."""
    try:
        f = d.get("file") if isinstance(d, dict) else None
        f = f if isinstance(f, str) else ""
        if f and not f.startswith(("http", "base64:", "file:")):
            return f[:200]
        url = d.get("url") if isinstance(d, dict) and isinstance(d.get("url"), str) else f
        try:
            u = urllib.parse.urlsplit(url or "")
            fid = urllib.parse.parse_qs(u.query).get("fileid", [""])[0]
            return "fileid:" + (fid[:200] or hashlib.sha256((u.netloc + u.path).encode()).hexdigest()[:32])
        except ValueError:
            return "url:" + hashlib.sha256((url or "").encode()).hexdigest()[:32]
    except Exception:
        return "seg:" + hashlib.sha256(repr(d).encode()).hexdigest()[:32]


def _seg_text(s):
    if not isinstance(s, dict):
        return ""
    if s.get("type") == "text":
        t = (s.get("data") or {}).get("text", "") if isinstance(s.get("data"), dict) else ""
        return t if isinstance(t, str) else str(t)
    return f"[{s.get('type')}]"


def base_text(message, raw_message=""):
    """Exactly the pre-S8 text: placeholders for every non-text segment. Rows stored before S8 have this text."""
    if isinstance(message, list):
        return "".join(_seg_text(s) for s in message)
    return raw_message if isinstance(raw_message, str) else ""


def strip_ocr(text):
    return MARKER.sub("", text or "")


# ---------------------------------------------------------------- weekday-only pictures (round 3, user-approved)
import datetime as _dt
CN_TZ = _dt.timezone(_dt.timedelta(hours=8))           # Asia/Shanghai, no DST
SUGGEST_LABEL = "(suggested date from the message send date, UNCONFIRMED:"
_EN_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_CN_DAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAY = re.compile(r"(?i:\b(next\s+|this\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b)"
                      r"|(下|本|这)?(?:周|星期|礼拜)([一二三四五六日天])")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_HAS_DATE = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日号]?"
                       r"|(?<![\d.:])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d/.:])"     # 11/17, 11.17 (range-checked)
                       r"|第\s*[(（]?\s*[\d一二三四五六七八九十]+\s*[)）]?\s*周"                       # teaching week: anchors the weekday
                       r"|(?i:\b" + _MONTHS + r"\s*\d{1,2}(?:st|nd|rd|th)?\b|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS + r")"
                       r"|\d{1,2}\s*[日号](?!\s*[-–~至到]?\s*\d)")
_TODAY = re.compile(r"(?i:\btoday\b|\btonight\b)|今天|今日|今晚")


def has_date(text):
    """A calendar date (or a teaching-week number) anywhere in the text. 'm/d' and 'm.d' only count with a real month
    and day, so OCR junk like '25/48' does not suppress a suggestion."""
    for m in _HAS_DATE.finditer(text or ""):
        if m.group("m") is None or (1 <= int(m.group("m")) <= 12 and 1 <= int(m.group("d")) <= 31):
            return True
    return False


def suggest_dates(text, ts):
    """A picture names a weekday but no calendar date: suggest the NEXT occurrence of that weekday after the send date
    (China time). 'today'/'今天' in the picture lets the send day itself count; '下周X' / 'next X' means the X of the
    following Monday-to-Sunday week. Returns one labelled line, or None (a date is present, or no weekday). This is a
    GUESS from the send time, never something read from the picture; extraction must still ask to confirm."""
    try:
        if not text or not ts or has_date(text):
            return None
        sent = _dt.datetime.fromtimestamp(int(ts), CN_TZ)
        today_ok = bool(_TODAY.search(text))
        seen, parts = set(), []
        for m in _WEEKDAY.finditer(text):
            if m.group(2):
                wd, name, nxt = _EN_DAYS.index(m.group(2).lower()), m.group(2).capitalize(), (m.group(1) or "").strip().lower() == "next"
                if m.group(1): name = m.group(1).strip().capitalize() + " " + name
            else:
                wd, nxt = _CN_DAYS[m.group(4)], m.group(3) == "下"
                name = m.group(0)
            if name in seen:
                continue
            seen.add(name)
            if nxt:                                   # the X of next Monday-to-Sunday week
                d = sent.date() + _dt.timedelta(days=7 - sent.weekday() + wd)
            else:
                ahead = (wd - sent.weekday()) % 7
                if ahead == 0 and not today_ok:
                    ahead = 7
                d = sent.date() + _dt.timedelta(days=ahead)
            parts.append(f"{name} = {d.isoformat()}")
        if not parts:
            return None
        return f"{SUGGEST_LABEL} {', '.join(parts)}; sent {sent:%a %Y-%m-%d %H:%M} China time)"
    except Exception:
        return None


NOTEXT = ("no_text", "unreadable")       # the picture was read fine and simply holds no text (a photo, a QR code)


def marker(r, ts=None):
    if r.get("reason") in NOTEXT:
        return "[image text (OCR: no text found)]"
    if r.get("ok"):
        c = f"conf {int(round(r['mean_conf']))}%" if r.get("mean_conf") is not None else "conf ?"
        sug = suggest_dates(r["text"], ts) if ts else None
        return f"[image text (OCR, {c}, {r.get('lang') or '?'}): {r['text']}" + ("\n" + sug if sug else "") + "]"
    return f"[image text (OCR failed: {r.get('reason') or 'unknown'})]"


def compose(message, results, ts=None):
    """Stored text: the pre-S8 text with each read picture's marker right after its [image] placeholder. Pictures past
    the MAX_IMAGES_PER_MSG-th get a visible 'skipped' marker (checker S8 L4)."""
    if not isinstance(message, list):
        return base_text(message)
    it = iter(results); pics = _pictures(message)
    want = {id(d) for d in pics[:MAX_IMAGES_PER_MSG]}; over = {id(d) for d in pics[MAX_IMAGES_PER_MSG:]}
    out = []
    for s in message:
        out.append(_seg_text(s))
        d = s.get("data") if isinstance(s, dict) else None
        if id(d) in want:
            r = next(it, None)
            if r is not None:
                out.append(marker(r, ts))
        elif id(d) in over:
            out.append(f"[image text (OCR skipped: more than {MAX_IMAGES_PER_MSG} pictures)]")
    return "".join(out)


def gate_text(message, results):
    """What the keyword gate sees: caption plus the raw picture text (no marker words)."""
    return base_text(message) + "".join("\n" + r["text"].replace(EXTRA_LABEL, "\n") for r in results if r.get("ok") and r.get("text"))


def summary(results, gate=None):
    if not results:
        return None
    keep = ("file", "sha256", "file_size", "ok", "reason", "mean_conf", "lang")
    return json.dumps({"v": 1, "engine": "tesseract " + LANGS, "gate": gate,
                       "images": [{k: r.get(k) for k in keep} | {"chars": len(r.get("text") or "")} for r in results]},
                      ensure_ascii=False)


# ---------------------------------------------------------------- DB: migration, cache/log, status
def ensure(db):
    """Safe on the live DB: only CREATE IF NOT EXISTS and one nullable ADD COLUMN."""
    db.execute("""CREATE TABLE IF NOT EXISTS msg(
        id INTEGER PRIMARY KEY, group_id INTEGER, user_id INTEGER, nick TEXT,
        ts INTEGER, text TEXT, raw TEXT, priority INTEGER DEFAULT 0, role TEXT, UNIQUE(group_id, user_id, ts, text))""")
    cols = [r[1] for r in db.execute("PRAGMA table_info(msg)")]
    if "ocr" not in cols:
        db.execute("ALTER TABLE msg ADD COLUMN ocr TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS ocr_log(
        key TEXT PRIMARY KEY, group_id INTEGER, msg_ts INTEGER, sha256 TEXT, file_size INTEGER, ok INTEGER,
        reason TEXT, mean_conf REAL, lang TEXT, text TEXT, tries INTEGER DEFAULT 0, first_ts INTEGER, last_ts INTEGER)""")
    db.commit()


def cached(db, key, retry=False):
    r = db.execute("SELECT ok,reason,mean_conf,lang,text,sha256,file_size,tries,last_ts FROM ocr_log WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    ok, reason, conf, lang, text, sha, size, tries, last = r
    if (reason or "").startswith("pending:"):
        return None          # read, but the gate decision was never persisted (crash / error before store): read again
    res = dict(ok=bool(ok), reason=reason, mean_conf=conf, lang=lang, text=text or "", sha256=sha, file_size=size, file=key, cached=True)
    if ok:
        return None if (retry and not text) else res               # --reocr re-reads a picture whose text was forgotten
    if reason == "disabled":
        return None
    if reason in NOTEXT and not retry:
        return res                                                   # a photo stays a photo: never downloaded again
    if not retry and (tries >= MAX_TRIES or time.time() - (last or 0) < RETRY_AFTER):
        return res                                                   # recent / repeated failure: do not hammer the CDN
    return None


def log(db, key, res, group_id=None, msg_ts=None):
    """One row per picture. The picture's TEXT is never written here (checker S8 F3): it is written by remember() only
    once the message is stored or held, so a crash before the gate decides leaves no words of a dropped message."""
    now = int(time.time())
    db.execute("""INSERT INTO ocr_log(key,group_id,msg_ts,sha256,file_size,ok,reason,mean_conf,lang,text,tries,first_ts,last_ts)
                  VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
                  ON CONFLICT(key) DO UPDATE SET sha256=coalesce(excluded.sha256,sha256), file_size=coalesce(excluded.file_size,file_size),
                  ok=excluded.ok, reason=excluded.reason, mean_conf=excluded.mean_conf, lang=excluded.lang, text=excluded.text,
                  tries=tries+1, last_ts=excluded.last_ts""",
               (key, group_id, msg_ts, res.get("sha256"), res.get("file_size"), int(bool(res.get("ok"))),
                ("pending:" + str(res.get("reason"))) if res.get("ok") else res.get("reason"),   # N3: undecided != dropped
                res.get("mean_conf"), res.get("lang"), "", now, now))     # text only after the gate keeps it: remember()
    db.commit()
    bump(res)


def bump(res, path=None):
    """Counters in capture/ocr_status.json (bridge_guard owns bridge_status.json and rewrites it whole)."""
    path = path or STATUS
    try:
        try:
            st = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        st.setdefault("counts", {})
        k = "no_text" if res.get("reason") in NOTEXT else "ok" if res.get("ok") else "failed"
        st["counts"][k] = st["counts"].get(k, 0) + 1
        if k == "failed":
            st.setdefault("fail_reasons", {})
            st["fail_reasons"][res.get("reason") or "unknown"] = st["fail_reasons"].get(res.get("reason") or "unknown", 0) + 1
            st["last_failure"] = {"at": int(time.time()), "reason": res.get("reason")}
        elif res.get("reason") == "low_conf":
            st["counts"]["low_conf"] = st["counts"].get("low_conf", 0) + 1
        st["last"] = {"at": int(time.time()), "ok": bool(res.get("ok")), "reason": res.get("reason")}
        st["tesseract"] = bool(tesseract_bin()); st["enabled"] = enabled()
        _write_status(st, path)
    except Exception:
        pass


def _write_status(st, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:            # closed before os.replace: Windows cannot rename an open file
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- S8b: prune ocr_log after N days
LOG_DAYS_DEFAULT = 30


def log_days():
    """How many days ocr_log rows are kept: env CNB_OCR_LOG_DAYS, else "ocr_log_days" in config.json, else 30.
    0 means keep forever. A bad value (text, negative) falls back to the default, so a typo never deletes everything."""
    for v in (os.environ.get("CNB_OCR_LOG_DAYS"), _config_days()):
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            n = int(str(v).strip())
        except ValueError:
            return LOG_DAYS_DEFAULT
        return n if n >= 0 else LOG_DAYS_DEFAULT
    return LOG_DAYS_DEFAULT


def _config_days():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f).get("ocr_log_days")
    except (OSError, ValueError, AttributeError):
        return None


def prune(db, days=None, now=None, status_path=None):
    """Delete ocr_log rows whose last activity is older than `days` (default log_days()). Only the per-picture cache
    and log go: msg / held rows and their text are never touched. A pruned picture that QQ delivers again is simply
    read again. Records {"at", "days", "rows"} under "prune" in ocr_status.json. -> rows deleted. Never raises."""
    try:
        days = log_days() if days is None else int(days)
        if days <= 0:
            return 0
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_log'").fetchone():
            return 0
        now = int(now if now is not None else time.time())
        cut = now - days * 86400
        n = db.execute("DELETE FROM ocr_log WHERE coalesce(last_ts, first_ts, msg_ts, 0) < ?", (cut,)).rowcount
        db.commit()
        path = status_path or STATUS
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    st = json.load(f)
            except (OSError, ValueError):
                st = {}
            st["prune"] = {"at": now, "days": days, "rows": n}
            _write_status(st, path)
        except Exception:
            pass                                        # the counters file is a convenience; the prune itself is done
        if n:
            print(f"ocr_log: pruned {n} row(s) older than {days} days", flush=True)
        return n
    except Exception as e:
        print("ocr_log prune failed:", type(e).__name__, flush=True)
        return 0


async def read_images(db, message, group_id=None, msg_ts=None, retry=False, ocr_fn=None, count=True):
    """OCR every picture of one message, off the event loop. Cache lookups/writes stay on the caller's thread (sqlite)."""
    ocr_fn = ocr_fn or ocr_url
    out = []
    for d in image_segments(message):
        key = image_key(d)
        res = cached(db, key, retry) if db is not None else None
        if res is None:
            if not enabled():
                res = _fail("disabled")
            else:
                try:
                    res = await asyncio.to_thread(ocr_fn, d.get("url") or d.get("file") or "")
                except Exception as e:
                    res = _fail("ocr_error:" + type(e).__name__)
            res = dict(res, file=key)
            if db is not None:
                log(db, key, res, group_id, msg_ts)
            elif count:
                bump(res)
            print(f"ocr g{group_id} {key[:16]}: {'ok' if res.get('ok') else 'FAILED'} {res.get('reason')}"
                  f" conf={res.get('mean_conf')} chars={len(res.get('text') or '')}", flush=True)
        out.append(res)
    return out


def _final(r):
    return str(r.get("reason") or "ok").replace("pending:", "")


def remember(db, results):
    """The message was stored or held: keep its pictures' text in ocr_log too (cache for re-sweeps)."""
    if db is None:
        return
    for r in results:
        if r.get("file") and r.get("ok") and r.get("text"):
            db.execute("UPDATE ocr_log SET text=?, reason=? WHERE key=? AND ok=1", (r["text"], _final(r), r["file"]))
    db.commit()


def forget(db, results):
    """The message was DROPPED. log() never writes picture text, so normally there is nothing to clear; this is a
    belt-and-braces guarantee for rows this message itself logged. A cached reading that a stored or held message
    already keeps is left alone."""
    if db is None:
        return
    for r in results:
        if r.get("file") and r.get("ok") and not r.get("cached"):
            db.execute("UPDATE ocr_log SET text='', reason=? WHERE key=? AND reason LIKE 'pending:%'", (_final(r), r["file"]))
    db.commit()


def find_existing(db, table, g, u, ts, base):
    """A row for this same message, stored with or without picture text (so OCR never creates a second row)."""
    for rid, t in db.execute(f"SELECT id,text FROM {table} WHERE group_id=? AND user_id=? AND ts=?", (g, u, ts)):
        if t == base or strip_ocr(t) == base:
            return rid, t
    return None


async def gate(db, ev_message, raw_message, g, u, ts, prio, kw_match, retry=False, ocr_fn=None, dry=False):
    """The S8 gate for one whitelisted-group message.
    -> dict(hit, text, ocr, base, results, existing)
       existing: id of a msg row already stored for this message (then nothing is inserted and no OCR is run).
    dry=True: read the pictures but write nothing (no ocr_log, no counters)."""
    base = base_text(ev_message, raw_message)
    imgs = image_segments(ev_message)
    if not imgs:
        return dict(hit=kw_match(base, prio), text=base, ocr=None, base=base, results=[], existing=None)
    ex = find_existing(db, "msg", g, u, ts, base) if db is not None else None
    if ex:
        return dict(hit=None, text=ex[1], ocr=None, base=base, results=[], existing=ex[0])
    cdb = None if dry else db
    try:
        results = await read_images(cdb, ev_message, g, ts, retry=retry, ocr_fn=ocr_fn, count=not dry)
    except Exception as e:                    # a broken picture must never cost the caption (checker S8 F2)
        print("picture reading failed:", type(e).__name__, e, flush=True)
        results = [dict(_fail("ocr_error:" + type(e).__name__), file=image_key(d)) for d in imgs]
        if not dry: bump(results[0])
    cap_hit = kw_match(base, prio)
    if (cap_hit or prio) and any(r.get("ok") and not r.get("text") for r in results):
        # it will be stored (caption) or held (priority sender), but a picture's text was forgotten on an earlier drop:
        # read it again so the stored/held text never shows an empty reading
        results = await read_images(cdb, ev_message, g, ts, retry=True, ocr_fn=ocr_fn, count=not dry)
    hit = cap_hit or kw_match(gate_text(ev_message, results), prio)
    return dict(hit=hit, text=compose(ev_message, results, ts), ocr=summary(results, "caption" if cap_hit else "ocr" if hit else None),
                base=base, results=results, existing=None)


def store(db, dyn, g, u, nick, ts, message, prio, role, r):
    """Act on a gate() result exactly like pre-S8 capture did, plus the picture text. -> 'stored' | 'duplicate' |
    'existing' | 'held' | 'held+strike' | 'updated' | 'dropped'. Shared by listener.py and backfill.py."""
    if r["existing"]:
        return "existing"
    if r["hit"] is not None:
        if r["results"]:                      # re-check after the OCR await: the same message may have been stored
            ex = find_existing(db, "msg", g, u, ts, r["base"])     # meanwhile by another handler (checker S8 F4)
            if ex:
                return "existing"
        remember(db, r["results"])
        cur = db.execute("INSERT OR IGNORE INTO msg(group_id,user_id,nick,ts,text,raw,priority,role,ocr) VALUES(?,?,?,?,?,?,?,?,?)",
                         (g, u, nick, ts, r["text"], json.dumps(message, ensure_ascii=False), prio, role, r["ocr"]))
        db.commit()
        return "stored" if cur.rowcount else "duplicate"
    if prio:
        out = hold(db, dyn, g, u, nick, ts, r["base"], r["text"], r["results"])
        (remember if out != "dropped" else forget)(db, r["results"])
        return out
    forget(db, r["results"])
    return "dropped"


PHOTO_LABEL = "[image text (OCR: photo only, no text found - held so you can glance at the photo in QQ)]"


def hold(db, dyn, g, u, nick, ts, base, text, results):
    """A PRIORITY sender's message that matched nothing. Held WITH the picture text so the reviewer sees it.
    Strikes only count the sender's own words: a picture-only post is held but never strikes (OCR noise must not
    demote a teacher). S8b: a photo-only post whose pictures hold no text is held too, labelled PHOTO_LABEL at the
    front (it is marker-shaped, so strip_ocr() still recognises the row as this message on redelivery)."""
    dyn.ensure(db)
    ex = find_existing(db, "held", g, u, ts, base)
    if ex:
        if ex[1] != text and READ_OK in text and READ_OK not in (ex[1] or ""):   # upgrade an unread held row, never downgrade
            db.execute("UPDATE held SET text=? WHERE id=? AND state='held'", (text, ex[0])); db.commit()
        return "updated"
    if len(dyn.PLACEHOLDER.sub("", base or "")) >= 4:
        dyn.irrelevant(db, g, u, nick, ts, text); return "held+strike"
    if results and all(x.get("reason") in NOTEXT for x in results):
        text = PHOTO_LABEL + text                  # S8b: text-free photos from a priority sender are held, never dropped
    if results:                                    # picture-only (with or without text) or a failed read: held
        db.execute("INSERT OR IGNORE INTO held(group_id,user_id,nick,ts,text) VALUES(?,?,?,?,?)", (g, u, nick, ts, text))
        db.commit(); return "held"
    return "dropped"


def fill_row(db, mid, new_text, ocr_json):
    """--reocr: replace one stored row's text with the OCR'd text. Keeps the FTS index in step (it has no UPDATE
    trigger) and re-queues the row for extraction only if no item was made from it yet."""
    old = db.execute("SELECT text FROM msg WHERE id=?", (mid,)).fetchone()[0]
    fts = db.execute("SELECT 1 FROM sqlite_master WHERE name='msg_fts'").fetchone()
    has_items = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='item_sources'").fetchone()
                     and db.execute("SELECT 1 FROM item_sources WHERE msg_id=?", (mid,)).fetchone())
    cols = [r[1] for r in db.execute("PRAGMA table_info(msg)")]
    try:
        db.execute("UPDATE msg SET text=?, ocr=? WHERE id=?", (new_text, ocr_json, mid))
    except Exception as e:                       # UNIQUE clash: an identical row exists already; keep the old text
        db.execute("UPDATE msg SET ocr=? WHERE id=?", (ocr_json, mid)); db.commit()
        return "conflict:" + type(e).__name__
    if fts and old != new_text:
        db.execute("INSERT INTO msg_fts(msg_fts,rowid,text) VALUES('delete',?,?)", (mid, old))
        db.execute("INSERT INTO msg_fts(rowid,text) VALUES(?,?)", (mid, new_text))
    if "processed" in cols and not has_items and old != new_text and READ_OK in new_text:   # only real picture text is worth an LLM call
        db.execute("UPDATE msg SET processed=0 WHERE id=?", (mid,))
    db.commit()
    return "has_items" if has_items else "requeued" if READ_OK in new_text and old != new_text else "unchanged_queue"


# ---------------------------------------------------------------- extraction guard (for S2's extract.py; see build.md)
def from_picture(rows):
    """True if any source row's text came (partly) from OCR. rows: sqlite3.Row or dicts with 'text' (and maybe 'ocr')."""
    for s in rows:
        try:
            o = s["ocr"]
        except (KeyError, IndexError, TypeError):
            o = None
        if o or MARKER.search(s["text"] or ""):
            return True
    return False


def guard_item(it, rows, quote=None):
    """OCR'd text is lower confidence: force needs_confirm, cap confidence, tag the item 'from a picture'.
    Never trusts OCR'd digits for date/time/room without a human looking at the original picture."""
    if not from_picture(rows):
        return it
    it["needs_confirm"] = 1
    it["confidence"] = min(float(it.get("confidence") or 0.5), 0.5)
    it["from_picture"] = True
    return it


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        a = sys.argv[1]
        r = ocr_url(a) if a.startswith("http") else ocr_bytes(open(a, "rb").read())
        print(json.dumps(r, ensure_ascii=False, indent=1))
