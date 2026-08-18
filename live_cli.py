"""PoE Live Search — headless CLI whisper tool.

A no-GUI counterpart to live.py, meant for a VPS: cookies are hardcoded
below instead of read out of a browser, offers print to stdout as they
arrive, and a single keypress fires the whisper. See README.md for how the
underlying trade API calls work.

    python live_cli.py https://www.pathofexile.com/trade/search/Standard/AbCdEf
    python live_cli.py AbCdEf --league Standard
    python live_cli.py AbCdEf --auto          # whisper without a keypress

Keys while running:
    Enter / space  whisper the newest live offer
    1..9           whisper that slot
    l              list live offers
    q              quit
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
try:
    import requests
    from curl_cffi import requests as cffi_requests
except ImportError as e:
    # The likeliest first-run failure on a fresh minimal box, and a bare
    # traceback doesn't say what to do about it.
    raise SystemExit(
        f"Missing dependency: {e.name or e}\n"
        "    python3 -m pip install requests curl_cffi\n"
        "curl_cffi needs Python 3.10+ and supplies the browser TLS "
        "fingerprint the live-search socket requires; there is no substitute."
    )

# --------------------------------------------------------------- Cookies --
# Paste these from the browser you are logged into pathofexile.com with:
# F12 -> Application -> Storage -> Cookies -> https://www.pathofexile.com
# Never share these values; they grant full account access.
POESESSID = ""
CF_CLEARANCE = ""
POETOKEN = ""

# Which browser the cookies above were copied from. Cloudflare binds a
# cf_clearance cookie to the User-Agent it was issued under *and*, via
# JA3/JA4, to that browser's TLS handshake — so the WebSocket has to present
# that same browser's fingerprint or the socket is opened and then closed
# with 1008. One of: firefox, brave, edge, chrome.
COOKIE_BROWSER = "firefox"

# Defaults used when the corresponding CLI argument is omitted.
LEAGUE = "Standard"
SEARCH_ID = ""

# ----------------------------------------------------------------- Config --

# GGG API policy requires setting an identifiable User-Agent
USER_AGENT = "PoeLiveSearchApp/1.0 (contact: guspisia@gmail.com)"

FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0"
)
CHROMIUM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

# name -> (curl_cffi impersonate target, WebSocket User-Agent). Brave
# deliberately reports Chrome's exact User-Agent — no "Brave" token, minor
# version frozen at 0.0.0 — so plain Chrome is the right profile for it.
BROWSER_PROFILES = {
    "firefox": ("firefox135", FIREFOX_UA),
    "brave": ("chrome146", CHROMIUM_UA),
    "edge": ("chrome146", f"{CHROMIUM_UA} Edg/146.0.0.0"),
    "chrome": ("chrome146", CHROMIUM_UA),
}

LEAGUES_URL = "https://www.pathofexile.com/api/trade/data/leagues"
WHISPER_URL = "https://www.pathofexile.com/api/trade/whisper"

# Whisper tokens are only good for seconds, so a slot older than this is
# dead weight — drop it so the digit keys keep pointing at live listings.
OFFER_EXPIRY_SECONDS = 60
# Digits 1..9 address slots; the oldest is evicted when they are all taken.
MAX_SLOTS = 9
RECONNECT_DELAY_SECONDS = 2

# WebSocket close codes worth naming; anything else is shown as-is.
CLOSE_REASONS = {
    1000: "idle timeout",
    1001: "server going away",
    1006: "dropped",
    1008: "rejected — cookies/fingerprint no longer accepted",
    1011: "server error",
    1012: "server restart",
    1013: "try again later",
}

WS_CLOSE_OPCODE = 8


def parse_trade_url(text: str):
    """Extracts league and search_id from a PoE trade search URL, if present."""
    match = re.search(r"pathofexile\.com/trade/search/([^/]+)/([^/?]+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


# ------------------------------------------------------------------ Output --

# A Windows console still defaults to a legacy code page (cp1252) with no
# emoji in it, and a stream like that raises UnicodeEncodeError *mid-print* —
# which would kill the listener the moment an offer arrived. Same story on a
# POSIX box running under LANG=C. Anything the stream can't encode is swapped
# for an ASCII stand-in instead.
ASCII_FALLBACK = {
    "✅": "[ok]", "❌": "[x]", "⚠": "[!]", "⚡": ">>",
    "💰": "$", "👤": "@", "→": "->", "…": "...", "—": "-",
}


def fallback_table(encoding):
    """Maps just the characters `encoding` cannot represent; usually empty."""
    table = {}
    for char, replacement in ASCII_FALLBACK.items():
        try:
            char.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            table[ord(char)] = replacement
    return table


class Console:
    """Serialises writes from the socket, reaper and key threads.

    Colour is only emitted to a real terminal, so piping to a file or to
    systemd's journal stays readable.
    """

    COLORS = {
        "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "cyan": "36", "gray": "90",
    }

    def __init__(self, use_color=None, bell=True):
        self._lock = threading.Lock()
        # A bell byte written into a log file or journald is just noise.
        self.bell = bell and sys.stdout.isatty()
        if use_color is None:
            # TERM=dumb (and an unset TERM, as in a bare container) means the
            # terminal makes no promises about escape sequences.
            dumb = os.environ.get("TERM", "") in ("", "dumb")
            use_color = sys.stdout.isatty() and not dumb and self._enable_vt()
        self.use_color = use_color
        self._table = fallback_table(sys.stdout.encoding or "utf-8")
        # The table above only covers this tool's own glyphs. Item and seller
        # names carry their own non-ASCII (PoE has a "Maelström Staff"), and on
        # a narrow stream that would raise UnicodeEncodeError mid-write and
        # take the listener down with it. Degrade those characters instead.
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    @staticmethod
    def _enable_vt():
        """Windows consoles need ANSI escapes switched on explicitly."""
        if os.name != "nt":
            return True
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32")
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            return False

    def paint(self, text, color=None, bold=False):
        if not self.use_color or (color is None and not bold):
            return text
        codes = []
        if bold:
            codes.append("1")
        if color in self.COLORS:
            codes.append(self.COLORS[color])
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    def write(self, text="", color=None, bold=False, ring=False):
        line = self.paint(text, color, bold)
        if self._table:
            line = line.translate(self._table)
        with self._lock:
            if ring and self.bell:
                sys.stdout.write("\a")
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def status(self, text, color=None):
        stamp = time.strftime("%H:%M:%S")
        self.write(f"{self.paint(stamp, 'gray')}  {self.paint(text, color)}")


# ------------------------------------------------------------------- Keys --


class KeyReader:
    """Reads single keypresses, and hands the terminal back as it found it.

    Use as a context manager. `available` is False when there is no keyboard
    to read — a systemd unit, `nohup`, or `< /dev/null` all give a stdin that
    cannot be put into cbreak mode — and the caller should just keep listening.
    """

    def __init__(self):
        self.available = True
        self._fd = None
        self._saved = None

    def __enter__(self):
        if os.name == "nt":
            return self
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            # cbreak, not raw: this leaves ISIG on so Ctrl+C still interrupts,
            # and leaves output post-processing on so concurrent prints from
            # the socket thread don't lose their carriage returns.
            #
            # Set once for the whole session, not around each read: setcbreak
            # flushes the input queue, so toggling it per keypress can swallow
            # a key typed while the previous one was still being handled.
            tty.setcbreak(self._fd)
        except Exception:
            self.available = False
            return self

        # Without this a default SIGTERM (systemctl stop, plain kill) ends the
        # process before the restore in __exit__ runs, leaving the shell that
        # launched us with echo and line editing switched off.
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                signal.signal(signum, self._on_terminate)
            except (ValueError, OSError):
                # Not the main thread, or the platform won't take a handler.
                pass
        return self

    @staticmethod
    def _on_terminate(_signum, _frame):
        # Unwinds through __exit__ the same way Ctrl+C does, so the caller's
        # existing quit path handles it.
        raise KeyboardInterrupt

    def __exit__(self, *_exc):
        if self._saved is None:
            return False
        import termios

        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        return False

    def read(self):
        """Blocks for one keypress.

        Returns "" for keys with no useful single-character form (arrows,
        F-keys), and None once stdin is closed.
        """
        if os.name == "nt":
            import msvcrt

            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # prefix byte for a function/arrow key
                msvcrt.getwch()
                return ""
            return ch

        return sys.stdin.read(1) or None


# ----------------------------------------------------------------- Offers --


class Offer:
    __slots__ = ("slot", "name", "price", "seller", "token", "created", "sent")

    def __init__(self, slot, name, price, seller, token):
        self.slot = slot
        self.name = name
        self.price = price
        self.seller = seller
        self.token = token
        self.created = time.monotonic()
        self.sent = False

    @property
    def age(self):
        return time.monotonic() - self.created


class LiveCli:

    def __init__(self, league, search_id, *, console, auto=False,
                 expiry=OFFER_EXPIRY_SECONDS, browser=COOKIE_BROWSER,
                 poesessid=None, cf_clearance=None, poetoken=None):
        self.league = league
        self.search_id = search_id
        self.console = console
        self.auto = auto
        self.expiry = expiry

        self.poesessid = POESESSID if poesessid is None else poesessid
        self.cf_clearance = CF_CLEARANCE if cf_clearance is None else cf_clearance
        self.poetoken = POETOKEN if poetoken is None else poetoken
        self.ws_impersonate, self.ws_user_agent = BROWSER_PROFILES.get(
            browser, BROWSER_PROFILES["firefox"]
        )

        self.is_running = False
        self.current_ws = None
        self._offers = {}          # slot -> Offer
        self._newest_slot = None
        self._offers_lock = threading.Lock()

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

    # -------------------------------------------------------- Offer slots --

    def _add_offer(self, name, price, seller, token):
        with self._offers_lock:
            self._prune_locked()
            slot = next(
                (n for n in range(1, MAX_SLOTS + 1) if n not in self._offers), None
            )
            if slot is None:
                # All nine digits are in use and none have expired yet; the
                # oldest is the least likely to still be claimable.
                oldest = min(self._offers.values(), key=lambda o: o.created)
                slot = oldest.slot
                del self._offers[slot]
            offer = Offer(slot, name, price, seller, token)
            self._offers[slot] = offer
            self._newest_slot = slot
        return offer

    def _prune_locked(self):
        for slot, offer in list(self._offers.items()):
            if offer.age > self.expiry:
                del self._offers[slot]
        if self._newest_slot not in self._offers:
            self._newest_slot = None

    def _take(self, slot=None):
        """Returns a live offer by slot, or the newest one when slot is None."""
        with self._offers_lock:
            self._prune_locked()
            if slot is None:
                slot = self._newest_slot
            if slot is None:
                return None
            return self._offers.get(slot)

    def _reaper(self):
        """Drops expired offers so the digit keys never fire a dead token."""
        while self.is_running:
            time.sleep(1)
            with self._offers_lock:
                self._prune_locked()

    # ------------------------------------------------------------- Render --

    def _print_offer(self, offer):
        c = self.console
        stamp = time.strftime("%H:%M:%S")
        c.write(
            f"{c.paint(stamp, 'gray')}  "
            f"{c.paint(f'[{offer.slot}]', 'cyan', bold=True)} "
            f"{c.paint(offer.name, None, bold=True)}",
            ring=True,
        )
        c.write(f"      💰 {offer.price}   👤 {offer.seller}")
        if not offer.token:
            # The fetch response occasionally omits the token even though the
            # listing is live, so still let the user try it.
            c.write("      ⚠ no whisper token — a send will likely fail", "yellow")
        if not self.auto:
            hint = f"Enter = whisper  |  {offer.slot} = whisper this"
            c.write(f"      {c.paint(hint, 'gray')}")

    def list_offers(self):
        with self._offers_lock:
            self._prune_locked()
            offers = sorted(self._offers.values(), key=lambda o: o.created)
            newest = self._newest_slot
        if not offers:
            self.console.write("      (no live offers)", "gray")
            return
        for offer in offers:
            marks = []
            if offer.slot == newest:
                marks.append("newest")
            if offer.sent:
                marks.append("sent")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            left = int(max(0, self.expiry - offer.age))
            self.console.write(
                f"      [{offer.slot}] {offer.name} — {offer.price} — "
                f"{offer.seller} — {left}s left{suffix}",
                "cyan",
            )

    # ------------------------------------------------------------ Whisper --

    def whisper(self, offer):
        """POST /api/trade/whisper for one offer, on a background thread.

        A resend is always allowed: a whisper can fail for reasons that clear
        up on a retry (token race, transient 5xx, rate limit).
        """
        label = f"[{offer.slot}] {offer.name}"
        verb = "re-sending" if offer.sent else "sending"
        offer.sent = True
        self.console.write(f"      → {verb} whisper for {label}…", "blue")

        def _error_text(res):
            try:
                message = res.json().get("error", {}).get("message", "")
            except Exception:
                message = res.text[:80] if res.text else ""
            if res.status_code == 404:
                return f"❌ {label}: item no longer available"
            if res.status_code == 429:
                return f"❌ {label}: rate limited (429) — slow down"
            return f"❌ {label}: error {res.status_code} {message}".rstrip()

        def _post():
            try:
                res = requests.post(
                    WHISPER_URL,
                    json={"token": offer.token, "continue": True},
                    headers=self._api_headers(json_body=True),
                    timeout=15,
                )
            except Exception as e:
                self.console.write(f"      ❌ {label}: request failed ({e!r})", "red")
                return
            if res.status_code != 200:
                self.console.write("      " + _error_text(res), "red")
                return
            self.console.write(f"      ✅ {label}: sent to client", "green")

        threading.Thread(target=_post, daemon=True).start()

    # ---------------------------------------------------------- WebSocket --

    def _close_reason(self, code):
        if code is None:
            return ""
        return CLOSE_REASONS.get(code, f"closed {code}")

    def _listen_websocket(self):
        ws_url = (
            f"wss://www.pathofexile.com/api/trade/live/"
            f"{self.league}/{self.search_id}"
        )
        # Only send headers a real browser WebSocket handshake can produce (no
        # X-Requested-With / Referer — those don't exist on WS upgrades and are
        # a bot-detection tripwire). This route is proxied through Cloudflare,
        # which fingerprints the TLS handshake itself, so plain Python ssl gets
        # closed with 1008 regardless of cookies; hence curl_cffi impersonating
        # the browser the cookies came from.
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

        while self.is_running:
            reason = ""
            session = None
            try:
                session = cffi_requests.Session(impersonate=self.ws_impersonate)
                ws = session.ws_connect(ws_url, headers=headers)
                self.current_ws = ws
                self.console.status("⚡ connected (listening)", "green")

                while self.is_running:
                    msg, opcode = ws.recv()

                    if opcode == WS_CLOSE_OPCODE:
                        # Server closed the connection — msg here is a raw
                        # 2-byte close code, not JSON. Reconnect.
                        reason = self._close_reason(ws.close_code)
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
                    # Acking a close frame can itself fail if the peer is
                    # already gone; the code it sent is still the useful part.
                    code = getattr(self.current_ws, "close_code", None)
                    reason = self._close_reason(code) or f"{type(e).__name__}: {e}"
            finally:
                self.current_ws = None
                if session is not None:
                    # Not closing this leaks a libcurl handle per reconnect.
                    try:
                        session.close()
                    except Exception:
                        pass

            if not self.is_running:
                break

            # The reason matters: an idle 1000 is routine, whereas a repeating
            # 1008 means the cookies/fingerprint were rejected and reconnecting
            # will never fix itself. Without it every failure looks identical.
            text = "reconnecting…" if not reason else f"reconnecting… ({reason})"
            self.console.status(text, "yellow")
            time.sleep(RECONNECT_DELAY_SECONDS)

    def _fetch_items(self, result_token):
        """Retrieves item metadata and whisper token from GGG API.

        result_token is the short-lived signed token from the live-search push
        message, used in place of a raw comma-separated ID list.
        """
        fetch_url = (
            f"https://www.pathofexile.com/api/trade/fetch/{result_token}"
            f"?query={self.search_id}"
        )
        try:
            res = requests.get(fetch_url, headers=self._api_headers(), timeout=15)
        except Exception as e:
            self.console.status(f"fetch failed: {e!r}", "red")
            return

        if res.status_code != 200:
            self.console.status(
                f"fetch failed ({res.status_code}): {res.text[:120]}", "red"
            )
            return

        try:
            entries = res.json().get("result", [])
        except Exception as e:
            self.console.status(f"fetch returned non-JSON: {e!r}", "red")
            return

        for entry in entries:
            if not entry:
                continue
            listing = entry.get("listing", {})
            item = entry.get("item", {})

            name = item.get("name") or item.get("typeLine", "Unknown Item")
            price = listing.get("price") or {}
            price_str = (
                f"{price.get('amount', '')} {price.get('currency', '')}"
            ).strip()

            offer = self._add_offer(
                name,
                price_str or "Unpriced",
                listing.get("account", {}).get("name", "Unknown"),
                listing.get("hideout_token"),
            )
            self._print_offer(offer)
            if self.auto:
                self.whisper(offer)

    # -------------------------------------------------------------- Loops --

    def start(self):
        self.is_running = True
        threading.Thread(target=self._listen_websocket, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()

    def stop(self):
        self.is_running = False
        if self.current_ws is not None:
            try:
                self.current_ws.close()
            except Exception:
                pass

    def key_loop(self):
        """Reads keys on the main thread; offers keep printing meanwhile.

        Deliberately never blocks the socket thread: a whisper token dies in
        seconds, so waiting on a keypress must not stop the next offer from
        arriving and being printed.
        """
        with KeyReader() as keys:
            if not keys.available:
                # No keyboard on stdin (systemd unit, nohup, < /dev/null):
                # nothing can ever be typed, so just keep the socket alive.
                self.console.status(
                    "no keyboard on stdin — listening only (see --auto)", "yellow"
                )
                return self.idle()

            while self.is_running:
                try:
                    key = keys.read()
                except (KeyboardInterrupt, EOFError):
                    return
                except Exception as e:
                    self.console.status(
                        f"keyboard unavailable ({e!r}) — listening only", "yellow"
                    )
                    return self.idle()

                if key is None:
                    self.console.status(
                        "stdin closed — listening only", "yellow"
                    )
                    return self.idle()

                if key in ("q", "Q", "\x03", "\x04"):
                    return
                if key in ("\r", "\n", " "):
                    self._act(None)
                elif key in "123456789":
                    self._act(int(key))
                elif key in ("l", "L"):
                    self.list_offers()
                elif key in ("?", "h", "H"):
                    self.console.write(
                        "      Enter/space = newest  |  1-9 = slot  |  l = list  "
                        "|  q = quit",
                        "gray",
                    )

    def _act(self, slot):
        offer = self._take(slot)
        if offer is None:
            where = "offer" if slot is None else f"offer in slot [{slot}]"
            self.console.write(
                f"      (no live {where} — expired, or none arrived yet)", "gray"
            )
            return
        self.whisper(offer)

    def idle(self):
        while self.is_running:
            time.sleep(1)


# ------------------------------------------------------------------- Main --


def unknown_impersonate_target(profile):
    """True when the installed curl_cffi has never heard of `profile`.

    An older curl_cffi — the version pip settles on for Python 3.9 and below —
    predates the browser targets named in BROWSER_PROFILES. curl_cffi does not
    validate the name, so the only symptom would be a WebSocket that never
    connects, reconnecting every 2s forever. Returns False when the list can't
    be read, so a version that moves this API is never treated as an error.
    """
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        known = set(typing.get_args(BrowserTypeLiteral))
    except Exception:
        return False
    return bool(known) and profile not in known


def fetch_leagues():
    """Current PC league ids.

    The endpoint lists every league once per platform realm (pc/xbox/sony)
    with the same id, tripling the list. This tool only ever talks to
    www.pathofexile.com, so keep pc.
    """
    res = requests.get(LEAGUES_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
    res.raise_for_status()
    return [
        entry["id"]
        for entry in res.json().get("result", [])
        if entry.get("realm", "pc") == "pc"
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless PoE live search with keypress-triggered whispers.",
        epilog="Cookies are hardcoded at the top of this file.",
    )
    parser.add_argument(
        "search", nargs="?", default=SEARCH_ID,
        help="search ID or a full trade URL (default: SEARCH_ID in this file)",
    )
    parser.add_argument(
        "--league", default=LEAGUE,
        help=f"league name, ignored when a full URL is given (default: {LEAGUE!r})",
    )
    parser.add_argument(
        "--browser", default=COOKIE_BROWSER, choices=sorted(BROWSER_PROFILES),
        help="browser the hardcoded cookies came from (default: %(default)s)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="whisper every new offer immediately, without a keypress",
    )
    parser.add_argument(
        "--expiry", type=int, default=OFFER_EXPIRY_SECONDS, metavar="SECONDS",
        help="drop an offer's slot after this long (default: %(default)s)",
    )
    parser.add_argument(
        "--no-bell", action="store_true",
        help="don't ring the terminal bell on a new offer",
    )
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument(
        "--list-leagues", action="store_true",
        help="print current PC league names and exit",
    )
    args = parser.parse_args(argv)

    if args.list_leagues:
        try:
            for name in fetch_leagues():
                print(name)
        except Exception as e:
            print(f"Could not fetch leagues: {e!r}", file=sys.stderr)
            return 1
        return 0

    if not POESESSID:
        print(
            "POESESSID is empty — paste your cookies into the Cookies section "
            f"at the top of {os.path.basename(__file__)}.",
            file=sys.stderr,
        )
        return 2

    parsed = parse_trade_url(args.search) if args.search else None
    if parsed:
        league, search_id = parsed
    else:
        league, search_id = args.league, (args.search or "").strip()
    if not search_id:
        print(
            "No search given — pass a search ID or trade URL, or set SEARCH_ID "
            "at the top of this file.",
            file=sys.stderr,
        )
        return 2
    if not league:
        print(
            "No league given — pass --league, or use a full trade URL instead.",
            file=sys.stderr,
        )
        return 2

    console = Console(use_color=False if args.no_color else None, bell=not args.no_bell)
    app = LiveCli(
        league, search_id, console=console, auto=args.auto,
        expiry=args.expiry, browser=args.browser,
    )

    console.write(f"League: {league}   Search: {search_id}", bold=True)
    console.write(
        f"Cookies: {args.browser} profile"
        + ("" if CF_CLEARANCE else " (no cf_clearance — expect close code 1008)"),
        "gray",
    )
    if args.auto:
        console.write(
            "AUTO mode: every new offer is whispered on arrival. Mind the rate "
            "limits.",
            "yellow",
        )
    else:
        console.write(
            "Keys: Enter/space = whisper newest  |  1-9 = whisper slot  |  "
            "l = list  |  q = quit",
            "gray",
        )
    console.write(f"Offers leave the slot list after {args.expiry}s.", "gray")
    if unknown_impersonate_target(app.ws_impersonate):
        console.write(
            f"⚠ This curl_cffi doesn't know the {app.ws_impersonate!r} TLS "
            "profile — the socket will not connect. Upgrade it: "
            "python3 -m pip install -U curl_cffi",
            "red",
        )
    console.write()

    app.start()
    try:
        if args.auto:
            app.idle()
        else:
            app.key_loop()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    console.write("Stopped.", "gray")
    return 0


if __name__ == "__main__":
    sys.exit(main())
