"""Reddit bot using web session (cookies) — no API app required.

Uses Reddit's public JSON endpoints + session cookies for authenticated actions.
This is the fallback when the user doesn't have API credentials (PRAW).
"""

import json
import os
import time
import random
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

import requests

from platforms.base_platform import BasePlatform
from core.database import Database
from core.content_gen import ContentGenerator

logger = logging.getLogger(__name__)

# Reddit JSON endpoints (public, no API key needed)
REDDIT_BASE = "https://www.reddit.com"
REDDIT_OLD = "https://old.reddit.com"

# Rotate User-Agents to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]


def _random_ua() -> str:
    return random.choice(USER_AGENTS)


class RedditWebBot(BasePlatform):
    """Reddit scanner + commenter using web session cookies.

    Two modes:
    - READ (anonymous): Uses Reddit's .json endpoints to scan posts.
      No login needed. Works for all public subreddits.
    - WRITE (authenticated): Uses session cookies to post comments.
      Requires username/password login via old.reddit.com.
    """

    def __init__(
        self,
        db: Database,
        content_gen: ContentGenerator,
        account_config: Dict,
    ):
        super().__init__(db, content_gen, account_config)
        self.account_config = account_config
        self._username = account_config.get("username", "unknown")
        self._password = account_config.get("password", "")
        self._cookies_file = account_config.get(
            "cookies_file", f"data/cookies/reddit_{self._username}.json"
        )

        # Session for authenticated requests
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _random_ua()})
        self._authenticated = False
        self._modhash = ""

        # Circuit breaker: stop trying after repeated failures
        self._consecutive_failures = 0
        self._max_failures = 5

        # Track subscribed subreddits to avoid re-subscribing every cycle
        self._subscribed_subs: set = set()

        # Load cookies if they exist
        self._load_cookies()

    # ── Authentication ───────────────────────────────────────────────

    def _load_cookies(self):
        """Load saved session cookies."""
        if os.path.exists(self._cookies_file):
            try:
                with open(self._cookies_file) as f:
                    cookies = json.load(f)
                self.session.cookies.update(cookies)
                self._authenticated = True
                logger.debug(f"Loaded Reddit cookies from {self._cookies_file}")
            except Exception as e:
                logger.warning(f"Failed to load Reddit cookies: {e}")

    def _save_cookies(self):
        """Save session cookies to file."""
        os.makedirs(os.path.dirname(self._cookies_file), exist_ok=True)
        cookies = dict(self.session.cookies)
        with open(self._cookies_file, "w") as f:
            json.dump(cookies, f)
        logger.debug(f"Saved Reddit cookies to {self._cookies_file}")

    def _login(self) -> bool:
        """Attempt Reddit authentication.

        Reddit blocks all programmatic login (API returns 403).
        This method only succeeds if cookies already exist on disk
        (from 'login' or 'paste-cookies' CLI commands).

        Accepts either:
        - reddit_session cookie (from old.reddit.com)
        - token_v2 JWT with sub=t2_xxx (from new reddit, logged in)
        """
        # Reload cookies from disk (user may have just run 'login' command)
        self._load_cookies()
        if not self._authenticated:
            logger.error(
                "No Reddit cookies found. "
                "To authenticate, run one of:\n"
                "  python miloagent.py login reddit         (opens browser)\n"
                "  python miloagent.py paste-cookies reddit  (manual paste)"
            )
            return False

        # Check for reddit_session (old reddit auth)
        if "reddit_session" in self.session.cookies:
            logger.info(f"Reddit cookies loaded (reddit_session) for u/{self._username}")
            return True

        # Check for authenticated token_v2 (new reddit auth)
        token_v2 = self.session.cookies.get("token_v2", "")
        if token_v2 and self._is_token_v2_logged_in(token_v2):
            logger.info(f"Reddit cookies loaded (token_v2) for u/{self._username}")
            return True

        logger.error(
            "Reddit cookies exist but no valid session found. "
            "Re-login required:\n"
            "  python miloagent.py login reddit"
        )
        self._authenticated = False
        return False

    @staticmethod
    def _is_token_v2_logged_in(token: str) -> bool:
        """Check if token_v2 JWT belongs to a logged-in user."""
        try:
            import base64
            parts = token.split(".")
            if len(parts) < 2:
                return False
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return data.get("sub", "").startswith("t2_")
        except Exception:
            return False


    def _ensure_auth(self) -> bool:
        """Ensure we're authenticated for write operations.

        Also fetches the modhash (CSRF token) required for all POST requests
        to old.reddit.com/api/*.
        """
        if self._authenticated:
            try:
                resp = self.session.get(
                    f"{REDDIT_OLD}/api/me.json",
                    headers={"User-Agent": self.session.headers.get("User-Agent", _random_ua())},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    username = data.get("data", {}).get("name")
                    if username:
                        logger.debug(f"Session valid for u/{username}")
                        # Modhash is REQUIRED for all write operations
                        modhash = data.get("data", {}).get("modhash", "")
                        if modhash:
                            self._modhash = modhash
                            logger.debug(f"Got modhash: {modhash[:8]}...")
                        else:
                            logger.warning("No modhash in /api/me.json response")
                        return True
            except Exception as e:
                logger.debug(f"Auth check failed: {e}")
            # Session expired, try re-login
            self._authenticated = False
            logger.info("Reddit session expired, re-authenticating...")

        return self._login()

    # ── Scanning (anonymous, no auth needed) ─────────────────────────

    def scan(self, project: Dict) -> List[Dict]:
        """Scan subreddits using public JSON endpoints.

        Multi-strategy scanning:
        1. Keyword search in each subreddit
        2. Hot/new listing fallback when keyword search yields few results
        3. Negative keyword filtering

        SAFETY: Respects subreddit/keyword limits set by orchestrator.
        The orchestrator limits to ~4 subreddits and ~3 keywords per cycle.
        Total requests per scan: ~4 subs × 3 keywords + fallback = ~16 max.
        """
        opportunities = []
        reddit_config = project.get("reddit", {})
        keywords = reddit_config.get("keywords", [])
        negative_keywords = reddit_config.get("exclude_keywords", [])
        min_score = reddit_config.get("min_post_score", 1)
        max_age_hours = reddit_config.get("max_post_age_hours", 24)
        project_name = project.get("project", {}).get("name", "unknown")

        # Combine subreddits (primary + secondary)
        subreddits = []
        subs = reddit_config.get("target_subreddits", {})
        if isinstance(subs, dict):
            subreddits.extend(subs.get("primary", []))
            subreddits.extend(subs.get("secondary", []))
        elif isinstance(subs, list):
            subreddits = subs

        seen_ids = set()
        request_count = 0
        max_requests = 40  # Hard cap on total HTTP requests per scan

        # Rotate User-Agent per scan cycle
        self.session.headers["User-Agent"] = _random_ua()

        for sub_name in subreddits:
            if request_count >= max_requests:
                logger.debug(f"Hit request cap ({max_requests}), stopping scan")
                break

            sub_opps = 0

            # Strategy 1: Keyword search
            for keyword in keywords:
                if request_count >= max_requests:
                    break
                try:
                    posts = self._search_subreddit(sub_name, keyword)
                    request_count += 1
                    for post in posts:
                        opp = self._process_post(
                            post, sub_name, keyword, project,
                            min_score, max_age_hours,
                            negative_keywords, seen_ids,
                        )
                        if opp:
                            opportunities.append(opp)
                            sub_opps += 1
                except Exception as e:
                    logger.error(
                        f"Error scanning r/{sub_name} for '{keyword}': {e}"
                    )
                time.sleep(random.uniform(1.0, 2.5))

            # Strategy 2: Browse hot + new if keyword search found few results
            if sub_opps < 2 and request_count < max_requests:
                for sort in ("hot", "new"):
                    if request_count >= max_requests:
                        break
                    try:
                        posts = self._browse_subreddit(sub_name, sort, limit=15)
                        request_count += 1
                        for post in posts:
                            opp = self._process_post(
                                post, sub_name, "", project,
                                min_score, max_age_hours,
                                negative_keywords, seen_ids,
                            )
                            if opp:
                                opportunities.append(opp)
                                sub_opps += 1
                        time.sleep(random.uniform(1.0, 2.0))
                    except Exception as e:
                        logger.debug(f"Browse r/{sub_name}/{sort} fallback failed: {e}")

            time.sleep(random.uniform(1.5, 3.0))

        opportunities.sort(
            key=lambda x: x["relevance_score"], reverse=True
        )
        logger.info(
            f"Reddit scan for {project_name}: "
            f"found {len(opportunities)} opportunities "
            f"({request_count} requests)"
        )
        return opportunities

    def _process_post(
        self,
        post: Dict,
        sub_name: str,
        keyword: str,
        project: Dict,
        min_score: int,
        max_age_hours: int,
        negative_keywords: List[str],
        seen_ids: set,
    ) -> Optional[Dict]:
        """Process a single post and return opportunity dict if valid."""
        post_id = post.get("id", "")
        if not post_id or post_id in seen_ids:
            return None
        seen_ids.add(post_id)

        if self._already_acted(post_id):
            return None

        post_score = post.get("score", 0)
        if post_score < min_score:
            return None

        created_utc = post.get("created_utc", 0)
        if created_utc:
            age_hours = (
                datetime.now(timezone.utc).timestamp() - created_utc
            ) / 3600
            if age_hours > max_age_hours:
                return None

        # Negative keyword filter
        title_lower = post.get("title", "").lower()
        body_lower = post.get("selftext", "").lower()
        text = f"{title_lower} {body_lower}"
        if negative_keywords and any(nk.lower() in text for nk in negative_keywords):
            return None

        # Skip locked/archived posts
        if post.get("locked") or post.get("archived"):
            return None

        project_name = project.get("project", {}).get("name", "unknown")

        opp = {
            "platform": "reddit",
            "target_id": post_id,
            "title": post.get("title", ""),
            "body": post.get("selftext", "")[:500],
            "subreddit": sub_name,
            "post_score": post_score,
            "url": f"https://reddit.com{post.get('permalink', '')}",
            "created_utc": created_utc,
            "num_comments": post.get("num_comments", 0),
            "keyword": keyword,
            "fullname": post.get("name", ""),
            "author": post.get("author", ""),
            "upvote_ratio": post.get("upvote_ratio", 0.5),
        }
        opp["relevance_score"] = self._score_opportunity(opp, project)

        self.db.log_opportunity(
            platform="reddit",
            target_id=post_id,
            title=post.get("title", ""),
            subreddit_or_query=sub_name,
            score=opp["relevance_score"],
            project=project_name,
            metadata={
                "keyword": keyword,
                "post_score": post_score,
                "num_comments": post.get("num_comments", 0),
                "upvote_ratio": post.get("upvote_ratio", 0.5),
            },
        )

        return opp

    def _search_subreddit(
        self, subreddit: str, query: str, limit: int = 15
    ) -> List[Dict]:
        """Search a subreddit using JSON API with session cookies.

        Uses self.session (with cookies) to avoid IP blocks on servers.
        Falls back to anonymous requests if session not available.
        """
        url = f"{REDDIT_BASE}/r/{subreddit}/search.json"
        params = {
            "q": query,
            "sort": "new",
            "t": "week",
            "limit": limit,
            "restrict_sr": "true",
        }

        for attempt in range(2):
            try:
                resp = self.session.get(
                    url, params=params,
                    headers={"Accept": "application/json"},
                    timeout=10,
                )

                if resp.status_code == 200:
                    ct = resp.headers.get("Content-Type", "")
                    if "json" not in ct and "html" in ct.lower():
                        logger.warning(
                            f"Reddit returned HTML instead of JSON for r/{subreddit} "
                            f"(IP blocked?) — try loading cookies"
                        )
                        return []
                    data = resp.json()
                    posts = []
                    for child in data.get("data", {}).get("children", []):
                        if child.get("kind") == "t3":
                            posts.append(child.get("data", {}))
                    return posts

                if resp.status_code == 429:
                    wait = (2 ** attempt) * 5
                    logger.warning(f"Reddit rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                if resp.status_code in (403, 404):
                    ct = resp.headers.get("Content-Type", "")
                    if "html" in ct.lower():
                        logger.warning(
                            f"Reddit blocked r/{subreddit} (403 HTML) — "
                            f"check cookies or proxy"
                        )
                    else:
                        logger.debug(f"r/{subreddit} returned {resp.status_code}")
                    return []

                logger.warning(f"Reddit search returned {resp.status_code}")

            except requests.Timeout:
                logger.debug(f"Search timeout for r/{subreddit}, attempt {attempt+1}")
                time.sleep(2 ** attempt)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Reddit returned non-JSON for r/{subreddit}: {e}")
                return []
            except Exception as e:
                logger.debug(f"Search error for r/{subreddit}: {e}")
                break

        return []

    def _browse_subreddit(
        self, subreddit: str, sort: str = "hot", limit: int = 10
    ) -> List[Dict]:
        """Browse a subreddit's hot/new/rising listings.

        Uses self.session (with cookies) to avoid IP blocks.
        """
        url = f"{REDDIT_BASE}/r/{subreddit}/{sort}.json"
        params = {"limit": limit, "t": "day"}

        try:
            resp = self.session.get(
                url, params=params,
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            posts = []
            for child in data.get("data", {}).get("children", []):
                if child.get("kind") == "t3":
                    posts.append(child.get("data", {}))
            return posts
        except Exception:
            return []

    # ── Posting (requires authentication) ────────────────────────────

    def act(self, opportunity: Dict, project: Dict) -> bool:
        """Generate, validate, and post a comment."""
        project_name = project.get("project", {}).get("name", "unknown")

        # Circuit breaker
        if self._consecutive_failures >= self._max_failures:
            logger.warning(
                f"Circuit breaker open: {self._consecutive_failures} "
                f"consecutive failures, skipping"
            )
            return False

        # Need auth for posting
        if not self._ensure_auth():
            logger.error("Cannot post: Reddit authentication failed")
            self._consecutive_failures += 1
            return False

        try:
            # Stage-aware promotional decision
            stage = opportunity.get("_community_stage", "new")
            is_promo = self.content_gen.should_be_promotional(
                subreddit=opportunity.get("subreddit", ""),
                project=project_name,
                stage=stage,
            )
            comment_text = self.content_gen.generate_reddit_comment(
                post_title=opportunity["title"],
                post_body=opportunity.get("body", ""),
                subreddit=opportunity["subreddit"],
                project=project,
                is_promotional=is_promo,
            )

            # Content validation
            comment_text = self._validate_content(
                comment_text, opportunity, project, is_promo
            )
            if not comment_text:
                logger.warning("Content validation failed after retry, skipping")
                return False

            logger.info(
                f"Generated {'promo' if is_promo else 'organic'} comment "
                f"for r/{opportunity['subreddit']}: "
                f"{opportunity['title'][:50]}..."
            )

            # Human-like delay based on reading time + thinking + typing
            delay = self._human_reading_delay(
                opportunity.get("title", ""),
                opportunity.get("body", ""),
                comment_text,
            )
            logger.debug(f"Human reading delay: {delay:.1f}s")
            time.sleep(delay)

            # Post comment
            fullname = opportunity.get("fullname", "")
            if not fullname:
                fullname = f"t3_{opportunity['target_id']}"

            # Modhash (CSRF token) is REQUIRED for old.reddit.com API
            if not self._modhash:
                logger.error("No modhash available — cannot post (CSRF protection)")
                self._consecutive_failures += 1
                return False

            post_data = {
                "thing_id": fullname,
                "text": comment_text,
                "uh": self._modhash,
                "api_type": "json",
            }

            post_headers = {
                "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                "Referer": opportunity.get("url", f"{REDDIT_OLD}/"),
                "Origin": REDDIT_OLD,
            }

            resp = self.session.post(
                f"{REDDIT_OLD}/api/comment",
                data=post_data,
                headers=post_headers,
                timeout=30,
            )

            logger.debug(f"Comment POST status: {resp.status_code}")

            # Handle HTTP errors specifically
            if resp.status_code == 429:
                logger.warning("Reddit rate limited on comment POST")
                self._consecutive_failures += 1
                return False
            if resp.status_code == 403:
                logger.error(
                    f"Reddit 403 on comment POST. "
                    f"modhash={'yes' if self._modhash else 'no'}, "
                    f"cookies={list(self.session.cookies.keys())}"
                )
                self._authenticated = False
                self._consecutive_failures += 1
                return False

            result = resp.json()
            errors = result.get("json", {}).get("errors", [])
            if errors:
                error_msg = str(errors)
                logger.error(f"Reddit comment error: {error_msg}")
                self.db.log_action(
                    platform="reddit",
                    action_type="comment",
                    account=self._username,
                    project=project_name,
                    target_id=opportunity["target_id"],
                    content=comment_text,
                    success=False,
                    error_message=error_msg,
                )
                self._consecutive_failures += 1
                return False

            # Success — reset circuit breaker
            self._consecutive_failures = 0

            comment_data = (
                result.get("json", {})
                .get("data", {})
                .get("things", [{}])[0]
                .get("data", {})
            )
            comment_id = comment_data.get("id", "unknown")

            self.db.log_action(
                platform="reddit",
                action_type="comment",
                account=self._username,
                project=project_name,
                target_id=opportunity["target_id"],
                content=comment_text,
                metadata={
                    "comment_id": comment_id,
                    "promotional": is_promo,
                    "subreddit": opportunity["subreddit"],
                    "method": "web_session",
                },
            )
            self.db.update_opportunity_status(
                opportunity["target_id"], "acted"
            )

            logger.info(
                f"Posted comment on r/{opportunity['subreddit']}: "
                f"{opportunity['title'][:50]}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to comment: {e}")
            self._consecutive_failures += 1
            self.db.log_action(
                platform="reddit",
                action_type="comment",
                account=self._username,
                project=project_name,
                target_id=opportunity["target_id"],
                content="",
                success=False,
                error_message=str(e),
            )
            return False

    def _validate_content(
        self,
        content: str,
        opportunity: Dict,
        project: Dict,
        is_promo: bool,
    ) -> Optional[str]:
        """Validate content and retry once if invalid."""
        try:
            from core.content_validator import ContentValidator
            validator = ContentValidator()

            is_valid, score, issues = validator.validate(
                content, project, "reddit"
            )

            if is_valid:
                logger.debug(f"Content validated: score={score:.2f}")
                return content

            logger.info(
                f"Content validation failed (score={score:.2f}): {issues}. "
                f"Regenerating..."
            )

            # Retry once with same parameters
            content_retry = self.content_gen.generate_reddit_comment(
                post_title=opportunity["title"],
                post_body=opportunity.get("body", ""),
                subreddit=opportunity["subreddit"],
                project=project,
                is_promotional=is_promo,
            )

            is_valid2, score2, issues2 = validator.validate(
                content_retry, project, "reddit"
            )

            if is_valid2:
                logger.info(f"Retry succeeded: score={score2:.2f}")
                return content_retry

            logger.warning(
                f"Retry also failed (score={score2:.2f}): {issues2}"
            )
            # If retry score is better than 0.4, use it anyway
            if score2 >= 0.4:
                return content_retry
            return None

        except Exception as e:
            logger.debug(f"Validation error (continuing anyway): {e}")
            return content

    def _human_reading_delay(
        self, post_title: str, post_body: str, comment_text: str
    ) -> float:
        """Calculate human-like delay based on content length.

        Simulates: reading the post + thinking + typing the reply.
        """
        post_words = len(f"{post_title} {post_body}".split())
        reply_words = len(comment_text.split())

        # Reading time (250 wpm avg, 60% skim factor)
        read_time = (post_words / 250) * 60 * 0.6
        # Thinking pause
        think_time = random.uniform(5, 30)
        # Typing time (~40 wpm with pauses/corrections)
        type_time = (reply_words / 40) * 60 * random.uniform(0.7, 1.3)

        base_delay = read_time + think_time + type_time
        # Add jitter (+/- 30%)
        delay = base_delay * random.uniform(0.7, 1.3)
        # Clamp between 8s and 180s
        return max(8.0, min(delay, 180.0))

    def act_dry_run(self, opportunity: Dict, project: Dict) -> str:
        """Generate comment without posting."""
        is_promo = self.content_gen._should_be_promotional()
        return self.content_gen.generate_reddit_comment(
            post_title=opportunity["title"],
            post_body=opportunity.get("body", ""),
            subreddit=opportunity["subreddit"],
            project=project,
            is_promotional=is_promo,
        )

    def _score_opportunity(self, opp: Dict, project: Dict) -> float:
        """Score an opportunity 0-10 with advanced signals."""
        score = 0.0
        reddit_config = project.get("reddit", {})
        title_lower = opp.get("title", "").lower()
        body_lower = opp.get("body", "").lower()
        text = f"{title_lower} {body_lower}"
        keywords = reddit_config.get("keywords", [])

        # Keyword matches (0-4) — title matches worth more
        keyword_score = 0.0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in title_lower:
                keyword_score += 1.5  # Title match = high relevance
            elif kw_lower in body_lower:
                keyword_score += 0.8  # Body match = moderate
        score += min(keyword_score, 4.0)

        # Engagement velocity (0-2) — upvotes per hour
        post_score = opp.get("post_score", 0)
        created_utc = opp.get("created_utc", 0)
        if created_utc and post_score > 0:
            age_hours = max(0.1, (
                datetime.now(timezone.utc).timestamp() - created_utc
            ) / 3600)
            velocity = post_score / age_hours
            if velocity >= 50:
                score += 2.0
            elif velocity >= 20:
                score += 1.5
            elif velocity >= 5:
                score += 1.0
            elif velocity >= 1:
                score += 0.5
        elif post_score >= 5:
            score += 0.5

        # Competition window (0-1.5) — sweet spot: some activity but not buried
        # Rewards posts with moderate comments (active discussion, not dead or buried)
        num_comments = opp.get("num_comments", 0)
        if num_comments <= 5:
            score += 1.5   # Fresh post, early commenter advantage
        elif num_comments <= 15:
            score += 1.2   # Active discussion, still visible
        elif num_comments <= 30:
            score += 0.8   # Busy but still worth joining
        elif num_comments <= 60:
            score += 0.3   # Crowded, low visibility
        # 60+ comments: no bonus (comment will be buried)

        # Recency — exponential decay (0-2.0), half-life 8h
        if created_utc:
            age_hours = (
                datetime.now(timezone.utc).timestamp() - created_utc
            ) / 3600
            recency = 2.0 * (0.5 ** (age_hours / 8))
            score += max(0, min(recency, 2.0))

        # Subreddit tier (0-1.5)
        subs = reddit_config.get("target_subreddits", {})
        primary = subs.get("primary", []) if isinstance(subs, dict) else subs
        if opp.get("subreddit", "") in primary:
            score += 1.5
        else:
            score += 0.5

        # Intent signals — question, help, recommendation (0-1.5)
        question_signals = ["?", "how do i", "how to", "what is", "which",
                           "anyone know", "can someone", "should i"]
        if any(sig in text for sig in question_signals):
            score += 0.8

        help_signals = ["recommend", "looking for", "suggest", "alternative",
                       "advice", "what tool", "what app", "best way",
                       "struggling", "stuck", "doesn't work", "help me"]
        if any(sig in text for sig in help_signals):
            score += 0.7

        # Upvote ratio bonus (healthy discussion)
        upvote_ratio = opp.get("upvote_ratio", 0.5)
        if upvote_ratio >= 0.9:
            score += 0.3

        return min(score, 10.0)

    # ── Subreddit Seeding ─────────────────────────────────────────────

    def create_post(
        self,
        subreddit: str,
        title: str,
        body: str,
        project: Dict,
    ) -> Optional[str]:
        """Create a new post in a subreddit. Returns post URL or None."""
        if not self._ensure_auth():
            logger.error("Cannot create post: not authenticated")
            return None

        project_name = project.get("project", {}).get("name", "unknown")

        try:
            time.sleep(random.uniform(10, 30))

            if not self._modhash:
                logger.error("No modhash available — cannot post (CSRF protection)")
                return None

            resp = self.session.post(
                f"{REDDIT_OLD}/api/submit",
                data={
                    "sr": subreddit,
                    "kind": "self",
                    "title": title,
                    "text": body,
                    "uh": self._modhash,
                    "api_type": "json",
                    "resubmit": "true",
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/submit",
                    "Origin": REDDIT_OLD,
                },
                timeout=30,
            )

            if resp.status_code == 429:
                logger.warning("Rate limited on post creation")
                return None

            result = resp.json()
            errors = result.get("json", {}).get("errors", [])
            if errors:
                logger.error(f"Post creation error: {errors}")
                return None

            post_url = result.get("json", {}).get("data", {}).get("url", "")

            self.db.log_action(
                platform="reddit",
                action_type="post",
                account=self._username,
                project=project_name,
                target_id=f"post_{subreddit}",
                content=f"{title}\n\n{body}",
                metadata={
                    "subreddit": subreddit,
                    "url": post_url,
                    "method": "web_session",
                },
            )

            logger.info(f"Created post in r/{subreddit}: {title[:50]}")
            return post_url

        except Exception as e:
            logger.error(f"Failed to create post: {e}")
            return None

    def seed_subreddit(
        self,
        subreddit: str,
        project: Dict,
        topic: Optional[str] = None,
    ) -> Optional[str]:
        """Generate and post a valuable seed post in a subreddit.

        Creates organic, value-driven content that naturally positions
        the project's expertise area.
        """
        proj = project.get("project", project)
        if not topic:
            topic = proj.get("description", "")

        post_data = self.content_gen.generate_reddit_post(
            subreddit=subreddit,
            topic=topic,
            project=project,
            is_promotional=False,  # Seed posts should be organic/valuable
        )

        if not post_data.get("title") or not post_data.get("body"):
            logger.warning("Failed to generate seed post content")
            return None

        return self.create_post(
            subreddit=subreddit,
            title=post_data["title"],
            body=post_data["body"],
            project=project,
        )

    # ── Engagement Actions (upvote, subscribe, save) ─────────────────

    def upvote(self, thing_id: str) -> bool:
        """Upvote a post or comment. thing_id must include prefix (t3_ or t1_)."""
        if not self._ensure_auth():
            return False
        if not self._modhash:
            logger.warning("No modhash, cannot upvote")
            return False

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/vote",
                data={
                    "id": thing_id,
                    "dir": 1,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.debug(f"Upvoted {thing_id}")
                self.db.log_action(
                    platform="reddit",
                    action_type="upvote",
                    account=self._username,
                    project="engagement",
                    target_id=thing_id,
                    content="",
                )
                return True
            logger.warning(f"Upvote failed: {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"Upvote error: {e}")
            return False

    def subscribe(self, subreddit: str) -> bool:
        """Subscribe to a subreddit."""
        if not self._ensure_auth():
            return False
        if not self._modhash:
            logger.warning("No modhash, cannot subscribe")
            return False

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/subscribe",
                data={
                    "action": "sub",
                    "sr_name": subreddit,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Subscribed to r/{subreddit}")
                self._subscribed_subs.add(subreddit.lower())
                self.db.log_action(
                    platform="reddit",
                    action_type="subscribe",
                    account=self._username,
                    project="engagement",
                    target_id=subreddit,
                    content="",
                )
                return True
            logger.warning(f"Subscribe failed: {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"Subscribe error: {e}")
            return False

    def unsubscribe(self, subreddit: str) -> bool:
        """Unsubscribe from a subreddit."""
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/subscribe",
                data={
                    "action": "unsub",
                    "sr_name": subreddit,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Unsubscribed from r/{subreddit}")
                return True
            return False
        except Exception as e:
            logger.error(f"Unsubscribe error: {e}")
            return False

    def save_item(self, thing_id: str) -> bool:
        """Save a post or comment (bookmarking)."""
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/save",
                data={
                    "id": thing_id,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.debug(f"Saved {thing_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Save error: {e}")
            return False

    def get_user_info(self) -> Optional[Dict]:
        """Get current user profile info (karma, age, etc.)."""
        if not self._ensure_auth():
            return None

        try:
            resp = self.session.get(
                f"{REDDIT_OLD}/api/me.json",
                headers={"User-Agent": self.session.headers.get("User-Agent", _random_ua())},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "username": data.get("name", ""),
                    "comment_karma": data.get("comment_karma", 0),
                    "link_karma": data.get("link_karma", 0),
                    "created_utc": data.get("created_utc", 0),
                    "is_gold": data.get("is_gold", False),
                    "verified": data.get("has_verified_email", False),
                    "inbox_count": data.get("inbox_count", 0),
                }
        except Exception as e:
            logger.error(f"Failed to get user info: {e}")
        return None

    def warm_up(self, project: Dict) -> Dict:
        """Warm up the account: subscribe to target subs, upvote posts.

        Makes the account look natural before posting. Returns stats.
        """
        stats = {"subscribed": 0, "upvoted": 0, "saved": 0}

        if not self._ensure_auth():
            return stats

        reddit_config = project.get("reddit", {})
        subs = reddit_config.get("target_subreddits", {})
        if isinstance(subs, dict):
            all_subs = subs.get("primary", []) + subs.get("secondary", [])
        elif isinstance(subs, list):
            all_subs = subs
        else:
            all_subs = []

        # Subscribe to target subreddits (skip already-subscribed, max 5 per cycle)
        max_subs_per_cycle = 5
        new_subs = [s for s in all_subs if s.lower() not in self._subscribed_subs]
        if not new_subs:
            logger.debug(f"Already subscribed to all {len(all_subs)} target subs")
        else:
            # Randomize order to spread subscription pattern
            batch = random.sample(new_subs, min(max_subs_per_cycle, len(new_subs)))
            for sub_name in batch:
                try:
                    if self.subscribe(sub_name):
                        stats["subscribed"] += 1
                    time.sleep(random.uniform(3.0, 8.0))
                except Exception:
                    pass

        # Upvote some hot posts in target subs (look natural)
        subs_to_browse = random.sample(all_subs, min(3, len(all_subs)))
        for sub_name in subs_to_browse:
            try:
                posts = self._browse_subreddit(sub_name, "hot", limit=5)
                # Upvote 2-3 random posts
                to_upvote = random.sample(posts, min(random.randint(2, 3), len(posts)))
                for post in to_upvote:
                    fullname = post.get("name", "")
                    if fullname and self.upvote(fullname):
                        stats["upvoted"] += 1
                    time.sleep(random.uniform(2.0, 5.0))
            except Exception:
                pass
            time.sleep(random.uniform(3.0, 6.0))

        # Save 1-2 interesting posts
        try:
            posts = self._browse_subreddit(
                random.choice(all_subs) if all_subs else "popular", "hot", limit=10
            )
            to_save = random.sample(posts, min(2, len(posts)))
            for post in to_save:
                fullname = post.get("name", "")
                if fullname and self.save_item(fullname):
                    stats["saved"] += 1
                time.sleep(random.uniform(1.0, 3.0))
        except Exception:
            pass

        logger.info(
            f"Warm-up complete: subscribed={stats['subscribed']}, "
            f"upvoted={stats['upvoted']}, saved={stats['saved']}"
        )
        return stats

    # ── Comment Verification ──────────────────────────────────────────

    def verify_comment(self, comment_id: str) -> Dict:
        """Check if a posted comment still exists (not removed by mods/spam filter).

        Returns: {"exists": bool, "score": int, "removed": bool}
        """
        try:
            url = f"{REDDIT_BASE}/api/info.json"
            resp = requests.get(
                url,
                params={"id": f"t1_{comment_id}"},
                headers={"User-Agent": _random_ua(), "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return {"exists": False, "score": 0, "removed": True}

            data = resp.json()
            children = data.get("data", {}).get("children", [])
            if not children:
                return {"exists": False, "score": 0, "removed": True}

            comment = children[0].get("data", {})
            is_removed = (
                comment.get("body") == "[removed]"
                or comment.get("body") == "[deleted]"
                or comment.get("removed_by_category") is not None
            )

            # Extract reply bodies for sentiment analysis
            reply_bodies = []
            replies_data = comment.get("replies")
            reply_count = 0
            if isinstance(replies_data, dict):
                children = replies_data.get("data", {}).get("children", [])
                reply_count = len(children)
                for child in children[:5]:
                    body = child.get("data", {}).get("body", "")
                    if body and body not in ("[removed]", "[deleted]"):
                        reply_bodies.append(body[:300])

            return {
                "exists": True,
                "score": comment.get("score", 0),
                "removed": is_removed,
                "upvotes": comment.get("ups", 0),
                "replies": reply_count,
                "reply_bodies": reply_bodies,
            }
        except Exception as e:
            logger.debug(f"Comment verification failed: {e}")
            return {"exists": False, "score": 0, "removed": False}

    # ── DMs & Messaging (Phase 6) ────────────────────────────────────

    def send_dm(self, to_user: str, subject: str, body: str) -> bool:
        """Send a Reddit private message (DM).

        Uses REDDIT_OLD/api/compose with modhash CSRF token.
        """
        if not self._ensure_auth():
            logger.warning("Cannot send DM: not authenticated")
            return False

        # Human-like delay before sending
        time.sleep(random.uniform(2, 5))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/compose",
                data={
                    "api_type": "json",
                    "to": to_user,
                    "subject": subject[:100],
                    "text": body,
                    "uh": self._modhash,
                },
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                errors = data.get("json", {}).get("errors", [])
                if not errors:
                    logger.info(f"DM sent to u/{to_user}: {subject[:30]}")
                    return True
                else:
                    logger.warning(f"DM to u/{to_user} failed: {errors}")
                    return False
            else:
                logger.warning(f"DM failed: HTTP {resp.status_code}")
                return False

        except Exception as e:
            logger.error(f"DM send error: {e}")
            return False

    def check_inbox(self, limit: int = 25) -> list:
        """Check Reddit inbox for new messages.

        Returns list of {id, author, subject, body, created_utc, is_new}.
        """
        if not self._ensure_auth():
            return []

        try:
            resp = self.session.get(
                f"{REDDIT_OLD}/message/inbox.json?limit={limit}",
                timeout=15,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            messages = []
            for child in data.get("data", {}).get("children", []):
                msg = child.get("data", {})
                # Filter to actual DMs (not comment replies)
                if child.get("kind") == "t4":  # t4 = message
                    messages.append({
                        "id": msg.get("name", ""),  # e.g. t4_abc123
                        "author": msg.get("author", ""),
                        "subject": msg.get("subject", ""),
                        "body": msg.get("body", ""),
                        "created_utc": msg.get("created_utc", 0),
                        "is_new": msg.get("new", False),
                    })

            return messages

        except Exception as e:
            logger.debug(f"Inbox check failed: {e}")
            return []

    def reply_to_dm(self, thing_id: str, body: str) -> bool:
        """Reply to a Reddit DM.

        thing_id is the message ID (e.g., t4_abc123).
        """
        if not self._ensure_auth():
            return False

        time.sleep(random.uniform(2, 5))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/comment",
                data={
                    "api_type": "json",
                    "thing_id": thing_id,
                    "text": body,
                    "uh": self._modhash,
                },
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                errors = data.get("json", {}).get("errors", [])
                if not errors:
                    logger.info(f"Replied to DM {thing_id}")
                    return True
                else:
                    logger.warning(f"DM reply failed: {errors}")
                    return False
            return False

        except Exception as e:
            logger.error(f"DM reply error: {e}")
            return False

    def get_user_about(self, username: str) -> Optional[Dict]:
        """Get public info about a Reddit user.

        Returns {name, link_karma, comment_karma, created_utc, subreddit}.
        """
        try:
            resp = requests.get(
                f"{REDDIT_BASE}/user/{username}/about.json",
                headers={
                    "User-Agent": _random_ua(),
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return None
        except Exception as e:
            logger.debug(f"Failed to get user info for u/{username}: {e}")
            return None

    # ── Moderation & Admin ──────────────────────────────────────────

    def _get_subreddit_fullname(self, subreddit: str) -> Optional[str]:
        """Get the t5_ fullname for a subreddit (needed by /api/friend, /api/site_admin).

        Caches results to avoid repeated lookups.
        """
        if not hasattr(self, "_sr_fullnames"):
            self._sr_fullnames: Dict[str, str] = {}

        key = subreddit.lower()
        if key in self._sr_fullnames:
            return self._sr_fullnames[key]

        try:
            resp = self.session.get(
                f"{REDDIT_BASE}/r/{subreddit}/about.json",
                headers={"User-Agent": _random_ua(), "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                fullname = data.get("name", "")  # e.g. "t5_2qh33"
                if fullname:
                    self._sr_fullnames[key] = fullname
                    return fullname
            logger.debug(f"Could not get fullname for r/{subreddit}: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Fullname lookup failed for r/{subreddit}: {e}")
        return None

    def get_subreddit_about(self, subreddit: str) -> Optional[Dict]:
        """Get full subreddit info (subscribers, mods, settings, etc.)."""
        try:
            resp = self.session.get(
                f"{REDDIT_BASE}/r/{subreddit}/about.json",
                headers={"User-Agent": _random_ua(), "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {})
        except Exception as e:
            logger.debug(f"Subreddit about failed for r/{subreddit}: {e}")
        return None

    def get_subreddit_moderators(self, subreddit: str) -> List[Dict]:
        """Get list of moderators for a subreddit.

        Returns [{name, mod_permissions, date}] or empty list.
        """
        try:
            resp = self.session.get(
                f"{REDDIT_BASE}/r/{subreddit}/about/moderators.json",
                headers={"User-Agent": _random_ua(), "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                children = data.get("children", [])
                return [
                    {
                        "name": m.get("name", ""),
                        "mod_permissions": m.get("mod_permissions", []),
                        "date": m.get("date", 0),
                    }
                    for m in children
                ]
        except Exception as e:
            logger.debug(f"Mod list failed for r/{subreddit}: {e}")
        return []

    def get_mod_queue(self, subreddit: str, limit: int = 25) -> List[Dict]:
        """Get items from the moderation queue (needs mod access).

        Returns list of {id, kind, author, title/body, subreddit, created_utc}.
        """
        if not self._ensure_auth():
            return []

        try:
            resp = self.session.get(
                f"{REDDIT_OLD}/r/{subreddit}/about/modqueue.json?limit={limit}",
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Accept": "application/json",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                items = []
                for child in data.get("children", []):
                    d = child.get("data", {})
                    items.append({
                        "id": d.get("name", ""),  # fullname like t3_ or t1_
                        "kind": child.get("kind", ""),
                        "author": d.get("author", ""),
                        "title": d.get("title", ""),
                        "body": d.get("body", d.get("selftext", "")),
                        "subreddit": d.get("subreddit", ""),
                        "created_utc": d.get("created_utc", 0),
                        "num_reports": d.get("num_reports", 0),
                        "mod_reports": d.get("mod_reports", []),
                        "user_reports": d.get("user_reports", []),
                    })
                return items
            logger.debug(f"Mod queue failed for r/{subreddit}: {resp.status_code}")
        except Exception as e:
            logger.error(f"Mod queue error for r/{subreddit}: {e}")
        return []

    def remove_item(self, thing_id: str, spam: bool = False) -> bool:
        """Remove a post or comment as moderator.

        thing_id: fullname like t1_xxx (comment) or t3_xxx (post).
        spam: if True, marks as spam instead of just removing.
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            logger.warning("No modhash, cannot remove item")
            return False

        time.sleep(random.uniform(1, 3))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/remove",
                data={
                    "id": thing_id,
                    "spam": str(spam).lower(),
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Removed {thing_id} (spam={spam})")
                self.db.log_action(
                    platform="reddit", action_type="mod_remove",
                    account=self._username, project="moderation",
                    target_id=thing_id, content=f"spam={spam}",
                )
                return True
            logger.warning(f"Remove failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Remove error: {e}")
        return False

    def approve_item(self, thing_id: str) -> bool:
        """Approve a post or comment from the mod queue."""
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(1, 3))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/approve",
                data={
                    "id": thing_id,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Approved {thing_id}")
                self.db.log_action(
                    platform="reddit", action_type="mod_approve",
                    account=self._username, project="moderation",
                    target_id=thing_id, content="",
                )
                return True
            logger.warning(f"Approve failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Approve error: {e}")
        return False

    def distinguish_comment(self, thing_id: str, how: str = "yes") -> bool:
        """Mark a comment/post as moderator (green shield).

        how: 'yes' = mod distinguish, 'no' = remove distinguish,
             'admin' = admin (red), 'special' = special.
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(1, 3))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/distinguish",
                data={
                    "id": thing_id,
                    "how": how,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Distinguished {thing_id} ({how})")
                return True
            logger.warning(f"Distinguish failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Distinguish error: {e}")
        return False

    def sticky_post(self, thing_id: str, state: bool = True, num: int = 1) -> bool:
        """Pin/unpin a post at the top of a subreddit.

        state: True = pin, False = unpin.
        num: 1 or 2 (sticky slot position, max 2 stickies).
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(2, 5))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/set_subreddit_sticky",
                data={
                    "id": thing_id,
                    "state": str(state).lower(),
                    "num": num,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                action = "Pinned" if state else "Unpinned"
                logger.info(f"{action} {thing_id} in slot {num}")
                self.db.log_action(
                    platform="reddit", action_type="mod_sticky",
                    account=self._username, project="moderation",
                    target_id=thing_id, content=f"state={state},num={num}",
                )
                return True
            logger.warning(f"Sticky failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Sticky error: {e}")
        return False

    def ban_user(self, subreddit: str, username: str, reason: str = "",
                 duration: int = 0, note: str = "") -> bool:
        """Ban a user from a subreddit.

        duration: days (0 = permanent). note: internal mod note.
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        fullname = self._get_subreddit_fullname(subreddit)
        if not fullname:
            logger.error(f"Cannot ban: unknown subreddit fullname for r/{subreddit}")
            return False

        time.sleep(random.uniform(2, 5))

        try:
            data = {
                "api_type": "json",
                "type": "banned",
                "name": username,
                "container": fullname,
                "ban_reason": reason[:300],
                "note": note[:300],
                "uh": self._modhash,
            }
            if duration > 0:
                data["duration"] = str(duration)

            resp = self.session.post(
                f"{REDDIT_OLD}/api/friend",
                data=data,
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/about/banned",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                result = resp.json()
                if not result.get("json", {}).get("errors"):
                    logger.info(f"Banned u/{username} from r/{subreddit} ({duration}d)")
                    self.db.log_action(
                        platform="reddit", action_type="mod_ban",
                        account=self._username, project="moderation",
                        target_id=f"r/{subreddit}:u/{username}",
                        content=reason[:100],
                    )
                    return True
                logger.warning(f"Ban errors: {result['json']['errors']}")
            else:
                logger.warning(f"Ban failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Ban error: {e}")
        return False

    def invite_moderator(self, subreddit: str, username: str,
                         permissions: str = "+all") -> bool:
        """Invite a user as moderator of a subreddit.

        permissions: '+all', '+posts', '+wiki', '+flair', etc.
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        fullname = self._get_subreddit_fullname(subreddit)
        if not fullname:
            logger.error(f"Cannot invite mod: unknown fullname for r/{subreddit}")
            return False

        time.sleep(random.uniform(2, 5))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/friend",
                data={
                    "api_type": "json",
                    "type": "moderator_invite",
                    "name": username,
                    "container": fullname,
                    "permissions": permissions,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/about/moderators",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                result = resp.json()
                if not result.get("json", {}).get("errors"):
                    logger.info(f"Invited u/{username} as mod of r/{subreddit}")
                    self.db.log_action(
                        platform="reddit", action_type="mod_invite",
                        account=self._username, project="moderation",
                        target_id=f"r/{subreddit}:u/{username}",
                        content=permissions,
                    )
                    return True
                logger.warning(f"Mod invite errors: {result['json']['errors']}")
            else:
                logger.warning(f"Mod invite failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Mod invite error: {e}")
        return False

    def update_subreddit_settings(self, subreddit: str, **settings) -> bool:
        """Update subreddit settings (sidebar, description, type, etc.).

        Common settings keys:
            description: sidebar markdown text
            public_description: short public description (500 char)
            title: subreddit title
            type: 'public' | 'private' | 'restricted'
            link_type: 'any' | 'link' | 'self'
            allow_top: 'true'/'false'
            show_media: 'true'/'false'
            submit_text: text shown on submission page
            submit_link_label: custom label for link submit button
            submit_text_label: custom label for text submit button
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        fullname = self._get_subreddit_fullname(subreddit)
        if not fullname:
            logger.error(f"Cannot update settings: unknown fullname for r/{subreddit}")
            return False

        time.sleep(random.uniform(3, 8))

        try:
            data = {
                "api_type": "json",
                "sr": fullname,
                "name": subreddit,
                "type": "public",
                "link_type": "any",
                "allow_top": "true",
                "show_media": "true",
                "uh": self._modhash,
            }
            # Merge user-provided settings
            for k, v in settings.items():
                data[k] = v

            resp = self.session.post(
                f"{REDDIT_OLD}/api/site_admin",
                data=data,
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/about/edit",
                    "Origin": REDDIT_OLD,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                errors = result.get("json", {}).get("errors", [])
                if not errors:
                    logger.info(f"Updated settings for r/{subreddit}")
                    self.db.log_action(
                        platform="reddit", action_type="admin_settings",
                        account=self._username, project="moderation",
                        target_id=f"r/{subreddit}",
                        content=str(list(settings.keys()))[:200],
                    )
                    return True
                logger.warning(f"Settings update errors: {errors}")
            else:
                logger.warning(f"Settings update failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Settings update error: {e}")
        return False

    def add_subreddit_rule(self, subreddit: str, short_name: str,
                           description: str = "", kind: str = "all",
                           violation_reason: str = "") -> bool:
        """Add a community rule to a subreddit.

        kind: 'all' | 'link' | 'comment'
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(2, 5))

        try:
            data = {
                "api_type": "json",
                "r": subreddit,
                "short_name": short_name[:100],
                "kind": kind,
                "uh": self._modhash,
            }
            if description:
                data["description"] = description[:500]
            if violation_reason:
                data["violation_reason"] = violation_reason[:100]

            resp = self.session.post(
                f"{REDDIT_OLD}/api/add_subreddit_rule",
                data=data,
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/about/rules",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Added rule '{short_name}' to r/{subreddit}")
                return True
            logger.warning(f"Add rule failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Add rule error: {e}")
        return False

    def set_flair_template(self, subreddit: str, text: str,
                           css_class: str = "", flair_type: str = "LINK_FLAIR",
                           text_editable: bool = False,
                           background_color: str = "",
                           text_color: str = "dark") -> bool:
        """Create a flair template for posts or users.

        flair_type: 'LINK_FLAIR' (post flair) | 'USER_FLAIR'
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(1, 3))

        try:
            data = {
                "api_type": "json",
                "r": subreddit,
                "text": text[:64],
                "css_class": css_class,
                "flair_type": flair_type,
                "text_editable": str(text_editable).lower(),
                "uh": self._modhash,
            }
            if background_color:
                data["background_color"] = background_color
            if text_color:
                data["text_color"] = text_color

            resp = self.session.post(
                f"{REDDIT_OLD}/api/flairtemplate",
                data=data,
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/about/flair",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Created flair '{text}' in r/{subreddit}")
                return True
            logger.warning(f"Set flair template failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Set flair template error: {e}")
        return False

    def set_post_flair(self, subreddit: str, thing_id: str,
                       flair_text: str, flair_css: str = "") -> bool:
        """Set flair on a specific post."""
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(1, 2))

        try:
            resp = self.session.post(
                f"{REDDIT_OLD}/api/selectflair",
                data={
                    "api_type": "json",
                    "r": subreddit,
                    "link": thing_id,
                    "text": flair_text[:64],
                    "css_class": flair_css,
                    "uh": self._modhash,
                },
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Set flair '{flair_text}' on {thing_id}")
                return True
            logger.warning(f"Set post flair failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Set post flair error: {e}")
        return False

    def edit_wiki_page(self, subreddit: str, page: str, content: str,
                       reason: str = "") -> bool:
        """Edit a wiki page in a subreddit.

        For AutoModerator config, use page='config/automoderator'.
        """
        if not self._ensure_auth():
            return False
        if not self._modhash:
            return False

        time.sleep(random.uniform(2, 5))

        try:
            data = {
                "page": page,
                "content": content,
                "uh": self._modhash,
            }
            if reason:
                data["reason"] = reason[:256]

            resp = self.session.post(
                f"{REDDIT_OLD}/r/{subreddit}/wiki/edit",
                data=data,
                headers={
                    "User-Agent": self.session.headers.get("User-Agent", _random_ua()),
                    "Referer": f"{REDDIT_OLD}/r/{subreddit}/wiki/{page}",
                    "Origin": REDDIT_OLD,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"Edited wiki page r/{subreddit}/wiki/{page}")
                self.db.log_action(
                    platform="reddit", action_type="admin_wiki",
                    account=self._username, project="moderation",
                    target_id=f"r/{subreddit}/wiki/{page}",
                    content=reason[:100] if reason else f"Updated {page}",
                )
                return True
            # Wiki edit can return 302 redirect on success
            if resp.status_code in (302, 301):
                logger.info(f"Edited wiki page r/{subreddit}/wiki/{page} (redirect)")
                return True
            logger.warning(f"Wiki edit failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Wiki edit error: {e}")
        return False

    # ── Connection & Cleanup ──────────────────────────────────────────

    def test_connection(self) -> bool:
        """Test Reddit connectivity."""
        try:
            resp = requests.get(
                f"{REDDIT_BASE}/r/python/hot.json?limit=1",
                headers={"User-Agent": _random_ua(), "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(f"Reddit anonymous read failed: {resp.status_code}")
                return False
            logger.info("Reddit anonymous read: OK")
        except Exception as e:
            logger.error(f"Reddit connection failed: {e}")
            return False

        if self._username and not self._username.startswith("YOUR_"):
            if self._ensure_auth():
                logger.info(f"Reddit authenticated as: u/{self._username}")
            else:
                logger.warning(
                    "Reddit read works, but login failed. "
                    "Scanning will work, but posting won't.\n"
                    "  To authenticate, run:\n"
                    "    python miloagent.py login reddit\n"
                    "    python miloagent.py paste-cookies reddit"
                )
        return True

    def close(self):
        """Close the requests session to free resources."""
        try:
            self.session.close()
        except Exception:
            pass

    def __del__(self):
        try:
            self.session.close()
        except Exception:
            pass

    def get_account_info(self) -> Dict:
        return {
            "username": self._username,
            "method": "web_session",
            "authenticated": self._authenticated,
        }
