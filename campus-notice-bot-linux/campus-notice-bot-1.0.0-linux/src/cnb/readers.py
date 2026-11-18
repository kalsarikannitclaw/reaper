"""Turn a fetched byte string into text and tables (PLAN S5: "read notifications AND attached
files (PDF/docx/xlsx/web tables)"), and find what a notice actually says about WHEN and WHERE.

Stdlib only, by choice: .docx and .xlsx are just zipped XML, and HTML needs no more than
html.parser for our purposes, so three of the four formats need no dependency at all. PDF is the
exception - it has no tractable stdlib path - so it is OPTIONAL: if `pypdf` is not installed the
reader says so plainly instead of returning empty text that would look like an empty document.
Silent emptiness is the dangerous failure here, because an empty whitelist reads as "your school
recognises nothing", and an attachment we could not open reads as "the notice gives no place".

Everything returns the same shape, so the adapter above does not care what the file was:
    {"kind", "text", "tables": [[row, ...], ...], "links": [(text, href), ...], "note"}
plus, where they exist: "main" / "main_links" / "images" (the article body of a notice page, so
the site's navigation and footer never leak into a notice), "pages" (PDF text per page, for
page-level provenance) and "blocked" (why this is a wall, not a document).

S5 additions (2026-11-13, from a run against the real the school sites):
 - access walls: an anti-bot JavaScript challenge, a CAPTCHA download page or a login form comes
   back as HTTP 200 HTML. Read naively it is a short, empty-looking page. It is now named.
 - legacy Office (.doc/.xls, OLE2 binary) is detected and reported as unreadable, not mis-read.
 - cited_files(): a notice that CITES a file ("详见附件", "见附件", "附：...", a download link).
 - when_where(): exact time / place / deadline strings, verbatim, each with a location
   (line / page / sheet+row) - nothing is normalised, completed or guessed. A date without a year
   stays without a year.
"""
import html as _html, io, json, os, re, urllib.parse, zipfile, xml.etree.ElementTree as ET
from html.parser import HTMLParser

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# The article body of a notice page, by class or id. Common Chinese campus CMSes: VSB/WebPlus
# (v_news_content, vsb_content_*), WebPlus Pro (wp_articlecontent), TRS (TRS_Editor). A bare
# "content" is NOT in the list: on a homepage that is the whole page, navigation included.
# A site whose body uses another name sets "content" in its research_sources.json entry.
CONTENT_DEFAULT = (r"^(?:v_news_content|vsb_content\w*|wp_articlecontent|TRS_Editor|article-content|"
                   r"articleContent|news_content|newsContent|entry-content|art-content|zw-con|"
                   r"read-content|article_content)$")


class _HTML(HTMLParser):
    """Text, tables and links. Deliberately small: campus notice pages are plain documents, and a
    real DOM library would add a dependency for no gain. Script and style content is dropped."""

    SKIP = {"script", "style", "noscript", "svg"}
    BLOCK = ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "table")

    VOID = {"br", "img", "input", "meta", "link", "hr", "area", "base", "col", "embed", "source", "track", "wbr"}
    _HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|%)?\s*(?:;|$)|opacity\s*:\s*0\s*(?:;|$)", re.I)

    def __init__(self, content_re=None, strip_hidden=False):
        super().__init__(convert_charrefs=True)
        self._strip_hidden = strip_hidden             # S12: user-supplied pages: hidden elements are not shown text, so not read
        self._hid_tag, self._hid_depth = None, 0
        self.out, self.tables, self.links = [], [], []
        self._skip = 0
        self._tbl, self._row, self._cell = None, None, None
        self._href, self._atext = None, None
        self._cre = re.compile(content_re or CONTENT_DEFAULT, re.I)
        self._main_tag, self._main_depth, self._main_done = None, 0, False
        self.main, self.main_links, self.images = [], [], []

    def _in_main(self):
        return self._main_tag is not None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._strip_hidden and tag not in self.VOID:
            if self._hid_tag is None and ("hidden" in a or (a.get("aria-hidden") or "").lower() == "true"
                                          or self._HIDDEN_STYLE.search(a.get("style") or "")):
                self._hid_tag, self._hid_depth = tag, 0
            if self._hid_tag == tag:
                self._hid_depth += 1
        # the notice body: the FIRST element whose class or id names it. Nesting is counted on the
        # same tag name only, which survives the unclosed <p>s of hand-edited campus pages.
        if self._main_tag is None and not self._main_done:
            names = (a.get("class") or "").split() + [a.get("id") or ""]
            if any(n and self._cre.search(n) for n in names):
                self._main_tag, self._main_depth = tag, 0
        if self._main_tag == tag:
            self._main_depth += 1
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "table":
            self._tbl = []
        elif tag == "tr" and self._tbl is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._emit(" ")
        elif tag == "a":
            self._href, self._atext = a.get("href"), []
        elif tag == "img" and self._in_main():
            self.images.append({"src": a.get("src") or a.get("orisrc") or "", "alt": a.get("alt") or ""})
        if tag in self.BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if self._hid_tag == tag:
            self._hid_depth -= 1
            if self._hid_depth <= 0:
                self._hid_tag = None
                return
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "table" and self._tbl is not None:
            if self._tbl:
                self.tables.append(self._tbl)
            self._tbl = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._tbl.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "a" and self._href is not None:
            t = re.sub(r"\s+", " ", "".join(self._atext or [])).strip()
            if t or self._href:
                self.links.append((t, self._href))
                if self._in_main():
                    self.main_links.append((t, self._href))
            self._href, self._atext = None, None
        if tag in self.BLOCK:
            self._emit("\n")
        if self._main_tag == tag:
            self._main_depth -= 1
            if self._main_depth <= 0:
                self._main_tag, self._main_done = None, True

    def _emit(self, s):
        self.out.append(s)
        if self._in_main():
            self.main.append(s)

    def handle_data(self, d):
        if self._skip or self._hid_tag is not None:
            return
        if self._cell is not None:
            self._cell.append(d)
        if self._atext is not None:
            self._atext.append(d)
        self._emit(d)


