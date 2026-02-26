"""Account rotation and health management."""

import os
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import yaml

from core.database import Database

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages account rotation and health tracking.

    - Rotates between multiple accounts per platform
    - Tracks account health status in database
    - Puts accounts on cooldown after errors
    - Selects least-recently-used healthy account
    """

    # Account statuses
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    WARNED = "warned"
    BANNED = "banned"

    def __init__(self, db: Database, config_dir: str = "config/"):
        self.db = db
        self.config_dir = config_dir
        self._lock = threading.RLock()  # Protects _cooldowns, _statuses, _last_used
        self._cooldowns: Dict[str, datetime] = {}  # "platform:account" -> expires_at
        self._statuses: Dict[str, str] = {}
        self._last_used: Dict[str, str] = {}  # platform -> last used username
        self._rotation_index: Dict[str, int] = {}  # platform -> index for true round-robin
        # Hot-reload support
        self._file_mtimes: Dict[str, float] = {}
        self._on_reload_callbacks: List[Callable] = []
        self._watching = False
        self._watcher_thread: Optional[threading.Thread] = None
        self._snapshot_mtimes()
        self._restore_state_from_db()

    def _restore_state_from_db(self):
        """Restore cooldown/health state from account_health table on startup."""
        try:
            rows = self.db.conn.execute(
                """SELECT platform, account, status, timestamp
                   FROM account_health
                   WHERE id IN (
                       SELECT MAX(id) FROM account_health
                       GROUP BY platform, account
                   )"""
            ).fetchall()
            for row in rows:
                key = f"{row['platform']}:{row['account']}"
                status = row["status"]
                if status == self.COOLDOWN:
                    # Restore cooldown with platform-appropriate duration
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                        # Use shorter cooldown for Telegram (usually 3-5min)
                        platform = row["platform"]
                        if platform == "telegram":
                            cooldown_min = 5
                        else:
                            cooldown_min = 15
                        expires = ts + timedelta(minutes=cooldown_min)
                        if expires > datetime.now():
                            self._cooldowns[key] = expires
                            self._statuses[key] = self.COOLDOWN
                        else:
                            self._statuses[key] = self.HEALTHY
                    except Exception:
                        self._statuses[key] = self.HEALTHY
                elif status == self.BANNED:
                    self._statuses[key] = self.BANNED
                elif status == self.WARNED:
                    self._statuses[key] = self.WARNED
                else:
                    self._statuses[key] = self.HEALTHY
            logger.debug(f"Restored account state: {len(rows)} entries")
        except Exception as e:
            logger.debug(f"Could not restore account state: {e}")

    # ── Hot-Reload Support ─────────────────────────────────────────

    def _snapshot_mtimes(self):
        """Record current mtimes of account YAML files."""
        for platform in ("reddit", "twitter", "telegram"):
            fname = (
                "telegram_user_accounts.yaml"
                if platform == "telegram"
                else f"{platform}_accounts.yaml"
            )
            path = os.path.join(self.config_dir, fname)
            try:
                self._file_mtimes[path] = os.path.getmtime(path)
            except FileNotFoundError:
                pass

    def reload(self):
        """Re-read account configs from disk and notify callbacks."""
        self._cleanup_expired()
        self._snapshot_mtimes()
        logger.info("Account configs reloaded from disk")
        for cb in self._on_reload_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Account reload callback error: {e}")

    def start_watching(self, interval: float = 10.0):
        """Start polling account YAML files for changes."""
        if self._watching:
            return
        self._watching = True
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), daemon=True
        )
        self._watcher_thread.start()
        logger.debug(f"Account file watcher started (interval={interval}s)")

    def stop_watching(self):
        """Stop the file watcher thread."""
        self._watching = False

    def _watch_loop(self, interval: float):
        """Poll account YAML files for mtime changes."""
        while self._watching:
            time.sleep(interval)
            try:
                for path, old_mtime in list(self._file_mtimes.items()):
                    try:
                        current = os.path.getmtime(path)
                        if current != old_mtime:
                            logger.info(f"Account config changed: {path}")
                            self.reload()
                            break
                    except FileNotFoundError:
                        pass
            except Exception as e:
                logger.error(f"Account watcher error: {e}")

    def on_reload(self, callback: Callable):
        """Register a callback for account config changes."""
        self._on_reload_callbacks.append(callback)

    # ── Account Loading ────────────────────────────────────────────

    def load_accounts(self, platform: str) -> List[Dict]:
        """Load accounts for a platform from config."""
        if platform == "reddit":
            path = f"{self.config_dir}/reddit_accounts.yaml"
        elif platform == "twitter":
            path = f"{self.config_dir}/twitter_accounts.yaml"
        elif platform == "telegram":
            path = f"{self.config_dir}/telegram_user_accounts.yaml"
        else:
            return []

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            accounts = data.get("accounts", [])
            result = []
            for a in accounts:
                if not a.get("enabled", True):
                    continue
                identifier = a.get("username") or a.get("phone", "")
                if identifier.startswith("YOUR_"):
                    continue
                # Normalize: ensure 'username' key is set for telegram accounts
                if platform == "telegram" and not a.get("username"):
                    a["username"] = a.get("phone", "unknown")
                result.append(a)
            return result
        except FileNotFoundError:
            logger.warning(f"Config file not found: {path}")
            return []

    def _cleanup_expired(self):
        """Remove expired cooldown entries to prevent unbounded dict growth."""
        now = datetime.now()
        expired = [k for k, v in self._cooldowns.items() if now >= v]
        for key in expired:
            del self._cooldowns[key]
            if self._statuses.get(key) == self.COOLDOWN:
                self._statuses[key] = self.HEALTHY

    def get_next_account(self, platform: str) -> Optional[Dict]:
        """Get the next available (healthy, not on cooldown) account.

        Uses round-robin rotation: never picks the same account twice in a row,
        then falls back to LRU (fewest actions in 4h) as tiebreaker.
        Thread-safe via self._lock.
        """
        accounts = self.load_accounts(platform)
        if not accounts:
            return None

        with self._lock:
            # Periodically clean expired cooldowns
            self._cleanup_expired()

            # Filter out cooldown/banned accounts
            available = []
            for acc in accounts:
                key = f"{platform}:{acc['username']}"
                status = self._statuses.get(key, self.HEALTHY)

                if status == self.BANNED:
                    continue

                if key in self._cooldowns:
                    if datetime.now() < self._cooldowns[key]:
                        continue
                    else:
                        del self._cooldowns[key]
                        self._statuses[key] = self.HEALTHY

                available.append(acc)

            if not available:
                logger.warning(f"No available {platform} accounts")
                return None

            # True round-robin: rotate through ALL available accounts fairly
            # Step 1: advance the rotation index
            idx = self._rotation_index.get(platform, -1) + 1
            if idx >= len(available):
                idx = 0
            self._rotation_index[platform] = idx

            # Step 2: pick account at current index
            best = available[idx]

            # Step 3: if this account has way more actions than others (4h),
            # prefer the least-used one instead (fairness correction)
            if len(available) > 1:
                counts = []
                for acc in available:
                    c = self.db.get_action_count(
                        hours=4, account=acc["username"], platform=platform
                    )
                    counts.append((c, acc))
                counts.sort(key=lambda x: x[0])
                min_count = counts[0][0]
                best_count = self.db.get_action_count(
                    hours=4, account=best["username"], platform=platform
                )
                # If selected account has 3+ more actions than the least-used,
                # switch to the least-used instead
                if best_count > min_count + 2:
                    best = counts[0][1]
                    # Update index to match the corrected pick
                    for i, acc in enumerate(available):
                        if acc["username"] == best["username"]:
                            self._rotation_index[platform] = i
                            break

            if best:
                self._last_used[platform] = best["username"]
                logger.debug(
                    f"Account rotation [{platform}]: picked {best['username']} "
                    f"(index {self._rotation_index.get(platform, 0)}/{len(available)})"
                )

        return best

    def mark_cooldown(
        self,
        platform: str,
        account: str,
        minutes: int = 30,
    ):
        """Put an account on cooldown."""
        key = f"{platform}:{account}"
        with self._lock:
            self._cooldowns[key] = datetime.now() + timedelta(minutes=minutes)
            self._statuses[key] = self.COOLDOWN
        self.db.update_account_health(
            platform, account, self.COOLDOWN,
            notes=f"Cooldown for {minutes}min",
        )
        logger.info(f"Account {account} on {platform}: cooldown {minutes}min")

    def mark_warned(self, platform: str, account: str, reason: str):
        """Mark account as warned (suspicious activity detected)."""
        key = f"{platform}:{account}"
        with self._lock:
            self._statuses[key] = self.WARNED
        self.db.update_account_health(
            platform, account, self.WARNED, notes=reason,
        )
        logger.warning(f"Account {account} on {platform}: warned — {reason}")

    def mark_banned(self, platform: str, account: str, reason: str):
        """Mark account as banned."""
        key = f"{platform}:{account}"
        with self._lock:
            self._statuses[key] = self.BANNED
        self.db.update_account_health(
            platform, account, self.BANNED, notes=reason,
        )
        logger.error(f"Account {account} on {platform}: BANNED — {reason}")

    def mark_healthy(self, platform: str, account: str):
        """Mark account as healthy."""
        key = f"{platform}:{account}"
        with self._lock:
            self._statuses[key] = self.HEALTHY
            if key in self._cooldowns:
                del self._cooldowns[key]
        self.db.update_account_health(platform, account, self.HEALTHY)

    def get_preferred_subreddits(
        self, account: str, platform: str, max_subs: int = 5
    ) -> List[str]:
        """Get focused subreddits for an account to build recognition.

        Returns subreddits where the account has most activity,
        enabling expertise concentration.
        """
        try:
            top = self.db.get_top_subreddits_for_account(
                account, platform, limit=max_subs
            )
            return [s["subreddit"] for s in top] if top else []
        except Exception:
            return []

    def get_all_health(self) -> List[Dict]:
        """Get health status for all configured accounts."""
        results = []
        for platform in ("reddit", "twitter", "telegram"):
            accounts = self.load_accounts(platform)
            for acc in accounts:
                username = acc.get("username") or acc.get("phone", "?")
                key = f"{platform}:{username}"
                status = self._statuses.get(key, self.HEALTHY)
                action_count_24h = self.db.get_action_count(
                    hours=24, account=username, platform=platform,
                )
                results.append({
                    "platform": platform,
                    "username": username,
                    "status": status,
                    "actions_24h": action_count_24h,
                    "cooldown_until": (
                        self._cooldowns[key].isoformat()
                        if key in self._cooldowns
                        else None
                    ),
                })
        return results

    # ── Account Management ────────────────────────────────────────────

    def add_account(
        self,
        platform: str,
        username: str,
        password: str,
        email: str = "",
        projects: Optional[List[str]] = None,
        persona: str = "helpful_casual",
    ) -> str:
        """Add a new account to the platform config YAML.

        Returns status message.
        """
        if platform == "reddit":
            path = f"{self.config_dir}/reddit_accounts.yaml"
        elif platform == "twitter":
            path = f"{self.config_dir}/twitter_accounts.yaml"
        else:
            return f"Unknown platform: {platform}"

        # Load existing config
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}

        accounts = data.get("accounts", [])

        # Check if username already exists
        for acc in accounts:
            if acc.get("username", "").lower() == username.lower():
                return f"Account @{username} already exists on {platform}"

        # Build account entry
        cookie_index = len(accounts) + 1
        cookie_file = f"data/cookies/{platform}_account{cookie_index}.json"

        if platform == "reddit":
            new_account = {
                "username": username,
                "password": password,
                "client_id": "",
                "client_secret": "",
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                ),
                "cookies_file": cookie_file,
                "enabled": True,
                "persona": persona,
                "assigned_projects": projects or ["my_project"],
                "cooldown_minutes": 15,
                "max_actions_per_hour": 4,
            }
        else:  # twitter
            new_account = {
                "username": username,
                "email": email,
                "password": password,
                "totp_secret": "",
                "cookies_file": cookie_file,
                "enabled": True,
                "persona": persona,
                "assigned_projects": projects or ["my_project"],
                "cooldown_minutes": 20,
                "max_actions_per_hour": 3,
            }

        accounts.append(new_account)
        data["accounts"] = accounts

        # Preserve other top-level keys
        if platform == "reddit" and "auth_mode" not in data:
            data["auth_mode"] = "web"
        if platform == "reddit" and "default_cooldown_per_subreddit_minutes" not in data:
            data["default_cooldown_per_subreddit_minutes"] = 60

        # Ensure cookies directory exists
        os.makedirs("data/cookies", exist_ok=True)

        # Write back
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Added {platform} account: @{username}")
        return f"Added @{username} to {platform}. Cookie file: {cookie_file}"

    def remove_account(self, platform: str, username: str) -> str:
        """Disable an account in the config (sets enabled: false)."""
        if platform == "reddit":
            path = f"{self.config_dir}/reddit_accounts.yaml"
        elif platform == "twitter":
            path = f"{self.config_dir}/twitter_accounts.yaml"
        else:
            return f"Unknown platform: {platform}"

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return "Config file not found"

        accounts = data.get("accounts", [])
        found = False
        for acc in accounts:
            if acc.get("username", "").lower() == username.lower():
                acc["enabled"] = False
                found = True
                break

        if not found:
            return f"Account @{username} not found on {platform}"

        data["accounts"] = accounts
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Disabled {platform} account: @{username}")
        return f"Disabled @{username} on {platform}"

    def list_all_accounts(self) -> List[Dict]:
        """List all accounts across platforms (including disabled ones)."""
        results = []
        platform_paths = {
            "reddit": f"{self.config_dir}/reddit_accounts.yaml",
            "twitter": f"{self.config_dir}/twitter_accounts.yaml",
            "telegram": f"{self.config_dir}/telegram_user_accounts.yaml",
        }

        for platform, path in platform_paths.items():
            try:
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                for acc in data.get("accounts", []):
                    username = acc.get("username") or acc.get("phone", "?")
                    session_or_cookies = (
                        acc.get("session_file", "")
                        or acc.get("cookies_file", "")
                    )
                    results.append({
                        "platform": platform,
                        "username": username,
                        "enabled": acc.get("enabled", True),
                        "persona": acc.get("persona", "?"),
                        "projects": acc.get("assigned_projects", []),
                        "cookies_file": session_or_cookies,
                        "has_cookies": os.path.exists(session_or_cookies) if session_or_cookies else False,
                    })
            except FileNotFoundError:
                pass

        return results
