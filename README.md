# livesearch-poe

PoE Live Search — Direct Whisper Tool

## Purpose

Bypass the trade website's "This item is in high demand" countdown by calling
GGG's official trade API directly with your session cookies, instead of
clicking through the browser UI.

## How it works

1. Log in through the app's built-in **Login** button (opens a real browser
   window) so the app can capture your session cookies for you.
2. Pick a league and paste a search ID (or a full trade URL) into the app,
   then click **Start**.
3. The app connects to the live-search WebSocket for that query:
   `wss://www.pathofexile.com/api/trade/live/{league}/{search_id}`
4. When new listings appear, GGG pushes a token over the socket, the app
   plays a ping sound, and a card appears in the list.
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

1. Install dependencies:
   ```
   pip install requests curl_cffi selenium
   ```
   - `curl_cffi` is required instead of the `websockets` package: the live
     WebSocket is proxied through Cloudflare, which fingerprints the TLS
     handshake itself (JA3/JA4), not just headers/cookies. Plain Python TLS
     (what `websockets`/`ssl` produce) gets flagged and the connection is
     closed with code 1008 shortly after it opens, even with valid cookies.
     curl_cffi can impersonate a real browser's TLS handshake.
   - `selenium` drives the login browser window. It launches whichever
     browser is set as your Windows default (Chrome, Firefox, or Edge),
     falling back to Edge if detection or launch fails. Selenium 4.6+
     auto-downloads the matching driver (`chromedriver`/`geckodriver`/
     `msedgedriver`) on first use — no manual driver setup required, as
     long as that browser is installed.

2. Run the app:
   ```
   python live.py
   ```

3. Click **Login**. A browser window opens on the PoE login page — log in
   there as normal (solve any Cloudflare challenge, 2FA, etc.). The app
   polls the browser for your session cookies (`POESESSID`, `cf_clearance`,
   `POETOKEN`) and closes the window automatically once it has them. Never
   share these values; they grant full account access.

4. Pick your league from the dropdown (auto-populated from GGG's API), and
   paste a search ID or a full trade URL (e.g.
   `https://www.pathofexile.com/trade/search/Standard/AbCdEf123`) into the
   search field. Create the search on the trade site first, and click "Live
   Search" there once to register interest — this tool replaces keeping
   that browser tab open.

5. Click **Start** to connect. Click **Stop** to disconnect at any time.

6. Keep Path of Exile running and logged in on the same account you used to
   log in above.

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
- Login capture relies on Selenium browser automation; if GGG changes its
  cookie names or login flow, the capture step may need updates.

## Terms of service

Automating in-game actions may violate GGG's Terms of Service. This tool sends
the same API call the website would, but you are responsible for how you use
it. Prefer manual clicks and reasonable request rates.