def _decode(body, ctype=""):
    """Campus sites are still full of GB18030. Try the declared charset, then the meta tag, then
    UTF-8, then GB18030, before falling back to a lossy decode."""
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    cands = [m.group(1)] if m else []
    m2 = re.search(rb'charset=["\']?([\w-]+)', body[:4096], re.I)
    if m2:
        cands.append(m2.group(1).decode("ascii", "ignore"))
    cands += ["utf-8", "gb18030", "big5"]
    for enc in cands:
        if enc.lower().replace("-", "") in ("gb2312", "gbk"):
            enc = "gb18030"                     # a superset; GB2312-declared pages often contain GBK
        try:
            return body.decode(enc).lstrip("﻿")
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace").lstrip("﻿")


def _clean(s):
    s = re.sub(r"[ \t 　]+", " ", s)
    s = re.sub(r" *\n[ \n]*", "\n", s)         # whitespace-only lines are layout, not content
    return s.strip()


# ---------------- access walls: HTTP 200 pages that are not the document ----------------
WALLS = [
    # (reason, pattern, max visible characters). The size limit matters: news.example-school.edu.cn embeds
    # the same anti-bot bootstrap script ($_ts) in REAL notice pages that do carry their text, while
    # the challenge page itself (www.college.example-school.edu.cn, HTTP 202) has no visible text at all. A
    # pattern alone flagged 10 of 23 real notices as walls in the first live run.
    # Most specific first: the CAPTCHA download page on news.example-school.edu.cn ALSO carries the $_ts
    # script, and must be reported as the CAPTCHA it is.
    ("CAPTCHA required to download (a person has to open it; not bypassed)",
     re.compile(r"请输入验证码|输入验证码下载|captcha|滑动验证|拖动滑块|安全验证|人机验证|verify you are human|just a moment|checking your browser|cf-chl", re.I), 600),
    ("login required (unified identity / campus account; not attempted)",
     re.compile(r"统一身份认证|authserver/login|cas/login|(?:name|type)=[\"']?password|请先登录|用户登录", re.I), 600),
    # anti-bot JavaScript challenges (Ruishu-style $_ts, generic "enable JavaScript"): the
    # content only exists after a script runs. We do not run it or work around it.
    ("anti-bot JavaScript challenge (the site only serves content to a browser that runs its "
     "script; not bypassed)", re.compile(r"\$_ts\s*=|\$_ts\.|_\$[a-zA-Z]{2}\(\)|"
                                         r"window\.location\.href\s*=\s*['\"][^'\"]*(?:verify|challenge)", re.I), 200),
]


def access_wall(body, text=None):
    """-> reason string if this HTML page is a wall in front of the content, else None.
    Only near-empty pages are judged: a notice that mentions 登录 or embeds the anti-bot script
    while still carrying its text is not a wall."""
    head = body[:200000].decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    if text is None:
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", head))
    visible = len(re.sub(r"\s+", "", _html.unescape(text)))
    for reason, rx, most in WALLS:
        if visible <= most and rx.search(head):
            return reason                       # a page with real text is never a wall
    return None


def read_html(body, ctype="", content_re=None, strip_hidden=False):
    p = _HTML(content_re, strip_hidden)
    raw = _decode(body, ctype)
    try:
        p.feed(raw)
        p.close()
    except Exception as e:                     # noqa: BLE001 - malformed markup must not kill a run
        return {"kind": "html", "text": "", "tables": [], "links": [], "note": f"parse failed: {type(e).__name__}"}
    text = _clean("".join(p.out))
    out = {"kind": "html", "text": text, "tables": p.tables, "links": p.links, "note": None,
           "main": _clean("".join(p.main)) or None, "main_links": p.main_links, "images": p.images}
    wall = access_wall(raw, text)
    if wall:
        out["blocked"] = wall
        out["note"] = f"not the document: {wall}"
    return out


