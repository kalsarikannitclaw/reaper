# Campus Notice Bot - Windows package (native: no Docker, no WSL)

This is the WINDOWS package. It runs directly on Windows 10/11 with Python and a native NapCat for Windows. It needs no
Docker, no WSL, no virtual machine. (Linux users: use the `-linux` zip and README-INSTALL-LINUX.md.)

**Honest status: this Windows package has NOT yet been run on a real Windows PC.** It was written and tested on Linux with the Windows
calls faked. Anything below about how NapCat for Windows behaves is marked UNVERIFIED. Before anything else, double-click `tools\wincheck.cmd`
(read-only; it starts nothing) and send back `data\wincheck.txt` if a line says FAIL.

The bot quietly reads the QQ groups YOU choose, finds notices that need action (exams, deadlines, contests, venues and
times), and writes a calendar page you open in a browser. The report pages and many keywords are in Chinese (中文界面):
that is intended, the bot is built for Chinese campus groups. Nothing is sent to anyone: it only listens.

Privacy in one paragraph: everything stays on your computer (messages, database, reports, QQ login). The only thing that
leaves is the text sent to YOUR chat-API provider (DeepSeek by default) for extraction, with phone numbers and emails
removed first. You pay that provider directly with your own key. This package contains no data from its author.
Risk note: automating a QQ client is against Tencent's terms and can get an account limited. Use an account you accept
that risk for. The bot never sends messages, and the main account is only ever listened to.

## 1. What you need
| | What | Where from |
|---|---|---|
| 1 | Windows 10 or 11, a normal user account (no administrator rights needed) | |
| 2 | Python 3.14 (the only version tested, and that was on Linux only, never on Windows; older ones may not accept the pinned packages) | https://www.python.org/downloads/ , tick "Add python.exe to PATH" |
| 3 | **NapCat for Windows**, unpacked in a folder (the QQ bridge) | see "NapCat" below: you supply it, the installer does not download it |
| 4 | The QQ desktop client, ONLY IF your NapCat build says it needs one installed separately | official QQ site https://im.qq.com ; the NapCat folder's own README names the version |
| 5 | Tesseract OCR with Chinese (optional: reads text inside pictures) | UB-Mannheim build https://github.com/UB-Mannheim/tesseract/wiki , tick Chinese (Simplified) |
| 6 | A chat-API key (yours) | see First run, Step 1 |
| 7 | A QQ account (yours) and a phone with QQ | |

Full component list with versions and licences, and which facts are verified or UNVERIFIED: `COMPONENTS.md`.

### NapCat (read this once)
NapCat is the program that logs in to QQ and hands the bot the group messages. This package does NOT download it by itself.
Reason: on 2026-11-15 the official NapCat repository (github.com/NapNeko/NapCatQQ) had no published releases and its
README said only "Ending", and its documentation site was gone, so no official file name and no official checksum
could be verified. The installer therefore never fetches or runs a NapCat it cannot check. You give it a NapCat folder you
obtained yourself:
- **A folder you already unpacked**: `python install.py --napcat-dir C:\path\to\NapCat`
- **A zip**: `python install.py --napcat-zip C:\path\to\file.zip --sha256 HASH` where HASH is the SHA-256 published by the place you
  got the zip from. Without `--sha256` the installer shows the hash it computed and asks you to type `yes` after you have compared it.
  A zip whose hash you cannot compare is a program you cannot trust: do not use it.
- Then the installer lists start files it GUESSES at inside the folder (for example `napcat.bat`; the real names are UNVERIFIED) and you pick one, or pass
  `--napcat-launch napcat.bat`. It writes NapCat's settings next to it: WebSocket on 127.0.0.1:3001 with a random token and WebUI on
  127.0.0.1:6099, loopback only. If your NapCat has its own settings files they are kept, and you are told to check them.
You can install without NapCat first (the report and chat work); add NapCat later by re-running the installer with one of the options above.

## 2. Install
Unzip anywhere (a short path with plain letters is easiest, for example `C:\CampusBot`), then:

