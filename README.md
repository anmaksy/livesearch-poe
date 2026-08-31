# livesearch-poe

PoE Live Search — Direct Whisper Tool

## Purpose

Bypass the trade website's "This item is in high demand" countdown by calling
GGG's official trade API directly with your session cookies, instead of
clicking through the browser UI.

## How it works

1. Click the app's built-in **Import Cookies** button so it can read your
   session cookies straight out of your already-logged-in browser — no
   copy-pasting from DevTools, and no separate browser window to log into.
2. Pick a league and paste a search ID (or a full trade URL) into the app,
   optionally give it a short **Name**, then click **+ Add**. Repeat for as
   many searches as you want to watch — up to 20 — then click
   **Start All**.
3. The app opens one live-search WebSocket per search:
   `wss://www.pathofexile.com/api/trade/live/{league}/{search_id}`
   There is no way to multiplex several searches over one socket, so each
   search gets its own connection, its own thread and its own status row.
   Each search can name its own league, so a full trade URL from one league
   sits happily in the list next to a bare search ID from another.
4. When new listings appear, GGG pushes a token over the socket, the app
   plays `alert.mp3` (if present next to `live.py`; otherwise it falls back
   to the system bell), and a card appears in the list.
5. For each token, the app fetches listing details (including the
   `whisper_token`): `GET /api/trade/fetch/{token}?query={search_id}`
6. When you click a card's button, the app sends the whisper / travel-to-
   hideout command: `POST /api/trade/whisper` with `{"token": "<whisper_token>"}`.
   A 200 response means GGG accepted the action; the in-game client should
   receive the whisper or hideout invite shortly after.

   The button is never greyed out, only relabelled — `↻ Send Again` once it
   has been fired, `⚠` if the listing arrived without a whisper token — so a
   send that failed for a reason that may clear up (token race, transient
   5xx, rate limit) can always be retried. Mind the rate limits when you do.
7. Cards disappear 3 minutes after they appear, since a whisper token is
   only good for seconds and stale cards are just clutter.
8. Click **Stop All** at any time to disconnect every live search, or click
   a row's **✕** to drop just that one. Adding a search while the others
   are running connects it immediately — you never have to stop and
   restart the rest to pick up a new one.

## Watching several searches at once

The **Searches** panel is the list of what the app is subscribed to. Each row
shows a status dot, the search's name and `league/search_id`, its own
connection status, and a **✕** to remove it:

```
League:[Standard v]  Name:[chest]  Search ID / URL:[AbCdEf]  [+ Add]
┌ Searches ──────────────────────────────────────┐
│ ● chest — Standard/AbCdEf    connected       ✕ │
│ ● Hardcore/QqWw88            reconnecting…   ✕ │
│ ● ring — Standard/XyZ123     rejected        ✕ │
└────────────────────────────────────────────────┘
  [▶ Start All]  [■ Stop All]              ⚡ 2/3 connected
```

- **Dot colour**: grey = stopped, orange = connecting or reconnecting,
  green = connected, red = rejected (close code 1008 — see
  "The Reconnecting… status" below).
- **Name** is optional and cosmetic. Every item card is titled
  `[name] Item Name` so you can tell at a glance which search produced it;
  leave the name blank and the card is tagged with the search ID instead.
- The aggregate on the right (`⚡ 2/3 connected`) is green only when every
  search is connected, and red as soon as one is rejected. Because each row
  reports separately, a single red row against green ones tells you the
  problem is that search, not your session.
- Duplicates (same league *and* search ID) are refused; the same search ID
  under two different leagues is fine.
- **20 searches is the cap.** That ceiling is this tool's, not a documented
  GGG limit: every search fetches independently the moment a listing lands,
  and the fetch and whisper endpoints are rate-limited per account, so a
  wall of searches is what earns you a 429.

All searches share the one set of imported cookies, so re-importing fixes
every row at once — but note the reverse too: an expired `cf_clearance`
takes down all of them together.

## Does the "in demand" bypass work?

Yes, for the usual case. The countdown on pathofexile.com/trade is enforced
by the website frontend before it calls the same whisper endpoint this tool
uses. Sending `POST /api/trade/whisper` yourself skips that UI delay.

It does NOT guarantee you win the item. You can still fail because:
- The whisper_token expires quickly (seconds). Click fast.
- Someone else whispers first and buys the item.
- GGG rate-limits your account (HTTP 429) if you spam requests.
- The listing is gone, seller is offline, or token is invalid (4xx errors).
- Server-side anti-abuse may still throttle hot listings independently of
  the visible countdown (rare, but possible).

## Setup

