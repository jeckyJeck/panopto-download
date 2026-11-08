"""
panopto_auth.py - get fresh Panopto cookies automatically, without copying files.

How it works:
  * A dedicated browser profile (separate from your normal Edge/Firefox) is kept in
    %LOCALAPPDATA%\\panopto_dl\\browser_profile.
  * Each run opens the lecture page in that browser in the background (headless).
    If the university SSO still remembers you, Panopto logs you in silently.
  * If not, a visible browser window opens and you log in once. The script
    notices the login by itself and continues - no files to copy.
  * Cookies are written to %LOCALAPPDATA%\\panopto_dl\\cookies.txt (outside the repo)
    for yt-dlp to use.

Standalone usage (just to log in / refresh):
    python panopto_auth.py "<lecture URL>"

Requirements (one-time):
    pip install -U playwright
    (Uses the Microsoft Edge that ships with Windows. If that fails:
     python -m playwright install chromium)
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "panopto_dl"
PROFILE_DIR = APP_DIR / "browser_profile"
STATE_FILE = APP_DIR / "session_cookies.json"   # session cookies saved between runs
COOKIE_FILE = APP_DIR / "cookies.txt"            # what yt-dlp reads

AUTH_COOKIE = ".ASPXAUTH"   # Panopto's login cookie


# ---------------------------------------------------------------- browser

def _launch(p, headless):
    """Open the dedicated persistent profile. Prefer Edge (already on Windows)."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=headless,
                  viewport={"width": 1100, "height": 800})
    try:
        return p.chromium.launch_persistent_context(channel="msedge", **kwargs)
    except Exception:
        # Fallback: Playwright's own Chromium (needs `playwright install chromium`)
        return p.chromium.launch_persistent_context(**kwargs)


def _restore_session_cookies(context):
    """
    Session cookies (like .ASPXAUTH) are dropped whenever a browser closes.
    We save them ourselves and put them back, so a still-valid login is reused.
    """
    if STATE_FILE.exists():
        try:
            context.add_cookies(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass  # stale/corrupt file - just log in again


def _logged_in(context, page, host):
    """True once we are back on the Panopto site (not a login page) with the auth cookie."""
    url = urlparse(page.url)
    on_panopto = url.netloc == host and "/Auth/" not in url.path
    has_auth = any(c["name"] == AUTH_COOKIE for c in context.cookies(f"https://{host}"))
    return on_panopto and has_auth


def _try_login(p, lecture_url, headless, timeout_s):
    """Open the lecture page and wait until logged in. Returns cookies or None."""
    host = urlparse(lecture_url).netloc
    context = _launch(p, headless)
    try:
        _restore_session_cookies(context)
        page = context.pages[0] if context.pages else context.new_page()
        # The lecture URL contains '&instance=...', so Panopto bounces
        # straight to the university login provider if needed.
        page.goto(lecture_url, wait_until="domcontentloaded")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _logged_in(context, page, host):
                # Give the page a moment so any last cookies get set
                page.wait_for_timeout(1500)
                return context.cookies()
            page.wait_for_timeout(1000)
        return None
    finally:
        context.close()


# ---------------------------------------------------------------- output

def _write_netscape(cookies, path):
    """Write cookies in the Netscape cookies.txt format that yt-dlp reads."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c["domain"]
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        # Session cookies have expires = -1; Netscape format uses 0 for them
        expires = int(c["expires"]) if c.get("expires", -1) > 0 else 0
        lines.append("\t".join([domain, include_sub, c.get("path", "/"), secure,
                                str(expires), c["name"], c["value"]]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_cookie_file(lecture_url):
    """
    Make sure we are logged in to Panopto and return the path of a fresh cookies.txt.
    Silent if the login is still remembered; opens a window only when needed.
    """
    with sync_playwright() as p:
        cookies = _try_login(p, lecture_url, headless=True, timeout_s=20)
        if cookies is None:
            print("\nPanopto login needed - a browser window is opening.")
            print("Log in there (answer 'yes' to 'stay signed in' if asked).")
            print("The window will close by itself once you're in.\n")
            cookies = _try_login(p, lecture_url, headless=False, timeout_s=300)
        if cookies is None:
            sys.exit("Login was not completed within 5 minutes.")

    # Keep the session cookies for the next run (persistent ones live in the profile)
    session_only = [c for c in cookies if c.get("expires", -1) <= 0]
    STATE_FILE.write_text(json.dumps(session_only))
    _write_netscape(cookies, COOKIE_FILE)
    return COOKIE_FILE


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Usage: python panopto_auth.py "<lecture URL>"')
    print(f"Cookies written to: {get_cookie_file(sys.argv[1])}")