def read_docx(body):
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return {"kind": "docx", "text": "", "tables": [], "links": [], "note": f"not readable: {type(e).__name__}"}
    paras = []
    in_table = {id(p) for tbl in root.iter(W + "tbl") for p in tbl.iter(W + "p")}
    # paragraphs outside tables only; table cells are read as tables below, so nothing is doubled
    for p in root.iter(W + "p"):
        if id(p) in in_table:
            continue
        t = "".join(n.text or "" for n in p.iter(W + "t"))
        if t.strip():
            paras.append(t)
    tables = []
    for tbl in root.iter(W + "tbl"):
        rows = []
        for tr in tbl.iter(W + "tr"):
            rows.append(["".join(n.text or "" for n in tc.iter(W + "t")).strip() for tc in tr.iter(W + "tc")])
        if rows:
            tables.append(rows)
    return {"kind": "docx", "text": "\n".join(paras), "tables": tables, "links": [], "note": None}


def read_xlsx(body):
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        return {"kind": "xlsx", "text": "", "tables": [], "links": [], "note": f"not readable: {type(e).__name__}"}
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        try:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(S + "t")))
        except ET.ParseError:
            pass
    tables = []
    names = [n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    for name in sorted(names, key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1))):
        try:
            sheet = ET.fromstring(z.read(name))
        except ET.ParseError:
            continue
        rows = []
        for row in sheet.iter(S + "row"):
            cells = []
            for c in row.iter(S + "c"):
                # honour the column letter, so an empty cell does not shift the row left
                ref = re.match(r"([A-Z]+)", c.get("r") or "")
                if ref:
                    col = 0
                    for ch in ref.group(1):
                        col = col * 26 + ord(ch) - 64
                    while len(cells) < col - 1:
                        cells.append("")
                v = c.find(S + "v")
                raw = v.text if v is not None else ""
                if c.get("t") == "s" and raw and raw.isdigit() and int(raw) < len(shared):
                    raw = shared[int(raw)]
                elif c.get("t") == "inlineStr":
                    raw = "".join(t.text or "" for t in c.iter(S + "t"))
                cells.append((raw or "").strip())
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    text = "\n".join(" | ".join(r) for t in tables for r in t)
    note = None
    if tables and not shared and any(re.fullmatch(r"\d{5}(\.\d+)?", c) for t in tables for r in t for c in r):
        note = ("some cells are numbers that may be Excel dates (serial days); shown as stored, "
                "not converted")
    return {"kind": "xlsx", "text": text, "tables": tables, "links": [], "note": note}


def read_pdf(body):
    try:
        import pypdf
    except ImportError:
        return {"kind": "pdf", "text": "", "tables": [], "links": [], "pages": [], "note":
                "PDF reading needs pypdf (pip install pypdf). Reporting this rather than returning "
                "empty text, because an empty whitelist would read as 'your school recognises nothing'."}
    try:
        r = pypdf.PdfReader(io.BytesIO(body))
        if r.is_encrypted:
            try:
                r.decrypt("")
            except Exception:                  # noqa: BLE001
                return {"kind": "pdf", "text": "", "tables": [], "links": [], "pages": [],
                        "note": "encrypted PDF: not readable without its password"}
        pages = [(p.extract_text() or "") for p in r.pages]
    except Exception as e:                     # noqa: BLE001
        return {"kind": "pdf", "text": "", "tables": [], "links": [], "pages": [],
                "note": f"not readable: {type(e).__name__}"}
    text = "\n".join(pages).strip()
    note = None
    if not text:
        note = "no text layer: this PDF is probably a scan, and would need OCR. Not guessing at its contents."
    return {"kind": "pdf", "text": text, "tables": [], "links": [], "pages": pages, "note": note}


OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def read_ole(body, url=""):
    """Legacy binary Office (.doc / .xls / .ppt, 'OLE2'). Campus sites still publish 报名表.xls.
    There is no small pure-stdlib reader for these, so: say so, never return empty text."""
    u = (url or "").lower()
    kind = "xls" if (b"W\x00o\x00r\x00k\x00b\x00o\x00o\x00k" in body[:200000] or ".xls" in u) else \
           "doc" if (b"W\x00o\x00r\x00d\x00D\x00o\x00c" in body[:200000] or ".doc" in u) else "ole"
    return {"kind": kind, "text": "", "tables": [], "links": [], "note":
            f"legacy binary Office file (.{kind if kind != 'ole' else 'doc/xls'}): not readable here. "
            "Open the original, or save it as .docx/.xlsx/.pdf; its contents are NOT known."}


