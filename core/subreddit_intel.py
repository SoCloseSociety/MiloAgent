"""Subreddit Intelligence — discover high-opportunity communities.

Analyzes subreddit metadata to find dormant, under-moderated communities
with high subscriber counts where the bot can become a valued contributor.

Uses Reddit's public JSON endpoints (no auth required for reads):
- r/{sub}/about.json       -> subscribers, active_users, description
- r/{sub}/new.json          -> post frequency calculation
- r/{sub}/about/moderators.json -> mod list (may require auth)
"""

import math
import time
import random
import logging
from typing import Dict, List, Optional
from statistics import median

import requests

from core.database import Database

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
]

REDDIT_BASE = "https://www.reddit.com"


def _random_ua() -> str:
    return random.choice(USER_AGENTS)


class SubredditIntelligence:
    """Analyzes subreddits for opportunity scoring.

    Detects dormant/under-moderated communities with high subscriber counts
    that are relevant to project themes.

    Usage:
        intel = SubredditIntelligence(db)
        data = intel.analyze_subreddit("NewTubers")
        score = intel.score_subreddit_opportunity(data, project)
        intel.store_intel("NewTubers", "my_project", data, score)
    """

    def __init__(self, db: Database, session: Optional[requests.Session] = None):
        self.db = db
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", _random_ua())
        self._last_request_time = 0.0
        self._request_delay = 2.0

    def _throttled_get(self, url: str, params: dict = None, timeout: int = 10):
        """Rate-limited GET request."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._request_delay:
            time.sleep(self._request_delay - elapsed)
        self._last_request_time = time.time()
        return self.session.get(
            url, params=params,
            headers={"User-Agent": _random_ua(), "Accept": "application/json"},
            timeout=timeout,
        )

    # ── Analysis ──────────────────────────────────────────────────

    def analyze_subreddit(self, subreddit: str) -> Optional[Dict]:
        """Fetch and analyze metadata for one subreddit.

        Returns a dict with all intelligence data, or None on failure.
        """
        data = {}

        # 1. About info (subscribers, active users)
        about = self._fetch_about(subreddit)
        if not about:
            return None
        data.update(about)

        # 2. Post frequency from recent posts
        post_metrics = self._fetch_post_metrics(subreddit)
        data.update(post_metrics)

        # 3. Mod activity (optional — may 403 for non-members)
        mod_info = self._fetch_mod_info(subreddit)
        data.update(mod_info)

        return data

    def _fetch_about(self, subreddit: str) -> Optional[Dict]:
        """Fetch subreddit about info."""
        try:
            resp = self._throttled_get(f"{REDDIT_BASE}/r/{subreddit}/about.json")
            if resp.status_code != 200:
                logger.debug(f"r/{subreddit}/about.json returned {resp.status_code}")
                return None

            raw = resp.json().get("data", {})
            return {
                "subscribers": raw.get("subscribers", 0),
                "active_users": raw.get("accounts_active", 0) or raw.get("active_user_count", 0),
                "created_utc": raw.get("created_utc", 0),
                "description": (raw.get("public_description", "") or "")[:500],
                "subreddit_type": raw.get("subreddit_type", "public"),
                "over18": 1 if raw.get("over18") else 0,
                "display_name": raw.get("display_name", subreddit),
            }
        except Exception as e:
            logger.debug(f"Failed to fetch about for r/{subreddit}: {e}")
            return None

    def _fetch_post_metrics(self, subreddit: str, limit: int = 25) -> Dict:
        """Calculate post frequency and engagement from recent posts."""
        metrics = {
            "posts_per_day": 0.0,
            "avg_hours_between_posts": 999.0,
            "median_post_score": 0.0,
            "avg_comments_per_post": 0.0,
        }
        try:
            resp = self._throttled_get(
                f"{REDDIT_BASE}/r/{subreddit}/new.json",
                params={"limit": limit},
            )
            if resp.status_code != 200:
                return metrics

            children = resp.json().get("data", {}).get("children", [])
            if len(children) < 2:
                return metrics

            timestamps = []
            scores = []
            comments = []
            for child in children:
                post = child.get("data", {})
                created = post.get("created_utc", 0)
                if created:
                    timestamps.append(created)
                scores.append(post.get("score", 0))
                comments.append(post.get("num_comments", 0))

            if len(timestamps) >= 2:
                timestamps.sort(reverse=True)
                gaps = []
                for i in range(len(timestamps) - 1):
                    gap_hours = (timestamps[i] - timestamps[i + 1]) / 3600
                    gaps.append(gap_hours)

                avg_gap = sum(gaps) / len(gaps) if gaps else 999.0
                metrics["avg_hours_between_posts"] = round(avg_gap, 2)

                # Calculate posts per day from time span
                time_span_hours = (timestamps[0] - timestamps[-1]) / 3600
                if time_span_hours > 0:
                    metrics["posts_per_day"] = round(
                        len(timestamps) / (time_span_hours / 24), 2
                    )

            if scores:
                metrics["median_post_score"] = round(median(scores), 1)
            if comments:
                metrics["avg_comments_per_post"] = round(
                    sum(comments) / len(comments), 1
                )

        except Exception as e:
            logger.debug(f"Post metrics fetch failed for r/{subreddit}: {e}")

        return metrics

    def _fetch_mod_info(self, subreddit: str) -> Dict:
        """Fetch moderator count. May return -1 if access denied."""
        info = {"mod_count": -1, "active_mod_count": -1}
        try:
            resp = self._throttled_get(
                f"{REDDIT_BASE}/r/{subreddit}/about/moderators.json"
            )
            if resp.status_code != 200:
                return info  # 403 is expected for non-members

            children = resp.json().get("data", {}).get("children", [])
            info["mod_count"] = len(children)

            # Don't check individual mod profiles (too many requests)
            # Just count the moderators — a low count signals opportunity
            return info

        except Exception as e:
            logger.debug(f"Mod info fetch failed for r/{subreddit}: {e}")
            return info

    # ── Scoring ───────────────────────────────────────────────────

    def score_subreddit_opportunity(
        self, intel_data: Dict, project: Dict,
    ) -> float:
        """Score a subreddit 0-10 for opportunity.

        Components:
        - Subscriber volume (20%): higher = more reach
        - Post dormancy (25%): less frequent posts = less competition
        - Low mod presence (15%): fewer mods = more freedom
        - Low active/subscriber ratio (15%): low activity = underserved
        - Theme relevance (25%): keyword overlap with project
        """
        score = 0.0

        # 1. Subscriber volume (0-10, weight 0.20)
        subs = intel_data.get("subscribers", 0)
        if subs > 0:
            log_subs = math.log10(max(subs, 1))
            # 1k=3, 10k=4, 100k=5, 1M=6 -> normalize to 0-10
            sub_score = min(10.0, (log_subs - 2) * 2.5)
            score += max(0, sub_score) * 0.20

        # 2. Post dormancy (0-10, weight 0.25)
        avg_gap = intel_data.get("avg_hours_between_posts", 0)
        if avg_gap >= 48:
            dormancy = 10.0
        elif avg_gap >= 24:
            dormancy = 8.0
        elif avg_gap >= 12:
            dormancy = 6.0
        elif avg_gap >= 6:
            dormancy = 3.0
        elif avg_gap >= 2:
            dormancy = 1.5
        else:
            dormancy = 0.5
        score += dormancy * 0.25

        # 3. Low mod presence (0-10, weight 0.15)
        mod_count = intel_data.get("mod_count", -1)
        if mod_count == -1:
            mod_score = 5.0  # Unknown
        elif mod_count <= 1:
            mod_score = 10.0
        elif mod_count <= 3:
            mod_score = 7.0
        elif mod_count <= 5:
            mod_score = 4.0
        else:
            mod_score = 2.0
        score += mod_score * 0.15

        # 4. Low active/subscriber ratio (0-10, weight 0.15)
        active = intel_data.get("active_users", 0)
        if subs > 0 and active > 0:
            ratio = active / subs
            if ratio < 0.001:
                activity_score = 10.0
            elif ratio < 0.005:
                activity_score = 7.0
            elif ratio < 0.01:
                activity_score = 4.0
            else:
                activity_score = 2.0
        else:
            activity_score = 5.0
        score += activity_score * 0.15

        # 5. Theme relevance (0-10, weight 0.25)
        relevance = self._compute_relevance(intel_data, project)
        score += relevance * 0.25

        return round(min(score, 10.0), 2)

    def _compute_relevance(self, intel_data: Dict, project: Dict) -> float:
        """Compute keyword overlap between subreddit and project."""
        reddit_config = project.get("reddit", {})
        keywords = [kw.lower() for kw in reddit_config.get("keywords", [])]
        if not keywords:
            return 5.0  # Default if no keywords defined

        sub_text = (
            intel_data.get("description", "").lower() + " " +
            intel_data.get("display_name", "").lower()
        )

        matches = sum(1 for kw in keywords if kw in sub_text)
        if matches >= 3:
            return 10.0
        elif matches >= 2:
            return 8.0
        elif matches >= 1:
            return 5.0
        return 2.0

    # ── Target Gathering ──────────────────────────────────────────

    def get_target_subreddits(self, project: Dict) -> List[str]:
        """Get all subreddits to analyze for a project."""
        reddit_config = project.get("reddit", {})
        subs = reddit_config.get("target_subreddits", {})
        targets = set()

        if isinstance(subs, dict):
            targets.update(subs.get("primary", []))
            targets.update(subs.get("secondary", []))
        elif isinstance(subs, list):
            targets.update(subs)

        # Add approved discoveries
        proj_name = project.get("project", {}).get("name", "unknown")
        try:
            discoveries = self.db.get_discoveries(
                platform="reddit", project=proj_name, status="approved",
            )
            for d in discoveries:
                if d.get("discovery_type") == "subreddit":
                    targets.add(d["value"])
        except Exception:
            pass

        return list(targets)

    # ── Storage ───────────────────────────────────────────────────

    def store_intel(
        self, subreddit: str, project: str,
        intel_data: Dict, score: float,
    ):
        """Write intelligence data to DB."""
        data = dict(intel_data)
        data["opportunity_score"] = score
        self.db.upsert_subreddit_intel(subreddit, project, data)

    def get_top_opportunities(
        self, project: str, limit: int = 20,
    ) -> List[Dict]:
        """Get highest-scored subreddits from DB."""
        return self.db.get_subreddit_intel(project=project, limit=limit)

    def get_intel(self, subreddit: str) -> Optional[Dict]:
        """Get cached intelligence for a subreddit."""
        return self.db.get_subreddit_intel_single(subreddit)
