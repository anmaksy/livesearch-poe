"""Prints the pathofexile.com session cookies live_cli.py needs.

Run this on the machine whose browser is logged into pathofexile.com, then
paste the output into the Cookies block at the top of live_cli.py (on a VPS,
paste it there over SSH). Same cookie-reading path as live.py's "Import
Cookies" button, minus the GUI — no DevTools needed.

    python export_cookies.py                  # paste-ready block
    python export_cookies.py --browser brave   # force one browser
    python export_cookies.py --all             # what's readable where
    python export_cookies.py --json            # for scripting
    python export_cookies.py --mask            # check without exposing values

Only the assignments go to stdout; notes and warnings go to stderr, so
`python export_cookies.py > cookies.txt` gives a paste-clean file.

Note: only Firefox works reliably. Chromium 127+ (Brave, Chrome, Edge) ships
App-Bound Encryption, so their cookie files cannot be decrypted by anything
but the browser itself — see Known limitations in README.md.
"""

import argparse
import json
import sys
import time

# Same order live.py tries: Firefox first because it's the only browser
# without App-Bound Encryption, so it's the one that reliably decrypts.
BROWSERS = ("firefox", "brave", "edge", "chrome")

# POESESSID is the session itself; cf_clearance is what gets the WebSocket
# past Cloudflare; POETOKEN is optional and sent when present.
WANTED = ("POESESSID", "cf_clearance", "POETOKEN")
DOMAIN = "pathofexile.com"

# Which module-level constant in live_cli.py each cookie belongs to.
CONSTANTS = (
    ("POESESSID", "POESESSID"),
    ("cf_clearance", "CF_CLEARANCE"),
    ("POETOKEN", "POETOKEN"),
)

# ------------------------------------------------------------------ Output --

# A Windows console still defaults to a legacy code page (cp1252) with no
# emoji in it, and printing one to a stream like that raises
# UnicodeEncodeError. Anything the stream can't encode gets an ASCII stand-in
# instead. (live_cli.py carries its own copy of this, for the same reason.)
ASCII_FALLBACK = {"✅": "[ok]", "❌": "[x]", "⚠": "[!]", "…": "...", "—": "-"}


def _fallback_table(stream):
    """Maps just the characters `stream` cannot represent; usually empty."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    table = {}
    for char, replacement in ASCII_FALLBACK.items():
        try:
            char.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            table[ord(char)] = replacement
    return table


_TABLES = {}


def out(text="", stream=None):
    """print() that degrades unsupported glyphs instead of raising."""
    stream = sys.stdout if stream is None else stream
    table = _TABLES.setdefault(id(stream), _fallback_table(stream))
    print(text.translate(table) if table else text, file=stream)


def err(text=""):
    out(text, stream=sys.stderr)


# ------------------------------------------------------------------ Cookies --


def _domain_rank(domain: str) -> int:
    """Prefers the most specific host when a name is set on several domains.

    A browser can hold both a www.pathofexile.com and a .pathofexile.com copy
    of the same cookie; the one the trade site itself set is the one to export.
    """
    domain = (domain or "").lstrip(".")
    if domain == "www.pathofexile.com":
        return 2
    if domain == DOMAIN:
        return 1
    return 0


def read_browser(name):
    """Returns {cookie_name: (value, expires)} for one browser, or raises."""
    import browser_cookie3

    loader = getattr(browser_cookie3, name, None)
    if loader is None:
        # Older browser_cookie3 releases predate some of these.
        raise RuntimeError(f"browser_cookie3 has no {name} loader")

    best = {}
    for cookie in loader(domain_name=DOMAIN):
        rank = _domain_rank(cookie.domain)
        current = best.get(cookie.name)
        if current is None or rank >= current[0]:
            best[cookie.name] = (rank, cookie.value, cookie.expires)
    return {key: (value, expires) for key, (_, value, expires) in best.items()}


def scan(browsers):
    """Reads each browser in turn; returns [(name, cookies, error)]."""
    results = []
    for name in browsers:
        try:
            results.append((name, read_browser(name), None))
        except Exception as e:
            results.append((name, {}, str(e)))
    return results


def value_of(cookies, name):
    return cookies.get(name, ("", None))[0]


def mask(value):
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


FIFTY_YEARS = 50 * 365 * 86400


def _stamp(expires):
    """Local time for an expiry, or "" when the platform won't render it.

    Cookies routinely carry the year-9999 "never" sentinel, and Windows'
    localtime raises OSError on anything that far out.
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires))
    except (OSError, OverflowError, ValueError):
        return ""


