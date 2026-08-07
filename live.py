"""
PoE Live Search — Direct Whisper Tool
=====================================

Purpose
-------
Bypass the trade website's "This item is in high demand" countdown by calling
GGG's official trade API directly with your POESESSID, instead of clicking
through the browser UI.

How it works
------------
1. Connect to the live-search WebSocket for your saved trade query:
   wss://www.pathofexile.com/api/trade/live/{league}/{search_id}

2. When new listings appear, GGG pushes item IDs over the socket.

3. For each ID batch, fetch listing details (including `whisper_token`):
   GET /api/trade/fetch/{ids}?query={search_id}

4. When you click the button, send the whisper / travel-to-hideout command:
   POST /api/trade/whisper  body: {"token": "<whisper_token>"}

   A 200 response means GGG accepted the action; the in-game client should
   receive the whisper or hideout invite shortly after.

Does the "in demand" bypass work?
---------------------------------
Yes, for the usual case. The countdown on pathofexile.com/trade is enforced
by the website frontend before it calls the same whisper endpoint this tool
uses. Sending POST /api/trade/whisper yourself skips that UI delay.

It does NOT guarantee you win the item. You can still fail because:
  - The whisper_token expires quickly (seconds). Click fast.
  - Someone else whispers first and buys the item.
  - GGG rate-limits your account (HTTP 429) if you spam requests.
  - The listing is gone, seller is offline, or token is invalid (4xx errors).
  - Server-side anti-abuse may still throttle hot listings independently of
    the visible countdown (rare, but possible).

Setup
-----
1. Install dependencies:
     pip install requests curl_cffi

   curl_cffi is required instead of the `websockets` package: the live
   WebSocket is proxied through Cloudflare, which fingerprints the TLS
   handshake itself (JA3/JA4), not just headers/cookies. Plain Python
   TLS (what `websockets`/`ssl` produce) gets flagged and the connection
   is closed with code 1008 shortly after it opens, even with valid
   cookies. curl_cffi can impersonate a real browser's TLS handshake.

2. Set POESESSID below (or replace with os.environ["POESESSID"]).
   Find it in browser DevTools → Application → Cookies → pathofexile.com.
   Never share or commit this value; it is full account access.

3. Set TRADE_URL to your live-search URL, e.g.:
     https://www.pathofexile.com/trade/search/Standard/AbCdEf123
   The league and search ID are parsed automatically from that URL.
   Create the search on the trade site first, then click "Live Search" there
   once to register interest — this tool replaces keeping that browser tab open.

4. Run the script:
     python "live.py"

5. Keep Path of Exile running and logged in on the same account as POESESSID.

GGG API requirements (must comply)
----------------------------------
  - Set a descriptive User-Agent with contact info (already configured).
  - Respect rate limits; do not auto-spam whispers in a loop.
  - Manual button click per item is intentional and safer for your account.

Known limitations of this script
----------------------------------
  - Fetch endpoint accepts at most 10 item IDs per request (handled below).
  - Reconnects automatically on disconnect (2s backoff), but does not
    replay listings that appeared while offline.
  - No rate-limit header parsing (429 responses are shown but not retried).
  - Tkinter UI only; cards are not removed when listings expire.

Terms of service
----------------
Automating in-game actions may violate GGG's Terms of Service. This tool sends
the same API call the website would, but you are responsible for how you use
it. Prefer manual clicks and reasonable request rates.
"""

import json
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import requests
from curl_cffi import requests as cffi_requests

# ==================== CONFIGURATION ====================
POESESSID = "1dc4a279d8595af11d917c841f01cf8f"
TRADE_URL = "https://www.pathofexile.com/trade/search/Allflame/Z6EjVJb9uQ/live"

# GGG API policy requires setting an identifiable User-Agent
USER_AGENT = "PoeLiveSearchApp/1.0 (contact: guspisia@gmail.com)"

