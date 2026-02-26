"""Reddit bot — scans subreddits, scores opportunities, posts LLM-generated comments."""

import time
import random
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

import praw

from platforms.base_platform import BasePlatform
from core.database import Database
from core.content_gen import ContentGenerator

logger = logging.getLogger(__name__)


class RedditBot(BasePlatform):
    """Reddit scanner + commenter using PRAW.

    Flow:
    1. scan() — search target subreddits for keyword matches
    2. _score_opportunity() — rate relevance 0-10
    3. act() — generate and post comment via LLM
    """

    def __init__(
        self,
        db: Database,
        content_gen: ContentGenerator,
        account_config: Dict,
    ):
        super().__init__(db, content_gen, account_config)
        self.account_config = account_config
        self.reddit = praw.Reddit(
            client_id=account_config["client_id"],
            client_secret=account_config["client_secret"],
            username=account_config["username"],
            password=account_config["password"],
            user_agent=account_config["user_agent"],
        )
        self._username = account_config["username"]

    def scan(self, project: Dict) -> List[Dict]:
        """Scan subreddits for relevant posts matching project keywords.

        For each target subreddit:
          1. Search using project keywords
          2. Filter by min score and max age
          3. Skip already-acted targets
          4. Score each opportunity
          5. Log to database
        Returns sorted list (highest score first).
        """
        opportunities = []
        reddit_config = project.get("reddit", {})
        keywords = reddit_config.get("keywords", [])
        min_score = reddit_config.get("min_post_score", 1)
        max_age_hours = reddit_config.get("max_post_age_hours", 24)
        project_name = project.get("project", {}).get("name", "unknown")

        # Combine primary and secondary subreddits
        subreddits = []
        subs = reddit_config.get("target_subreddits", {})
        if isinstance(subs, dict):
            subreddits.extend(subs.get("primary", []))
            subreddits.extend(subs.get("secondary", []))
        elif isinstance(subs, list):
            subreddits = subs

        seen_ids = set()

        for sub_name in subreddits:
            for keyword in keywords:
                try:
                    subreddit = self.reddit.subreddit(sub_name)
                    for submission in subreddit.search(
                        keyword, sort="new", time_filter="day", limit=10
                    ):
                        # Skip duplicates within this scan
                        if submission.id in seen_ids:
                            continue
                        seen_ids.add(submission.id)

                        # Skip if already acted on
                        if self._already_acted(submission.id):
                            continue

                        # Skip low-score posts
                        if submission.score < min_score:
                            continue

                        # Skip old posts
                        post_age_hours = (
                            datetime.now(timezone.utc).timestamp()
                            - submission.created_utc
                        ) / 3600
                        if post_age_hours > max_age_hours:
                            continue

                        opp = {
                            "platform": "reddit",
                            "target_id": submission.id,
                            "title": submission.title,
                            "body": submission.selftext[:500] if submission.selftext else "",
                            "subreddit": sub_name,
                            "post_score": submission.score,
                            "url": f"https://reddit.com{submission.permalink}",
                            "created_utc": submission.created_utc,
                            "num_comments": submission.num_comments,
                            "keyword": keyword,
                        }
                        opp["relevance_score"] = self._score_opportunity(
                            opp, project
                        )
                        opportunities.append(opp)

                        # Log to database
                        self.db.log_opportunity(
                            platform="reddit",
                            target_id=submission.id,
                            title=submission.title,
                            subreddit_or_query=sub_name,
                            score=opp["relevance_score"],
                            project=project_name,
                            metadata={
                                "keyword": keyword,
                                "post_score": submission.score,
                                "num_comments": submission.num_comments,
                            },
                        )

                except Exception as e:
                    logger.error(
                        f"Error scanning r/{sub_name} for '{keyword}': {e}"
                    )

                # Small delay between searches
                time.sleep(random.uniform(1, 3))

            # Longer delay between subreddits
            time.sleep(random.uniform(2, 5))

        opportunities.sort(
            key=lambda x: x["relevance_score"], reverse=True
        )
        logger.info(
            f"Reddit scan for {project_name}: "
            f"found {len(opportunities)} opportunities"
        )
        return opportunities

    def act(self, opportunity: Dict, project: Dict) -> bool:
        """Generate and post a comment on the given opportunity.

        1. Decide if promotional (80/20 ratio)
        2. Generate comment via LLM
        3. Add human-like delay
        4. Post comment
        5. Log action to database
        """
        project_name = project.get("project", {}).get("name", "unknown")

        try:
            is_promo = self.content_gen._should_be_promotional()
            comment_text = self.content_gen.generate_reddit_comment(
                post_title=opportunity["title"],
                post_body=opportunity.get("body", ""),
                subreddit=opportunity["subreddit"],
                project=project,
                is_promotional=is_promo,
            )

            logger.info(
                f"Generated {'promo' if is_promo else 'organic'} comment "
                f"for r/{opportunity['subreddit']}: "
                f"{opportunity['title'][:50]}..."
            )
            logger.debug(f"Comment text: {comment_text[:200]}...")

            # Human-like delay before posting
            time.sleep(random.uniform(5, 20))

            submission = self.reddit.submission(id=opportunity["target_id"])
            comment = submission.reply(comment_text)

            # Log success
            self.db.log_action(
                platform="reddit",
                action_type="comment",
                account=self._username,
                project=project_name,
                target_id=opportunity["target_id"],
                content=comment_text,
                metadata={
                    "comment_id": comment.id,
                    "promotional": is_promo,
                    "subreddit": opportunity["subreddit"],
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

    def act_dry_run(self, opportunity: Dict, project: Dict) -> str:
        """Generate comment without posting (for preview)."""
        is_promo = self.content_gen._should_be_promotional()
        comment_text = self.content_gen.generate_reddit_comment(
            post_title=opportunity["title"],
            post_body=opportunity.get("body", ""),
            subreddit=opportunity["subreddit"],
            project=project,
            is_promotional=is_promo,
        )
        return comment_text

    # Negation words that invert keyword relevance
    _NEGATION_WORDS = (
        "don't", "dont", "not", "avoid", "hate", "stop using",
        "worst", "terrible", "useless", "overrated", "scam",
    )

    # Positive intent signals — user actively seeking solutions
    _POSITIVE_INTENT = (
        "looking for", "recommend", "suggest", "need a", "searching for",
        "any good", "best way", "how to", "help me", "what do you use",
    )

    def _score_opportunity(self, opp: Dict, project: Dict) -> float:
        """Score an opportunity 0-10 based on relevance signals.

        Signals:
        - Keyword density in title/body (0-3)
        - Negation penalty (up to -2)
        - Positive intent boost (0-1.5)
        - Post engagement / upvotes (0-2)
        - Competition level (fewer comments = better) (0-2)
        - Post recency (0-1.5)
        - Subreddit tier (primary vs secondary) (0-1.5)
        """
        score = 0.0
        reddit_config = project.get("reddit", {})
        text = f"{opp['title']} {opp.get('body', '')}".lower()
        title_lower = opp["title"].lower()
        body_lower = opp.get("body", "").lower()

        # ── Keyword matches (0-3) with negation awareness ──
        keywords = reddit_config.get("keywords", [])
        keyword_hits = 0
        negated_hits = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in title_lower or kw_lower in body_lower:
                # Check for negation preceding the keyword
                is_negated = False
                for neg in self._NEGATION_WORDS:
                    if f"{neg} {kw_lower}" in text or f"{neg} the {kw_lower}" in text:
                        is_negated = True
                        break
                if is_negated:
                    negated_hits += 1
                else:
                    keyword_hits += 1

        score += min(keyword_hits * 1.0, 3.0)
        score -= min(negated_hits * 1.0, 2.0)  # Penalty for negated keywords

        # Multi-keyword bonus: multiple relevant keywords = strong signal
        if keyword_hits >= 3:
            score += 0.5

        # ── Positive intent boost (0-1.5) ──
        if any(sig in text for sig in self._POSITIVE_INTENT):
            score += 1.5

        # ── Post engagement (0-2) ──
        post_score = opp.get("post_score", 0)
        if post_score >= 50:
            score += 2.0
        elif post_score >= 20:
            score += 1.5
        elif post_score >= 5:
            score += 1.0
        elif post_score >= 1:
            score += 0.5

        # ── Competition level (0-2): sweet spot is 3-15 comments ──
        num_comments = opp.get("num_comments", 0)
        if num_comments == 0:
            score += 1.0  # No comments, but could be low-quality
        elif num_comments <= 3:
            score += 2.0  # Few comments, easy to stand out
        elif num_comments <= 10:
            score += 1.5
        elif num_comments <= 25:
            score += 1.0
        elif num_comments <= 50:
            score += 0.5
        # 50+: no bonus, too crowded

        # ── Recency bonus (0-1.5) ──
        if opp.get("created_utc"):
            age_hours = (
                datetime.now(timezone.utc).timestamp() - opp["created_utc"]
            ) / 3600
            if age_hours <= 2:
                score += 1.5
            elif age_hours <= 6:
                score += 1.0
            elif age_hours <= 12:
                score += 0.5

        # ── Subreddit tier bonus (0-1.5) ──
        subs = reddit_config.get("target_subreddits", {})
        primary = subs.get("primary", []) if isinstance(subs, dict) else subs
        if opp["subreddit"] in primary:
            score += 1.5
        else:
            score += 0.5

        return max(min(score, 10.0), 0.0)

    def test_connection(self) -> bool:
        """Verify Reddit credentials work."""
        try:
            user = self.reddit.user.me()
            logger.info(f"Reddit connected as: u/{user.name}")
            return True
        except Exception as e:
            logger.error(f"Reddit connection failed: {e}")
            return False

    def get_account_info(self) -> Dict:
        """Get account info for status display."""
        try:
            user = self.reddit.user.me()
            return {
                "username": user.name,
                "karma": user.link_karma + user.comment_karma,
                "account_age_days": (
                    datetime.now(timezone.utc).timestamp() - user.created_utc
                ) / 86400,
            }
        except Exception:
            return {"username": self._username, "karma": 0, "account_age_days": 0}