def read(body, content_type="", url="", content_re=None):
    """Dispatch on the file's own magic bytes first (campus servers mislabel: a '.doc?e=.pdf' link
    serves a PDF, a download.jsp serves anything), then content type, then the URL."""
    ct = (content_type or "").lower()
    u = (url or "").lower().split("#")[0]
    path = urllib.parse.urlparse(u).path
    if body[:5] == b"%PDF-":
        return read_pdf(body)
    if body[:8] == OLE2:
        return read_ole(body, url)
    if body[:2] == b"PK":                       # a zip: decide by what is inside
        try:
            names = zipfile.ZipFile(io.BytesIO(body)).namelist()
            if "word/document.xml" in names:
                return read_docx(body)
            if any(n.startswith("xl/worksheets/") for n in names):
                return read_xlsx(body)
            return {"kind": "zip", "text": "", "tables": [], "links": [], "note":
                    "zip archive: not unpacked (files inside are not read); open the original"}
        except zipfile.BadZipFile:
            pass
    if "pdf" in ct or path.endswith(".pdf"):
        return read_pdf(body)
    if "wordprocessingml" in ct or path.endswith(".docx"):
        return read_docx(body)
    if "spreadsheetml" in ct or path.endswith((".xlsx", ".xlsm")):
        return read_xlsx(body)
    if "html" in ct or "xml" in ct or b"<html" in body[:2048].lower() or path.endswith((".html", ".htm", "/")) \
            or b"<!doctype html" in body[:2048].lower():
        return read_html(body, content_type, content_re)
    if path.endswith((".doc", ".xls", ".ppt")):
        return read_ole(body, url)
    if re.search(r"image/|\.(png|jpe?g|gif|bmp|webp)$", ct + " " + path):
        return {"kind": "image", "text": "", "tables": [], "links": [], "note":
                "image: its text (if any) is not read, there is no OCR here"}
    return {"kind": "text", "text": _decode(body, content_type), "tables": [], "links": [], "note": None}


# ======================= files a notice cites =======================
FILE_EXT = r"pdf|docx?|xlsx?|xlsm|pptx?|wps|et|zip|rar|7z|csv|txt"
_EXT_URL = re.compile(rf"[?&]e=\.({FILE_EXT})\b|\.({FILE_EXT})(?=$|[?#&])", re.I)
_EXT_TEXT = re.compile(rf"\.({FILE_EXT})\s*$", re.I)
_DOWNLOAD = re.compile(r"download\.jsp|DownloadAttach|/download[/?.]|attachment|fujian|/attach|"
                       r"/_upload/|/__local/|/uploadfile", re.I)
_FILEWORD = r"(?:附件|文件|附表|表格|材料|链接)"
_BELOWW = r"(?:下方|下面|下文|以下|下列|文末|文后|页面下方|本页下方|正文下方|附后|后附)"
CITE = re.compile(r"详见附件|具体[^。；\n]{0,10}?见附件|(?<![意可常])见附件|附件[一二三四五六七八九十\d]*[：:]|^附[：:]|"
                  r"附件[一二三四五六七八九十\d]*[《“\"]|详见[《“\"][^》”\"\n]{2,60}[》”\"]|(?<![意可常])见文件|"
                  r"详见[^。；\n]{0,20}?(?:文件|通知|附件|附表|网站|链接)|下载附件|附件下载|附件【|"
                  r"(?:查看|查阅|阅读|参阅|参见|详见|见)附[ 　]?(?:件|表|录)[一二三四五六七八九十\d]*|"
                  # S5 round 2, "the file BELOW" wordings: 见下方附件, 见下文件, 详见文末附件, 请见以下文件
                  rf"(?<![意可常不])见\s*(?:{_BELOWW}|下)\s*(?:的|所附)?\s*{_FILEWORD}|"
                  # 如下文件, 下列附件, 以下文件, 文件附后. NOT a bare 如下 ("通知如下：", "具体安排如下"): that
                  # introduces text that follows, not a file. NOT 文件精神/文件要求 (policy talk),
                  # and not 以下材料/下列表格 (a list of things to hand in).
                  rf"(?:如下|以下|下列|下方|文末)(?:的|所附)?(?:附件|文件|附表)(?!夹|精神|要求|规定)|文件(?:附后|后附|见下|如下[：:])|"
                  # 附件见下, 文件附后, 相关文件如下, 报名表见文末
                  rf"(?:附件|相关文件|有关文件|附表|报名表|申报书|汇总表)(?:见|详见)?(?:{_BELOWW}|如下|下)(?![一-鿿]{{0,2}}(?:精神|要求|规定))",
                  re.M)
# a citation that points DOWN the page: links that come after it on the page are what it cites
_BELOW = re.compile(rf"{_BELOWW}|如下|见下|附下")
DEFERRED = re.compile(r"[^。；\n]{0,30}?另行通知[^。；\n]{0,10}")


def file_ext(text, url):
    """The file type a link points at, by the URL (including the '?e=.pdf' form some campus CMSes
    use) or by the link text ('报名表.xls'). None for an ordinary page."""
    m = _EXT_URL.search(urllib.parse.urlparse(url or "").path + "?" + (urllib.parse.urlparse(url or "").query))
    if m:
        return (m.group(1) or m.group(2)).lower()
    m = _EXT_TEXT.search(text or "")
    return m.group(1).lower() if m else None


