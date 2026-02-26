"""Cookie Manager — Captures cookies from a real Chrome process.

Launches the real Google Chrome binary (not Playwright's Chromium)
as a normal process with a temp profile + remote debugging port.
Playwright only connects via CDP to READ cookies — zero automation
markers injected into the page.

Reddit/Twitter cannot distinguish this from a normal Chrome window.

Usage:
    python miloagent.py login reddit
    python miloagent.py login twitter
"""

import json
import logging
import os
import subprocess
import sys
import time
from typing import Dict, Optional

from core.environment import detect_environment, get_chrome_paths

logger = logging.getLogger(__name__)

# Chrome paths auto-detected for the current platform (macOS or Linux)
CHROME_PATHS = get_chrome_paths()

PLATFORMS = {
    "reddit": {
        "login_url": "https://www.reddit.com/login/",
        # reddit_session = real auth cookie (set by old.reddit.com)
        # token_v2 with sub != "loid" = new reddit auth
        "success_cookies": ["reddit_session"],
        "domain_filter": "reddit.com",
    },
    "twitter": {
        "login_url": "https://x.com/i/flow/login",
        "success_cookies": ["auth_token"],
        "domain_filter": "x.com",
    },
}


def _find_chrome() -> Optional[str]:
    """Find the real Chrome binary on the current platform."""
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    # Linux fallback: try 'which' for common names
    env = detect_environment()
    if env["is_linux"]:
        for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
            try:
                result = subprocess.run(
                    ["which", name], capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
    return None


class CookieManager:
    """Launches real Chrome, user logs in, cookies captured via CDP."""

    def login(
        self,
        platform: str,
        cookies_file: str,
        timeout: int = 120,
    ) -> Optional[Dict[str, str]]:
        """Open real Chrome for login, capture cookies when done.

        1. Launches Chrome binary with --remote-debugging-port (temp profile)
        2. User logs in normally (no automation markers on the page)
        3. Playwright connects via CDP to READ cookies only
        4. For Reddit: navigates to old.reddit.com to get reddit_session
        5. Saves cookies to disk
        """
        if platform not in PLATFORMS:
            logger.error(f"Unknown platform: {platform}")
            return None

        config = PLATFORMS[platform]
        chrome_path = _find_chrome()

        if not chrome_path:
            env = detect_environment()
            if env["is_headless"]:
                logger.error(
                    "Headless server detected — browser login not available.\n"
                    "  Use 'paste-cookies' or transfer cookies from a desktop machine."
                )
            else:
                logger.error(
                    "Google Chrome not found. Install Chrome or Chromium, "
                    "or use 'paste-cookies' to set cookies manually."
                )
            return None

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "Playwright not installed. Run:\n"
                "  pip install playwright && playwright install chromium"
            )
            return None

        os.makedirs(os.path.dirname(cookies_file), exist_ok=True)

        # Fresh temp profile per login — avoids inheriting a stale session
        import shutil
        tmp_profile = "/tmp/miloagent-chrome-profile"
        if os.path.exists(tmp_profile):
            shutil.rmtree(tmp_profile, ignore_errors=True)
        os.makedirs(tmp_profile, exist_ok=True)

        debug_port = 9222
        chrome_proc = None
        cookies = None

        # Kill any leftover Chrome on this debug port (works on macOS & Linux)
        try:
            env = detect_environment()
            if env["is_macos"]:
                os.system(f"lsof -ti:{debug_port} | xargs kill -9 2>/dev/null")
            elif env["is_linux"]:
                os.system(f"fuser -k {debug_port}/tcp 2>/dev/null")
            time.sleep(1)
        except Exception:
            pass

        try:
            # Step 1: Launch real Chrome as a normal process
            logger.info(f"Launching Chrome: {os.path.basename(chrome_path)}")
            chrome_proc = subprocess.Popen(
                [
                    chrome_path,
                    f"--remote-debugging-port={debug_port}",
                    f"--user-data-dir={tmp_profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--window-size=1280,800",
                    config["login_url"],
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            logger.info("Waiting for Chrome to start...")
            time.sleep(3)

            # Step 2: Connect Playwright via CDP (read-only)
            with sync_playwright() as pw:
                browser = None
                for attempt in range(5):
                    try:
                        browser = pw.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{debug_port}"
                        )
                        break
                    except Exception:
                        time.sleep(1)

                if not browser:
                    logger.error("Could not connect to Chrome debug port")
                    return None

                logger.info("Connected to Chrome. Waiting for login...")

                contexts = browser.contexts
                if not contexts:
                    logger.error("No browser contexts found")
                    return None

                context = contexts[0]

                # Step 3: Wait for user to actually log in
                # Detect by URL change (user leaves /login page)
                cookies = self._wait_for_login(
                    context, platform, config, timeout, chrome_proc
                )

                try:
                    browser.close()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Login error: {e}")
        finally:
            if chrome_proc and chrome_proc.poll() is None:
                try:
                    chrome_proc.terminate()
                    chrome_proc.wait(timeout=5)
                except Exception:
                    try:
                        chrome_proc.kill()
                    except Exception:
                        pass

        if cookies:
            self._save_cookies(cookies, cookies_file, platform)
            logger.info(f"Saved {len(cookies)} cookies to {cookies_file}")

        return cookies

    def _wait_for_login(
        self, context, platform: str, config: dict,
        timeout: int, chrome_proc,
    ) -> Optional[Dict[str, str]]:
        """Manual login: wait for user to press Enter, then capture cookies.

        The user logs in at their own pace and confirms in the terminal.
        If Reddit: also navigates to old.reddit.com to get reddit_session.
        """
        # ── Manual confirmation: user presses Enter when ready ──
        try:
            input(
                "\n    >>> Log in to the account in the Chrome window.\n"
                "    >>> Press ENTER here when you are logged in "
                "(or type 'q' to cancel): "
            )
        except (EOFError, KeyboardInterrupt):
            logger.info("Login cancelled by user")
            return None

        # Check if Chrome is still alive
        if chrome_proc.poll() is not None:
            logger.warning("Chrome was closed")
            return None

        try:
            pages = context.pages
            if not pages:
                logger.error("No browser pages found")
                return None

            # For Reddit: navigate to old.reddit.com to get reddit_session
            if platform == "reddit":
                logger.info("Navigating to old.reddit.com to capture session...")
                try:
                    pages[0].goto(
                        "https://old.reddit.com/",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    time.sleep(3)
                except Exception:
                    pass

            # For Twitter: give cookies a moment to settle
            if platform == "twitter":
                time.sleep(2)

            # Capture all cookies
            browser_cookies = context.cookies()
            cookie_dict = self._filter_cookies(
                browser_cookies, config["domain_filter"]
            )

            # Validate we got something useful
            if platform == "reddit":
                if "reddit_session" in cookie_dict:
                    logger.info("reddit_session cookie captured!")
                elif "token_v2" in cookie_dict:
                    if self._is_token_v2_authenticated(cookie_dict["token_v2"]):
                        logger.info("Authenticated token_v2 captured!")
                    else:
                        logger.warning(
                            "Got token_v2 but it looks logged-out. "
                            "Make sure you are logged in, then try again."
                        )
                        return None
                else:
                    logger.warning("No Reddit session cookies found. Are you logged in?")
                    return None

            elif platform == "twitter":
                has_auth = "auth_token" in cookie_dict
                has_ct0 = "ct0" in cookie_dict
                if has_auth:
                    logger.info(f"Twitter auth_token captured! ({len(cookie_dict)} cookies)")
                elif has_ct0:
                    logger.info("Got ct0 but no auth_token — cookies may be incomplete")
                else:
                    logger.warning("No Twitter auth cookies found. Are you logged in?")
                    return None

            return cookie_dict

        except Exception as e:
            logger.error(f"Cookie capture error: {e}")
            return None

    @staticmethod
    def _is_token_v2_authenticated(token: str) -> bool:
        """Check if a Reddit token_v2 JWT belongs to a logged-in user.

        Logged-out tokens have sub="loid".
        Logged-in tokens have sub="t2_xxxxx" (user ID).
        """
        try:
            import base64
            # JWT = header.payload.signature
            parts = token.split(".")
            if len(parts) < 2:
                return False
            # Decode payload (add padding)
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            sub = data.get("sub", "")
            # "loid" = logged out, "t2_xxx" = logged in user
            return sub.startswith("t2_")
        except Exception:
            return False

    @staticmethod
    def _filter_cookies(
        browser_cookies: list, domain_filter: str
    ) -> Dict[str, str]:
        """Filter cookies by domain and return {name: value} dict."""
        result = {}
        for cookie in browser_cookies:
            domain = cookie.get("domain", "")
            if domain_filter in domain:
                result[cookie["name"]] = cookie["value"]
        return result

    @staticmethod
    def _save_cookies(
        cookies: Dict[str, str], filepath: str, platform: str
    ):
        """Save cookies in the format expected by each platform.

        - Reddit: simple {name: value} dict (for requests.Session)
        - Twitter: simple {name: value} dict (Twikit's load_cookies format)
        """
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # Both platforms use simple {name: value} dict format
        with open(filepath, "w") as f:
            json.dump(cookies, f, indent=2)

        if platform == "twitter":
            # Log key Twitter cookies for debugging
            key_cookies = ["auth_token", "ct0", "twid", "kdt"]
            found = [k for k in key_cookies if k in cookies]
            logger.info(f"Twitter key cookies saved: {', '.join(found)}")

    @staticmethod
    def is_available() -> bool:
        """Check if Playwright + Chrome are available."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False
        return _find_chrome() is not None

    @staticmethod
    def install():
        """Install Playwright (Chrome must be installed separately)."""
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "playwright"],
            stdout=subprocess.DEVNULL,
        )
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
        )