def describe_expiry(expires):
    if not expires:
        return "session cookie (no expiry)"
    left = expires - time.time()
    stamp = _stamp(expires)
    suffix = f" ({stamp})" if stamp else ""
    if left <= 0:
        return f"EXPIRED{' at ' + stamp if stamp else ''}"
    if left > FIFTY_YEARS:
        return "no practical expiry"
    if left < 3600:
        return f"expires in {int(left // 60)} min{suffix}"
    if left < 86400:
        return f"expires in {left / 3600:.1f} h{suffix}"
    return f"expires in {int(left // 86400)} d{suffix}"


# ------------------------------------------------------------------- Report --


def print_block(browser, cookies, masked=False):
    """Prints the paste-ready assignments for live_cli.py's Cookies block."""
    show = mask if masked else (lambda v: v)
    out(f"# Read from {browser.capitalize()} on {time.strftime('%Y-%m-%d %H:%M')}")
    for cookie_name, const in CONSTANTS:
        out(f'{const} = "{show(value_of(cookies, cookie_name))}"')
    out(f'COOKIE_BROWSER = "{browser}"')


def print_json(browser, cookies, masked=False):
    show = mask if masked else (lambda v: v)
    payload = {
        const: show(value_of(cookies, cookie_name))
        for cookie_name, const in CONSTANTS
    }
    payload["COOKIE_BROWSER"] = browser
    out(json.dumps(payload, indent=2))


def print_survey(results):
    """--all: what each browser holds, values masked, failures included."""
    for name, cookies, error in results:
        if error:
            out(f"{name:8} ❌ {error[:100]}")
            continue
        found = [n for n in WANTED if value_of(cookies, n)]
        if not found:
            out(f"{name:8} — readable, but no PoE session cookies")
            continue
        out(f"{name:8} ✅ {', '.join(found)}")
        for cookie_name in found:
            value, expires = cookies[cookie_name]
            out(f"         {cookie_name}: {mask(value)} — "
                f"{describe_expiry(expires)}")


def print_notes(cookies):
    """Expiry and warnings — stderr, so stdout stays paste-clean."""
    err()
    for cookie_name in WANTED:
        value, expires = cookies.get(cookie_name, ("", None))
        if value:
            err(f"{cookie_name}: {describe_expiry(expires)}")

    if not value_of(cookies, "cf_clearance"):
        err(
            "\n⚠ No cf_clearance — the WebSocket will likely be closed with "
            "1008. Load pathofexile.com in this browser once, then re-run."
        )
    err(
        "\n⚠ These values grant full account access — treat them as passwords. "
        "cf_clearance is short-lived and tied to this browser and IP, so a VPS "
        "on a different IP may need it re-exported."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Print pathofexile.com cookies for live_cli.py.",
        epilog="Paste the output into the Cookies block at the top of live_cli.py.",
    )
    parser.add_argument(
        "--browser", choices=BROWSERS,
        help="read only this browser instead of trying each in turn",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="report every browser (masked), including ones that failed",
    )
    parser.add_argument("--json", action="store_true",
                        help="print a JSON object instead of a paste-ready block")
    parser.add_argument("--mask", action="store_true",
                        help="hide most of each value — for checking, not pasting")
    args = parser.parse_args(argv)

    try:
        import browser_cookie3  # noqa: F401  (imported here for the message)
    except ImportError:
        err("browser_cookie3 is not installed — pip install browser_cookie3")
        return 2

    results = scan((args.browser,) if args.browser else BROWSERS)

    if args.all:
        print_survey(results)
        return 0

    # Only a browser holding POESESSID is usable; a readable cookie store
    # without it just means you aren't logged in there.
    for name, cookies, _ in results:
        if value_of(cookies, "POESESSID"):
            browser, found = name, cookies
            break
    else:
        err("No pathofexile.com session found.")
        for name, _, error in results:
            reason = error[:100] if error else "no POESESSID (not logged in there?)"
            err(f"  {name}: {reason}")
        err(
            "\nLog into pathofexile.com in Firefox and retry. Brave/Chrome/Edge "
            "cookies cannot be decrypted (App-Bound Encryption) — for those, "
            "copy the values from DevTools instead."
        )
        return 1

    if args.json:
        print_json(browser, found, masked=args.mask)
    else:
        print_block(browser, found, masked=args.mask)
    print_notes(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
