"""Learning Engine — self-improving intelligence for Milo Agent.

Analyzes past performance to automatically:
- Adjust subreddit/keyword weights (focus on what works)
- Discover new subreddits and keywords via LLM + cross-referencing
- Track which tones, content lengths, and promo ratios perform best
- Provide insights to the strategy engine for smarter decisions
"""

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.database import Database

logger = logging.getLogger(__name__)

# Minimum samples before we trust a learned weight
_MIN_SAMPLES = 3
# Decay factor: recent data matters more (exponential decay)
_RECENCY_DECAY = 0.95  # per day

# Sentiment keyword scoring (no LLM needed)
_POSITIVE_SIGNALS = {
    "thanks": 0.3, "thank you": 0.3, "helpful": 0.4, "great advice": 0.5,
    "this helped": 0.5, "exactly what i needed": 0.5, "good point": 0.3,
    "agree": 0.2, "nice": 0.2, "awesome": 0.3, "love this": 0.4,
    "saved me": 0.5, "game changer": 0.4, "underrated": 0.3,
    "solid": 0.2, "appreciate": 0.3, "well said": 0.3, "spot on": 0.3,
    "this works": 0.4, "just tried": 0.3, "i needed this": 0.4,
}
_NEGATIVE_SIGNALS = {
    "spam": -0.8, "shill": -0.9, "bot": -0.7, "ad": -0.6,
    "reported": -0.9, "self-promo": -0.8, "self promo": -0.8,
    "wrong": -0.3, "doesn't work": -0.4, "terrible": -0.5,
    "useless": -0.4, "downvoted": -0.5, "cringe": -0.4,
    "suspicious": -0.6, "misleading": -0.5, "clickbait": -0.5,
    "not helpful": -0.4, "bad advice": -0.5, "scam": -0.9,
}


