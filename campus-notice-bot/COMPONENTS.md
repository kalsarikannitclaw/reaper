# Components of the Campus Notice Bot

Facts come from the project's own files (compose.yaml, source, the author's venv). UNKNOWN = not recorded in the
project; check before you rely on it. UNVERIFIED = a claim about something outside the project that could not be checked against an
official source on 2026-11-15. VERIFIED = read at the source URL named in the row. Licence notes marked (believed) come from
general knowledge: verify them at the source URL before redistributing anything.
"Bundled" = inside the zip. "Downloaded" = fetched by you / the installer from the internet.

This is the component list of the NATIVE WINDOWS package (no Docker, no WSL).

## NapCat for Windows: what is verified and what is not
| Claim | Status | Source |
|---|---|---|
| The official NapCatQQ repository had no releases and its README read only "Ending" on 2026-11-15 (its history shows commits "Keep README only" and "docs: ending" on 2026-11-12) | VERIFIED (read through GitHub's API and raw README that day) | https://github.com/NapNeko/NapCatQQ , https://raw.githubusercontent.com/NapNeko/NapCatQQ/main/README.md |
| The docs site napneko.github.io answered "Site not found" on 2026-11-15 | VERIFIED | https://napneko.github.io/ |
| The NapCat-Docker repository, source of the Linux image, answered 404 on 2026-11-15 | VERIFIED | https://github.com/NapNeko/NapCat-Docker |
| Therefore: no official Windows file name, version, SHA-256 or download URL can be quoted | consequence | |
| A NapCat Windows folder contains a start `.bat` and a `config` folder with `onebot11.json` and `webui.json` | UNVERIFIED (general knowledge; a search summary of the former official docs mentioned `napcat.bat` and `-q <QQ number>` quick login) | none readable today |
| The OneBot v11 WebSocket server settings written by the installer (host 127.0.0.1, port 3001, token) use the same field names the Linux install already used successfully with NapCat.Core 4.18.42 in Docker | VERIFIED for the Docker build (project history); UNVERIFIED for any Windows build | project files |
| Whether a particular NapCat Windows build needs a separately installed QQ, and which QQ version | UNVERIFIED | read the README that came with your NapCat; get QQ only from https://im.qq.com |
| NapCat's QR image location and the WebUI login token location | UNVERIFIED | the installer writes `webui.json` with a token; the guard looks for `qrcode.png` anywhere under the NapCat folder |
Consequences in the design: the installer never downloads NapCat or QQ; it takes a folder or zip you give it and shows/verifies its SHA-256;
which start file runs is recorded in `native.json` and chosen by you; `python stack\native_win.py --check` reports what is actually there.

## Summary table
| # | Component | Purpose | Version / pin | Licence note | Source | Install | Bundled? |
|---|---|---|---|---|---|---|---|
| 1 | NapCat for Windows (native) | QQ bridge: OneBot v11 WebSocket on 127.0.0.1:3001, WebUI/QR login on 127.0.0.1:6099 | UNKNOWN: pin only after you have verified a release yourself | UNKNOWN (it drives Tencent QQ, which has its own terms) | none official readable on 2026-11-15 | you supply the folder or zip: `install.py --napcat-dir / --napcat-zip + --sha256` | No (not downloaded by us) |
| 2 | QQ desktop client | only if your NapCat build needs it installed | UNVERIFIED | Tencent terms | https://im.qq.com | by you | No |
| 3 | Python | runs everything | author's runtime 3.14.9; code needs `zoneinfo`, so 3.9+ (minimum not tested: UNKNOWN) | PSF | https://www.python.org | python.org installer, tick "Add to PATH" | No |
| 4 | Python packages (in `.venv`) | `websockets` 17.4 OneBot client; `pillow` 13.0.0 OCR preprocessing (optional); `pypdf` PDF notices (optional; tested version UNKNOWN, `>=4.0` is a guess); `tzdata` (REQUIRED on Windows: time zones) | see `requirements.txt` | websockets BSD-3, Pillow HPND, pypdf BSD-3 (all believed) | https://pypi.org | `install.py` creates `.venv` | Downloaded by pip |
| 5 | Tesseract OCR | reads text in pictures posted in watched groups (`capture/ocr.py`, `chi_sim+eng`, two passes) | author used 5.5.3 with Leptonica 1.87.0; needs `chi_sim` traineddata (version UNKNOWN) | Apache-2.0 (believed) | https://github.com/UB-Mannheim/tesseract/wiki | optional; found via `CNB_TESSERACT`, then PATH, then `%ProgramFiles%`, `%ProgramFiles(x86)%`, `%LOCALAPPDATA%` `\Tesseract-OCR\tesseract.exe` | No |
| 6 | AI bot core `src/cnb` | extraction, items, contests, profile/memory, search, onboarding, HTML report/calendar/.ics, school-site reader, chat, open-it-yourself page queue (`manual.py`: pages the bot must not open itself; you open them and give it the text) | 1.0.0 | project's own | in package | `cnb <cmd>` | Bundled |
| 7 | Chat-API client (inside `cnb.extract`, `cnb.chat`, `cnb.research`) | OpenAI-compatible HTTPS calls to your provider; phones/emails scrubbed first | model `deepseek-chat`, base_url `https://api.deepseek.com` (config.json; swappable) | provider's service terms; DeepSeek is China-hosted | https://platform.deepseek.com | your own key via `cnb key set` | Service |
| 8 | Capture listener `capture/listener.py` (+ `kw.py`, `dyn.py`, `backfill.py`, `lookup_admins.py`) | reads OneBot events, keyword gate, stores only whitelisted-group messages in SQLite | in package | project's own | in package | Task `CampusNoticeBot-Capture` (pythonw, at logon) | Bundled |
| 9 | `stack/bridge_guard.py` + `stack/native_win.py` | ONLY starter of NapCat: login cap 6 per 24 h, backoff 10 min to 6 h, no retry of fatal states, min 120 s between two process starts, writes `capture/bridge_status.json`; `--qr`, `--status`, `--reset-cap`, `--once --dry-run` | in package | project's own | in package | Task `CampusNoticeBot-BridgeGuard` (registered disabled until `--enable-guard`) | Bundled |
| 10 | HTML report / digest / calendar | `reports/calendar.html`, `latest.html`, `.ics`, served with the chat on http://127.0.0.1:8790 by the bot's own program `tools/talk.py` (Python standard library) | in package | project's own | in package | Task `CampusNoticeBot-Web`, `open-report.cmd` | Bundled |
| 11 | `tools/health.py` | read-only capture-health summary | in package | project's own | in package | runs on Windows with reduced checks (the Linux journal checks report "cannot assess") | Bundled |
| 12 | Daily job | `cnb daily`: extract, write reports | 21:00 PC clock | | | Task `CampusNoticeBot-Daily` | Generated by `install.py` |
| 13 | Toast alerts (`cnb.notify`) | Windows toast for new urgent items via `powershell.exe` | in package | | | native | Bundled |
| 14 | Fetch relay `tunnel/relay.py` (+ `cnb.tunnel`) | optional IPv4/IPv6 or relay route to a school site | stage S5 round 3; included only if present at build time | project's own | in package | `tunnel\start_relay.cmd` | Bundled when built after S5 merge |
| 15 | `svc_launch.py` | task launcher: log file for output, hidden console windows, UTF-8 mode, backend name from `service.json` | 1.0.0 | project's own | in package | used by all 4 tasks | Bundled |
| 16 | `install.py`, `install.cmd`, `tools\wincheck.cmd` | install / read-only self-check; standard library only | 1.0.0 | project's own | in package | `install.cmd` | Bundled |

## Services (Task Scheduler, current user, no admin)
| Job | Task | Start | Restart behaviour |
|---|---|---|---|
| Bridge guard | `CampusNoticeBot-BridgeGuard` | at logon, only after `--enable-guard` | the GUARD process is restarted after a failure; the guard keeps its login ledger on disk, so a restart cannot reset the cap |
| Capture listener | `CampusNoticeBot-Capture` | at logon | after a failure |
| One program: calendar + report + chat | `CampusNoticeBot-Web` = tools/talk.py (127.0.0.1:8790 only) | at logon | after a failure |
| Daily run | `CampusNoticeBot-Daily` | 21:00, or when the PC is next available | two retries, 30 min apart |

NapCat itself is NOT a task and has no restart policy: only the guard starts it, one logged call at a time (`logs\napcat_launch.log`).
Ports: 3001 (OneBot), 6099 (NapCat WebUI), 8790 (report/chat). All three listen on 127.0.0.1 only.

## First run (summary; full steps in README-INSTALL-WINDOWS.md)
1. Key: `cnb key set`, then `cnb key test` (key never printed; stored in `stack\.env`).
2. QQ: `python stack\bridge_guard.py --qr`, scan with your own phone QQ, `--status` must say `online`. State `needs_qr` = you must scan; cap 6 per 24 h;
   never start NapCat yourself; no password or code is ever requested or shared.
3. Groups: your numbers in `capture\groups.json`.
4. Troubleshooting table in the README.

## Excluded from every package (privacy)
`stack\.env`, API keys, QQ numbers, NapCat login data, `qrcode.png`, all databases, `reports\`, `cache\`, logs,
backups, `stages\`, real `groups.json`, interests/profile data, `*.bak`, `__pycache__`. Templates are shipped instead.
