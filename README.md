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
   then click **Start**.
3. The app connects to the live-search WebSocket for that query:
   `wss://www.pathofexile.com/api/trade/live/{league}/{search_id}`
4. When new listings appear, GGG pushes a token over the socket, the app
   plays `alert.mp3` (if present next to `live.py`; otherwise it falls back
   to the system bell), and a card appears in the list.
5. For each token, the app fetches listing details (including the
   `whisper_token`): `GET /api/trade/fetch/{token}?query={search_id}`
6. When you click a card's button, the app sends the whisper / travel-to-
   hideout command: `POST /api/trade/whisper` with `{"token": "<whisper_token>"}`.
   A 200 response means GGG accepted the action; the in-game client should
   receive the whisper or hideout invite shortly after.
7. Click **Stop** at any time to disconnect the live search.

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
     browsers' cookie storage (Firefox, then Edge, then Chrome, in that
     order). An earlier version of this tool automated a browser with
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

5. Pick your league from the dropdown (auto-populated from GGG's API), and
   paste a search ID or a full trade URL (e.g.
   `https://www.pathofexile.com/trade/search/Standard/AbCdEf123`) into the
   search field. Create the search on the trade site first, and click "Live
   Search" there once to register interest — this tool replaces keeping
   that browser tab open.

6. Click **Start** to connect. Click **Stop** to disconnect at any time.

7. Keep Path of Exile running and logged in on the same account whose
   cookies you imported.

8. (Optional) Drop an `alert.mp3` file next to `live.py` to get a sound
   alert whenever a new listing appears. Played via Windows' built-in media
   control interface (`winmm.dll`) — no extra dependency needed. Without
   the file, the app just uses the system bell instead.

## GGG API requirements (must comply)

- Set a descriptive User-Agent with contact info (already configured).
- Respect rate limits; do not auto-spam whispers in a loop.
- Manual button click per item is intentional and safer for your account.

## Known limitations

- Fetch endpoint accepts at most 10 item IDs per request (handled below).
- Reconnects automatically on disconnect (2s backoff) while running, but
  does not replay listings that appeared while offline.
- No rate-limit header parsing (429 responses are shown but not retried).
- Tkinter UI only; cards are not removed when listings expire.
- **Import Cookies requires you to already be logged into pathofexile.com**
  in Firefox, Edge, or Chrome — it reads existing cookies, it doesn't log
  you in.
- **Firefox is the most reliable source.** Chrome and Edge (Chromium 127+)
  ship "App-Bound Encryption," which ties cookie decryption to the browser
  binary itself and blocks most third-party cookie readers, including
  `browser_cookie3`. If Import Cookies fails on Chrome/Edge, log into
  pathofexile.com in Firefox instead and re-import.
- Cookie-format/DB-schema changes in a future browser version could break
  `browser_cookie3`'s extraction.

## Terms of service

Automating in-game actions may violate GGG's Terms of Service. This tool sends
the same API call the website would, but you are responsible for how you use
it. Prefer manual clicks and reasonable request rates.
