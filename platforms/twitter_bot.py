"""Twitter bot — scans tweets, posts, replies, likes, follows using Twikit."""

import asyncio
import concurrent.futures
import os
import random
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional

from twikit import Client as TwikitClient

from platforms.base_platform import BasePlatform
from core.database import Database
from core.content_gen import ContentGenerator

logger = logging.getLogger(__name__)


import threading

# Persistent event loop for Twikit — avoids "bound to a different event loop" crashes.
# asyncio.run() creates+closes a new loop each time, but Twikit's internal asyncio
# objects (locks, events) stay bound to the first loop. Using a single persistent loop
# keeps everything on the same loop across all calls.
_twitter_loop: Optional[asyncio.AbstractEventLoop] = None
_twitter_loop_lock = threading.Lock()
_twitter_loop_thread: Optional[threading.Thread] = None
_twitter_loop_generation: int = 0  # Incremented on loop recreation


def _get_twitter_loop() -> asyncio.AbstractEventLoop:
    """Get or create the persistent event loop for Twitter operations."""
    global _twitter_loop, _twitter_loop_thread, _twitter_loop_generation
    with _twitter_loop_lock:
        if _twitter_loop is None or _twitter_loop.is_closed():
            _twitter_loop = asyncio.new_event_loop()
            _twitter_loop_generation += 1

            def _run_loop(loop):
                asyncio.set_event_loop(loop)
                loop.run_forever()

            _twitter_loop_thread = threading.Thread(
                target=_run_loop, args=(_twitter_loop,), daemon=True
            )
            _twitter_loop_thread.start()
    return _twitter_loop


def _get_loop_generation() -> int:
    """Get current loop generation (for stale client detection)."""
    return _twitter_loop_generation


def _run_async_safe(coro):
    """Run an async coroutine safely from any context.

    Uses a persistent event loop to avoid Twikit's internal asyncio objects
    being bound to a closed/different loop (the #1 cause of Twitter failures).
    """
    loop = _get_twitter_loop()
    if loop.is_closed():
        # Loop was closed externally — force recreation
        global _twitter_loop
        _twitter_loop = None
        loop = _get_twitter_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=120)


