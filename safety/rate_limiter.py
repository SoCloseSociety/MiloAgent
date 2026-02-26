"""Rate limiter with jitter to simulate human behavior."""

import time
import random
import logging
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Optional, Tuple

from core.database import Database

logger = logging.getLogger(__name__)

_MAX_TRACKED_ACCOUNTS = 100  # Max entries in last_action_time


class RateLimiter:
    """Enforces rate limits with human-like timing.

    Rules:
    - Max N actions per hour per account
    - Cooldown per subreddit/query
    - Random jitter on all delays
    - Active hours enforcement
    - Weekend activity reduction
    """

    # Cache TTL for hourly action counts (seconds)
    _ACTION_COUNT_CACHE_TTL = 60

    def __init__(self, db: Database, settings: Dict):
        self.db = db
        self.settings = settings
        self.last_action_time: OrderedDict[str, float] = OrderedDict()
        self._bot_settings = settings.get("bot", {})
        self._mode = self._bot_settings.get("mode", "background")

        # Action count cache: "account:platform" -> (count, fetched_at)
        self._action_count_cache: Dict[str, Tuple[int, float]] = {}

        # Get mode-specific scheduling config
        scheduling = settings.get("scheduling", {}).get(
            self._mode, settings.get("scheduling", {}).get("background", {})
        )
        self.max_actions_per_hour = scheduling.get(
            "max_actions_per_hour",
            self._bot_settings.get("max_actions_per_hour", 6),
        )
        self.action_interval_minutes = scheduling.get(
            "action_interval_minutes",
            self._bot_settings.get("action_interval_minutes", 10),
        )
        self.active_hours = scheduling.get(
            "active_hours",
            self._bot_settings.get("active_hours", [8, 23]),
        )

    def _get_cached_action_count(self, account: str, platform: str) -> int:
        """Get hourly action count with 60s cache to reduce DB queries."""
        cache_key = f"{account}:{platform}"
        now = time.time()

        cached = self._action_count_cache.get(cache_key)
        if cached and (now - cached[1]) < self._ACTION_COUNT_CACHE_TTL:
            return cached[0]

        # Cache miss or expired — query DB
        count = self.db.get_action_count(
            hours=1, account=account, platform=platform
        )
        self._action_count_cache[cache_key] = (count, now)
        return count

    def _invalidate_action_cache(self, account: str, platform: str):
        """Invalidate cache after recording an action."""
        cache_key = f"{account}:{platform}"
        self._action_count_cache.pop(cache_key, None)

    def can_act(
        self,
        account: str,
        platform: str,
        subreddit_or_query: Optional[str] = None,
        cooldown_minutes: int = 15,
    ) -> tuple:
        """Check if an action is allowed right now.

        Returns:
            (allowed: bool, reason: str)
        """
        # Check active hours
        if not self.is_active_hours():
            return False, "Outside active hours"

        # Check per-account hourly limit (cached)
        action_count = self._get_cached_action_count(account, platform)
        if action_count >= self.max_actions_per_hour:
            return False, f"Hourly limit reached ({action_count}/{self.max_actions_per_hour})"

        # Check minimum interval since last action
        last_key = f"{account}:{platform}"
        if last_key in self.last_action_time:
            elapsed = time.time() - self.last_action_time[last_key]
            min_interval = self.action_interval_minutes * 60
            if elapsed < min_interval:
                wait = int(min_interval - elapsed)
                return False, f"Too soon since last action (wait {wait}s)"

        # Check per-subreddit cooldown
        if subreddit_or_query:
            last_sub = self.db.get_last_action_in_subreddit(
                account, subreddit_or_query
            )
            if last_sub:
                elapsed_min = (
                    datetime.utcnow() - last_sub
                ).total_seconds() / 60
                if elapsed_min < cooldown_minutes:
                    return False, (
                        f"Subreddit cooldown ({int(elapsed_min)}/{cooldown_minutes}min)"
                    )

        return True, "OK"

    def wait_with_jitter(
        self,
        min_seconds: float = 5,
        max_seconds: float = 30,
    ):
        """Sleep for a random duration to simulate human timing."""
        delay = random.uniform(min_seconds, max_seconds)
        logger.debug(f"Human delay: {delay:.1f}s")
        time.sleep(delay)

    def record_action(self, account: str, platform: str):
        """Record that an action was taken (bounded LRU)."""
        key = f"{account}:{platform}"
        self.last_action_time[key] = time.time()
        self.last_action_time.move_to_end(key)
        # Evict oldest entries if over limit
        while len(self.last_action_time) > _MAX_TRACKED_ACCOUNTS:
            self.last_action_time.popitem(last=False)
        # Invalidate cached action count so next check is fresh
        self._invalidate_action_cache(account, platform)

    def is_active_hours(self) -> bool:
        """Check if current time is within configured active hours.

        Handles midnight-spanning ranges, e.g. [22, 4] means 10 PM to 4 AM.
        """
        hour = datetime.now().hour
        start, end = self.active_hours
        if start <= end:
            # Normal range: e.g. [8, 23]
            return start <= hour < end
        else:
            # Spans midnight: e.g. [22, 4] → active 22-23 + 0-3
            return hour >= start or hour < end

    def is_weekend(self) -> bool:
        """Check if today is a weekend."""
        return datetime.now().weekday() >= 5

    def get_weekend_factor(self) -> float:
        """Get activity reduction factor for weekends (0.5 = 50% less)."""
        return 0.5 if self.is_weekend() else 1.0

    def should_take_random_break(self, probability: float = 0.05) -> bool:
        """Random inactive period simulation (5% chance per check)."""
        return random.random() < probability

    def get_human_delay(self) -> float:
        """Get a human-like delay between actions (seconds)."""
        base = self.action_interval_minutes * 60
        jitter = random.uniform(-base * 0.3, base * 0.5)
        delay = max(30, base + jitter)

        # Weekend reduction
        if self.is_weekend():
            delay *= 1.5

        return delay
