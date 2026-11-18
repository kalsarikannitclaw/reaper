#!/usr/bin/env python3
"""Talk to the Campus Notice Bot on your own machine (stage S10). Windows and Linux.

    python tools/talk.py              ONE program: report, calendar and chat on http://127.0.0.1:8790/r/calendar.html
                              (reports are regenerated at start and by the Refresh link; Ctrl+C stops it)
    python tools/talk.py --check      test the plugged-in AI model (one tiny call)
    python tools/talk.py --terminal   the same conversation in this terminal
    options: --db PATH  --port N  --no-llm  --open

The AI reply uses YOUR own key from stack/.env / the environment (never printed). With no key, or --no-llm, the bot
still works: it quotes its own data and runs the scripted interview. Nothing is sent to QQ or anywhere else.
"""
import argparse, os, sys, webbrowser
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from cnb import db as D, converse as CV, webchat as W      # noqa: E402


def terminal(db, llm, input_fn=input, out=print):
    ses = CV.new_session()
    out("Campus Notice Bot. Type /help, /interview, or ask anything. Empty line or Ctrl+D quits.")
    while True:
        try:
            line = input_fn("> ")
        except (EOFError, KeyboardInterrupt):
            return
        if not line.strip():
            return
        r = CV.reply(db, line, ses, llm, channel="cli")
        out(r["reply"] if r["reply"] else f"(refused: {r['reason']})")
        q = r.get("question")
        if q and q["options"]:
            out("   options: " + " / ".join(q["options"]) + " (or /skip)")


def check_model(llm, out=print):
    """One tiny call with the user's own key: says whether the plugged-in model answers. Never prints the key or the raw error body."""
    import time
    if llm is None:
        out("Model check: no key set (stack/.env DEEPSEEK_API_KEY or your environment). Scripted mode only.")
        return 1
    t0 = time.time()
    try:
        txt = llm([{"role": "system", "content": "Reply with a JSON object: {\"ok\": true}"}, {"role": "user", "content": "ping"}])
    except Exception as e:                                  # status code / type only: an error body can echo request details
        code = getattr(e, "code", None)
        out(f"Model check FAILED: {type(e).__name__}" + (f" (HTTP {code})" if code else "") + " - check the key, the model name and your network.")
        return 2
    out(f"Model check OK: answered in {time.time() - t0:.1f}s ({len(str(txt))} chars).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--terminal", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--port", type=int, default=W.DEFAULT_PORT)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--check", action="store_true", help="test the plugged-in model with one tiny call, then exit")
    ap.add_argument("--open", action="store_true", help="open the page in your browser")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):                  # Windows consoles default to a legacy code page
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    llm = None if a.no_llm else CV.make_llm()
    if a.check:
        return check_model(llm)
    db = D.connect(a.db)
    print("AI replies: " + ("on (your own key)" if llm else "off (no key or --no-llm): scripted answers from my own data"))
    if a.terminal:
        terminal(db, llm)
        return 0
    db.close()
    try:
        if a.db: raise RuntimeError("custom --db: the project's own reports are left alone")
        # report generating is part of this program: fresh pages at start
        from cnb import digest as DG
        c = D.connect(a.db); DG.write_all(c, os.path.join(ROOT, "reports")); c.close()
    except Exception as e:                              # noqa: BLE001 - serve the last pages rather than refuse to start
        print(f"(report refresh skipped: {type(e).__name__})")
    srv = W.make_server(a.db or D.DB_PATH, llm, port=a.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/r/calendar.html"
    print("Report + calendar + chat, all here (this computer only):", url)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