class TwitterBot(BasePlatform):
    """Twitter scanner + poster using Twikit (async).

    Twikit is async-only. This class provides:
    - Async methods for use in event loops
    - Sync wrappers (via asyncio.run) for CLI usage

    Cookie management:
    - First login saves cookies to data/cookies/<account>.json
    - Subsequent runs load cookies, skipping login
    """

    def __init__(
        self,
        db: Database,
        content_gen: ContentGenerator,
        account_config: Dict,
        proxy: Optional[str] = None,
    ):
        super().__init__(db, content_gen, account_config)
        self.account_config = account_config
        # Client created lazily in authenticate() so asyncio objects
        # bind to the persistent Twitter event loop, not the main thread's loop.
        self.client: Optional[TwikitClient] = None
        self.cookies_file = account_config.get(
            "cookies_file", "data/cookies/default.json"
        )
        self._username = account_config.get("username", "unknown")
        self._authenticated = False
        self._loop_gen = 0  # Track which loop generation client was created on
        self._keyword_failures: Dict[str, int] = {}  # keyword -> consecutive 404 count
        self._proxy = proxy  # HTTP proxy for Cloudflare bypass

        # Ensure cookies directory exists
        os.makedirs(os.path.dirname(self.cookies_file), exist_ok=True)

    async def authenticate(self):
        """Login or load cookies.

        Supports multiple cookie file formats:
        1. Twikit native: {"name": "value", ...} dict
        2. Browser list: [{"name": "x", "value": "y", "domain": ".x.com"}, ...]
        3. Twikit login: auto-saves cookies after username/password login
        """
        # Detect event loop recreation — old client's asyncio objects are stale
        current_gen = _get_loop_generation()
        if self._authenticated and self._loop_gen == current_gen:
            return
        if self._loop_gen != current_gen and self._loop_gen > 0:
            logger.warning(
                f"Event loop changed (gen {self._loop_gen}→{current_gen}), "
                f"recreating Twitter client for @{self._username}"
            )
            self.client = None
            self._authenticated = False
        self._loop_gen = current_gen

        # Lazy-init: create TwikitClient on the persistent event loop
        # so its internal asyncio objects bind to the correct loop.
        if self.client is None:
            if self._proxy:
                self.client = TwikitClient("en-US", proxy=self._proxy)
                logger.info(f"Twitter client @{self._username} using proxy")
            else:
                self.client = TwikitClient("en-US")

        if os.path.exists(self.cookies_file):
            import json

            # Read raw cookie data to detect format
            try:
                with open(self.cookies_file) as f:
                    raw = json.load(f)
            except Exception as e:
                logger.warning(f"Cannot read cookie file: {e}")
                raw = None

            if raw is not None:
                # Format 1: List of {name, value, domain, path} (browser/paste format)
                if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "name" in raw[0]:
                    # Convert to Twikit's expected {name: value} dict format
                    twikit_dict = {c["name"]: c["value"] for c in raw}
                    tmp_path = self.cookies_file + ".twikit"
                    try:
                        with open(tmp_path, "w") as f:
                            json.dump(twikit_dict, f)
                        self.client.load_cookies(tmp_path)
                        os.remove(tmp_path)
                        self._authenticated = True
                        logger.info(f"Loaded Twitter cookies (list format, {len(twikit_dict)} cookies)")
                        return
                    except Exception as e:
                        logger.warning(f"List cookie load failed: {e}")
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

                # Format 2: Simple {name: value} dict (Twikit native / cookie_manager format)
                elif isinstance(raw, dict):
                    tmp_path = self.cookies_file + ".twikit"
                    try:
                        # Twikit load_cookies reads from file, ensure correct format
                        with open(tmp_path, "w") as f:
                            json.dump(raw, f)
                        self.client.load_cookies(tmp_path)
                        os.remove(tmp_path)
                        self._authenticated = True
                        key_cookies = [k for k in ["auth_token", "ct0", "twid"] if k in raw]
                        logger.info(
                            f"Loaded Twitter cookies ({len(raw)} cookies, "
                            f"keys: {', '.join(key_cookies)})"
                        )
                        return
                    except Exception as e:
                        logger.warning(f"Dict cookie load failed: {e}")
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

                        # Last resort: set cookies manually on httpx client
                        try:
                            for name, value in raw.items():
                                self.client.http.client.cookies.set(
                                    name, value, domain=".x.com"
                                )
                            self._authenticated = True
                            logger.info("Loaded Twitter cookies (manual httpx set)")
                            return
                        except Exception as e2:
                            logger.warning(f"Manual cookie set failed: {e2}")

            # Try Twikit's native load as final file attempt
            try:
                self.client.load_cookies(self.cookies_file)
                self._authenticated = True
                logger.info(f"Loaded Twitter cookies via twikit native")
                return
            except Exception as e:
                logger.warning(f"Twikit native load failed: {e}")

        # No cookies file or loading failed — try password login
        try:
            logger.info("Attempting Twitter login with credentials...")
            await self.client.login(
                auth_info_1=self.account_config["username"],
                auth_info_2=self.account_config.get("email", ""),
                password=self.account_config["password"],
            )
            # Save cookies in Twikit format for next time
            try:
                self.client.save_cookies(self.cookies_file)
                logger.info(f"Cookies saved to {self.cookies_file}")
            except Exception:
                pass
            self._authenticated = True
            logger.info("Twitter login successful")
        except Exception as e:
            logger.error(
                f"Twitter login failed: {e}\n"
                f"  To authenticate, run:\n"
                f"    python miloagent.py login twitter\n"
                f"    python miloagent.py paste-cookies twitter"
            )
            raise

    # ── Async methods ────────────────────────────────────────────────

    async def scan_async(self, project: Dict) -> List[Dict]:
        """Scan Twitter for relevant tweets by keywords."""
        await self.authenticate()

        opportunities = []
        twitter_config = project.get("twitter", {})
        keywords = twitter_config.get("keywords", [])
        project_name = project.get("project", {}).get("name", "unknown")
        seen_ids = set()

        for keyword in keywords:
            # Skip keywords that have failed 3+ times in a row (404 etc)
            if self._keyword_failures.get(keyword, 0) >= 3:
                logger.debug(f"Skipping keyword '{keyword}' (failed {self._keyword_failures[keyword]}x)")
                continue

            try:
                tweets = await self.client.search_tweet(
                    keyword, product="Latest"
                )
                if not tweets:
                    continue
                # Reset failure counter on success
                self._keyword_failures[keyword] = 0

                for tweet in tweets:
                    tweet_id = str(tweet.id)
                    if tweet_id in seen_ids:
                        continue
                    seen_ids.add(tweet_id)

                    if self._already_acted(tweet_id):
                        continue

                    tweet_text = tweet.text if hasattr(tweet, "text") else str(tweet)
                    user_name = (
                        tweet.user.screen_name
                        if hasattr(tweet, "user") and tweet.user
                        else "unknown"
                    )

                    # Extract engagement metrics
                    favorite_count = getattr(tweet, "favorite_count", 0) or 0
                    retweet_count = getattr(tweet, "retweet_count", 0) or 0
                    reply_count = getattr(tweet, "reply_count", 0) or 0
                    followers = (
                        getattr(tweet.user, "followers_count", 0)
                        if hasattr(tweet, "user") and tweet.user
                        else 0
                    ) or 0

                    opp = {
                        "platform": "twitter",
                        "target_id": tweet_id,
                        "text": tweet_text,
                        "user": user_name,
                        "keyword": keyword,
                        "favorite_count": favorite_count,
                        "retweet_count": retweet_count,
                        "reply_count": reply_count,
                        "followers": followers,
                    }

                    # Score the opportunity
                    opp["relevance_score"] = self._score_opportunity(
                        opp, project
                    )

                    opportunities.append(opp)

                    self.db.log_opportunity(
                        platform="twitter",
                        target_id=tweet_id,
                        title=tweet_text[:100],
                        subreddit_or_query=keyword,
                        score=opp["relevance_score"],
                        project=project_name,
                        metadata={
                            "keyword": keyword,
                            "user": user_name,
                            "text": tweet_text[:500],
                            "favorites": favorite_count,
                            "retweets": retweet_count,
                            "reply_count": reply_count,
                            "followers": followers,
                        },
                    )

            except Exception as e:
                err_str = str(e)
                logger.error(f"Twitter scan error for '{keyword}': {err_str}")
                # Track persistent failures (404s) to skip them next cycle
                if "404" in err_str or "not found" in err_str.lower():
                    self._keyword_failures[keyword] = self._keyword_failures.get(keyword, 0) + 1

            await asyncio.sleep(random.uniform(3, 8))

        opportunities.sort(
            key=lambda x: x.get("relevance_score", 0), reverse=True
        )
        logger.info(
            f"Twitter scan for {project_name}: "
            f"found {len(opportunities)} opportunities"
        )
        return opportunities

    # Negation words that invert keyword relevance
    _NEGATION_WORDS = (
        "don't", "dont", "not", "avoid", "hate", "stop using",
        "worst", "terrible", "useless", "overrated", "scam",
    )

    def _score_opportunity(self, opp: Dict, project: Dict) -> float:
        """Score a Twitter opportunity 0-10."""
        score = 0.0
        text_lower = opp.get("text", "").lower()
        twitter_config = project.get("twitter", {})
        keywords = twitter_config.get("keywords", [])

        # Keyword matches (0-3) with negation awareness
        kw_hits = 0
        negated_hits = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in text_lower:
                is_negated = any(
                    f"{neg} {kw_lower}" in text_lower
                    for neg in self._NEGATION_WORDS
                )
                if is_negated:
                    negated_hits += 1
                else:
                    kw_hits += 1
        score += min(kw_hits * 1.0, 3.0)
        score -= min(negated_hits * 1.0, 2.0)

        # Engagement signals (0-2.5)
        favs = opp.get("favorite_count", 0)
        rts = opp.get("retweet_count", 0)
        replies = opp.get("reply_count", 0)
        engagement = favs + rts * 2 + replies * 3
        if engagement >= 100:
            score += 2.5
        elif engagement >= 50:
            score += 2.0
        elif engagement >= 20:
            score += 1.5
        elif engagement >= 5:
            score += 1.0
        elif engagement >= 1:
            score += 0.5

        # Author reach (0-1.5) — sweet spot is 5k-50k followers
        followers = opp.get("followers", 0)
        if 5000 <= followers <= 50000:
            score += 1.5  # Best ROI: visible but not too crowded
        elif followers >= 50000:
            score += 0.8  # Huge reach but harder to stand out
        elif followers >= 1000:
            score += 1.2
        elif followers >= 100:
            score += 0.5

        # Intent signals (0-1.5) — questions, help requests
        question_signals = ["?", "how do", "how to", "what is", "which",
                           "anyone know", "recommend", "looking for",
                           "suggest", "alternative", "best way"]
        if any(sig in text_lower for sig in question_signals):
            score += 1.0

        help_signals = ["struggling", "stuck", "doesn't work", "help",
                       "need a tool", "what tool", "what app"]
        if any(sig in text_lower for sig in help_signals):
            score += 0.5

        # Low competition bonus (few replies = easier to stand out)
        if replies <= 2:
            score += 1.0
        elif replies <= 5:
            score += 0.5

        return max(min(score, 10.0), 0.0)

    async def post_tweet_async(
        self, text: str, project_name: str = "unknown"
    ) -> bool:
        """Post a new tweet."""
        if getattr(self, "_write_disabled", False):
            logger.debug("Twitter write disabled (code 226), skipping tweet")
            return False

        await self.authenticate()
        try:
            result = await self.client.create_tweet(text=text)
            tweet_id = str(result.id) if hasattr(result, "id") else "unknown"
            self.db.log_action(
                platform="twitter",
                action_type="tweet",
                account=self._username,
                project=project_name,
                target_id=tweet_id,
                content=text,
            )
            logger.info(f"Tweet posted: {text[:50]}...")
            return True
        except Exception as e:
            err_str = str(e)
            if "226" in err_str:
                if not getattr(self, "_226_warned", False):
                    logger.warning(
                        f"Twitter code 226 for @{self._username}: automated detection. "
                        f"Writing disabled. Use a residential proxy."
                    )
                    self._226_warned = True
                self._write_disabled = True
                return False
            logger.error(f"Failed to post tweet: {e}")
            self.db.log_action(
                platform="twitter",
                action_type="tweet",
                account=self._username,
                project=project_name,
                target_id="failed",
                content=text,
                success=False,
                error_message=str(e),
            )
            return False

    async def reply_async(
        self,
        tweet_id: str,
        text: str,
        project_name: str = "unknown",
    ) -> bool:
        """Reply to a tweet."""
        # Skip if writing is disabled (code 226 detected)
        if getattr(self, "_write_disabled", False):
            logger.debug("Twitter write disabled (code 226), skipping reply")
            return False

        await self.authenticate()
        try:
            await self.client.create_tweet(
                text=text, reply_to=tweet_id
            )
            self.db.log_action(
                platform="twitter",
                action_type="reply",
                account=self._username,
                project=project_name,
                target_id=tweet_id,
                content=text,
            )
            logger.info(f"Replied to tweet {tweet_id}: {text[:50]}...")
            return True
        except Exception as e:
            err_str = str(e)
            if "226" in err_str:
                if not getattr(self, "_226_warned", False):
                    logger.warning(
                        f"Twitter code 226 for @{self._username}: automated detection. "
                        f"Writing disabled for this session. "
                        f"Fix: set http.twitter_proxy to a residential proxy in settings.yaml, "
                        f"then re-login from that proxy IP."
                    )
                    self._226_warned = True
                self._write_disabled = True
                return False
            logger.error(f"Failed to reply: {e}")
            self.db.log_action(
                platform="twitter",
                action_type="reply",
                account=self._username,
                project=project_name,
                target_id=tweet_id,
                content=text,
                success=False,
                error_message=str(e),
            )
            return False

    async def like_async(self, tweet_id: str) -> bool:
        """Like a tweet."""
        await self.authenticate()
        try:
            await self.client.favorite_tweet(tweet_id)
            self.db.log_action(
                platform="twitter",
                action_type="like",
                account=self._username,
                project="engagement",
                target_id=tweet_id,
                content="",
            )
            logger.debug(f"Liked tweet {tweet_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to like tweet {tweet_id}: {e}")
            return False

    async def retweet_async(self, tweet_id: str) -> bool:
        """Retweet a tweet."""
        await self.authenticate()
        try:
            await self.client.retweet(tweet_id)
            self.db.log_action(
                platform="twitter",
                action_type="retweet",
                account=self._username,
                project="engagement",
                target_id=tweet_id,
                content="",
            )
            logger.debug(f"Retweeted {tweet_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to retweet {tweet_id}: {e}")
            return False

    async def follow_async(self, user_id: str) -> bool:
        """Follow a user by ID."""
        await self.authenticate()
        try:
            await self.client.follow_user(user_id)
            self.db.log_action(
                platform="twitter",
                action_type="follow",
                account=self._username,
                project="engagement",
                target_id=user_id,
                content="",
            )
            logger.debug(f"Followed user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to follow {user_id}: {e}")
            return False

    async def bookmark_async(self, tweet_id: str) -> bool:
        """Bookmark a tweet."""
        await self.authenticate()
        try:
            await self.client.bookmark_tweet(tweet_id)
            logger.debug(f"Bookmarked tweet {tweet_id}")
            return True
        except Exception as e:
            logger.debug(f"Bookmark failed for {tweet_id}: {e}")
            return False

    async def quote_tweet_async(
        self, tweet_id: str, text: str, project_name: str = "unknown"
    ) -> bool:
        """Quote-retweet with comment."""
        if getattr(self, "_write_disabled", False):
            logger.debug("Twitter write disabled (code 226), skipping quote tweet")
            return False

        await self.authenticate()
        try:
            # Twikit quote tweet via create_tweet with attachment_url
            await self.client.create_tweet(
                text=text,
                attachment_url=f"https://x.com/i/status/{tweet_id}",
            )
            self.db.log_action(
                platform="twitter",
                action_type="quote_tweet",
                account=self._username,
                project=project_name,
                target_id=tweet_id,
                content=text,
            )
            logger.info(f"Quote-tweeted {tweet_id}: {text[:50]}...")
            return True
        except Exception as e:
            logger.error(f"Quote tweet failed: {e}")
            return False

    async def warm_up_async(self, project: Dict) -> Dict:
        """Warm up the account: like tweets, follow relevant users.

        Makes the account look natural and engaged before posting.
        """
        await self.authenticate()
        stats = {"liked": 0, "followed": 0, "bookmarked": 0, "retweeted": 0}

        twitter_config = project.get("twitter", {})
        keywords = twitter_config.get("keywords", [])

        # Like and engage with tweets in our niche
        kws_to_scan = random.sample(keywords, min(3, len(keywords)))
        for keyword in kws_to_scan:
            try:
                tweets = await self.client.search_tweet(keyword, product="Top")
                if not tweets:
                    continue

                # Like 2-4 tweets per keyword
                to_like = list(tweets)[:random.randint(2, 4)]
                for tweet in to_like:
                    tweet_id = str(tweet.id)
                    try:
                        if await self.like_async(tweet_id):
                            stats["liked"] += 1
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(2.0, 5.0))

                    # Occasionally bookmark (20% chance)
                    if random.random() < 0.2:
                        try:
                            if await self.bookmark_async(tweet_id):
                                stats["bookmarked"] += 1
                        except Exception:
                            pass

                    # Follow user if they have good engagement (30% chance)
                    if random.random() < 0.3 and hasattr(tweet, "user") and tweet.user:
                        user_id = str(tweet.user.id)
                        try:
                            if await self.follow_async(user_id):
                                stats["followed"] += 1
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(1.0, 3.0))

                    # Retweet occasionally (15% chance)
                    if random.random() < 0.15:
                        try:
                            if await self.retweet_async(tweet_id):
                                stats["retweeted"] += 1
                        except Exception:
                            pass

            except Exception as e:
                logger.debug(f"Warm-up scan error for '{keyword}': {e}")

            await asyncio.sleep(random.uniform(5.0, 10.0))

        logger.info(
            f"Twitter warm-up: liked={stats['liked']}, "
            f"followed={stats['followed']}, "
            f"bookmarked={stats['bookmarked']}, "
            f"retweeted={stats['retweeted']}"
        )
        return stats

    def warm_up(self, project: Dict) -> Dict:
        """Sync wrapper for warm_up_async."""
        return self._retry_on_loop_error(
            _run_async_safe, self.warm_up_async(project)
        )

    def _human_reading_delay(self, tweet_text: str, reply_text: str) -> float:
        """Calculate human-like delay for reading a tweet and typing a reply."""
        tweet_words = len(tweet_text.split())
        reply_words = len(reply_text.split())

        read_time = (tweet_words / 250) * 60 * 0.6
        think_time = random.uniform(3, 15)
        type_time = (reply_words / 50) * 60 * random.uniform(0.7, 1.3)

        base_delay = read_time + think_time + type_time
        delay = base_delay * random.uniform(0.7, 1.3)
        return max(3.0, min(delay, 60.0))

    def _validate_and_retry(
        self, content: str, project: Dict, platform: str = "twitter"
    ) -> Optional[str]:
        """Validate content and retry once if invalid."""
        try:
            from core.content_validator import ContentValidator
            validator = ContentValidator()

            is_valid, score, issues = validator.validate(content, project, platform)
            if is_valid:
                logger.debug(f"Twitter content validated: score={score:.2f}")
                return content

            logger.info(f"Twitter validation failed (score={score:.2f}): {issues}")
            if score >= 0.4:
                return content
            return None
        except Exception as e:
            logger.debug(f"Validation error (continuing anyway): {e}")
            return content

    async def _act_async(self, opportunity: Dict, project: Dict) -> bool:
        """Generate reply and post it."""
        await self.authenticate()
        project_name = project.get("project", {}).get("name", "unknown")

        try:
            is_promo = self.content_gen._should_be_promotional()
            # Pass engagement metadata for richer prompt context
            tweet_meta = {
                "followers": opportunity.get("followers", 0),
                "favorite_count": opportunity.get("favorite_count", 0),
                "retweet_count": opportunity.get("retweet_count", 0),
                "reply_count": opportunity.get("reply_count", 0),
            }
            reply_text = self.content_gen.generate_twitter_reply(
                tweet_text=opportunity.get("text", ""),
                tweet_author=opportunity.get("user", "unknown"),
                project=project,
                persona=self.account_config.get("persona", "tech_enthusiast"),
                is_promotional=is_promo,
                tweet_meta=tweet_meta,
            )

            # Validate content before posting
            reply_text = self._validate_and_retry(reply_text, project)
            if not reply_text:
                logger.warning("Twitter content validation failed, skipping")
                return False

            # Human-like delay based on reading time
            delay = self._human_reading_delay(
                opportunity.get("text", ""), reply_text
            )
            logger.debug(f"Twitter reading delay: {delay:.1f}s")
            await asyncio.sleep(delay)

            success = await self.reply_async(
                opportunity["target_id"], reply_text, project_name
            )

            if success:
                self.db.update_opportunity_status(
                    opportunity["target_id"], "acted"
                )

            return success

        except Exception as e:
            logger.error(f"Failed to act on Twitter opportunity: {e}")
            return False

    # ── Sync wrappers (safe for both CLI and scheduler threads) ─────

    def _retry_on_loop_error(self, fn, *args):
        """Call fn(*args), retrying once on event-loop mismatch after resetting client."""
        try:
            return fn(*args)
        except RuntimeError as e:
            if "bound to a different event loop" in str(e):
                logger.warning(f"Event loop mismatch for @{self._username}, resetting client")
                self.client = None
                self._authenticated = False
                self._loop_gen = 0  # Force re-detection
                return fn(*args)
            raise

    def scan(self, project: Dict) -> List[Dict]:
        """Sync wrapper for scan_async."""
        return self._retry_on_loop_error(
            _run_async_safe, self.scan_async(project)
        )

    def act(self, opportunity: Dict, project: Dict) -> bool:
        """Sync wrapper for _act_async."""
        return self._retry_on_loop_error(
            _run_async_safe, self._act_async(opportunity, project)
        )

    def test_connection(self) -> bool:
        """Verify Twitter credentials work."""
        return self._retry_on_loop_error(
            _run_async_safe, self._test_async()
        )

    async def _test_async(self) -> bool:
        """Test authentication."""
        try:
            await self.authenticate()
            logger.info(f"Twitter connected as @{self._username}")
            return True
        except Exception as e:
            logger.error(f"Twitter connection failed: {e}")
            return False

    # ── DMs & Messaging (Phase 6) ────────────────────────────────────

    async def send_dm_async(self, user_id: str, text: str) -> bool:
        """Send a Twitter DM to a user by their user ID."""
        try:
            await self.authenticate()
            import asyncio
            await asyncio.sleep(random.uniform(2, 5))

            if hasattr(self.client, "send_dm"):
                await self.client.send_dm(user_id, text)
                logger.info(f"Twitter DM sent to user {user_id}")
                return True
            elif hasattr(self.client, "create_dm"):
                await self.client.create_dm(user_id, text)
                logger.info(f"Twitter DM sent to user {user_id}")
                return True
            else:
                logger.warning("Twikit does not support DM sending")
                return False

        except Exception as e:
            logger.error(f"Twitter DM send failed: {e}")
            return False

    def send_dm(self, user_id: str, text: str) -> bool:
        """Sync wrapper for send_dm_async."""
        return _run_async_safe(self.send_dm_async(user_id, text))

    async def check_dms_async(self, limit: int = 20) -> list:
        """Check Twitter DMs for new messages."""
        try:
            await self.authenticate()

            messages = []
            if hasattr(self.client, "get_dm_history"):
                # Twikit DM inbox
                dms = await self.client.get_dm_history()
                for dm in (dms or [])[:limit]:
                    messages.append({
                        "id": getattr(dm, "id", ""),
                        "author": getattr(dm, "sender_id", ""),
                        "text": getattr(dm, "text", ""),
                        "body": getattr(dm, "text", ""),
                        "timestamp": getattr(dm, "time", ""),
                    })
            else:
                logger.debug("Twikit does not support DM reading")

            return messages

        except Exception as e:
            logger.debug(f"Twitter DM check failed: {e}")
            return []

    def check_dms(self, limit: int = 20) -> list:
        """Sync wrapper for check_dms_async."""
        return _run_async_safe(self.check_dms_async(limit))

    async def get_user_by_name_async(self, screen_name: str) -> Optional[Dict]:
        """Get a Twitter user's info by screen name."""
        try:
            await self.authenticate()
            user = await self.client.get_user_by_screen_name(screen_name)
            if user:
                return {
                    "id": getattr(user, "id", ""),
                    "name": getattr(user, "name", screen_name),
                    "screen_name": screen_name,
                    "bio": getattr(user, "description", ""),
                    "followers_count": getattr(user, "followers_count", 0),
                }
            return None
        except Exception as e:
            logger.debug(f"Failed to get Twitter user @{screen_name}: {e}")
            return None

    def get_user_by_name(self, screen_name: str) -> Optional[Dict]:
        """Sync wrapper for get_user_by_name_async."""
        return _run_async_safe(self.get_user_by_name_async(screen_name))