def cited_files(doc, base=""):
    """What does this notice point at for its details? Looks in the notice BODY only (doc['main']
    when the page has one), so the site menu is never mistaken for a citation.

    -> {"files": [{"name", "url", "ext", "kind": "file|page", "how"}],
        "phrases": [{"text", "line"}],      # 详见附件 / 见附件 / 附：... - cited in words
        "unlinked": bool,                   # cited in words but no file link found on the page
        "deferred": [{"text", "line"}]}     # "另行通知": details will come in a LATER notice
    """
    body = doc.get("main") or doc.get("text") or ""
    links = doc.get("main_links") if doc.get("main") else doc.get("links")
    out, seen = [], set()
    lines = body.split("\n")
    phrases = [{"text": m.group(0), "line": i + 1} for i, ln in enumerate(lines) for m in CITE.finditer(ln)]
    # "详见下方文件" / "附件如下" point DOWN the page: a link written after such a phrase is what it
    # cites, even when its URL and text carry no file marker (e.g. a plain 关于…办法 link).
    below = [p["line"] for p in phrases if _BELOW.search(p["text"])]
    first_below = min(below) if below else None
    for t, h in links or []:
        h = (h or "").strip()
        if not h or h.startswith(("javascript:", "mailto:", "#", "tel:")):
            continue
        url = urllib.parse.urljoin(base, h)
        ext = file_ext(t, url)
        how = None
        if ext:
            how = f"links a .{ext} file"
        elif _DOWNLOAD.search(url):
            how = "download link"
        elif re.match(r"\s*附件", t or ""):
            how = "link text says 附件"
        elif t and len(t) >= 4:
            # an ordinary page counts only when the text cites it ("附：关于...的通知", "详见《...》")
            k = re.escape(t[:12])
            if re.search(r"(?:附|附件[一二三四五六七八九十\d]*|详见|见)\s*[：:]?\s*[《“\"]?\s*" + k, body):
                how = "page cited by name in the text"
        if not how and t and len(t) >= 2 and first_below is not None and not re.match(r"\s*(?:上一篇|下一篇|上一条|下一条|联系|返回|首页|版权|相关(?:新闻|链接|阅读))", t):
            at = next((i + 1 for i, ln in enumerate(lines) if t in ln), None)
            if at is not None and at >= first_below:
                if at > first_below or lines[at - 1].find(t) > lines[at - 1].find(
                        next(p["text"] for p in phrases if p["line"] == at and _BELOW.search(p["text"]))):
                    how = "linked after a citation pointing below"
        if not how or url in seen:
            continue
        seen.add(url)
        out.append({"name": t or os.path.basename(urllib.parse.urlparse(url).path) or url, "url": url,
                    "ext": ext, "kind": "page" if how.startswith("page") or (how.startswith("linked after") and not ext) else "file",
                    "how": how})
    deferred = [{"text": m.group(0).strip(), "line": i + 1} for i, ln in enumerate(lines)
                for m in DEFERRED.finditer(ln)]
    return {"files": out, "phrases": phrases, "unlinked": bool(phrases) and not out, "deferred": deferred}