1. Install dependencies — double-click `install_dependencies.bat`, or run:
   ```
   pip install requests curl_cffi browser_cookie3
   ```
   - `curl_cffi` is required instead of the `websockets` package: the live
     WebSocket is proxied through Cloudflare, which fingerprints the TLS
     handshake itself (JA3/JA4), not just headers/cookies. Plain Python TLS
     (what `websockets`/`ssl` produce) gets flagged and the connection is
     closed with code 1008 shortly after it opens, even with valid cookies.
     curl_cffi can impersonate a real browser's TLS handshake.
   - `browser_cookie3` reads cookies directly out of your installed
     browsers' cookie storage (Firefox, then Brave, then Edge, then Chrome,
     in that order). Whichever browser supplies the cookies also decides the
     TLS fingerprint and User-Agent used for the WebSocket, because a
     `cf_clearance` cookie is only valid for the browser it was issued to.
     An earlier version of this tool automated a browser with
     Selenium to log in, but Cloudflare's challenge detects the WebDriver
     protocol itself (Firefox even shows a "Browser is under remote
     control" banner) and fails the check regardless of language/framework
     — reading the cookies of a browser you're already logged into sidesteps
     that entirely.

2. Log into `pathofexile.com` normally in your regular browser (Firefox
   recommended — see Known limitations) if you haven't already.

3. Run the app — double-click `start_livesearch.bat` (launches with no
   console window), or run:
   ```
   python live.py
   ```

4. Click **Import Cookies**. The app reads `POESESSID`, `cf_clearance`, and
   `POETOKEN` from your browser's cookie store for `pathofexile.com`. Never
   share these values; they grant full account access.

   On Brave, Chrome or Edge this will usually fail — see App-Bound
   Encryption under Known limitations. Use **Paste Cookies…** instead: open
   `pathofexile.com` in that browser, press F12 → Application → Storage →
   Cookies → `https://www.pathofexile.com`, and copy `POESESSID` and
   `cf_clearance` into the dialog. Pick the browser you copied from in the
   dropdown so the WebSocket presents that browser's fingerprint.

5. Pick your league from the dropdown (auto-populated from GGG's API), and
   paste a search ID or a full trade URL (e.g.
   `https://www.pathofexile.com/trade/search/Standard/AbCdEf123`) into the
   search field. Create the search on the trade site first, and click "Live
   Search" there once to register interest — this tool replaces keeping
   that browser tab open.

   Optionally type a short **Name** ("chest", "ring") to label that search,
   then click **+ Add** — or just press Enter in either field. Add as many
   searches as you want to watch, up to 20; see "Watching several searches at
   once". A full trade URL carries its own league, so the dropdown is only
   used for bare search IDs.

6. Click **Start All** to connect every search in the list. **Stop All**
   disconnects them all; a row's **✕** removes just that one, and a search
   added while the rest are running connects straight away.

7. Keep Path of Exile running and logged in on the same account whose
   cookies you imported.

8. (Optional) Drop an `alert.mp3` file next to `live.py` to get a sound
   alert whenever a new listing appears. Played via Windows' built-in media
   control interface (`winmm.dll`) — no extra dependency needed. Without
   the file, the app just uses the system bell instead.

## Headless CLI (`live_cli.py`)

For a VPS or any box with no display, `live_cli.py` does the same job without
Tkinter and without `browser_cookie3`. Cookies are hardcoded in the file
(there is no browser to read them out of on a server), and a keypress replaces
the card button. Dependencies are just `requests` and `curl_cffi`.

1. Get your cookies into the **Cookies** block at the top of `live_cli.py`.
   Run `python export_cookies.py` on your local machine (see below) and paste
   its output over that block, or copy the values out of DevTools by hand
   (F12 → Application → Storage → Cookies → `https://www.pathofexile.com`).
   Either way, `COOKIE_BROWSER` has to name the browser the cookies came from
   — that choice picks the TLS fingerprint and User-Agent the WebSocket
   presents, and `cf_clearance` is only valid for the browser it was issued
   to.

2. Run it with a search ID or a full trade URL:

   ```
   python live_cli.py https://www.pathofexile.com/trade/search/Standard/AbCdEf
   python live_cli.py AbCdEf --league Standard
   python live_cli.py --list-leagues        # current PC league names
   ```

3. Offers print as they arrive, each with a slot number:

   ```
   14:22:07  [1] Headhunter
         💰 200 divine   👤 Exile_Trader
         Enter = whisper  |  1 = whisper this
   14:22:09  [2] Mageblood
         💰 180 divine   👤 SomeGuy
   ```

   | Key | Action |
   | --- | --- |
   | `Enter` / `space` | whisper the newest live offer |
   | `1`–`9` | whisper that slot (a slot can be fired again) |
   | `l` | list live offers with their remaining time |
   | `q` / `Ctrl+C` | quit |

   The keyboard is read on the main thread and never blocks the socket, so new
   offers keep printing while you have not pressed anything — important,
   because a whisper token dies within seconds and a blocking prompt would
   hold up the next listing behind the current one.

4. Slots expire after 60 seconds (`--expiry SECONDS`) and are recycled, so on
   a busy search you are never pressing keys to clear a backlog of listings
   that are already gone. Only nine offers are addressable at once; the oldest
   is evicted when all digits are taken.

Options: `--auto` whispers every new offer on arrival with no keypress (mind
the rate limits, and see Terms of service), `--no-bell` suppresses the
terminal bell that rings on each new offer, `--no-color` drops ANSI colour.
Colour is off automatically when stdout is not a terminal, and with no usable
stdin at all (`nohup`, a detached systemd unit) the tool says so and keeps
listening instead of exiting — combine that with `--auto` if nothing will be
there to press keys.

The reconnect behaviour and close-code reasons below apply identically; the
CLI prints them as timestamped status lines.

### Running on a minimal Linux VPS

`live_cli.py` imports nothing Windows-only and nothing GUI-related at all — no
Tkinter, no `browser_cookie3`, no audio. On a bare Debian/Ubuntu/Alpine box:

```
python3 -m pip install requests curl_cffi
python3 live_cli.py AbCdEf --league Standard
```

- **Python 3.10+.** That floor comes from `curl_cffi`, not this script. Distros
  still on 3.9 (Debian 11, RHEL 9) will resolve an older `curl_cffi` that
  predates the browser TLS profiles named in `BROWSER_PROFILES`; the socket
  then never connects. The tool checks the installed version's profile list at
  startup and says so rather than looping. `curl_cffi` publishes musllinux
  wheels, so Alpine works without a compiler.
- **Locale.** With a UTF-8 locale you get the emoji; on an ASCII-only stream
  they degrade to `[ok]`, `$`, `@`, and non-ASCII item names (PoE's
  `Maelström Staff`) are replaced rather than raising mid-print.
- **Piping and logging.** Colour is suppressed when stdout is not a terminal
  or when `TERM` is `dumb`/unset, and the new-offer bell is never written into
  a redirected log or the journal.
- **Interactive over SSH.** Run it inside `tmux` or `screen` so the keypress
  workflow survives a dropped connection. `Ctrl+C` quits, and so does a
  `SIGTERM` from `systemctl stop` or `kill` — both restore the terminal's echo
  and line editing on the way out.
- **Unattended.** Under `nohup`, a systemd unit, or `< /dev/null` there is no
  keyboard to read; the tool reports that once and keeps listening instead of
  exiting. Pair it with `--auto` if nothing will be there to press keys.

## Exporting cookies without DevTools (`export_cookies.py`)

`export_cookies.py` reads the same cookies live.py's **Import Cookies** button
reads and prints them as a paste-ready block. Run it on the machine whose
browser is logged into pathofexile.com — not on the VPS, which has no browser —
then paste the output into `live_cli.py` over SSH. It needs `browser_cookie3`
(and nothing else).

```
$ python export_cookies.py
# Read from Firefox on 2026-08-18 14:02
POESESSID = "<32 chars>"            # real output prints the values in full
CF_CLEARANCE = "<533 chars>"
POETOKEN = "<483 chars>"
COOKIE_BROWSER = "firefox"

POESESSID: session cookie (no expiry)      # ← stderr from here down
cf_clearance: no practical expiry
```

Only the assignments go to stdout; the expiry lines and warnings go to stderr,
so `python export_cookies.py > cookies.txt` gives a paste-clean file. What each
cookie's expiry reads as depends on what the browser recorded — `EXPIRED at …`
is worth acting on, since a `cf_clearance` that has run out is the usual cause
of a repeating 1008. Use `--mask` to see what you have without putting the
values on screen.

| Flag | Effect |
| --- | --- |
| `--browser NAME` | read only `firefox`/`brave`/`edge`/`chrome` instead of trying each in turn |
| `--all` | report what every browser holds, values masked, failures included |
| `--json` | print a JSON object instead of a paste-ready block |
| `--mask` | hide most of each value — for checking what you have, not for pasting |

`--all` is the one to run when nothing is found; it names the reason per
browser:

```
$ python export_cookies.py --all
firefox  ✅ POESESSID, cf_clearance, POETOKEN
brave    ❌ Failed to decrypt the cipher text with DPAPI
edge     — readable, but no PoE session cookies
chrome   ❌ Unable to get key for cookie decryption
```

Those Brave and Chrome failures are App-Bound Encryption, not a bug — see
Known limitations. Firefox is the browser this works on; for the others, fall
back to copying from DevTools.

## GGG API requirements (must comply)

- Set a descriptive User-Agent with contact info (already configured).
- Respect rate limits; do not auto-spam whispers in a loop.
- Manual button click per item is intentional and safer for your account.

## The "Reconnecting…" status

The live-search socket is not a permanent connection, so seeing
**reconnecting…** is normal — it only matters how often, and why. Each
search's row names its own reason, taken from that socket's close code:

| Status | Close code | What it means |
| --- | --- | --- |
| `Reconnecting… (idle timeout)` | 1000 | GGG closed an idle socket cleanly. Routine — expect it every few minutes on a quiet search. The app reconnects after 2s and carries on. |
| `Reconnecting… (dropped)` | 1006 | Connection died without a close frame — usually your own network (Wi-Fi drop, VPN, sleep/resume). Recovers by itself. |
| `Reconnecting… (rejected — re-import cookies)` | 1008 | Cloudflare or GGG rejected the handshake. This one does **not** heal: it will loop every 2s forever. |
| `Reconnecting… (server error / restart / try again later)` | 1011/1012/1013 | GGG's side. Wait it out. |
| `Reconnecting… (ConnectionError, …)` | — | The connection attempt itself failed; the exception name is shown. |

A repeating **1008** means one of:

- `POESESSID` expired, or you logged out / logged in elsewhere. Re-import.
- `cf_clearance` no longer matches this client. Cloudflare binds that cookie
  to the User-Agent it was issued under and to the browser's TLS
  fingerprint (JA3/JA4), so cookies taken from one browser and replayed with
  another browser's fingerprint get thrown out. This is why the app now picks
  its impersonation target from whichever browser the cookies came from, and
  why the Paste Cookies dialog asks which browser you copied from.
- Your IP changed since the clearance was issued (VPN toggled, ISP re-lease).
- The `cf_clearance` cookie simply expired — they are short-lived. Reload
  pathofexile.com in the browser, then re-import.

Note that a search with no new listings looks identical to a working one, so
a row alternating between `connected` and `reconnecting — idle timeout` is
the healthy steady state, not a fault. With several searches running they
reconnect independently, so the rows will rarely all be green at the same
instant — `⚡ 2/3 connected` flickering is normal; a row *stuck* on
`rejected` is not.

## Known limitations

- Fetch endpoint accepts at most 10 item IDs per request (handled below).
- Reconnects automatically on disconnect (2s backoff) while running, but
  does not replay listings that appeared while offline.
- No rate-limit header parsing (429 responses are shown but not retried).
- Two frontends: the Tkinter UI (`live.py`) and the headless CLI
  (`live_cli.py`). Cards clear themselves 3 minutes after they appear (CLI
  slots after 60 seconds), and neither keeps a history of what scrolled past.
- **Multiple searches are a GUI feature only.** `live_cli.py` still takes
  exactly one search per process; run one process per search on a VPS.
- The GUI caps the list at 20 searches, and opens a separate WebSocket per
  search — there is no multiplexing to be had, so 20 searches means 20
  sockets and 20 independent fetches whenever listings land. The item card
  list is shared and unsorted across searches; each card is tagged with its
  search, but there is no per-search filtering or view.
- The searches list is not saved between runs — relaunching the app means
  re-adding them.
- A search cannot be paused individually, only removed and re-added; **Start
  All** / **Stop All** act on the whole list.
- The CLI addresses at most 9 offers at a time (digits `1`-`9`) and reads
  cookies only from the hardcoded values at the top of the file.
- **Import Cookies requires you to already be logged into pathofexile.com**
  in Firefox, Brave, Edge, or Chrome — it reads existing cookies, it doesn't
  log you in.
- **Firefox is the only browser Import Cookies reliably works on.** Brave,
  Chrome and Edge are all Chromium 127+, which ships "App-Bound Encryption":
  cookies are stored with a `v20` prefix and their key is held by the
  browser's own elevation service, so no third-party reader —
  `browser_cookie3` included — can decrypt them. The Brave loader is wired
  up and will work if that ever changes (or on an older profile whose
  cookies are still `v10`), but today it fails with a DPAPI decrypt error.
  For those browsers use **Paste Cookies…**, or log into pathofexile.com in
  Firefox and re-import.
- Cookie-format/DB-schema changes in a future browser version could break
  `browser_cookie3`'s extraction.

## Terms of service

Automating in-game actions may violate GGG's Terms of Service. This tool sends
the same API call the website would, but you are responsible for how you use
it. Prefer manual clicks and reasonable request rates.
