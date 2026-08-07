"""PoE Live Search — Direct Whisper Tool. See README.md for full docs."""

import json
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import requests
from curl_cffi import requests as cffi_requests

# GGG API policy requires setting an identifiable User-Agent
USER_AGENT = "PoeLiveSearchApp/1.0 (contact: guspisia@gmail.com)"
# Fallback WS User-Agent, overwritten with the real one captured at login.
DEFAULT_WS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0"
)
LEAGUES_URL = "https://www.pathofexile.com/api/trade/data/leagues"
DEFAULT_LEAGUES = ["Standard", "Hardcore"]
LOGIN_TIMEOUT_S = 300


def parse_trade_url(text: str):
    """Extracts league and search_id from a PoE trade search URL, if present."""
    match = re.search(r"pathofexile\.com/trade/search/([^/]+)/([^/?]+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


class TradeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("PoE Live Search - On-Demand Whisper Tool")
        self.geometry("700x650")
        self.attributes("-topmost", True)

        # Populated by the Login flow; required before Start will connect.
        self.poesessid = None
        self.cf_clearance = None
        self.poetoken = None
        self.ws_user_agent = DEFAULT_WS_USER_AGENT

        self.league = None
        self.search_id = None
        self.is_running = False
        self.ws_thread = None
        self.current_ws = None

        self._setup_ui()
        self._refresh_leagues()

    # ---------------------------------------------------------------- UI --

    def _setup_ui(self):
        login_row = ttk.Frame(self, padding=(10, 10, 10, 0))
        login_row.pack(fill=tk.X)

        self.login_btn = ttk.Button(
            login_row, text="Login", command=self._on_login_click
        )
        self.login_btn.pack(side=tk.LEFT)

        self.login_status = ttk.Label(
            login_row, text="Not logged in", foreground="red"
        )
        self.login_status.pack(side=tk.LEFT, padx=10)

        connect_row = ttk.Frame(self, padding=10)
        connect_row.pack(fill=tk.X)

        ttk.Label(connect_row, text="League:").pack(side=tk.LEFT)
        self.league_var = tk.StringVar()
        self.league_combo = ttk.Combobox(
            connect_row, textvariable=self.league_var, values=DEFAULT_LEAGUES,
            width=16, state="normal",
        )
        self.league_combo.pack(side=tk.LEFT, padx=(5, 15))

        ttk.Label(connect_row, text="Search ID / URL:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(
            connect_row, textvariable=self.search_var, width=24
        )
        self.search_entry.pack(side=tk.LEFT, padx=(5, 15), fill=tk.X, expand=True)

        self.start_btn = ttk.Button(
            connect_row, text="▶ Start", command=self._on_start_click
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = ttk.Button(
            connect_row, text="■ Stop", command=self._on_stop_click,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT)

        header = ttk.Frame(self, padding=10)
        header.pack(fill=tk.X)

        self.league_label = ttk.Label(header, text="League: -", font=("Arial", 10, "bold"))
        self.league_label.pack(side=tk.LEFT, padx=5)
        self.search_id_label = ttk.Label(header, text="Search ID: -")
        self.search_id_label.pack(side=tk.LEFT, padx=5)

        self.status_label = ttk.Label(header, text="Not connected", foreground="gray")
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

    # ------------------------------------------------------------ Leagues --

    def _refresh_leagues(self):
        def _fetch():
            try:
                res = requests.get(
                    LEAGUES_URL, headers={"User-Agent": USER_AGENT}, timeout=10
                )
                res.raise_for_status()
                leagues = [entry["id"] for entry in res.json().get("result", [])]
            except Exception:
                leagues = []
            if leagues:
                self.after(0, lambda: self._set_leagues(leagues))

        threading.Thread(target=_fetch, daemon=True).start()

    def _set_leagues(self, leagues):
        self.league_combo["values"] = leagues
        if not self.league_var.get():
            self.league_var.set(leagues[0])

    # -------------------------------------------------------------- Login --

    def _on_login_click(self):
        self.login_btn.config(state=tk.DISABLED)
        self.login_status.config(text="Opening browser…", foreground="orange")
        threading.Thread(target=self._do_login, daemon=True).start()

    def _detect_default_browser(self):
        """Reads Windows' registered default browser for https links."""
        try:
            import winreg
            key_path = (
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                r"\https\UserChoice"
            )
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        except Exception:
            return "edge"

        prog_id = prog_id.lower()
        if "chrome" in prog_id:
            return "chrome"
        if "firefox" in prog_id:
            return "firefox"
        return "edge"

    def _launch_browser(self, webdriver):
        """Launches the user's default browser, falling back to Edge."""
        browser = self._detect_default_browser()
        if browser == "chrome":
            try:
                return webdriver.Chrome()
            except Exception:
                pass
        elif browser == "firefox":
            try:
                return webdriver.Firefox()
            except Exception:
                pass
        return webdriver.Edge()

    def _do_login(self):
        try:
            from selenium import webdriver
        except ImportError:
            self.after(
                0,
                lambda: self._login_failed(
                    "selenium not installed (pip install selenium)"
                ),
            )
            return

        driver = None
        cookies = {}
        ua = None
        try:
            driver = self._launch_browser(webdriver)
            driver.get("https://www.pathofexile.com/login")

            deadline = time.time() + LOGIN_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(1)
                raw = driver.get_cookies()
                cookies = {c["name"]: c["value"] for c in raw}
                if "POESESSID" in cookies:
                    break

            if "POESESSID" in cookies:
                # Visiting the trade site lets Cloudflare/GGG set the
                # cf_clearance / POETOKEN cookies this tool also needs.
                driver.get("https://www.pathofexile.com/trade")
                time.sleep(3)
                cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
                ua = driver.execute_script("return navigator.userAgent")
        except Exception as e:
            self.after(0, lambda: self._login_failed(str(e)))
            return
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        if "POESESSID" not in cookies:
            self.after(
                0,
                lambda: self._login_failed(
                    "Login window closed before POESESSID was captured."
                ),
            )
            return

        self.poesessid = cookies["POESESSID"]
        self.cf_clearance = cookies.get("cf_clearance", "")
        self.poetoken = cookies.get("POETOKEN", "")
        if ua:
            self.ws_user_agent = ua

        self.after(0, self._login_success)

    def _login_success(self):
        self.login_status.config(text="✅ Logged in", foreground="green")
        self.login_btn.config(state=tk.NORMAL, text="Re-login")
        self._refresh_leagues()

    def _login_failed(self, msg):
        self.login_status.config(text=f"❌ {msg[:80]}", foreground="red")
        self.login_btn.config(state=tk.NORMAL)

    # --------------------------------------------------------- Start/Stop --

    def _resolve_search(self, text, league):
        text = text.strip()
        if not text:
            raise ValueError("Enter a search ID or paste a full trade URL.")
        parsed = parse_trade_url(text)
        if parsed:
            return parsed
        if not league:
            raise ValueError("Select a league, or paste a full trade URL instead.")
        return league, text

    def _on_start_click(self):
        if not self.poesessid:
            messagebox.showwarning("Not logged in", "Click Login first.")
            return
        try:
            league, search_id = self._resolve_search(
                self.search_var.get(), self.league_var.get()
            )
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return

        self.league = league
        self.search_id = search_id
        self.league_label.config(text=f"League: {league}")
        self.search_id_label.config(text=f"Search ID: {search_id}")

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_label.config(text="Connecting...", foreground="orange")

        self.ws_thread = threading.Thread(target=self._listen_websocket, daemon=True)
        self.ws_thread.start()

    def _on_stop_click(self):
        self.is_running = False
        if self.current_ws is not None:
            try:
                self.current_ws.close()
            except Exception:
                pass
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_label.config(text="Stopped", foreground="gray")

    # --------------------------------------------------------------- Ping --

    def _ping(self):
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            self.bell()

    # ------------------------------------------------------------ Headers --

    def _api_headers(self, *, json_body: bool = False) -> dict:
        """Headers GGG expects for trade API calls."""
        headers = {
            "Cookie": f"POESESSID={self.poesessid}",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.pathofexile.com/trade",
            "X-Requested-With": "XMLHttpRequest",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ------------------------------------------------------------- Cards --

    def add_item_card(self, item_data):
        """Thread-safe UI update to insert a new item card."""
        self.after(0, self._create_card_widget, item_data)

    NEW_INDICATOR_TIMEOUT_MS = 15_000
    NEW_INDICATOR_BLINK_MS = 500

    def _create_card_widget(self, item):
        """Renders an item card with a manual action button."""
        self._ping()

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
            headers = self._api_headers(json_body=True)

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

    # -------------------------------------------------------- WebSocket --

    def _listen_websocket(self):
        ws_url = f"wss://www.pathofexile.com/api/trade/live/{self.league}/{self.search_id}"
        # Only send headers a real browser WebSocket handshake can produce
        # (no X-Requested-With / Referer — those don't exist on WS upgrades
        # and are a bot-detection tripwire). This route is proxied through
        # Cloudflare, which fingerprints the TLS handshake itself — plain
        # Python ssl gets closed with 1008 regardless of cookies, so this
        # uses curl_cffi's Firefox impersonation instead of `websockets`.
        cookie_parts = [f"POESESSID={self.poesessid}"]
        if self.cf_clearance:
            cookie_parts.append(f"cf_clearance={self.cf_clearance}")
        if self.poetoken:
            cookie_parts.append(f"POETOKEN={self.poetoken}")

        headers = {
            "Cookie": "; ".join(cookie_parts),
            "User-Agent": self.ws_user_agent,
            "Origin": "https://www.pathofexile.com",
        }

        WS_CLOSE_OPCODE = 8

        while self.is_running:
            try:
                session = cffi_requests.Session(impersonate="firefox135")
                ws = session.ws_connect(ws_url, headers=headers)
                self.current_ws = ws

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
                if self.is_running:
                    print(f"WebSocket error: {e!r}")
            finally:
                self.current_ws = None

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
        headers = self._api_headers()
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
