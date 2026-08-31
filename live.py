"""PoE Live Search — Direct Whisper Tool. See README.md for full docs."""

import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import requests
from curl_cffi import requests as cffi_requests

# Launched via pythonw.exe (no console), sys.stdout/stderr are None, and
# the print() calls below would raise. Redirect them somewhere harmless.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# GGG API policy requires setting an identifiable User-Agent
USER_AGENT = "PoeLiveSearchApp/1.0 (contact: guspisia@gmail.com)"

# The WebSocket handshake has to look like it came from the same browser the
# cookies were imported from: Cloudflare binds a cf_clearance cookie to the
# User-Agent string it was issued under *and*, via JA3/JA4, to that browser's
# TLS handshake. Import from Brave but connect as Firefox and the clearance is
# rejected — the socket opens and is then closed with 1008.
FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0"
)
CHROMIUM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_WS_USER_AGENT = FIREFOX_UA
DEFAULT_WS_IMPERSONATE = "firefox135"
LEAGUES_URL = "https://www.pathofexile.com/api/trade/data/leagues"
DEFAULT_LEAGUES = ["Standard", "Hardcore"]
ALERT_SOUND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert.mp3")


def parse_trade_url(text: str):
    """Extracts league and search_id from a PoE trade search URL, if present."""
    match = re.search(r"pathofexile\.com/trade/search/([^/]+)/([^/?]+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


# One socket per search, so this also bounds concurrent fetches and alert
# noise. The ceiling is ours, not a documented GGG limit: every search fetches
# independently the moment a listing arrives, and the fetch/whisper endpoints
# are rate-limited per account, so a wall of searches is what draws a 429.
MAX_SEARCHES = 20


class Search:
    """One live-search subscription: its own socket, thread and status row.

    GGG's live route is keyed by /{league}/{search_id} and there is no way to
    multiplex several searches over one socket, so N searches means N sockets.
    Each carries its own `is_running` rather than sharing the app's — that is
    what lets a single row be removed, or a new one added and connected, while
    every other search keeps listening.
    """

    __slots__ = (
        "league", "search_id", "name", "state",
        "is_running", "thread", "ws",
        "row", "dot", "status_label",
    )

    def __init__(self, league, search_id, name=""):
        self.league = league
        self.search_id = search_id
        self.name = name
        self.state = "stopped"

        self.is_running = False
        self.thread = None
        self.ws = None

        # Row widgets; filled in by TradeApp._add_search_row and set back to
        # None when the row is removed, so a late status update from this
        # search's thread has something to test.
        self.row = None
        self.dot = None
        self.status_label = None

    @property
    def label(self):
        """Short tag stamped on every item card this search produces."""
        return self.name or self.search_id

    @property
    def target(self):
        return f"{self.league}/{self.search_id}"


class TradeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("PoE Live Search - On-Demand Whisper Tool")
        self.geometry("760x760")
        self.attributes("-topmost", True)

        # Populated by the Login flow; required before Start will connect.
        self.poesessid = None
        self.cf_clearance = None
        self.poetoken = None
        self.cookie_browser = None
        self.ws_user_agent = DEFAULT_WS_USER_AGENT
        self.ws_impersonate = DEFAULT_WS_IMPERSONATE

        # One Search per row in the list panel, each owning a socket thread.
        # `is_running` here means "Start All has been pressed" — it decides
        # whether a search added afterwards connects straight away.
        self.searches = []
        self.is_running = False

        self._setup_ui()
        self._refresh_leagues()

    # ---------------------------------------------------------------- UI --

    def _setup_ui(self):
        login_row = ttk.Frame(self, padding=(10, 10, 10, 0))
        login_row.pack(fill=tk.X)

        self.login_btn = ttk.Button(
            login_row, text="Import Cookies", command=self._on_login_click
        )
        self.login_btn.pack(side=tk.LEFT)

        self.paste_btn = ttk.Button(
            login_row, text="Paste Cookies…", command=self._on_paste_click
        )
        self.paste_btn.pack(side=tk.LEFT, padx=(5, 0))

        self.login_status = ttk.Label(
            login_row, text="No session imported", foreground="red"
        )
        self.login_status.pack(side=tk.LEFT, padx=10)

        connect_row = ttk.Frame(self, padding=(10, 10, 10, 0))
        connect_row.pack(fill=tk.X)

        ttk.Label(connect_row, text="League:").pack(side=tk.LEFT)
        self.league_var = tk.StringVar()
        self.league_combo = ttk.Combobox(
            connect_row, textvariable=self.league_var, values=DEFAULT_LEAGUES,
            width=14, state="normal",
        )
        self.league_combo.pack(side=tk.LEFT, padx=(5, 12))

        # Optional: a name is only a display label. Left blank, cards fall back
        # to tagging with the search ID (see Search.label).
        ttk.Label(connect_row, text="Name:").pack(side=tk.LEFT)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(connect_row, textvariable=self.name_var, width=12)
        self.name_entry.pack(side=tk.LEFT, padx=(5, 12))

        ttk.Label(connect_row, text="Search ID / URL:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(
            connect_row, textvariable=self.search_var, width=20
        )
        self.search_entry.pack(side=tk.LEFT, padx=(5, 8), fill=tk.X, expand=True)

        self.add_btn = ttk.Button(
            connect_row, text="+ Add", command=self._on_add_click
        )
        self.add_btn.pack(side=tk.LEFT)
        # Enter in either field adds, so paste-and-go never needs the mouse.
        self.search_entry.bind("<Return>", lambda _e: self._on_add_click())
        self.name_entry.bind("<Return>", lambda _e: self._on_add_click())

        # The list scrolls inside a fixed height: at MAX_SEARCHES rows an
        # unbounded frame would push the item cards off the window.
        list_frame = ttk.LabelFrame(self, text="Searches", padding=(6, 4))
        list_frame.pack(fill=tk.X, padx=10, pady=(8, 0))

        self.search_canvas = tk.Canvas(
            list_frame, height=self.SEARCH_LIST_HEIGHT, highlightthickness=0
        )
        search_scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.search_canvas.yview
        )
        self.search_list = ttk.Frame(self.search_canvas)
        self.search_list.bind(
            "<Configure>",
            lambda _e: self.search_canvas.configure(
                scrollregion=self.search_canvas.bbox("all")
            ),
        )
        self._search_window = self.search_canvas.create_window(
            (0, 0), window=self.search_list, anchor="nw"
        )
        # Rows are full-width so every ✕ button lines up on the right edge;
        # a canvas window otherwise shrinks to its content.
        self.search_canvas.bind(
            "<Configure>",
            lambda e: self.search_canvas.itemconfigure(
                self._search_window, width=e.width
            ),
        )
        self.search_canvas.configure(yscrollcommand=search_scroll.set)
        self.search_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        search_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._show_empty_label()

        control_row = ttk.Frame(self, padding=10)
        control_row.pack(fill=tk.X)

        self.start_btn = ttk.Button(
            control_row, text="▶ Start All", command=self._on_start_click
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = ttk.Button(
            control_row, text="■ Stop All", command=self._on_stop_click,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT)

        # Per-search state lives in its row; this is the aggregate only.
        self.status_label = ttk.Label(
            control_row, text="No searches", foreground="gray",
            font=("Arial", 10, "bold"),
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

    # ------------------------------------------------------------ Leagues --

    def _refresh_leagues(self):
        def _fetch():
            try:
                res = requests.get(
                    LEAGUES_URL, headers={"User-Agent": USER_AGENT}, timeout=10
                )
                res.raise_for_status()
                # The endpoint lists every league once per platform realm
                # (pc/xbox/sony) with the same id, tripling the list. This
                # tool only ever talks to www.pathofexile.com, so keep pc.
                leagues = [
                    entry["id"]
                    for entry in res.json().get("result", [])
                    if entry.get("realm", "pc") == "pc"
                ]
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

    # Launching an automated browser to log in gets flagged by Cloudflare's
    # challenge as a bot (Selenium's WebDriver protocol — Marionette on
    # Firefox, CDP on Chrome — is visible to the page and to the browser's
    # own UI, which is why Firefox shows "Browser is under remote control").
    # No amount of flag-hiding reliably beats that. Instead, this reads the
    # session cookies straight out of your existing, already-logged-in
    # browser's cookie storage — no automation involved, so there's nothing
    # for Cloudflare to detect. Firefox is tried first because it's the only
    # one without App-Bound Encryption, so its cookie store is the one that
    # reliably decrypts; whichever browser does supply the cookies also
    # decides the TLS fingerprint and User-Agent used for the WebSocket.
    #
    # (loader name, curl_cffi impersonate target, WebSocket User-Agent)
    COOKIE_BROWSERS = (
        ("firefox", DEFAULT_WS_IMPERSONATE, FIREFOX_UA),
        # Brave deliberately reports Chrome's exact User-Agent — no "Brave"
        # token, and the minor version frozen at 0.0.0 — so plain Chrome is
        # the correct profile to impersonate for it, not a Brave-specific one.
        ("brave", "chrome146", CHROMIUM_UA),
        ("edge", "chrome146", f"{CHROMIUM_UA} Edg/146.0.0.0"),
        ("chrome", "chrome146", CHROMIUM_UA),
    )

    def _on_login_click(self):
        self.login_btn.config(state=tk.DISABLED)
        self.login_status.config(text="Reading browser cookies…", foreground="orange")
        threading.Thread(target=self._do_login, daemon=True).start()

    def _do_login(self):
        try:
            import browser_cookie3
        except ImportError:
            self.after(
                0,
                lambda: self._login_failed(
                    "browser_cookie3 not installed (pip install browser_cookie3)"
                ),
            )
            return

        cookies = {}
        source = None
        last_error = ""
        for name, impersonate, user_agent in self.COOKIE_BROWSERS:
            loader = getattr(browser_cookie3, name, None)
            if loader is None:
                # Older browser_cookie3 releases predate some of these.
                continue
            try:
                jar = loader(domain_name="pathofexile.com")
                found = {c.name: c.value for c in jar}
            except Exception as e:
                last_error = str(e)
                continue
            if "POESESSID" in found:
                cookies = found
                source = (name, impersonate, user_agent)
                break

        if source is None:
            msg = (
                "No readable session found — log into pathofexile.com in "
                "Firefox, or use Paste Cookies."
            )
            if last_error:
                msg += f" ({last_error[:60]})"
            self.after(0, lambda: self._login_failed(msg))
            return

        self.poesessid = cookies["POESESSID"]
        self.cf_clearance = cookies.get("cf_clearance", "")
        self.poetoken = cookies.get("POETOKEN", "")
        self.cookie_browser, self.ws_impersonate, self.ws_user_agent = source

        self.after(0, self._login_success)

    def _login_success(self, verb="imported from"):
        self.login_status.config(
            text=f"✅ Cookies {verb} {self.cookie_browser.capitalize()}",
            foreground="green",
        )
        self.login_btn.config(state=tk.NORMAL, text="Re-import")
        self._refresh_leagues()

    # ------------------------------------------------------ Manual cookies --

    # Chromium 127+ ships App-Bound Encryption: cookies are stored with a
    # "v20" prefix under a key held by the browser's own elevation service,
    # so no third-party reader — browser_cookie3 included — can decrypt them.
    # Brave, Chrome and Edge are all affected, which leaves Firefox as the
    # only browser Import Cookies works on. For the others, copy the values
    # out of DevTools (F12 → Application → Storage → Cookies →
    # https://www.pathofexile.com) and paste them here instead.
    def _on_paste_click(self):
        dlg = tk.Toplevel(self)
        dlg.title("Paste session cookies")
        dlg.transient(self)
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            body,
            text="DevTools (F12) → Application → Cookies → pathofexile.com",
            font=("Arial", 9, "italic"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(body, text="Copied from:").grid(row=1, column=0, sticky="w")
        browser_var = tk.StringVar(value="brave")
        ttk.Combobox(
            body,
            textvariable=browser_var,
            values=[name for name, _, _ in self.COOKIE_BROWSERS],
            state="readonly",
            width=38,
        ).grid(row=1, column=1, sticky="we", pady=2)

        entries = {}
        rows = (
            ("POESESSID", "POESESSID (required)"),
            ("cf_clearance", "cf_clearance"),
            ("POETOKEN", "POETOKEN (optional)"),
        )
        for offset, (key, label) in enumerate(rows, start=2):
            ttk.Label(body, text=f"{label}:").grid(row=offset, column=0, sticky="w")
            var = tk.StringVar()
            ttk.Entry(body, textvariable=var, width=40).grid(
                row=offset, column=1, sticky="we", pady=2
            )
            entries[key] = var

        error = ttk.Label(body, text="", foreground="red")
        error.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def _save():
            poesessid = entries["POESESSID"].get().strip()
            if not poesessid:
                error.config(text="POESESSID is required.")
                return
            name = browser_var.get()
            profile = next(
                (p for p in self.COOKIE_BROWSERS if p[0] == name),
                self.COOKIE_BROWSERS[0],
            )
            self.poesessid = poesessid
            self.cf_clearance = entries["cf_clearance"].get().strip()
            self.poetoken = entries["POETOKEN"].get().strip()
            self.cookie_browser, self.ws_impersonate, self.ws_user_agent = profile
            dlg.destroy()
            self._login_success(verb="pasted from")

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Use these", command=_save).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        dlg.bind("<Return>", lambda _e: _save())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()

    def _login_failed(self, msg):
        self.login_status.config(text=f"❌ {msg[:80]}", foreground="red")
        self.login_btn.config(state=tk.NORMAL)

    # ---------------------------------------------------------- Searches --

    SEARCH_LIST_HEIGHT = 108   # roughly five rows before the list scrolls

    # Row dot colour per state, so one glance down the list says which
    # searches are actually live. "rejected" is close code 1008 â the one that
    # never heals on its own â so it gets a colour of its own instead of
    # looking like a routine reconnect.
    STATE_COLORS = {
        "stopped": "gray",
        "connecting": "orange",
        "connected": "green",
        "reconnecting": "orange",
        "rejected": "red",
    }

    def _show_empty_label(self):
        self.empty_label = ttk.Label(
            self.search_list,
            text="No searches yet — add a search ID or trade URL above.",
            foreground="gray",
        )
        self.empty_label.pack(anchor="w", padx=4, pady=2)

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

    def _add_search_row(self, search):
        if self.empty_label is not None:
            self.empty_label.destroy()
            self.empty_label = None

        row = ttk.Frame(self.search_list)
        row.pack(fill=tk.X, padx=2, pady=1)
        search.row = row

        search.dot = ttk.Label(row, text="○", foreground="gray", width=2)
        search.dot.pack(side=tk.LEFT)

        title = (
            f"{search.name} — {search.target}" if search.name else search.target
        )
        ttk.Label(row, text=title).pack(side=tk.LEFT)

        ttk.Button(
            row, text="✕", width=3, command=lambda: self._remove_search(search)
        ).pack(side=tk.RIGHT)
        search.status_label = ttk.Label(row, text="stopped", foreground="gray")
        search.status_label.pack(side=tk.RIGHT, padx=6)

    def _on_add_click(self):
        try:
            league, search_id = self._resolve_search(
                self.search_var.get(), self.league_var.get()
            )
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return

        if any(s.league == league and s.search_id == search_id for s in self.searches):
            messagebox.showinfo(
                "Already added", f"{league}/{search_id} is already in the list."
            )
            return
        if len(self.searches) >= MAX_SEARCHES:
            messagebox.showwarning(
                "Too many searches",
                f"{MAX_SEARCHES} live searches is the cap — remove one first.",
            )
            return

        search = Search(league, search_id, self.name_var.get().strip())
        self.searches.append(search)
        self._add_search_row(search)
        self.search_var.set("")
        self.name_var.set("")

        # Added mid-session: connect it now. Otherwise picking up one new
        # search would mean stopping and restarting every other one.
        if self.is_running:
            self._start_search(search)
        self._refresh_summary()

    def _remove_search(self, search):
        self._stop_search(search)
        if search in self.searches:
            self.searches.remove(search)
        if search.row is not None and search.row.winfo_exists():
            search.row.destroy()
        # Its socket thread may still be unwinding and post a status update;
        # clearing these is what tells _set_search_status the row is gone.
        search.row = search.dot = search.status_label = None
        if not self.searches:
            self._show_empty_label()
        self._refresh_summary()

    def _set_search_status(self, search, state, text=None):
        """Thread-safe row update; also recomputes the aggregate status."""
        def _apply():
            search.state = state
            color = self.STATE_COLORS.get(state, "gray")
            dot = "○" if state == "stopped" else "●"
            if search.dot is not None and search.dot.winfo_exists():
                search.dot.config(text=dot, foreground=color)
            if search.status_label is not None and search.status_label.winfo_exists():
                search.status_label.config(text=text or state, foreground=color)
            self._refresh_summary()

        self.after(0, _apply)

    def _refresh_summary(self):
        total = len(self.searches)
        if total == 0:
            self.status_label.config(text="No searches", foreground="gray")
            return
        plural = "search" if total == 1 else "searches"
        if not self.is_running:
            self.status_label.config(
                text=f"{total} {plural} — stopped", foreground="gray"
            )
            return
        live = sum(1 for s in self.searches if s.state == "connected")
        if live == total:
            color = "green"
        elif any(s.state == "rejected" for s in self.searches):
            color = "red"
        else:
            color = "orange"
        self.status_label.config(
            text=f"⚡ {live}/{total} connected", foreground=color
        )

    # --------------------------------------------------------- Start/Stop --

    def _start_search(self, search):
        if search.is_running:
            return
        search.is_running = True
        self._set_search_status(search, "connecting", "connecting…")
        search.thread = threading.Thread(
            target=self._listen_websocket, args=(search,), daemon=True
        )
        search.thread.start()

    def _stop_search(self, search):
        search.is_running = False
        if search.ws is not None:
            # Closing from here is what unblocks the ws.recv() its own thread
            # is parked on; the loop then sees is_running False and exits.
            try:
                search.ws.close()
            except Exception:
                pass
        self._set_search_status(search, "stopped")

    def _on_start_click(self):
        if not self.poesessid:
            messagebox.showwarning("Not logged in", "Click Import Cookies first.")
            return
        if not self.searches:
            messagebox.showwarning(
                "No searches", "Add at least one search ID or trade URL first."
            )
            return

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        for search in self.searches:
            self._start_search(search)
        self._refresh_summary()

    def _on_stop_click(self):
        self.is_running = False
        for search in self.searches:
            self._stop_search(search)
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self._refresh_summary()

    # --------------------------------------------------------------- Ping --

    def _ping(self):
        if not os.path.exists(ALERT_SOUND_PATH):
            self.bell()
            return
        threading.Thread(target=self._play_alert, daemon=True).start()

    def _play_alert(self):
        # Plays via the Windows Media Control Interface (winmm.dll) instead
        # of a third-party mp3 package — no extra dependency, and it's
        # built into every Windows install.
        import ctypes

        alias = "poe_livesearch_alert"
        winmm = ctypes.WinDLL("winmm.dll")

        def _mci(cmd):
            buf = ctypes.create_unicode_buffer(128)
            if winmm.mciSendStringW(cmd, buf, len(buf), 0) != 0:
                raise RuntimeError(f"MCI command failed: {cmd!r}")

        try:
            _mci(f'open "{ALERT_SOUND_PATH}" type mpegvideo alias {alias}')
            try:
                _mci(f"play {alias} wait")
            finally:
                _mci(f"close {alias}")
        except Exception:
            self.after(0, self.bell)

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
    # Whisper tokens expire within seconds, so a card older than this is only
    # clutter — drop it rather than let the list grow all session.
    CARD_EXPIRY_MS = 180_000

    SEND_TEXT = "💬 Send Whisper / Travel to Hideout"
    # The button is never disabled — a token that looks missing or spent can
    # still work, so it stays clickable and is only marked. See send_whisper_action.
    SEND_TEXT_NO_TOKEN = "⚠ Send Whisper / Travel to Hideout"
    SEND_TEXT_RETRY = "↻ Send Again / Travel to Hideout"

    def _create_card_widget(self, item):
        """Renders an item card with a manual action button."""
        self._ping()

        # Titled with the search tag so a card is attributable at a glance
        # when several searches are feeding the same list.
        card = ttk.LabelFrame(
            self.scroll_frame, text=f"[{item['search']}] {item['name']}", padding=10
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

        def _expire():
            _stop_blink()
            if card.winfo_exists():
                card.destroy()

        self.after(self.CARD_EXPIRY_MS, _expire)

        details = f"💰 Price: {item['price']}  |  👤 Seller: {item['seller']}"
        ttk.Label(card, text=details).pack(anchor="w", pady=2)

        # Status indicator for button click
        action_status = ttk.Label(card, text="", font=("Arial", 9, "italic"))

        def _on_send_click():
            _stop_blink()
            self.send_whisper_action(item["token"], action_btn, action_status)

        # Manual Action Button
        action_btn = ttk.Button(card, text=self.SEND_TEXT, command=_on_send_click)

        if not item["token"]:
            # Marked, not disabled: the fetch response occasionally omits the
            # token even though the listing is live, so let the user try.
            action_btn.config(text=self.SEND_TEXT_NO_TOKEN)
            action_status.config(
                text="⚠ No whisper token — will likely fail", foreground="orange"
            )

        action_btn.pack(side=tk.LEFT, pady=5)
        action_status.pack(side=tk.LEFT, padx=10)

    def send_whisper_action(self, token, button, status_label):
        """Sends the POST /api/trade/whisper request when user clicks button.

        The button deliberately stays enabled: a whisper can fail for reasons
        that clear up on a retry (token race, transient 5xx, rate limit), so
        it's only relabelled to show it has already been fired once.
        """
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
            # Cards are removed after CARD_EXPIRY_MS, so the widgets may be
            # gone by the time a slow request comes back.
            def _apply():
                if status_label.winfo_exists():
                    status_label.config(text=text, foreground=color)
                if button.winfo_exists():
                    button.config(text=self.SEND_TEXT_RETRY)

            self.after(0, _apply)

        def _post():
            url = "https://www.pathofexile.com/api/trade/whisper"
            headers = self._api_headers(json_body=True)

            try:
                res = requests.post(
                    url,
                    json={"token": token, "continue": True},
                    headers=headers,
                )
            except Exception:
                _set_status("❌ Request Failed", "red")
                return

            if res.status_code != 200:
                # e.g. 404 = the listing is already gone; nothing to confirm.
                _set_status(_error_text(res), "red")
                return

            _set_status("✅ Action Sent to Client!", "green")

        threading.Thread(target=_post, daemon=True).start()

    # -------------------------------------------------------- WebSocket --

    def _listen_websocket(self, search):
        """Reconnect loop for one search, on that search's own daemon thread."""
        ws_url = (
            "wss://www.pathofexile.com/api/trade/live/"
            f"{search.league}/{search.search_id}"
        )
        # Only send headers a real browser WebSocket handshake can produce
        # (no X-Requested-With / Referer — those don't exist on WS upgrades
        # and are a bot-detection tripwire). This route is proxied through
        # Cloudflare, which fingerprints the TLS handshake itself — plain
        # Python ssl gets closed with 1008 regardless of cookies, so this
        # uses curl_cffi's impersonation of the cookie browser (see
        # COOKIE_BROWSERS) instead of the `websockets` package.
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

        while search.is_running:
            reason = ""
            code = None
            session = None
            try:
                session = cffi_requests.Session(impersonate=self.ws_impersonate)
                ws = session.ws_connect(ws_url, headers=headers)
                search.ws = ws

                self._set_search_status(search, "connected", "connected")

                while search.is_running:
                    msg, opcode = ws.recv()

                    if opcode == WS_CLOSE_OPCODE:
                        # Server closed the connection — msg here is a raw
                        # 2-byte close code, not JSON. Reconnect.
                        code = ws.close_code
                        reason = self._close_reason(code)
                        break

                    data = json.loads(msg)

                    # GGG's live-search push no longer sends raw item IDs; it
                    # sends a short-lived signed token (expires in seconds,
                    # like whisper_token) that must be handed straight to the
                    # fetch endpoint in place of an ID list.
                    if "result" in data and isinstance(data["result"], str):
                        self._fetch_items(search, data["result"])

            except Exception as e:
                if search.is_running:
                    print(f"WebSocket error ({search.target}): {e!r}")
                    # Acking a close frame can itself fail if the peer is
                    # already gone; the code it sent is still the useful part.
                    code = getattr(search.ws, "close_code", None)
                    reason = self._close_reason(code) or type(e).__name__
            finally:
                search.ws = None
                if session is not None:
                    # Not closing this leaks a libcurl handle per reconnect.
                    try:
                        session.close()
                    except Exception:
                        pass

            if not search.is_running:
                break

            # The reason matters: an idle 1000 is routine, whereas a repeating
            # 1008 means the cookies/fingerprint were rejected and reconnecting
            # will never fix itself. Without it every failure looks identical.
            # A 1008 on one row while the others stay green also narrows the
            # cause: it is that search, not the session.
            text = "reconnecting…" if not reason else f"reconnecting — {reason}"
            state = "rejected" if code == 1008 else "reconnecting"
            self._set_search_status(search, state, text)
            time.sleep(2)

    # WebSocket close codes worth naming; anything else is shown as-is.
    CLOSE_REASONS = {
        1000: "idle timeout",
        1001: "server going away",
        1006: "dropped",
        1008: "rejected — re-import cookies",
        1011: "server error",
        1012: "server restart",
        1013: "try again later",
    }

    def _close_reason(self, code):
        if code is None:
            return ""
        return self.CLOSE_REASONS.get(code, f"closed {code}")

    def _fetch_items(self, search, result_token):
        """Retrieves item metadata and whisper token from GGG API.

        result_token is the short-lived signed token from the live-search
        push message, used in place of a raw comma-separated ID list. The
        query parameter has to be *this* search's ID — a token from one
        search does not fetch against another.
        """
        headers = self._api_headers()
        fetch_url = (
            f"https://www.pathofexile.com/api/trade/fetch/{result_token}"
            f"?query={search.search_id}"
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
                    "search": search.label,
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