# ======================= exact time and place, verbatim =======================
_DATE = (r"(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
         r"(?:\s*[（(]?\s*(?:星期|周)[一二三四五六日天]\s*[）)]?)?"
         r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
_CLOCK = (r"(?:上午|下午|晚上|中午|早上|凌晨|傍晚)?\s*\d{1,2}\s*[:：]\s*\d{2}"
          r"(?:\s*[-—–~～至到]\s*\d{1,2}\s*[:：]\s*\d{2})?"
          r"|(?:上午|下午|晚上|中午|早上|傍晚)\s*\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分|半)?")
WHEN = re.compile(rf"(?:{_DATE})(?:\s*(?:{_CLOCK}))?(?:\s*[-—–~～至到]+\s*(?:{_DATE}|{_CLOCK}|\d{{1,2}}\s*[日号]))?"
                  rf"|(?<![\d:：])(?:{_CLOCK})")
LABEL = re.compile(r"(?P<label>[一-鿿]{0,8}?(?:时\s*间|日\s*期|地\s*点|场\s*地|地\s*址|截\s*止|期\s*限|"
                   r"会\s*场|考\s*场|教\s*室)[一-鿿]{0,4}?)\s*[：:]\s*(?P<value>.+)")
_NEXT_LABEL = re.compile(r"\s+\|\s+|\s{2,}|[，,；;]\s*(?=[一-鿿]{0,6}(?:时间|日期|地点|场地|地址|截止)[：:])"
                         r"|\s(?=[一-鿿]{0,6}(?:时间|日期|地点|场地|地址|截止)[：:])")
_HAPPEN = re.compile(r"在\s*(?P<place>[^，。；,\s]{2,30}?)\s*(?:隆重|盛大|正式|如期|顺利|统一)?\s*"
                     r"(?:举行|召开|举办|集合|开设|上课|考试)")
_PLACEISH = re.compile(r"厅|楼|室|馆|中心|场|校区|广场|礼堂|堂|教室|园|[A-Za-z]\s?-?\d{3}")
_PHONE = re.compile(r"\d{3,4}-\d{7,8}|电话|手机|邮箱|传真")
_DEADLINE_CUE = re.compile(r"截止|截至|之前|前提交|前送交|前完成|前报名|前发送|ddl|deadline", re.I)
_ROOM = r"(?<![A-Za-z0-9])[A-Za-z]\s?-?\d{3}(?!\d)"


def _keywords_cfg():
    """capture/keywords.json (tiers, venues, room pattern) and interests.json, READ ONLY. Missing
    files mean no extra keywords, never an error."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    kw, it = {}, {}
    try:
        kw = json.load(open(os.path.join(root, "capture", "keywords.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        it = json.load(open(os.path.join(root, "interests.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return kw, it


def notice_keywords(text, title="", kw=None, interests=None):
    """The words that say what this notice is about, taken ONLY from words the notice itself uses:
    the keyword tiers of capture/keywords.json and the interest keywords that occur in it. These
    drive the search of a cited file for the matching time/place line."""
    if kw is None and interests is None:
        kw, interests = _keywords_cfg()
    kw, interests = kw or {}, interests or {}
    hay = f"{title}\n{text}"
    excl = set(kw.get("exclude", []))
    words = []
    for tier in (kw.get("tiers") or {}).values():
        words += [w for w in tier if w not in excl]
    for tp in interests.get("topics", []) if kw.get("use_interest_keywords", True) else []:
        words += tp.get("keywords", [])
    hits = []
    for w in words:
        if len(w) >= 2 and w not in hits and (w in hay if not w.isascii() else
                                               re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", hay, re.I)):
            hits.append(w)
    return sorted(hits, key=len, reverse=True)


def doc_lines(doc):
    """[(location, line)] over everything a document says, for provenance: 'line 7' for a web
    page, 'page 2 line 5' for a PDF, 'table 1 row 4' for a sheet or a Word table. A table row whose
    header names time/place columns is written as 'header：cell | header：cell', so a labelled
    cell reads exactly like a labelled line - the cells themselves are never changed."""
    out = []
    if doc.get("pages"):
        for p, txt in enumerate(doc["pages"], 1):
            for j, ln in enumerate(txt.split("\n"), 1):
                if ln.strip():
                    out.append((f"page {p} line {j}", ln.strip()))
    elif doc.get("kind") != "xlsx":
        body = doc.get("main") or doc.get("text") or ""
        for j, ln in enumerate(body.split("\n"), 1):
            if ln.strip():
                out.append((f"line {j}", ln.strip()))
    if doc.get("kind") in ("xlsx", "docx"):
        for t, rows in enumerate(doc.get("tables") or [], 1):
            hdr = None
            for r, row in enumerate(rows, 1):
                if hdr is None and any(re.search(r"时间|日期|地点|场地|地址|截止|名称|项目", c or "") for c in row):
                    hdr = row
                    out.append((f"table {t} row {r}", " | ".join(row)))
                    continue
                if hdr:
                    names = [re.sub(r"\s+", "", h or "") for h in hdr]
                    cells = [names[i] + "：" + c if i < len(names) and names[i] and c else c
                             for i, c in enumerate(row)]
                    out.append((f"table {t} row {r}", " | ".join(x for x in cells if x)))
                else:
                    out.append((f"table {t} row {r}", " | ".join(c for c in row if c)))
    return out


def _cut(v, field=None):
    v = _NEXT_LABEL.split(v, 1)[0]
    if field in ("time", "deadline"):
        m = WHEN.search(v)
        if m:                                   # '9月1日12:00--9月17日17:00，请各单位...' -> the time
            tail = re.match(r"[^，。；;,]*", v[m.end():]).group(0)
            v = v[:m.end()] + (tail if len(tail) <= 12 else "")
    return v.strip().rstrip("，,；;。.、")


def _classify(label):
    lb = re.sub(r"\s+", "", label)
    if re.search(r"截止|期限", lb):
        return "deadline"
    if lb.startswith("联系") and re.search(r"地点|地址", lb):
        return "contact_place"
    if re.search(r"地点|场地|地址|会场|考场|教室", lb):
        return "place"
    return "time"


def _venue_terms(kw):
    return [v for v in (kw or {}).get("venues", []) if v]


def when_where(doc, keywords=None, kw=None):
    """Exact time / place / deadline strings this document states, verbatim, each with where it
    was found. Three ways, in order of trust:
      labelled  '时间：3月19日下午16:30', '地点：教学楼A102', table columns headed 时间/地点
      pattern   a date/clock in the text, '在XX举行', a known venue word or room number
      keyword   (only when `keywords` is given) the pattern hits restricted to lines that use one of
                the notice's own keywords - how a long attachment is searched for THE event the
                notice is about, rather than every date in it
    Nothing is completed: '9月9日' stays without a year; a venue stays as written.
    -> {"time": [...], "place": [...], "deadline": [...], "contact_place": [...]}
       each {"value", "label", "quote", "loc", "how"}"""
    if kw is None:
        kw, _ = _keywords_cfg()
    venues = _venue_terms(kw)
    room = re.compile((kw or {}).get("venue_room_regex") or _ROOM)
    res = {"time": [], "place": [], "deadline": [], "contact_place": []}
    seen = set()

    def add(field, value, label, quote, loc, how):
        value = (value or "").strip()
        if not value or (field, value, loc) in seen:
            return
        seen.add((field, value, loc))
        res[field].append({"value": value, "label": label, "quote": quote[:200], "loc": loc, "how": how})

    for loc, ln in doc_lines(doc):
        kws = [k for k in (keywords or []) if (k in ln if not k.isascii() else k.lower() in ln.lower())]
        if keywords is not None and not kws:
            continue
        how_p = f"keyword {kws[0]}" if kws else "pattern"
        labelled = False
        segs = [x for s0 in re.split(r"\s+\|\s+", ln)
                for x in re.split(r"[；;，,]\s*(?=[一-鿿]{0,8}?(?:时\s*间|日\s*期|地\s*点|场\s*地|地\s*址|截\s*止)\s*[：:])", s0)]
        for seg in segs:
            for m in LABEL.finditer(seg):
                label = re.sub(r"\s+", "", m.group("label"))
                f = _classify(label)
                value = _cut(m.group("value"), f)
                if not value or re.fullmatch(r"[\d\-—\s()（）]{7,}", value):
                    continue                    # '联系电话' style numbers are not times
                if f in ("time", "deadline") and not WHEN.search(value):
                    if not re.search(r"另行通知|待定|另行|TBD", value):
                        continue                # '时间：' followed by prose is not a time
                add(f, value, label, ln, loc, "labelled" if not kws else f"labelled, {how_p}")
                labelled = True
        taken = " ".join(e["value"] for f in ("time", "deadline") for e in res[f] if e["loc"] == loc)
        times = []
        for m in WHEN.finditer(ln):
            t = m.group(0).strip()
            if labelled and t in taken:
                continue                        # already reported under its label
            times.append(t)
            after = ln[m.end():m.end() + 5]
            dl = re.match(r"\s*(?:之?前|以前|截止|为止)", after) or (not labelled and _DEADLINE_CUE.search(ln))
            add("deadline" if dl else "time", t, None, ln, loc, how_p)
        if labelled:
            continue
        for m in _HAPPEN.finditer(ln):
            if _PLACEISH.search(m.group("place")) or any(v in m.group("place") for v in venues):
                add("place", m.group("place"), None, ln, loc, how_p)
        if not times and not kws:
            continue                            # a bare venue word with no time is a name, not a place
        for v in venues:
            if v in ln and not any(v in p["value"] for p in res["place"] if p["loc"] == loc):
                clause = next((c for c in re.split(r"[，。；,;！!？?]", ln) if v in c), ln).strip()
                if len(clause) <= 40 and not _PHONE.search(clause):
                    add("place", clause, None, ln, loc, how_p)
        for m in room.finditer(ln):
            if not any(m.group(0) in p["value"] for p in res["place"] if p["loc"] == loc):
                add("place", m.group(0), None, ln, loc, how_p)
    return res


# ======================= the year a document's HEADER states (S5 round 2) =======================
# Notices and their files often write "9月9日" with no year, and put the year once at the top:
# "2026年秋季学期安排", "关于举办2026年…的通知", "2025—2026学年第二学期". The user's rule
# (2026-11-13): take the year from the header, attach it to the year-less dates, and record where it
# came from; a date whose document header states no year stays year-less. NEVER a guessed year:
#  - the verbatim value is never changed; the year is a separate field with its basis,
#  - a header with two different years, or a body that states a year the header does not, gives
#    no year (with a note) - the header is then not a safe guide,
#  - dates near a year end under a header that spans one (秋季学期 / 寒假 / 元旦 ...) get no year,
#  - a range that crosses the year end ("12月28日至1月3日") gets no year,
#  - 2026届 / 2023级 / 2025年度 are cohorts and reporting periods, not the year of an event.
_HDR_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*年(?!度|级|届|份?以前)|(?<!\d)(20\d{2})[-/.](?:0?[1-9]|1[0-2])[-/.]\d{1,2}(?!\d)")
_ACAD = re.compile(r"(?<!\d)(20\d{2})\s*(?:年)?\s*[-—–~～至/]+\s*(20\d{2})\s*(?:年)?\s*学年")
_ANY_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*年\s*\d{1,2}\s*月|(?<!\d)(20\d{2})[-/.](?:0?[1-9]|1[0-2])[-/.]\d{1,2}(?!\d)")
_MD = re.compile(r"(\d{1,2})\s*月\s*\d{1,2}\s*[日号]")
_AUTUMN = re.compile(r"秋季|秋学期|第一学期|上学期")
_SPRING = re.compile(r"春季|春学期|第二学期|下学期")
_SUMMER = re.compile(r"暑期|暑假|夏季学期|夏令营")
_YEAREND = re.compile(r"寒假|元旦|春节|跨年|冬季|冬令营|新年")
_HDR_MAX = 60          # a header/title line is short; a long line is already the body's prose


def header_lines(doc, title=None, max_lines=4):
    """[(where, text)] - the document's own header: its title / file name, then its first short
    lines up to the first labelled time/place line or the first line of prose."""
    out = [("title", title.strip())] if title and title.strip() else []
    for loc, ln in doc_lines(doc)[:max_lines]:
        if len(ln) > _HDR_MAX or LABEL.search(ln):
            break
        out.append((loc, ln))
    return out


def header_year(doc, title=None):
    """-> {"year": int|None, "range": (y1, y2)|None, "where", "quote", "note"} from the header only.
    The body is consulted for ONE thing: if it states a year the header does not, the header is
    not trusted (that is how "关于公布2025年…名单" with a 2026 deadline is caught)."""
    hl = header_lines(doc, title)
    res = {"year": None, "range": None, "where": None, "quote": None, "note": None, "season": None}
    years, acad = [], []
    for where, ln in hl:
        for m in _ACAD.finditer(ln):
            acad.append((int(m.group(1)), int(m.group(2)), where, ln))
        rest = _ACAD.sub(" ", ln)
        for m in _HDR_YEAR.finditer(rest):
            years.append((int(m.group(1) or m.group(2)), where, ln))
    if not years and not acad:
        return res
    hdr_text = " ".join(ln for _, ln in hl)
    res["season"] = ("autumn" if _AUTUMN.search(hdr_text) else "spring" if _SPRING.search(hdr_text)
                     else "summer" if _SUMMER.search(hdr_text) else None)
    res["yearend"] = bool(_YEAREND.search(hdr_text))
    if acad:
        spans = {(a, b) for a, b, _, _ in acad}
        a, b, where, ln = acad[0]
        res.update(where=where, quote=ln)
        if len(spans) > 1 or b != a + 1 or any(y not in (a, b) for y, _, _ in years):
            res["note"] = "the header names more than one year or academic year; no year attached"
            return res
        res["range"] = (a, b)
        allowed = {a, b}
    else:
        distinct = sorted({y for y, _, _ in years})
        res.update(where=years[0][1], quote=years[0][2])
        if len(distinct) > 1:
            res["note"] = f"the header names several years ({', '.join(map(str, distinct))}); no year attached"
            return res
        if "学年" in hdr_text:
            res["note"] = "the header names a 学年 with one year; a 学年 spans two calendar years; no year attached"
            return res
        res["year"] = distinct[0]
        allowed = {distinct[0]} | ({distinct[0] + 1} if res["season"] == "autumn" or res["yearend"] else set())
    body_years = {int(m.group(1) or m.group(2)) for _, ln in doc_lines(doc) for m in _ANY_YEAR.finditer(ln)}
    stray = sorted(body_years - allowed)
    if stray:
        res["year"], res["range"] = None, None
        res["note"] = (f"the document also states the year {', '.join(map(str, stray))}, which its header "
                       "does not; no year attached")
    return res


def year_for(value, hy):
    """-> (year|None, note|None) for ONE verbatim time/deadline value, given header_year()."""
    if not hy or not (hy.get("year") or hy.get("range")):
        return None, (hy or {}).get("note")
    if re.search(r"(?<!\d)\d{4}\s*年|(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", value):
        return None, None                      # states its own year
    months = [int(m) for m in _MD.findall(value)]
    if not months or any(not 1 <= m <= 12 for m in months):
        return None, None                      # a clock time or 另行通知: no date to give a year to
    if any(b < a for a, b in zip(months, months[1:])):
        return None, "the range crosses the year end; no year attached"
    if hy.get("range"):
        a, b = hy["range"]
        season = hy.get("season")

        def one(m):
            if m >= 9:
                return a
            if m <= 7:
                return b
            return {"autumn": a, "spring": b, "summer": b}.get(season)      # August: only if the term says
        ys = {one(m) for m in months}
        if None in ys or len(ys) != 1:
            return None, "August in an academic year, with no term named in the header; no year attached"
        return ys.pop(), None
    y = hy["year"]
    if hy.get("yearend") and any(m in (12, 1, 2) for m in months):
        return None, "near the year end under a header that spans it (寒假/元旦/…); no year attached"
    if hy.get("season") == "autumn" and any(m in (1, 2) for m in months):
        return None, "January/February under an autumn-term header may be the next year; no year attached"
    return y, None


def attach_years(entries, hy, origin):
    """Give each year-less time/deadline entry the header's year, recorded with its basis:
    e["year"], e["year_from"] = "header of <origin> (<where>): <quote>". The value is untouched.
    An entry the header cannot safely date gets e["year_note"] instead (and no year)."""
    for e in entries:
        if e.get("year"):
            continue
        y, note = year_for(e.get("value") or "", hy)
        if y:
            basis = f"{hy['range'][0]}-{hy['range'][1]}学年" if hy.get("range") else str(y)
            e["year"] = y
            e["year_from"] = f"year {basis} from the header of {origin} ({hy['where']}): {hy['quote'][:80]}"
        elif note and _MD.search(e.get("value") or "") and not re.search(r"\d{4}\s*年", e.get("value") or ""):
            e["year_note"] = note
    return entries
