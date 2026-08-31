# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`livesearch-poe` — a Path of Exile trade tool that connects to GGG's live-search
WebSocket and fires `POST /api/trade/whisper` directly, skipping the trade
website's "this item is in high demand" countdown (which is enforced by the
site's frontend, not the API).

Two frontends over one shared flow, plus a cookie helper. No package, no tests,
no build step — three standalone scripts you run with `python`.

## Inventory

| File | Lines | What it is |
| --- | --- | --- |
| `live.py` | ~730 | Tkinter GUI app. Single `TradeApp(tk.Tk)` class. The primary frontend, Windows-oriented. |
| `live_cli.py` | ~850 | Headless CLI for a VPS. No Tkinter, no `browser_cookie3`, no audio. Cookies hardcoded at the top of the file. |
| `export_cookies.py` | ~285 | Reads PoE cookies out of an installed browser and prints a paste-ready block for `live_cli.py`'s Cookies section. |
| `README.md` | — | User-facing docs. Thorough — read it before answering "how does X work" questions. |
| `install_dependencies.bat` | — | `pip install requests curl_cffi browser_cookie3`, with a `pause` so double-clicking shows errors. |
| `start_livesearch.bat` | — | Launches `live.py` via `pythonw` (no console window), falling back to `python`. |
| `alert.mp3` | — | Sound played on a new listing (GUI only). Optional at runtime — absent, the app rings the system bell. |
| `.claude/settings.local.json` | — | Pre-approved Bash permissions (py_compile, git add/commit/push, pip install, `python -c`). |
| `.gitignore` | — | Bytecode only: `__pycache__/`, `*.py[cod]`. |

`.gitignore` covers `__pycache__/` and `*.py[cod]` only — the three scripts are
tracked normally.

## Dependencies

```
pip install requests curl_cffi browser_cookie3   # GUI
pip install requests curl_cffi                   # CLI only (VPS)
```

- **`curl_cffi` is not optional and has no substitute.** The live-search route
  is proxied through Cloudflare, which fingerprints the TLS handshake itself
  (JA3/JA4). Plain Python `ssl` — which is what the `websockets` package
  produces — opens the socket and is then closed with code 1008 regardless of
  valid cookies. `curl_cffi` impersonates a real browser's handshake. Never
  "simplify" this to `websockets`/`aiohttp`.
- **Python 3.10+**, a floor that comes from `curl_cffi`, not from this code. On
  3.9 pip resolves an old `curl_cffi` that predates the TLS profiles named in
  `BROWSER_PROFILES`; `unknown_impersonate_target()` in `live_cli.py` detects
  that at startup and says so rather than reconnect-looping forever.
- Dev box currently: Python 3.14.4, curl_cffi 0.16.0.

## Running

```
python live.py                                              # GUI
python live_cli.py https://www.pathofexile.com/trade/search/Standard/AbCdEf
python live_cli.py AbCdEf --league Standard
python live_cli.py --list-leagues
python export_cookies.py [--browser NAME|--all|--json|--mask]
```

Neither script can be exercised end-to-end without a live PoE session, so the
practical check after an edit is a compile plus a targeted `python -c`:

```
python -m py_compile live.py live_cli.py export_cookies.py
python -c "import live_cli; print(live_cli.parse_trade_url('https://www.pathofexile.com/trade/search/Standard/AbCdEf'))"
```

`live_cli.py --list-leagues` is the one command that hits the network without
needing cookies — a good smoke test for the `requests` path.

## Architecture — the shared flow

Both frontends implement the same six steps; the code is deliberately
duplicated rather than factored into a shared module.

1. **Cookies.** `POESESSID` (the session), `cf_clearance` (the Cloudflare pass),
   `POETOKEN` (optional). GUI reads them from the browser via `browser_cookie3`
   or a Paste dialog; CLI takes them from module constants.
2. **WebSocket.** `wss://www.pathofexile.com/api/trade/live/{league}/{search_id}`,
   opened with `cffi_requests.Session(impersonate=...).ws_connect(...)`.
3. **Push message.** GGG sends `{"result": "<signed token>"}` — a *string*, not
   the raw ID list older code expected. Both readers guard with
   `isinstance(data["result"], str)`.
4. **Fetch.** `GET /api/trade/fetch/{result_token}?query={search_id}`.
5. **Card / slot.** Item name, price, seller, token rendered to the UI or stdout.
6. **Whisper.** `POST /api/trade/whisper` with `{"token": ..., "continue": true}`.

Threading in both: the socket runs on a daemon thread, each whisper POST gets
its own daemon thread, and the UI/keyboard stays on the main thread. The rule
behind that is the same in both files — **a whisper token dies within seconds,
so nothing may block the socket thread**, or the next listing queues up behind
the current one.

## Invariants — do not break these

