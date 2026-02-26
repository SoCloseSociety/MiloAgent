"""Strategy engine — self-improving intelligence for decision-making.

Uses the LearningEngine to adapt scoring weights over time.
The more data the bot collects, the smarter it gets.
"""

import random
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

from core.database import Database

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Self-improving intelligence layer for opportunity scoring.

    Opportunity Scoring (0-10) — ADAPTIVE:
    - Relevance to project keywords (0-4) × learned keyword weight
    - Engagement velocity (upvotes/hour) (0-2)
    - Comment velocity (comments/hour, lower = less competition) (0-2)
    - Recency with exponential decay (0-1.5)
    - Subreddit quality (0-1.5) × learned subreddit weight
    - Intent signals (question, help, recommendation) (0-1.0)

    Self-Improvement (via LearningEngine):
    - Subreddit weights: boost subreddits where comments get engagement
    - Keyword weights: boost keywords that find high-quality opportunities
    - Tone optimization: use the tone style that performs best
    - Promo ratio: auto-adjust promotional/organic ratio
    - Discovery: find new subreddits/keywords via LLM analysis
    """

    def __init__(self, db: Database, settings: Dict):
        self.db = db
        self.settings = settings
        self._last_project_index = -1
        self._learning = None
        self._subreddit_intel = None

    @property
    def learning(self):
        """Lazy-load learning engine."""
        if self._learning is None:
            from core.learning_engine import LearningEngine
            self._learning = LearningEngine(self.db)
        return self._learning

    def set_learning_engine(self, engine):
        """Set a pre-configured learning engine (with LLM for discovery)."""
        self._learning = engine

    @property
    def subreddit_intel(self):
        """Lazy-load subreddit intelligence."""
        if self._subreddit_intel is None:
            from core.subreddit_intel import SubredditIntelligence
            self._subreddit_intel = SubredditIntelligence(self.db)
        return self._subreddit_intel

    def set_subreddit_intel(self, intel):
        """Set a pre-configured subreddit intelligence instance."""
        self._subreddit_intel = intel

    def score_opportunity(self, opportunity: Dict, project: Dict) -> float:
        """Adaptive scoring using multiple signals + learned weights."""
        score = 0.0
        platform = opportunity.get("platform", "")

        if platform == "reddit":
            score = self._score_reddit(opportunity, project)
        elif platform == "twitter":
            score = self._score_twitter(opportunity, project)

        return min(max(score, 0.0), 10.0)

    def _score_reddit(self, opp: Dict, project: Dict) -> float:
        """Score a Reddit opportunity with adaptive weights."""
        score = 0.0
        reddit_config = project.get("reddit", {})
        keywords = reddit_config.get("keywords", [])
        proj_name = project.get("project", {}).get("name", "unknown")

        title = opp.get("title", "").lower()
        body = opp.get("body", "").lower()
        text = f"{title} {body}"

        # Keyword relevance (0-4) with title bonus + learned boost
        keyword_score = 0.0
        for kw in keywords:
            kw_lower = kw.lower()
            boost = self.learning.get_scoring_boost("keyword", kw, proj_name)
            if kw_lower in title:
                keyword_score += 1.5 * boost  # Title match = high relevance
            elif kw_lower in body:
                keyword_score += 0.8 * boost
        score += min(keyword_score, 4.0)

        # Engagement velocity (0-2) — upvotes per hour
        post_score = opp.get("post_score", opp.get("score", 0))
        created = opp.get("created_utc")
        if created and isinstance(post_score, (int, float)) and post_score > 0:
            age_hours = max(0.1, (
                datetime.now(timezone.utc).timestamp() - created
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
        elif isinstance(post_score, (int, float)) and post_score >= 5:
            score += 0.5

        # Comment velocity (0-2) — low comment rate = less competition
        num_comments = opp.get("num_comments", 0)
        if created and isinstance(num_comments, int):
            age_hours = max(0.1, (
                datetime.now(timezone.utc).timestamp() - created
            ) / 3600)
            comment_vel = num_comments / age_hours
            if comment_vel <= 1:
                score += 2.0
            elif comment_vel <= 3:
                score += 1.5
            elif comment_vel <= 8:
                score += 1.0
            elif comment_vel <= 15:
                score += 0.5

        # Recency — exponential decay (0-1.5)
        if created:
            age_hours = (
                datetime.now(timezone.utc).timestamp() - created
            ) / 3600
            recency = 1.5 * (0.5 ** (age_hours / 4))
            score += max(0, min(recency, 1.5))

        # Subreddit tier (0-2) × learned subreddit boost
        subs = reddit_config.get("target_subreddits", {})
        primary = subs.get("primary", []) if isinstance(subs, dict) else subs
        subreddit = opp.get("subreddit", "")
        sub_boost = self.learning.get_scoring_boost("subreddit", subreddit, proj_name)

        if subreddit in primary:
            score += 1.5 * sub_boost
        else:
            score += 0.5 * sub_boost

        # Intel boost: high-opportunity subreddits get extra score
        try:
            intel = self.subreddit_intel.get_intel(subreddit)
            if intel and intel.get("opportunity_score", 0) > 6.0:
                score += 0.5
        except Exception:
            pass

        # Intent signals (0-1.0)
        question_signals = ["?", "how do i", "how to", "what is", "which",
                           "anyone know", "can someone", "should i"]
        if any(sig in text for sig in question_signals):
            score += 0.5

        help_signals = ["recommend", "looking for", "suggest", "alternative",
                       "advice", "what tool", "what app", "best way",
                       "struggling", "stuck", "doesn't work", "help me"]
        if any(sig in text for sig in help_signals):
            score += 0.5

        # Upvote ratio bonus
        upvote_ratio = opp.get("upvote_ratio", 0.5)
        if upvote_ratio >= 0.9:
            score += 0.3

        return score

    def _score_twitter(self, opp: Dict, project: Dict) -> float:
        """Score a Twitter opportunity with adaptive weights."""
        score = 0.0
        twitter_config = project.get("twitter", {})
        keywords = twitter_config.get("keywords", [])
        proj_name = project.get("project", {}).get("name", "unknown")

        text = opp.get("text", "").lower()

        keyword_score = 0.0
        for kw in keywords:
            if kw.lower() in text:
                boost = self.learning.get_scoring_boost("keyword", kw, proj_name)
                keyword_score += 1.5 * boost
        score += min(keyword_score, 4.0)

        if "?" in opp.get("text", ""):
            score += 1.0

        help_signals = ["looking for", "recommend", "suggest", "any tool",
                       "what do you use", "alternative", "struggling"]
        if any(sig in text for sig in help_signals):
            score += 0.5

        hashtags = twitter_config.get("hashtags", [])
        for tag in hashtags:
            if tag.lower() in text:
                score += 0.5

        return score

    def select_project(self, projects: List[Dict]) -> Optional[Dict]:
        """Select which project to promote next."""
        if not projects:
            return None
        if len(projects) == 1:
            return projects[0]

        weighted = []
        for proj in projects:
            proj_name = proj.get("project", {}).get("name", "unknown")
            base_weight = proj.get("project", {}).get("weight", 1.0)

            recent_actions = self.db.get_action_count(hours=24)
            proj_actions = len([
                a for a in self.db.get_recent_actions(hours=24)
                if a.get("project", "").lower() == proj_name.lower()
            ])

            if recent_actions > 0:
                expected_share = base_weight / sum(
                    p.get("project", {}).get("weight", 1.0) for p in projects
                )
                actual_share = proj_actions / recent_actions if recent_actions > 0 else 0
                catch_up = max(0.5, expected_share / max(actual_share, 0.01))
                weight = base_weight * min(catch_up, 3.0)
            else:
                weight = base_weight

            weighted.append((proj, weight))

        total = sum(w for _, w in weighted)
        r = random.uniform(0, total)
        cumulative = 0
        for proj, weight in weighted:
            cumulative += weight
            if r <= cumulative:
                return proj

        return weighted[-1][0]

    def select_action_type(self, opportunity: Dict, platform: str) -> str:
        """Decide what action to take."""
        if platform == "reddit":
            return "comment"
        if platform == "twitter":
            r = random.random()
            if r < 0.60:
                return "reply"
            elif r < 0.85:
                return "like"
            else:
                return "retweet"
        return "comment"

    def get_expanded_keywords(self, project: Dict) -> List[str]:
        """Get original keywords + approved discovered keywords."""
        proj_name = project.get("project", {}).get("name", "unknown")
        base_kw = project.get("reddit", {}).get("keywords", [])
        discovered = self.learning.get_approved_discoveries(
            "reddit", proj_name, "keyword",
        )
        return list(set(base_kw + discovered))

    def get_expanded_subreddits(self, project: Dict) -> List[str]:
        """Get original subreddits + approved discovered subreddits."""
        proj_name = project.get("project", {}).get("name", "unknown")
        reddit_config = project.get("reddit", {})
        subs = reddit_config.get("target_subreddits", {})
        base_subs = []
        if isinstance(subs, dict):
            base_subs = subs.get("primary", []) + subs.get("secondary", [])
        elif isinstance(subs, list):
            base_subs = subs
        discovered = self.learning.get_approved_discoveries(
            "reddit", proj_name, "subreddit",
        )
        return list(set(base_subs + discovered))

    def should_seed_subreddit(self, project: Dict) -> Optional[str]:
        """Decide if we should create a seed post in a subreddit.

        Returns subreddit name if we should seed, None otherwise.
        Low probability (10%) to avoid spam.
        """
        if random.random() > 0.10:
            return None

        reddit_config = project.get("reddit", {})
        subs = reddit_config.get("target_subreddits", {})
        secondary = subs.get("secondary", []) if isinstance(subs, dict) else []

        if not secondary:
            return None

        # Prefer high-opportunity subreddits from intel
        proj_name = project.get("project", {}).get("name", "unknown")
        try:
            top_intel = self.db.get_subreddit_intel(project=proj_name, min_score=6.0, limit=5)
            intel_subs = [s["subreddit"] for s in top_intel if s["subreddit"] in secondary]
            if intel_subs:
                sub = random.choice(intel_subs)
            else:
                sub = random.choice(secondary)
        except Exception:
            sub = random.choice(secondary)

        # Check if we already posted there recently
        recent = self.db.get_recent_actions(hours=48, platform="reddit")
        for action in recent:
            metadata = action.get("metadata", "")
            if isinstance(metadata, str) and sub.lower() in metadata.lower():
                if action.get("action_type") == "post":
                    return None

        return sub

    # ── Community Presence ─────────────────────────────────────────

    def compute_warmth_score(self, presence: Dict) -> float:
        """Compute community warmth score (0-10) from presence data."""
        score = 0.0

        # Interaction volume (0-3)
        total = presence.get("total_comments", 0) + presence.get("total_posts", 0)
        if total >= 20:
            score += 3.0
        elif total >= 10:
            score += 2.0
        elif total >= 5:
            score += 1.5
        elif total >= 2:
            score += 0.5

        # Reputation (0-3)
        surviving = presence.get("comments_surviving", 0)
        removed = presence.get("comments_removed", 0)
        total_c = surviving + removed
        if total_c > 0:
            survival_rate = surviving / total_c
            score += min(3.0, survival_rate * 3.0)

        # Time invested (0-2)
        days = presence.get("days_active", 0)
        if days >= 30:
            score += 2.0
        elif days >= 14:
            score += 1.5
        elif days >= 7:
            score += 1.0
        elif days >= 3:
            score += 0.5

        # Engagement quality (0-2)
        avg_score = presence.get("avg_comment_score", 0)
        if avg_score >= 5:
            score += 2.0
        elif avg_score >= 3:
            score += 1.5
        elif avg_score >= 1:
            score += 1.0

        return min(score, 10.0)

    def determine_stage(self, presence: Dict) -> str:
        """Determine engagement stage from presence data.

        Stages: new -> warming -> established -> trusted
        """
        total = presence.get("total_comments", 0) + presence.get("total_posts", 0)
        days = presence.get("days_active", 0)
        removed = presence.get("comments_removed", 0)
        surviving = presence.get("comments_surviving", 0)
        total_c = removed + surviving
        removal_rate = removed / total_c if total_c > 0 else 0
        avg_score = presence.get("avg_comment_score", 0)

        if total >= 20 and days >= 30 and removal_rate < 0.1 and avg_score > 2:
            return "trusted"
        elif total >= 10 and days >= 14 and removal_rate < 0.2:
            return "established"
        elif total >= 3 and days >= 3 and removal_rate < 0.5:
            return "warming"
        return "new"

    def can_promote_in_subreddit(
        self, subreddit: str, project: str, account: str,
    ) -> bool:
        """Check if we've earned enough trust to promote."""
        presence = self.db.get_presence_for_subreddit(subreddit, project, account)
        if not presence:
            return False
        return presence.get("stage") == "trusted"

    def can_seed_in_subreddit(
        self, subreddit: str, project: str, account: str,
    ) -> bool:
        """Check if we can create seed posts."""
        presence = self.db.get_presence_for_subreddit(subreddit, project, account)
        if not presence:
            return False
        return presence.get("stage") in ("established", "trusted")

    def get_subreddits_needing_activity(
        self, project: Dict, account: str,
    ) -> List[Dict]:
        """Get subreddits that need activity to maintain presence (>48h idle)."""
        proj_name = project.get("project", {}).get("name", "unknown")
        return self.db.get_neglected_subreddits(
            project=proj_name, account=account, hours=48,
        )

    def get_prioritized_subreddits(self, project: Dict) -> List[str]:
        """Get subreddits ordered by combined strategy + intelligence score."""
        base_subs = self.get_expanded_subreddits(project)
        proj_name = project.get("project", {}).get("name", "unknown")

        scored = []
        for sub in base_subs:
            try:
                intel = self.subreddit_intel.get_intel(sub)
                opp_score = intel.get("opportunity_score", 5.0) if intel else 5.0
            except Exception:
                opp_score = 5.0
            learned_boost = self.learning.get_scoring_boost("subreddit", sub, proj_name)
            combined = opp_score * learned_boost
            scored.append((sub, combined))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [sub for sub, _ in scored]

    # ── Smart Scheduling ──────────────────────────────────────────

    def should_delay_action(self, project: Dict) -> Optional[int]:
        """Check if we should delay posting for a better time slot.

        Returns minutes to delay (0 = post now), or None if no data.
        Only delays up to 2 hours max.
        """
        from datetime import datetime as dt
        proj_name = project.get("project", {}).get("name", "unknown")
        best_times = self.learning.get_best_posting_times(proj_name)

        if not best_times:
            return None

        current_hour = dt.now().hour
        current_day = dt.now().weekday()

        # Check if current time is already in a top slot
        for t in best_times[:3]:
            if t["hour_of_day"] == current_hour and t["day_of_week"] == current_day:
                return 0  # Already in a good window

        # Find next good slot within 2 hours
        best_hour = best_times[0]["hour_of_day"]
        hours_until = (best_hour - current_hour) % 24
        if hours_until <= 2:
            return hours_until * 60

        return 0  # Too far away, post now

    # ── User Post Strategy ────────────────────────────────────────

    def should_create_user_post(
        self, project: Dict, account: str,
    ) -> Optional[Dict]:
        """Decide if we should create an autonomous user-style post.

        Returns dict with {subreddit, post_type, is_promotional, trend_context}
        or None if we should not post.
        """
        # Check config
        user_posts_cfg = self.settings.get("user_posts", {})
        if not user_posts_cfg.get("enabled", True):
            return None

        max_per_day = user_posts_cfg.get("max_per_day", 2)
        min_gap_hours = user_posts_cfg.get("min_gap_hours", 12)
        base_prob = user_posts_cfg.get("base_probability", 0.25)

        # Check daily limit
        recent_posts = self.db.get_recent_actions(
            hours=24, platform="reddit",
        )
        user_post_count = sum(
            1 for a in (recent_posts or [])
            if a.get("action_type") in ("user_post", "post")
            and a.get("account") == account
        )
        if user_post_count >= max_per_day:
            logger.debug(f"User post skipped: {user_post_count}/{max_per_day} daily limit")
            return None

        # Check min gap
        for a in (recent_posts or []):
            if (a.get("action_type") in ("user_post", "post")
                    and a.get("account") == account):
                try:
                    from datetime import datetime, timedelta
                    ts = datetime.fromisoformat(a["timestamp"])
                    if datetime.utcnow() - ts < timedelta(hours=min_gap_hours):
                        logger.debug("User post skipped: min gap not met")
                        return None
                except Exception:
                    pass
                break  # Only check most recent

        # Probability gate
        if random.random() > base_prob:
            return None

        # Pick subreddit — prefer established/trusted stages
        proj_name = project.get("project", {}).get("name", "unknown")
        prioritized = self.get_prioritized_subreddits(project)
        if not prioritized:
            return None

        chosen_sub = None
        chosen_stage = "new"
        for sub in prioritized:
            try:
                presence = self.db.get_presence_for_subreddit(
                    sub, proj_name, account
                )
                stage = self.determine_stage(presence) if presence else "new"
                if stage in ("established", "trusted"):
                    chosen_sub = sub
                    chosen_stage = stage
                    break
                elif stage == "warming" and not chosen_sub:
                    chosen_sub = sub
                    chosen_stage = stage
            except Exception:
                continue

        if not chosen_sub:
            # Fallback: random subreddit (new stage → tip/question only)
            chosen_sub = random.choice(prioritized[:5])
            chosen_stage = "new"

        # Select post type based on stage (with learned weights if available)
        from core.content_gen import ContentGenerator
        learned_weights = None
        if self.learning:
            learned_weights = self.learning.get_optimal_post_type_weights(
                proj_name, chosen_stage
            )
        post_type = ContentGenerator.select_post_type(
            None, chosen_stage, learned_weights
        )

        # Check if post type is enabled in config
        enabled_types = user_posts_cfg.get("post_types", {})
        if not enabled_types.get(post_type, True):
            # Try a safe fallback
            post_type = "tip" if enabled_types.get("tip", True) else "question"

        # Determine promotional
        is_promotional = False
        if chosen_stage in ("established", "trusted"):
            from core.content_gen import ContentGenerator as CG
            # Use static-like logic: established=5%, trusted=20%
            if chosen_stage == "established":
                is_promotional = random.random() < 0.05
            else:
                is_promotional = random.random() < 0.20

        # Check for trend context
        trend_context = ""
        try:
            trends = self.db.get_recent_research(project=proj_name, limit=3)
            if trends:
                trend_context = trends[0].get("summary", "")
        except Exception:
            pass

        # If we have trend context, bias toward trend_react
        if trend_context and chosen_stage in ("established", "trusted"):
            if random.random() < 0.4:
                post_type = "trend_react"

        logger.info(
            f"User post decision: r/{chosen_sub} type={post_type} "
            f"stage={chosen_stage} promo={is_promotional}"
        )

        return {
            "subreddit": chosen_sub,
            "post_type": post_type,
            "is_promotional": is_promotional,
            "trend_context": trend_context,
        }

    def should_create_user_tweet(self, project: Dict) -> Optional[Dict]:
        """Check if a recent Reddit post should be cross-shared to Twitter.

        Returns dict with {tweet_type, is_promotional, trend_context, reddit_url}
        or None.
        """
        user_posts_cfg = self.settings.get("user_posts", {})
        if not user_posts_cfg.get("cross_platform_share", True):
            return None

        proj_name = project.get("project", {}).get("name", "unknown")

        # Find recent Reddit posts that haven't been shared to Twitter
        try:
            recent = self.db.get_recent_actions(hours=48, platform="reddit")
        except Exception:
            return None

        import json
        for action in (recent or []):
            if action.get("action_type") not in ("user_post", "post"):
                continue
            if action.get("project", "").lower() != proj_name.lower():
                continue
            metadata = action.get("metadata", "")
            if not isinstance(metadata, str):
                continue
            try:
                meta = json.loads(metadata)
                url = meta.get("url", "")
                if url and not meta.get("shared_to_twitter"):
                    return {
                        "tweet_type": meta.get("post_type", "tip"),
                        "is_promotional": False,
                        "trend_context": "",
                        "reddit_url": url,
                        "action_id": action.get("id"),
                    }
            except (json.JSONDecodeError, TypeError):
                continue

        return None

    def get_analytics_summary(self, hours: int = 24) -> Dict:
        """Get comprehensive analytics summary + learning insights."""
        stats = self.db.get_stats_summary(hours=hours)

        recent = self.db.get_recent_actions(hours=hours, limit=100)
        project_stats = {}
        for action in recent:
            proj = action.get("project", "unknown")
            if proj not in project_stats:
                project_stats[proj] = {
                    "actions": 0, "successes": 0, "by_platform": {},
                }
            project_stats[proj]["actions"] += 1
            if action.get("success"):
                project_stats[proj]["successes"] += 1
            p = action.get("platform", "unknown")
            project_stats[proj]["by_platform"][p] = (
                project_stats[proj]["by_platform"].get(p, 0) + 1
            )

        stats["by_project"] = project_stats

        total = sum(p["actions"] for p in project_stats.values())
        successes = sum(p["successes"] for p in project_stats.values())
        stats["success_rate"] = (
            round(successes / total * 100, 1) if total > 0 else 0
        )

        stats["learning"] = self.learning.get_insights()
        return stats