| What | How |
|---|---|
| Preview, changes nothing | `install.cmd --dry-run` |
| Install | double-click `install.cmd`, or run it in a terminal (add the NapCat option from above) |

The installer: creates `.venv` and installs pinned packages; creates your config files from templates; asks for your
API key (hidden typing, never shown or logged, stored only in `stack/.env`); sets up the NapCat folder as described above;
registers four background jobs in Task Scheduler for YOUR user only (no admin rights): `CampusNoticeBot-BridgeGuard`,
`CampusNoticeBot-Capture`, `CampusNoticeBot-Web` (the one program: calendar + report + chat on http://127.0.0.1:8790/r/calendar.html) and
`CampusNoticeBot-Daily` (21:00 by the PC clock). The bridge guard task is registered DISABLED: starting it would start NapCat and spend one
of your 6 daily QQ logins before you are ready; you enable it on purpose in Step 2 with `python install.py --enable-guard`.
NapCat itself is never a scheduled task, so Windows cannot restart it by itself.
Existing install: if tasks with these names already exist and were not made by this folder, the installer stops without changing
anything; use `python install.py --name second` to pick other names (the fixed ports 3001, 6099, 8790 mean only one bridge can run at a time).
Remove only what this installer created with `python install.py --uninstall` (your files, NapCat and its login data stay).
Jobs write their output to `logs\<job>.log` (rotated at 1 MB, one old file kept). Start the jobs now with
`schtasks /Run /TN CampusNoticeBot-Capture` and `schtasks /Run /TN CampusNoticeBot-Web` (otherwise they start at next logon).
A helper runs any bot command: `cnb <command>` (the file `cnb.cmd`). Run `tools\wincheck.cmd` any time for a read-only health check.

## 3. First run (step by step)
### Step 1 - your own chat-API key
1. Get a key from the provider named in `config.json` (default DeepSeek: create an account at platform.deepseek.com,
   top up a few yuan, create an API key). It is yours and pays for your use only.
2. Enter it: the installer asks, or later:

| What | Command |
|---|---|
| Set / replace | `cnb key set` |
| Verify (one cheap test call, key never printed) | `cnb key test` |
| Status (masked) | `cnb key status` |
| Remove | `cnb key remove` |

   It lives only in `stack/.env` as `DEEPSEEK_API_KEY` (the variable name is `key_env` in `config.json`). Never paste it in
   a group chat or email. If the test says invalid: re-copy the key without spaces, check the provider balance, check
   `model` and `base_url` in `config.json`. No key = capture still works, extraction just waits.
3. Then run `cnb setup` once (model, timezone, school, groups to watch; resumable).

### Step 2 - log in your own QQ (you scan, nobody types a password)
Only the guard starts NapCat. NEVER start NapCat yourself (not by double-clicking its .bat, not from Task Manager, not from
Task Scheduler): QQ allows a handful of logins per day, and the guard enforces a cap of **6 per 24 hours** with
growing waits. A restart storm (about 100 logins in two hours happened once) makes QQ invalidate the session.
1. Start the login: `.venv\Scripts\python stack\bridge_guard.py --qr`
   It starts NapCat (one login), then shows where to scan: UNVERIFIED for native Windows: the QR picture may be saved as `stack\qrcode.png` (only if NapCat writes one), and NapCat's
   WebUI should be at http://127.0.0.1:6099 (its token should be in `napcat\config\webui.json`, which the installer wrote). If neither appears, read `logs\napcat.log`.
2. On your phone: QQ -> scan -> confirm. The QR expires after a short time (about two minutes with the Linux build; UNVERIFIED here); if it does, the guard shows it again later,
   do not keep re-triggering. (UNVERIFIED) if your NapCat asks you to pick an account on screen, pick your own.
3. Check it is online: `.venv\Scripts\python stack\bridge_guard.py --status` shows `"state": "online"`. The report pages also show a banner.
4. Then let the guard watch from now on: `python install.py --enable-guard` (add `--name NAME` if you used one).
5. States you may see: `online` fine | `waiting` guard is backing off, do nothing | `needs_qr` only you can fix it: QQ asked
   for a fresh scan (session invalidated or QR unscanned). Run `--qr` ONCE, scan, wait | `capped` 6 logins used in 24 h: stop
   touching it, check QQ on your phone, after a rescan run `--reset-cap`.
6. If QQ says your session was invalidated ("用户身份已失效 / account safety"): wait (an hour or more), scan once. Never
   share a password, verification code or the `napcat` folder with anyone; the bot and this package never ask for them.

### Step 3 - choose the groups to watch
Edit `capture/groups.json` (it starts with FAKE ids, replace them with your group numbers):
```
{"groups": [123456789, 987654321], "priority_members": [], "priority_by_group": {}, "priority_roles": [], "ignore_users": []}
```
Group number = QQ group info page. Messages from any other group are dropped before storage. The file is re-read on every
message, no restart. `priority_members` = teachers/tutors (QQ numbers) whose messages always count. Optional: send
nothing; `capture/lookup_admins.py` can fill group admins as priorities.
Open the report: `open-report.cmd` in the package folder, or http://127.0.0.1:8790/r/calendar.html (installed as a background job, so it is usually already running).

### Step 4 - troubleshooting
| Symptom | Likely cause | What to do |
|---|---|---|
| `python` not found | Python not installed / not on PATH | install Python 3.14 from python.org with "Add to PATH", open a NEW terminal |
| `key test` fails | wrong key, no balance, wrong `base_url` | re-enter with `cnb key set`, check provider account |
| Report empty | no key yet, or groups.json still has sample ids | Steps 1 and 3; `cnb status` |
| Banner "bridge offline" / `needs_qr` | QQ wants a scan | Step 2, one scan, do not start anything by hand |
| `capped` | 6 logins in 24 h | wait, check phone, then `--reset-cap` after a rescan |
| Guard says NapCat "is not set up" | no NapCat folder or start file recorded in `native.json` | re-run `python install.py --napcat-dir ... --napcat-launch ...` (safe to repeat) |
| Listener "ws error, retry" | NapCat not online yet, or token mismatch | confirm `--status` online; `CAPTURE_WS_TOKEN` in `stack\.env` must equal the token in `napcat\config\onebot11*.json` |
| NapCat never shows a QR / exits at once | wrong NapCat build or a missing QQ client | read `logs\napcat.log`, follow the NapCat folder's own README; run `python stack\native_win.py --check` |
| Port 3001 / 6099 / 8790 "in use" | another program (or a second NapCat) uses it | close it; only one bridge can run at a time |
| Windows SmartScreen / antivirus warns about NapCat | it is an unsigned program that drives QQ | only run a NapCat whose hash you compared; if unsure, stop here |
| Picture text missing ("OCR failed: no_tesseract") | Tesseract not installed | install it (default folder `C:\Program Files\Tesseract-OCR` is searched, as is PATH), or set `CNB_TESSERACT` to the full path of `tesseract.exe`; `CNB_OCR=0` turns OCR off |
| Time zone error | missing `tzdata` | `.venv\Scripts\python -m pip install tzdata` |
| Chinese text shows as ?? in a console | legacy code page | the bot's own jobs run in UTF-8 mode; for your own commands use `cnb`, or `chcp 65001` |
| Daily run at the wrong hour | Windows uses the PC clock | change the trigger in Task Scheduler |
| Known limit: a `--qr` or `--reset-cap` typed during the second the guard is mid-check can be lost | tiny timing window | run it again and check `--status` | 
| Known limit: NapCat alive but its ports never open, or Windows refuses to end it | NapCat behaviour UNVERIFIED | the guard may start a second copy after 10+ minutes (capped by the 6 per 24 h rule): close QQ/NapCat yourself if the status says so |
| Something else | | double-click `tools\wincheck.cmd` and send `data\wincheck.txt` |

## 4. Add your own school (config, not code)
A second school is a config entry, not a code change. All three files are plain JSON, edit with any editor.
### 4a. The school's notice website: `research_sources.json`
Open `_example_school` in the file (a FAKE `www.example-school.edu.cn`), rename the key to `school`, fill it in:
`name`; `host`; `follow_hosts` (other hosts a notice's cited files/pages may be fetched from, usually the school's whole
registrable domain); `notice_urls` (list pages, each a URL or `{"url","name","notice_link" (regex for notice links),
"content" (regex for the body's class/id),"max_notices","max_files","enabled"}`). The `allow` list is the allowlist: a host
not in it (or in `follow_hosts`) is never opened.
Politeness is built in and not switchable: robots.txt is obeyed, 2 s between requests to one host, an identifying
User-Agent, a cache, bounded crawl. Test ONE page first: `cnb checkurl https://www.your-school.edu.cn/tzgg/` shows
robots result, status and what was read. If the site answers with an anti-bot challenge, CAPTCHA or login page the bot
reports it and stops: open the page by hand and read it yourself, never try to solve or bypass it.
If your network reaches the site over only IPv4 or IPv6, or not at all from this machine, builds that include `tunnel/`
support `_example_tunnel` (rename to `tunnel`): `"family": "auto" | "4" | "6"` plus per-host `routes` (`via` relay or direct);
start `tunnel/relay.py --allow your-school.edu.cn` (`start_relay.cmd`) on the machine that can reach the site.
### 4b. The school's contest whitelist: `contest_whitelist.json`
Shipped with 2 FAKE entries and `"confirmed_by_user": false`. Replace `entries` with your school/faculty's recognised-contest
list: `{"no","name","aliases":[...],"organiser","level","credit"}`; fill `provenance` (`source_url`, `publish_date`,
`file_name`, `page_row`) and `scope`. Or add one at a time: `cnb whitelist "Contest name" --alias ABC --level A`.
Until you confirm, every list is UNCONFIRMED: the bot shows a hit as "listed, unconfirmed", never as recognised. Check the
entries against the original document, then confirm: `cnb wl-confirm <school> <version>` (see `wl-versions`, `wl-show`),
or set `"confirmed_by_user": true` yourself. Reading the list from the school site is automatic where the adapter can.
### 4c. Keywords and interests
`capture/keywords.json`: Chinese keyword tiers (A coursework & exams ... G admin, health & safety), `venues` (your campus's
buildings: the package ships only generic ones, add yours), `exclude`, `venue_room_regex`. Re-read per message.
`interests.json`: your topics `{"name":..., "keywords":[...]}`; `filters.json`: block senders/keywords.

## 5. Everyday use and safety rules
Daily run happens by itself; run it now with `cnb daily`. Self-test: double-click `tools\wincheck.cmd` (writes `data\wincheck.txt`, no keys in it).
Never expose ports 6099/3001 beyond localhost, never copy `stack/.env` or `the NapCat folder` to anyone, never start NapCat by hand (window, .bat, or Task Scheduler "Run" on it). To share your own setup with others, use a fresh package, not a copy of your folder.

## Chat with the bot (ask for clarification and advice)
`cnb talk --open` (or `tools\talk.cmd`)
Opens a chat page on this computer only (127.0.0.1). Add `--terminal` for a text chat, `--no-llm` to use no AI (it then only quotes its own data). With your own chat-API key set, each reply is one call to your provider (cap 60 per hour, phones and emails removed first). Ask about a notice, what to do, what matters most this week, whether a contest suits your level, or type `/level`, `/profile`, `/forget <x>`, `/remember <sentence>`. Answers cite the notice they come from and say so when your data does not state something. Level answers stay "claimed" until you back them up. The bot never sends QQ messages; the QQ adapter in `capture/chat_adapter.py` is off and never sends.

### Chat inside the report page (one program on 127.0.0.1:8790)
The report, calendar and chat are ONE program, `tools/talk.py`, on **http://127.0.0.1:8790/r/calendar.html** only (Task `CampusNoticeBot-Web` runs it in the background; `/r/latest.html` is the list by chat group, and any weekly page `/r/weekly-<year>-W<nn>.html`). Every report page has an **Ask the bot** button at the bottom right that talks to the same bot, in the page itself. If you open `reports/calendar.html` straight from the folder instead, the report works as before but the button only tells you the address above: a page opened from a file may not talk to the local server (that is on purpose).

### Pages I could not open (open it yourself)
Some school sites refuse programs (a bot is turned away, or a CAPTCHA appears). The bot never gets around that and never opens such a page itself: the college site in `blocked_hosts` stays blocked for automatic reading. Instead it **asks you**. When a page is needed (a link in a notice to such a site, or the AI model says it would need it) the request is listed under **Pages I could not open**: in the chat panel of the report page (http://127.0.0.1:8790/r/calendar.html, the link at the top of the panel) and on http://127.0.0.1:8790/chat. Each request shows the address and why. You choose:
- **Approve: I will open it myself** opens the address in YOUR OWN browser (the bot does not load it). Look at the page, then give the bot what you see: paste the page text, or pick a file you saved from the page (`.html`, `.htm`, `.txt`, `.pdf`, at most 2 MB; a PDF needs the optional package `pypdf`, otherwise paste the text).
- **Skip** drops the request; the bot will not ask about that page again for 30 days.
What you give is kept as **a copy you supplied**, labelled "provided by you on <date>, claimed address <address>, NOT verified by the bot" (the bot cannot know a pasted page is genuine). The chat can then use it and cites it as `page:<number>`; dated notices in it become items that always say "please confirm". From a saved page the bot removes scripts, styles, comments and elements marked hidden (other visible-looking tricks are not caught), and phone numbers and emails are removed before anything goes to your AI model. The page text is treated as untrusted data, but the AI model could still be misled by it: always check dates and places in the original page. No cookies, passwords or logins are asked for or stored. At most 20 requests wait at a time.
The same from the command line: `cnb page list`, `cnb page approve <number>` (prints the address for you to open), `cnb page provide <number> --file saved.html` or `--paste`, `cnb page skip <number>`, `cnb page scan` (queue links to user-only sites found in saved notices).
**The option:** in `config.json`, `"manual_page_assist": true` (the default) lets your AI model (DeepSeek by default, or any OpenAI-compatible model you chose) ask you for such a page. Set it to `false` and the model is never offered this; the bot then just says it could not read the page. `cnb setup --status` and `cnb key status` show the current value.

### Check that your AI model is connected
`cnb talk --check` makes one tiny call with your key and prints `Model check OK` with the time, or `FAILED` with the error type (never the key). No key set prints that scripted mode is in use. Run this right after you paste your key.

### Confirm tasks (so expired ones stop being red)
Every card in the calendar and in the by-chat-group list has a **✔ Mark done** button when the page is opened through the local program (`/r/calendar.html`). Press it once you have handed the task in or attended the event: the card turns grey ("Done") and the page refreshes. **↩ Not done after all** puts it back. The change only writes the item's status and one history line in your own database; nothing is sent anywhere. Opened straight from the folder, the buttons are hidden because that page cannot write.

### If your computer was off for a day or two
Nothing is lost as long as QQ still holds the group history (it usually keeps weeks). Capture keeps a "complete up to" time (`capture/coverage.json`, shown by `cnb status`). Every time the bridge reconnects, the listener re-reads the history from that time, not from the newest stored message, retries a failed read after a minute, and only moves the time forward after every group was read completely. While it is catching up, the report shows a banner saying so. To pull a longer stretch by hand: `.venv\Scripts\python -X utf8 capture/backfill.py --days 7`. Keep Windows awake (Settings > System > Power: never sleep while plugged in): sleeping stops the bridge until you wake it. After a restart or logon, the capture and report jobs start by themselves; NapCat is started only by the guard (see Step 2), and if QQ asks for a scan you will see `needs_qr`.