- **`hideout_token`, not `whisper_token`.** Both files read
  `listing.get("hideout_token")` from the fetch response. The README's prose
  still says `whisper_token` (GGG's older name); the code is what's correct.
  Don't "fix" the code to match the README.
- **The whisper body sends `continue: true` once.** An earlier version posted
  twice; commit `9df99a5` fixed that. One POST per click.
- **The send button is never disabled, only relabelled** (`↻ Send Again`, or
  `⚠` when the listing arrived tokenless). A whisper fails for reasons that
  clear up on retry — token race, transient 5xx, rate limit — so a retry must
  always be possible. Same rule in the CLI: a slot can be fired again.
- **Cookie browser determines the TLS fingerprint.** `cf_clearance` is bound by
  Cloudflare to the User-Agent it was issued under *and* to that browser's
  JA3/JA4. Cookies from Brave replayed with Firefox's fingerprint get a 1008.
  Hence `COOKIE_BROWSERS` (GUI) / `BROWSER_PROFILES` (CLI) carry the
  `(impersonate target, User-Agent)` pair alongside the browser name, and the
  Paste dialog asks which browser you copied from. Brave maps to `chrome146`,
  not a Brave-specific profile — Brave deliberately reports Chrome's exact UA
  with the minor version frozen at `0.0.0`.
- **WebSocket handshake headers stay minimal**: `Cookie`, `User-Agent`,
  `Origin`. No `Referer` or `X-Requested-With` — those don't exist on a real
  browser WS upgrade and are a bot-detection tripwire. REST calls
  (`_api_headers`) *do* send them; the two header sets are intentionally
  different.
- **Close the `cffi_requests.Session` in the reconnect loop's `finally`.** Not
  doing so leaks a libcurl handle per reconnect.
- **Never re-introduce Selenium.** Commit `35509a3` removed it: Cloudflare's
  challenge detects the WebDriver protocol itself (Marionette/CDP), which no
  amount of flag-hiding beats. Reading an already-logged-in browser's cookies
  sidesteps detection entirely.
- **`sys.stdout`/`sys.stderr` are `None` under `pythonw`.** `live.py` reopens
  them onto `os.devnull` at import time, before anything can `print()`.

## Secrets

`live_cli.py` holds real cookies in the file when in use. `POESESSID` and
`cf_clearance` grant full account access. Before any commit that touches
`live_cli.py`, check that the `POESESSID` / `CF_CLEARANCE` / `POETOKEN`
constants are still empty strings — `git diff --cached live_cli.py`. The same
goes for pasting terminal output into a message: `export_cookies.py` prints
values in full unless `--mask` is given.

## Reconnect semantics

Reconnecting is normal, not a fault — GGG closes idle sockets. Both files map
close codes through a `CLOSE_REASONS` dict so the status names the cause:
`1000` idle timeout (routine), `1006` dropped (local network), `1008` rejected,
`1011/1012/1013` GGG's side.

**1008 is the one that never heals** and will loop every 2s forever. Causes, in
rough order of likelihood: `cf_clearance` expired; cookies replayed under a
different browser's fingerprint; IP changed since the clearance was issued
(VPN, ISP re-lease); `POESESSID` expired or logged out elsewhere. When
debugging a "doesn't connect" report, get the close code first — it partitions
the problem space immediately.

## Browser cookie reality

Firefox is the only browser `browser_cookie3` reliably reads. Chromium 127+
(Brave, Chrome, Edge) ships App-Bound Encryption: cookies carry a `v20` prefix
and their key lives in the browser's elevation service, so no third-party
reader can decrypt them. The Brave/Chrome/Edge loaders are wired up and will
work if that changes (or on an old `v10` profile), but today they fail with a
DPAPI error. That is expected behaviour, not a bug to fix — the fallback is the
Paste Cookies dialog or DevTools.

## Conventions

- Comments explain **why**, not what, and are often several lines long. The
  Cloudflare/TLS, App-Bound Encryption, and threading notes in the source are
  load-bearing documentation — keep them near the code they justify, and update
  them when the reasoning changes.
- Tunables live as module- or class-level constants near their use
  (`CARD_EXPIRY_MS`, `OFFER_EXPIRY_SECONDS`, `MAX_SLOTS`,
  `RECONNECT_DELAY_SECONDS`), not as magic numbers.
- Non-ASCII output degrades rather than raising: both `live_cli.py` and
  `export_cookies.py` build a `fallback_table` from the stream's encoding
  because a `UnicodeEncodeError` mid-print would kill the listener. Any new
  emoji added to output needs an entry in `ASCII_FALLBACK`.
- The CLI writes only through `Console` (a lock serialises the socket, reaper
  and key threads); colour is suppressed off a TTY and when `TERM` is
  `dumb`/unset, and the bell is never written into a redirected log.
- Broad `except Exception` around every network and platform call is
  deliberate: this is a long-running listener that must not die on one bad
  response.
- `parse_trade_url()` exists identically in both frontends — accept either a
  bare search ID or a full trade URL anywhere a search is taken.
- Commit messages are imperative, one line, describing the user-visible change
  ("Expire item cards after 3 minutes, keep the whisper button clickable").
- README changes ship with the code change; the README is detailed and users
  rely on it. Cross-check the Known limitations section when behaviour changes.

## Gotchas

- The leagues endpoint returns each league once per realm (pc/xbox/sony) with
  the same id. Both files filter to `realm == "pc"`; dropping that triples the
  dropdown.
- The fetch endpoint accepts at most 10 item IDs per request.
- 429 responses are surfaced but never retried — there is no rate-limit header
  parsing anywhere. Adding auto-retry would be an account risk, not a feature.
- `--auto` in the CLI whispers every arrival with no keypress. It exists, but it
  is the mode most likely to draw rate limits or ToS attention.
- CLI slots are digits `1`–`9` only; the tenth live offer evicts the oldest.
- Neither frontend keeps history. Cards expire at 3 minutes, CLI slots at 60s
  (`--expiry`), and nothing replays listings missed while disconnected.
- `alert.mp3` plays through `winmm.dll` MCI — Windows-only, by design (no
  third-party audio dependency). The CLI has no audio at all, just the bell.

## Terms of service

Automating in-game actions may violate GGG's ToS. The project's stance is a
manual click/keypress per item and reasonable request rates; keep changes on
that side of the line, and keep the User-Agent identifiable with contact info
(`USER_AGENT` in both files) as GGG's API policy requires.