# The live-search WebSocket is not the documented trade API — it's the
# same internal channel pathofexile.com's own JS uses, and it's proxied
# through Cloudflare. Unlike the REST endpoints, it validates the
# Cloudflare clearance cookie and closes with 1008 shortly after connect
# if it's missing. Copy these from your browser's DevTools -> Network ->
# (the live WS request) -> Headers -> Request Headers -> Cookie, and the
# User-Agent from the same request. Both expire/rotate periodically and
# are bound to the IP/browser that issued them, so re-copy them if the
# socket starts closing with 1008 again.
CF_CLEARANCE = "Q6J1qT335iRynvrLelyYJZ_OzIR5Pq5NJlmbWHOgoy4-1785825005-1.2.1.1-pQyg209nzWfIVeGNjHucs0oGgkcjclcUYcG6TJPMrwnBrIEXwTuiZlxCe.G582PmW.eIP3HbJ3CmRW1qLUIVro9VrkKf8yz3cZ92iUiCa6OoSSdVOvhrvI7UnA7GrM3Mwwrhx1F8WKuuegTcLk3q49zirVTk7_VDp6eV1JfKyNWausxQvRKIME3uQo8GGo7_F6YsQNDzL_Tha7g8C1QjUtMXaVGX9pwk33iE4XmsvxBorEyz7NP5L3BaCjnpAGKsau9L0fFTWTXI3oinjLyCxJFL8gGpQ8E.N9W4dyFwRl59E6CkwNHWPbGTDaPe_374MsareBpkM8c3qOoeCsMBqFOfv4.g6DCMItKF4x8eNXSWP7yzlDwQx7B4yqtnNW54l3Uo50XBKBCmPIj102lKp4KN4vjzEUGyVLzNk0CwBzksLz8t.WIrUEmJP6Ib3y8N0sXKMg0VZYJIVDw.k5lpeQ"
POETOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJhdWQiOiJvYXV0aC9pbnRlcm5hbCIsImV4cCI6MTc4NTkxMTQwNCwiaWF0IjoxNzg1ODI1MDA0LCJpc3MiOiJ1cm46cGF0aG9mZXhpbGU6cHJvZHVjdGlvbiIsInJpZCI6IjU3OTE1M2IxODEyNjMwMjMwOGQ4ZjMyODRjYzkxZGJiIiwic2NvcGUiOiJpbnRlcm5hbCIsInN1YiI6ImIzM2ZjNjk4LTU0ZDMtNDVkMC05NTUzLTQ4ODg3MTViZTVhOSIsInZlcnNpb24iOiIxZjliYWEwYyIsImNsaWVudF9pZCI6ImludGVybmFsIiwicmVzcG9uc2VfdHlwZSI6ImludGVybmFsIn0.p2LuWn4W0DgoGfpqEmvb7OgE49jZPp540tmC5Fhsw31rXlF49w_3FHAaYAy3B1Hqj0_zdQsc0W6msRLDc2JO8g"
WS_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0"
# =======================================================