class LearningEngine:
    """Self-improving intelligence layer.

    Three feedback loops:
    1. Performance tracking — record engagement for every action
    2. Weight learning — adjust scoring weights based on what works
    3. Discovery — find new subreddits/keywords the bot should target

    Usage:
        engine = LearningEngine(db, llm)
        engine.record_outcome(action_id, ...)  # after each action
        engine.learn()                          # periodic (every few hours)
        weights = engine.get_scoring_boost("subreddit", "r/NewTubers", "MyProject")
    """

    def __init__(self, db: Database, llm=None):
        self.db = db
        self.llm = llm  # Optional: used for discovery via LLM

    # ── Recording ────────────────────────────────────────────────────

    def record_outcome(
        self,
        action_id: int,
        platform: str,
        project: str,
        subreddit_or_query: str = "",
        keyword: str = "",
        action_type: str = "comment",
        was_promotional: bool = False,
        engagement_score: float = 0.0,
        upvotes: int = 0,
        replies: int = 0,
        was_removed: bool = False,
        content_length: int = 0,
        tone_style: str = "",
        post_type: str = "",
    ):
        """Record the outcome of a bot action for learning."""
        self.db.log_performance(
            action_id=action_id,
            platform=platform,
            project=project,
            subreddit_or_query=subreddit_or_query,
            keyword=keyword,
            action_type=action_type,
            was_promotional=was_promotional,
            engagement_score=engagement_score,
            upvotes=upvotes,
            replies=replies,
            was_removed=was_removed,
            content_length=content_length,
            tone_style=tone_style,
            post_type=post_type,
        )

    # ── Learning (periodic) ──────────────────────────────────────────

    def learn(self, project: str = ""):
        """Run the full learning cycle. Call every few hours.

        1. Analyze subreddit performance → update weights
        2. Analyze keyword performance → update weights
        3. Analyze tone/length performance → update weights
        4. Analyze promo vs organic → update weights
        5. Discover new subreddits/keywords via LLM
        6. Learn time-of-day performance
        7. Analyze failure patterns
        """
        logger.info("Learning engine: starting analysis...")

        projects = [project] if project else self._get_all_projects()
        for proj in projects:
            self._learn_subreddit_weights(proj)
            self._learn_keyword_weights(proj)
            self._learn_tone_weights(proj)
            self._learn_tone_from_sentiment(proj)
            self._learn_post_type_weights(proj)
            self._learn_promo_ratio(proj)
            self._learn_time_performance(proj)
            self._analyze_failures(proj)
            self._discover_new_targets(proj)
            self._learn_strategy_rules(proj)
            self._evolve_prompts(proj)

        logger.info("Learning engine: analysis complete")

    def _get_all_projects(self) -> List[str]:
        """Get all projects that have performance data."""
        rows = self.db.conn.execute(
            "SELECT DISTINCT project FROM performance"
        ).fetchall()
        return [row["project"] for row in rows]

    def _learn_subreddit_weights(self, project: str):
        """Learn which subreddits give best engagement."""
        stats = self.db.get_performance_stats(
            project=project, platform="reddit", days=30,
        )

        subreddit_data: Dict[str, Dict] = {}
        for row in stats:
            sub = row["subreddit_or_query"]
            if not sub:
                continue
            if sub not in subreddit_data:
                subreddit_data[sub] = {
                    "count": 0, "total_engagement": 0.0,
                    "total_upvotes": 0, "removed": 0,
                }
            subreddit_data[sub]["count"] += row["count"]
            subreddit_data[sub]["total_engagement"] += (
                row["avg_engagement"] * row["count"]
            )
            subreddit_data[sub]["total_upvotes"] += row["total_upvotes"] or 0
            subreddit_data[sub]["removed"] += row["removed_count"] or 0

        for sub, data in subreddit_data.items():
            if data["count"] < _MIN_SAMPLES:
                continue

            avg_eng = data["total_engagement"] / data["count"]
            removal_rate = data["removed"] / data["count"]

            # Weight formula: engagement reward - removal penalty
            weight = max(0.1, avg_eng * (1 - removal_rate * 2))

            self.db.update_learned_weight(
                category="subreddit",
                key=sub,
                project=project,
                weight=round(weight, 3),
                sample_count=data["count"],
                avg_engagement=round(avg_eng, 3),
            )
            logger.debug(
                f"Learned: r/{sub} for {project} → weight={weight:.2f} "
                f"(n={data['count']}, eng={avg_eng:.2f})"
            )

    def _learn_keyword_weights(self, project: str):
        """Learn which keywords yield best opportunities."""
        stats = self.db.get_performance_stats(
            project=project, days=30,
        )

        keyword_data: Dict[str, Dict] = {}
        for row in stats:
            kw = row["keyword"]
            if not kw:
                continue
            if kw not in keyword_data:
                keyword_data[kw] = {"count": 0, "total_engagement": 0.0}
            keyword_data[kw]["count"] += row["count"]
            keyword_data[kw]["total_engagement"] += (
                row["avg_engagement"] * row["count"]
            )

        for kw, data in keyword_data.items():
            if data["count"] < _MIN_SAMPLES:
                continue

            avg_eng = data["total_engagement"] / data["count"]
            weight = max(0.1, avg_eng)

            self.db.update_learned_weight(
                category="keyword",
                key=kw,
                project=project,
                weight=round(weight, 3),
                sample_count=data["count"],
                avg_engagement=round(avg_eng, 3),
            )

    def _learn_tone_weights(self, project: str):
        """Learn which tone styles get best engagement."""
        rows = self.db.conn.execute(
            """SELECT tone_style, COUNT(*) as count,
                      AVG(engagement_score) as avg_eng,
                      AVG(content_length) as avg_len
               FROM performance
               WHERE project = ? AND tone_style != ''
               GROUP BY tone_style
               HAVING count >= ?""",
            (project, _MIN_SAMPLES),
        ).fetchall()

        for row in rows:
            self.db.update_learned_weight(
                category="tone",
                key=row["tone_style"],
                project=project,
                weight=round(row["avg_eng"], 3),
                sample_count=row["count"],
                avg_engagement=round(row["avg_eng"], 3),
            )

    def _learn_post_type_weights(self, project: str):
        """Learn which post types get best engagement."""
        stats = self.db.get_post_type_stats(project=project, days=30)
        for row in stats:
            if row["count"] < _MIN_SAMPLES:
                continue
            avg_eng = row["avg_engagement"] or 0.0
            removal_rate = (row["removed_count"] or 0) / max(row["count"], 1)
            weight = max(0.1, avg_eng * (1 - removal_rate * 2))
            self.db.update_learned_weight(
                category="post_type",
                key=row["post_type"],
                project=project,
                weight=round(weight, 3),
                sample_count=row["count"],
                avg_engagement=round(avg_eng, 3),
            )

    # ── Sentiment Analysis ───────────────────────────────────────────

    def analyze_reply_sentiment(self, reply_bodies: List[str]) -> Dict:
        """Analyze sentiment of reply texts using keyword scoring.

        No LLM call — pure keyword matching for speed and cost.
        Returns: {score: float[-1,1], positive: list, negative: list}
        """
        if not reply_bodies:
            return {"score": 0.0, "positive": [], "negative": []}

        total_score = 0.0
        positives = []
        negatives = []

        for body in reply_bodies:
            body_lower = body.lower()
            for signal, weight in _POSITIVE_SIGNALS.items():
                if signal in body_lower:
                    total_score += weight
                    if signal not in positives:
                        positives.append(signal)
            for signal, weight in _NEGATIVE_SIGNALS.items():
                if signal in body_lower:
                    total_score += weight
                    if signal not in negatives:
                        negatives.append(signal)

        count = len(reply_bodies)
        normalized = max(-1.0, min(1.0, total_score / max(count, 1)))
        return {
            "score": round(normalized, 3),
            "positive": positives,
            "negative": negatives,
        }

    def _learn_tone_from_sentiment(self, project: str):
        """Adjust tone weights based on reply sentiment data.

        Blends 70% engagement + 30% sentiment for tone scoring.
        """
        sentiment_data = self.db.get_sentiment_by_tone(project)
        if not sentiment_data:
            return

        current_weights = self.db.get_learned_weights("tone", project)
        weight_map = {w["key"]: w for w in current_weights}

        for row in sentiment_data:
            tone = row["tone_style"]
            if tone not in weight_map or row["count"] < _MIN_SAMPLES:
                continue

            w = weight_map[tone]
            # Blend: 70% engagement weight + 30% sentiment bonus
            sentiment_bonus = row["avg_sentiment"] * 0.3
            adjusted = w["weight"] + sentiment_bonus

            self.db.update_learned_weight(
                category="tone",
                key=tone,
                project=project,
                weight=round(max(0.1, adjusted), 3),
                sample_count=w["sample_count"],
                avg_engagement=w["avg_engagement"],
            )

        logger.debug(f"Tone weights adjusted with sentiment for {project}")

    def _learn_promo_ratio(self, project: str):
        """Learn optimal promo vs organic ratio based on performance."""
        rows = self.db.conn.execute(
            """SELECT was_promotional, COUNT(*) as count,
                      AVG(engagement_score) as avg_eng,
                      SUM(was_removed) as removed
               FROM performance
               WHERE project = ?
               GROUP BY was_promotional""",
            (project,),
        ).fetchall()

        for row in rows:
            label = "promotional" if row["was_promotional"] else "organic"
            removal_rate = (row["removed"] or 0) / max(row["count"], 1)
            effective_eng = row["avg_eng"] * (1 - removal_rate)

            self.db.update_learned_weight(
                category="content_type",
                key=label,
                project=project,
                weight=round(effective_eng, 3),
                sample_count=row["count"],
                avg_engagement=round(row["avg_eng"], 3),
            )

    # ── Time Performance ────────────────────────────────────────────

    def _learn_time_performance(self, project: str):
        """Analyze which posting times get best engagement."""
        try:
            rows = self.db.conn.execute(
                """SELECT hour_of_day, day_of_week,
                          COUNT(*) as count,
                          AVG(engagement_score) as avg_eng,
                          AVG(upvotes) as avg_up,
                          SUM(was_removed) as removed
                   FROM performance
                   WHERE project = ? AND hour_of_day >= 0
                   GROUP BY hour_of_day, day_of_week
                   HAVING count >= 2""",
                (project,),
            ).fetchall()

            for row in rows:
                self.db.log_time_performance(
                    project=project,
                    subreddit="_all",
                    hour=row["hour_of_day"],
                    day=row["day_of_week"],
                    engagement=row["avg_eng"] or 0,
                    upvotes=row["avg_up"] or 0,
                    removed=row["removed"] or 0,
                )
        except Exception as e:
            logger.debug(f"Time performance learning failed: {e}")

    def get_best_posting_times(
        self, project: str, subreddit: str = "",
    ) -> List[Dict]:
        """Get top posting time slots ranked by engagement."""
        return self.db.get_best_posting_times(project, subreddit or "_all", limit=5)

    # ── Failure Analysis ──────────────────────────────────────────

    def _analyze_failures(self, project: str):
        """Identify patterns in removed/downvoted content."""
        if not self.llm:
            return
        try:
            removed = self.db.conn.execute(
                """SELECT subreddit_or_query, tone_style, content_length,
                          was_promotional, keyword
                   FROM performance
                   WHERE project = ? AND (was_removed = 1 OR engagement_score < 0)
                   AND timestamp > datetime('now', '-30 days')
                   ORDER BY timestamp DESC LIMIT 20""",
                (project,),
            ).fetchall()

            if len(removed) < 3:
                return

            # Group by subreddit
            by_sub: Dict[str, list] = {}
            for r in removed:
                sub = r["subreddit_or_query"]
                if sub:
                    by_sub.setdefault(sub, []).append(dict(r))

            for sub, failures in by_sub.items():
                if len(failures) < 3:
                    continue

                summary = json.dumps([{
                    "tone": f["tone_style"],
                    "length": f["content_length"],
                    "promotional": f["was_promotional"],
                    "keyword": f["keyword"],
                } for f in failures[:10]])

                prompt = (
                    f"Analyze these {len(failures)} removed/downvoted Reddit comments "
                    f"in r/{sub}:\n{summary}\n\n"
                    f"What patterns do you see? Give 1-3 specific avoidance rules.\n"
                    f"Format: RULE: <rule text>"
                )

                try:
                    result = self.llm.generate(
                        prompt=prompt, max_tokens=200,
                        system_prompt="You are a Reddit content strategist. Be specific.",
                        task="analytical",
                    )
                    for line in result.split("\n"):
                        if line.strip().upper().startswith("RULE:"):
                            rule = line.split(":", 1)[1].strip()
                            if rule:
                                self.db.log_failure_pattern(
                                    project=project,
                                    subreddit=sub,
                                    failure_type="removed",
                                    pattern=f"{len(failures)} removals in 30 days",
                                    avoidance_rule=rule,
                                )
                except Exception as e:
                    logger.debug(f"Failure analysis LLM call failed: {e}")

        except Exception as e:
            logger.debug(f"Failure analysis failed: {e}")

    # ── Performance Benchmarking ──────────────────────────────────

    def get_performance_benchmark(self, project: str) -> Dict:
        """Compare this week's performance vs last week."""
        this_week = self.db.get_performance_stats(project=project, days=7)
        last_week = self.db.get_performance_stats_range(project, 14, 7)

        this_total = sum(r["count"] for r in this_week) if this_week else 0
        last_total = sum(r["count"] for r in last_week) if last_week else 0

        this_avg_eng = (
            sum(r["avg_engagement"] * r["count"] for r in this_week) / max(this_total, 1)
            if this_week else 0
        )
        last_avg_eng = (
            sum(r["avg_engagement"] * r["count"] for r in last_week) / max(last_total, 1)
            if last_week else 0
        )

        this_removals = sum(r.get("removed_count") or 0 for r in this_week)
        last_removals = sum(r.get("removed_count") or 0 for r in last_week)

        delta = 0
        if last_avg_eng > 0:
            delta = round((this_avg_eng - last_avg_eng) / last_avg_eng * 100, 1)

        return {
            "this_week_avg_engagement": round(this_avg_eng, 2),
            "last_week_avg_engagement": round(last_avg_eng, 2),
            "engagement_delta_pct": delta,
            "this_week_actions": this_total,
            "last_week_actions": last_total,
            "this_week_removals": this_removals,
            "last_week_removals": last_removals,
        }

    # ── Discovery ────────────────────────────────────────────────────

    def _discover_new_targets(self, project: str):
        """Use LLM to discover new subreddits/keywords for a project."""
        if not self.llm:
            return

        # Only discover if we have some performance data
        stats = self.db.get_performance_stats(project=project, days=30)
        if len(stats) < 3:
            return

        # Get current best-performing subreddits
        top_subs = self.db.get_learned_weights("subreddit", project)[:5]
        top_keywords = self.db.get_learned_weights("keyword", project)[:5]

        if not top_subs and not top_keywords:
            return

        sub_names = [w["key"] for w in top_subs]
        kw_names = [w["key"] for w in top_keywords]

        # Include intel data for richer context
        intel_context = ""
        try:
            top_intel = self.db.get_subreddit_intel(project=project, min_score=6.0, limit=5)
            if top_intel:
                intel_context = "\nHigh-opportunity subreddits I've found:\n" + "\n".join(
                    f"r/{s['subreddit']}: {s['subscribers']} subscribers, "
                    f"{s['posts_per_day']:.1f} posts/day, opportunity={s['opportunity_score']:.1f}"
                    for s in top_intel
                ) + "\nSuggest similar under-moderated or dormant subreddits.\n"
        except Exception:
            pass

        prompt = (
            f"I'm promoting a product called '{project}' on Reddit.\n"
            f"My best-performing subreddits are: {', '.join(sub_names)}\n"
            f"My best keywords are: {', '.join(kw_names)}\n"
            f"{intel_context}\n"
            f"Suggest 5 NEW subreddits (not in my list) where I could find "
            f"relevant discussions. Also suggest 5 NEW search keywords.\n\n"
            f"Format:\n"
            f"SUBREDDITS: sub1, sub2, sub3, sub4, sub5\n"
            f"KEYWORDS: kw1, kw2, kw3, kw4, kw5"
        )

        try:
            result = self.llm.generate(
                prompt=prompt,
                system_prompt=(
                    "You are a Reddit marketing strategist. "
                    "Output only the requested format, nothing else."
                ),
                max_tokens=200,
                task="analytical",
            )
            self._parse_discoveries(result, project)
        except Exception as e:
            logger.debug(f"Discovery LLM call failed: {e}")

    def _parse_discoveries(self, text: str, project: str):
        """Parse LLM discovery output and store in DB."""
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("SUBREDDITS:"):
                items = line.split(":", 1)[1].strip().split(",")
                for item in items:
                    sub = item.strip().lstrip("r/").strip()
                    if sub and len(sub) > 2:
                        self.db.log_discovery(
                            platform="reddit",
                            project=project,
                            discovery_type="subreddit",
                            value=sub,
                            source="llm_expansion",
                            score=5.0,
                        )
            elif line.upper().startswith("KEYWORDS:"):
                items = line.split(":", 1)[1].strip().split(",")
                for item in items:
                    kw = item.strip().strip('"').strip("'")
                    if kw and len(kw) > 2:
                        self.db.log_discovery(
                            platform="reddit",
                            project=project,
                            discovery_type="keyword",
                            value=kw,
                            source="llm_expansion",
                            score=5.0,
                        )

    # ── Dynamic Skill Acquisition ─────────────────────────────────────

    def _learn_strategy_rules(self, project: str):
        """Extract strategy rules from performance patterns via LLM.

        Analyzes: which subreddit + post_type + tone combos work best.
        Stores as strategy_rule in knowledge_base.
        Uses Ollama (analytical) = free.
        """
        if not self.llm:
            return

        total = self.db.conn.execute(
            "SELECT COUNT(*) FROM performance WHERE project = ?", (project,)
        ).fetchone()[0]
        if total < 30:
            return

        try:
            top_combos = self.db.conn.execute(
                """SELECT subreddit_or_query, post_type, tone_style,
                          COUNT(*) as count, AVG(engagement_score) as avg_eng,
                          SUM(was_removed) as removed
                   FROM performance
                   WHERE project = ? AND post_type != '' AND tone_style != ''
                   AND timestamp > datetime('now', '-30 days')
                   GROUP BY subreddit_or_query, post_type, tone_style
                   HAVING count >= 3
                   ORDER BY avg_eng DESC LIMIT 10""",
                (project,),
            ).fetchall()

            if len(top_combos) < 3:
                return

            combo_text = "\n".join(
                f"r/{c['subreddit_or_query']} + {c['post_type']} + "
                f"{c['tone_style']}: avg_eng={c['avg_eng']:.2f}, "
                f"n={c['count']}, removed={c['removed']}"
                for c in top_combos
            )

            prompt = (
                f"Analyze these content performance patterns for '{project}':\n\n"
                f"{combo_text}\n\n"
                f"Extract 3-5 strategy rules. Format: RULE: <rule text>\n"
                f"Example: RULE: In r/NewTubers, use experience posts "
                f"with creator_mentor tone"
            )

            result = self.llm.generate(
                prompt=prompt, max_tokens=300,
                system_prompt="You are a content strategy analyst. "
                              "Output only rules.",
                task="analytical",
            )

            rules_added = 0
            for line in result.split("\n"):
                if line.strip().upper().startswith("RULE:"):
                    rule = line.split(":", 1)[1].strip()
                    if rule and len(rule) > 10:
                        self.db.log_knowledge(
                            project=project,
                            category="strategy_rule",
                            topic="auto_strategy",
                            content=rule,
                            source="llm_analysis",
                            relevance_score=top_combos[0]["avg_eng"],
                        )
                        rules_added += 1

            if rules_added:
                logger.info(
                    f"Learned {rules_added} strategy rules for {project}"
                )

        except Exception as e:
            logger.debug(f"Strategy rule extraction failed: {e}")

    def _evolve_prompts(self, project: str):
        """Analyze top-performing content and improve prompt templates.

        Uses Ollama (analytical task) = free and unlimited.
        Only runs if 20+ performance records with post_type data.
        Max 3 evolutions per cycle.
        """
        if not self.llm:
            return

        import os

        try:
            top_content = self.db.conn.execute(
                """SELECT post_type, COUNT(*) as count,
                          AVG(engagement_score) as avg_eng
                   FROM performance
                   WHERE project = ? AND engagement_score > 2.0
                   AND post_type != ''
                   AND timestamp > datetime('now', '-30 days')
                   GROUP BY post_type
                   HAVING count >= 5
                   ORDER BY avg_eng DESC LIMIT 3""",
                (project,),
            ).fetchall()

            if not top_content:
                return

            evolved_count = 0
            for pt_row in top_content:
                if evolved_count >= 3:
                    break

                post_type = pt_row["post_type"]
                template_name = f"reddit_user_{post_type}"

                # 7-day cooldown between evolutions
                existing = self.db.conn.execute(
                    """SELECT id FROM prompt_evolution_log
                       WHERE project = ? AND template_name = ?
                       AND timestamp > datetime('now', '-7 days')""",
                    (project, template_name),
                ).fetchone()
                if existing:
                    continue

                # Get high-engagement content samples
                samples = self.db.conn.execute(
                    """SELECT a.content FROM actions a
                       JOIN performance p ON a.id = p.action_id
                       WHERE p.project = ? AND p.post_type = ?
                       AND p.engagement_score > 2.0
                       ORDER BY p.engagement_score DESC LIMIT 3""",
                    (project, post_type),
                ).fetchall()

                if len(samples) < 2:
                    continue

                # Get current template (evolved or file-based)
                current_template = self.db.get_evolved_prompt(
                    project, template_name
                )
                if not current_template:
                    template_path = os.path.join("prompts", f"{template_name}.txt")
                    if os.path.exists(template_path):
                        with open(template_path) as f:
                            current_template = f.read().strip()
                    else:
                        continue

                sample_texts = "\n---\n".join(
                    s["content"][:400] for s in samples
                )

                prompt = (
                    f"Here is a prompt template for generating Reddit "
                    f"'{post_type}' posts:\n\n"
                    f"CURRENT TEMPLATE:\n{current_template}\n\n"
                    f"Here are high-performing posts generated from "
                    f"similar templates:\n{sample_texts}\n\n"
                    f"Suggest an IMPROVED version of the template that "
                    f"captures what makes these posts successful. "
                    f"Keep the same {{variable}} placeholders like "
                    f"{{subreddit}}, {{promotional_instruction}}, "
                    f"{{business_context}}, {{research_context}}, {{topic}}. "
                    f"Output ONLY the new template text, nothing else."
                )

                try:
                    new_template = self.llm.generate(
                        prompt=prompt,
                        system_prompt="You are a Reddit content strategy "
                                      "expert. Improve templates while "
                                      "keeping the same variable placeholders.",
                        max_tokens=500,
                        task="analytical",
                    )

                    # Validate placeholders are preserved
                    required = ["{subreddit}", "{promotional_instruction}"]
                    if all(v in new_template for v in required):
                        self.db.log_knowledge(
                            project=project,
                            category="evolved_prompt",
                            topic=template_name,
                            content=new_template,
                            source="llm_evolution",
                            relevance_score=pt_row["avg_eng"],
                            metadata={
                                "version": 2,
                                "base_template": template_name,
                            },
                        )
                        self.db.log_prompt_evolution(
                            project=project,
                            template_name=template_name,
                            version=2,
                            change_description=(
                                f"Auto-evolved from {len(samples)} "
                                f"high-performing samples"
                            ),
                            perf_before=0.0,
                            perf_after=pt_row["avg_eng"],
                        )
                        evolved_count += 1
                        logger.info(
                            f"Evolved prompt: {template_name} for {project}"
                        )
                    else:
                        logger.debug(
                            f"Evolved prompt rejected: missing placeholders"
                        )

                except Exception as e:
                    logger.debug(f"Prompt evolution LLM failed: {e}")

        except Exception as e:
            logger.debug(f"Prompt evolution failed: {e}")

    # ── Query (used by strategy engine) ──────────────────────────────

    def get_scoring_boost(
        self, category: str, key: str, project: str,
    ) -> float:
        """Get the learned scoring boost for a subreddit/keyword/tone.

        Returns a multiplier: 1.0 = neutral, >1 = boost, <1 = penalty.
        """
        weights = self.db.get_learned_weights(category, project)
        for w in weights:
            if w["key"] == key:
                if w["sample_count"] < _MIN_SAMPLES:
                    return 1.0
                # Normalize: average weight = 1.0
                return max(0.3, min(w["weight"] / max(self._avg_weight(weights), 0.1), 3.0))
        return 1.0  # No data, neutral

    @staticmethod
    def _avg_weight(weights: List[Dict]) -> float:
        """Average weight across all items with enough samples."""
        valid = [w["weight"] for w in weights if w["sample_count"] >= _MIN_SAMPLES]
        return sum(valid) / len(valid) if valid else 1.0

    def get_best_tone(self, project: str) -> str:
        """Get the best-performing tone style for a project."""
        weights = self.db.get_learned_weights("tone", project)
        if weights and weights[0]["sample_count"] >= _MIN_SAMPLES:
            return weights[0]["key"]
        return "helpful_casual"  # default

    def get_optimal_promo_ratio(self, project: str) -> float:
        """Get the learned optimal promo ratio (0.0-1.0).

        If promotional posts get removed more, lower the ratio.
        If they get good engagement, increase it.
        """
        weights = self.db.get_learned_weights("content_type", project)
        promo_score = 0.0
        organic_score = 0.0
        for w in weights:
            if w["key"] == "promotional":
                promo_score = w["weight"]
            elif w["key"] == "organic":
                organic_score = w["weight"]

        if promo_score == 0 and organic_score == 0:
            return 0.2  # default 20%

        total = promo_score + organic_score
        if total == 0:
            return 0.2

        # Ratio = promotional share, clamped to [0.05, 0.4]
        ratio = promo_score / total
        return max(0.05, min(ratio, 0.4))

    def get_approved_discoveries(
        self, platform: str, project: str, discovery_type: str,
    ) -> List[str]:
        """Get approved discoveries (auto-approved or manually approved)."""
        rows = self.db.get_discoveries(
            platform=platform, project=project, status="approved",
        )
        return [r["value"] for r in rows if r["discovery_type"] == discovery_type]

    def get_optimal_post_type_weights(
        self, project: str, stage: str,
    ) -> Optional[Dict[str, float]]:
        """Get learned post-type weights merged with stage defaults.

        Returns None if not enough data (caller should use static defaults).
        """
        from core.content_gen import ContentGenerator
        defaults = ContentGenerator.POST_TYPE_WEIGHTS.get(
            stage, ContentGenerator.POST_TYPE_WEIGHTS["new"]
        )
        learned = self.db.get_learned_weights("post_type", project)
        if not learned or all(w["sample_count"] < _MIN_SAMPLES for w in learned):
            return None  # Not enough data

        result = dict(defaults)
        avg_w = self._avg_weight(learned)
        for w in learned:
            key = w["key"]
            if key in result and w["sample_count"] >= _MIN_SAMPLES:
                boost = w["weight"] / max(avg_w, 0.1)
                boost = max(0.3, min(boost, 3.0))
                result[key] = result[key] * boost

        # Normalize to sum = 1.0
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}
        return result

    def get_insights(self, project: str = "") -> Dict:
        """Get human-readable learning insights."""
        insights = {}

        # Best subreddits
        sub_weights = self.db.get_learned_weights("subreddit", project)
        insights["top_subreddits"] = [
            {"name": w["key"], "score": w["weight"],
             "samples": w["sample_count"], "avg_eng": w["avg_engagement"]}
            for w in sub_weights[:10]
            if w["sample_count"] >= _MIN_SAMPLES
        ]

        # Best keywords
        kw_weights = self.db.get_learned_weights("keyword", project)
        insights["top_keywords"] = [
            {"name": w["key"], "score": w["weight"], "samples": w["sample_count"]}
            for w in kw_weights[:10]
            if w["sample_count"] >= _MIN_SAMPLES
        ]

        # Best tone
        insights["best_tone"] = self.get_best_tone(project)
        insights["optimal_promo_ratio"] = self.get_optimal_promo_ratio(project)

        # Pending discoveries
        discoveries = self.db.get_discoveries(
            project=project, status="candidate",
        )
        insights["pending_discoveries"] = len(discoveries)

        return insights