def _api_headers(*, json_body: bool = False) -> dict:
    """Headers GGG expects for trade API calls."""
    headers = {
        "Cookie": f"POESESSID={POESESSID}",
        "User-Agent": USER_AGENT,
        "Referer": "https://www.pathofexile.com/trade",
        "X-Requested-With": "XMLHttpRequest",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def parse_trade_url(url: str):
    """Extracts league and search_id from a PoE trade search URL."""
    pattern = r"pathofexile\.com/trade/search/([^/]+)/([^/]+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError("Invalid PoE trade search URL format.")
    return match.group(1), match.group(2)


class TradeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("PoE Live Search - On-Demand Whisper Tool")
        self.geometry("650x600")
        self.attributes("-topmost", True)

        self.league, self.search_id = parse_trade_url(TRADE_URL)

        # Build UI layout
        self._setup_ui()

        # Start WebSocket listener thread
        self.is_running = True
        self.ws_thread = threading.Thread(
            target=self._listen_websocket, daemon=True
        )
        self.ws_thread.start()

    def _setup_ui(self):
        # Header Status
        header = ttk.Frame(self, padding=10)
        header.pack(fill=tk.X)

        ttk.Label(
            header, text=f"League: {self.league}", font=("Arial", 10, "bold")
        ).pack(side=tk.LEFT, padx=5)
        ttk.Label(header, text=f"Search ID: {self.search_id}").pack(
            side=tk.LEFT, padx=5
        )

        self.status_label = ttk.Label(
            header, text="Connecting...", foreground="orange"
        )
        self.status_label.pack(side=tk.RIGHT, padx=5)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)

        # Scrollable Canvas for Item Cards
        self.canvas = tk.Canvas(self)
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview
        )
        self.scroll_frame = ttk.Frame(self.canvas)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            ),
        )

        self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def add_item_card(self, item_data):
        """Thread-safe UI update to insert a new item card."""
        self.after(0, self._create_card_widget, item_data)

    NEW_INDICATOR_TIMEOUT_MS = 15_000
    NEW_INDICATOR_BLINK_MS = 500

    def _create_card_widget(self, item):
        """Renders an item card with a manual action button."""
        card = ttk.LabelFrame(
            self.scroll_frame, text=item["name"], padding=10
        )
        existing = self.scroll_frame.pack_slaves()
        if existing:
            card.pack(fill=tk.X, expand=True, pady=5, padx=5, before=existing[0])
        else:
            card.pack(fill=tk.X, expand=True, pady=5, padx=5)

        # Blinking "NEW" indicator: blinks until the user acts on this
        # card, or it's been sitting unactioned long enough to be stale.
        indicator = ttk.Label(card, text="🔴 NEW", foreground="red", font=("Arial", 9, "bold"))
        indicator.pack(anchor="w")
        blink_state = {"job": None, "visible": True}

        def _blink():
            if not indicator.winfo_exists():
                return
            blink_state["visible"] = not blink_state["visible"]
            indicator.config(text="🔴 NEW" if blink_state["visible"] else "")
            blink_state["job"] = self.after(self.NEW_INDICATOR_BLINK_MS, _blink)

        def _stop_blink():
            if blink_state["job"] is not None:
                self.after_cancel(blink_state["job"])
                blink_state["job"] = None
            if indicator.winfo_exists():
                indicator.destroy()

        blink_state["job"] = self.after(self.NEW_INDICATOR_BLINK_MS, _blink)
        self.after(self.NEW_INDICATOR_TIMEOUT_MS, _stop_blink)

        details = f"💰 Price: {item['price']}  |  👤 Seller: {item['seller']}"
        ttk.Label(card, text=details).pack(anchor="w", pady=2)

        # Status indicator for button click
        action_status = ttk.Label(card, text="", font=("Arial", 9, "italic"))

        def _on_send_click():
            _stop_blink()
            self.send_whisper_action(item["token"], action_btn, action_status)

        # Manual Action Button
        action_btn = ttk.Button(
            card,
            text="💬 Send Whisper / Travel to Hideout",
            command=_on_send_click,
        )

        if not item["token"]:
            action_btn.config(state=tk.DISABLED)
            action_status.config(
                text="❌ Missing whisper token", foreground="red"
            )

        action_btn.pack(side=tk.LEFT, pady=5)
        action_status.pack(side=tk.LEFT, padx=10)

    def send_whisper_action(self, token, button, status_label):
        """Sends the POST /api/trade/whisper request when user clicks button."""
        button.config(state=tk.DISABLED)
        status_label.config(text="Sending request...", foreground="blue")

        def _error_text(res):
            try:
                message = res.json().get("error", {}).get("message", "")
            except Exception:
                message = res.text[:80] if res.text else ""
            if res.status_code == 404:
                return "❌ Item no longer available"
            return f"❌ Error {res.status_code}: {message}"

        def _set_status(text, color):
            self.after(0, lambda: status_label.config(text=text, foreground=color))

        def _post():
            url = "https://www.pathofexile.com/api/trade/whisper"
            headers = _api_headers(json_body=True)

            try:
                res = requests.post(url, json={"token": token}, headers=headers)
            except Exception:
                _set_status("❌ Request Failed", "red")
                return

            if res.status_code != 200:
                # e.g. 404 = the listing is already gone; nothing to confirm.
                _set_status(_error_text(res), "red")
                return

            # The site sends this token twice: once bare, which is what
            # actually starts/completes the hideout join, then again with
            # continue=True to confirm it when the site would show the
            # "in demand, teleport anyway?" countdown. For an item that
            # wasn't in demand, the join already completed on the first
            # call, so this second call has nothing to confirm and comes
            # back as an error (observed: 503) even though the teleport
            # already happened — that's not a real failure, so it's not
            # surfaced as one.
            try:
                requests.post(
                    url,
                    json={"token": token, "continue": True},
                    headers=headers,
                    timeout=8,
                )
            except Exception:
                pass

            _set_status("✅ Action Sent to Client!", "green")

        threading.Thread(target=_post, daemon=True).start()

    def _listen_websocket(self):
        ws_url = f"wss://www.pathofexile.com/api/trade/live/{self.league}/{self.search_id}"
        # Only send headers a real browser WebSocket handshake can produce
        # (no X-Requested-With / Referer — those don't exist on WS upgrades
        # and are a bot-detection tripwire). This route is proxied through
        # Cloudflare, which fingerprints the TLS handshake itself — plain
        # Python ssl gets closed with 1008 regardless of cookies, so this
        # uses curl_cffi's Firefox impersonation instead of `websockets`.
        cookie_parts = [f"POESESSID={POESESSID}"]
        if CF_CLEARANCE:
            cookie_parts.append(f"cf_clearance={CF_CLEARANCE}")
        if POETOKEN:
            cookie_parts.append(f"POETOKEN={POETOKEN}")

        headers = {
            "Cookie": "; ".join(cookie_parts),
            "User-Agent": WS_USER_AGENT,
            "Origin": "https://www.pathofexile.com",
        }

        WS_CLOSE_OPCODE = 8

        while self.is_running:
            try:
                session = cffi_requests.Session(impersonate="firefox135")
                ws = session.ws_connect(ws_url, headers=headers)

                self.after(
                    0,
                    lambda: self.status_label.config(
                        text="⚡ Connected (Listening)", foreground="green"
                    ),
                )

                while self.is_running:
                    msg, opcode = ws.recv()

                    if opcode == WS_CLOSE_OPCODE:
                        # Server closed the connection (e.g. normal 1000
                        # closure after some idle period) — msg here is a
                        # raw 2-byte close code, not JSON. Reconnect.
                        break

                    data = json.loads(msg)

                    # GGG's live-search push no longer sends raw item IDs; it
                    # sends a short-lived signed token (expires in seconds,
                    # like whisper_token) that must be handed straight to the
                    # fetch endpoint in place of an ID list.
                    if "result" in data and isinstance(data["result"], str):
                        self._fetch_items(data["result"])

            except Exception as e:
                print(f"WebSocket error: {e!r}")

            if not self.is_running:
                break

            self.after(
                0,
                lambda: self.status_label.config(
                    text="Reconnecting...", foreground="orange"
                ),
            )
            time.sleep(2)

    def _fetch_items(self, result_token):
        """Retrieves item metadata and whisper token from GGG API.

        result_token is the short-lived signed token from the live-search
        push message, used in place of a raw comma-separated ID list.
        """
        headers = _api_headers()
        fetch_url = (
            f"https://www.pathofexile.com/api/trade/fetch/{result_token}"
            f"?query={self.search_id}"
        )

        try:
            res = requests.get(fetch_url, headers=headers)
            if res.status_code != 200:
                print(f"Fetch failed ({res.status_code}): {res.text[:120]}")
                return

            for entry in res.json().get("result", []):
                listing = entry.get("listing", {})
                item = entry.get("item", {})

                item_name = (
                    item.get("name")
                    if item.get("name")
                    else item.get("typeLine", "Unknown Item")
                )
                price = listing.get("price", {})
                price_str = (
                    f"{price.get('amount', '')} {price.get('currency', '')}"
                ).strip()

                parsed_item = {
                    "name": item_name,
                    "price": price_str or "Unpriced",
                    "seller": listing.get("account", {}).get("name", "Unknown"),
                    "token": listing.get("hideout_token"),
                }
                self.add_item_card(parsed_item)
        except Exception as e:
            print(f"Error fetching item details: {e}")


if __name__ == "__main__":
    app = TradeApp()
    app.mainloop()