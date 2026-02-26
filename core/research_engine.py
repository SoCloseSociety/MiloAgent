"""Research & Intelligence Engine — builds per-project knowledge bases.

Periodic research cycles:
1. Subreddit trend analysis (top posts, recurring themes, hot questions)
2. Industry news scanning (extends ContentCurator)
3. LLM synthesis of raw data into actionable talking points
4. Knowledge base management (insert, expire, deduplicate)
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from core.database import Database

logger = logging.getLogger(__name__)

# Knowledge entry TTLs (hours)
_TTL = {
    "trend": 48,
    "news": 72,
    "fact": 720,
    "talking_point": 168,
    "competitor_update": 168,
}

MAX_SUBREDDITS_PER_RESEARCH = 6
RESEARCH_TIMEOUT_SECONDS = 120


class ResearchEngine:
    """Builds and maintains a per-project knowledge base.

    Usage:
        engine = ResearchEngine(db, llm, curator)
        engine.run_research(project)              # periodic (every 12h)
        context = engine.get_context_for_topic(project, topic)  # for content_gen
        trends = engine.get_trending_topics(project)             # for strategy
    """

    def __init__(self, db: Database, llm=None, curator=None):
        self.db = db
        self.llm = llm
        self.curator = curator

    # ── Main Entry Point ──────────────────────────────────────────

    def run_research(self, project: Dict):
        """Run full research cycle for a project."""
        proj_name = project.get("project", {}).get("name", "unknown")
        logger.info(f"Research cycle starting for {proj_name}...")

        try:
            self._analyze_subreddit_trends(project)
        except Exception as e:
            logger.debug(f"Trend analysis failed for {proj_name}: {e}")

        try:
            self._scan_industry_news(project)
        except Exception as e:
            logger.debug(f"News scan failed for {proj_name}: {e}")

        try:
            self._synthesize_insights(project)
        except Exception as e:
            logger.debug(f"Insight synthesis failed for {proj_name}: {e}")

        # Cleanup expired entries
        try:
            self.db.cleanup_expired_knowledge()
        except Exception:
            pass

        logger.info(f"Research cycle complete for {proj_name}")

    # ── Subreddit Trend Analysis ──────────────────────────────────

    def _analyze_subreddit_trends(self, project: Dict):
        """Fetch hot posts from target subs, extract themes via LLM."""
        if not self.curator or not self.llm:
            return

        proj_name = project.get("project", {}).get("name", "unknown")
        reddit_config = project.get("reddit", {})
        subs = reddit_config.get("target_subreddits", {})

        all_subs = []
        if isinstance(subs, dict):
            all_subs = subs.get("primary", []) + subs.get("secondary", [])
        elif isinstance(subs, list):
            all_subs = subs

        # Limit and shuffle for variety
        sample = random.sample(all_subs, min(MAX_SUBREDDITS_PER_RESEARCH, len(all_subs)))

        for sub in sample:
            try:
                posts = self.curator.scrape_subreddit_hot(sub, limit=25)
                if not posts:
                    continue

                # Build summary for LLM
                post_summaries = []
                total_score = 0
                for p in posts[:15]:
                    title = p.get("title", "")[:100]
                    score = p.get("score", 0)
                    comments = p.get("num_comments", p.get("comments", 0))
                    total_score += score
                    post_summaries.append(
                        f"- [{score} pts, {comments} comments] {title}"
                    )

                if not post_summaries:
                    continue

                prompt = (
                    f"Analyze these top posts from r/{sub}:\n"
                    + "\n".join(post_summaries)
                    + "\n\nIdentify:\n"
                    "1. Top 3-5 recurring themes (short labels)\n"
                    "2. Top 3 common questions or pain points\n\n"
                    "Format:\n"
                    "THEMES: theme1, theme2, theme3\n"
                    "QUESTIONS: question1 | question2 | question3"
                )

                result = self.llm.generate(
                    prompt=prompt,
                    system_prompt="You analyze Reddit trends. Output only the requested format.",
                    max_tokens=200,
                    task="analytical",
                )

                themes, questions = self._parse_trends(result)

                if themes or questions:
                    avg_score = total_score / len(posts) if posts else 0
                    self.db.log_subreddit_trends(
                        subreddit=sub, project=proj_name,
                        themes=themes, questions=questions,
                        avg_score=round(avg_score, 1),
                        hot_count=len(posts),
                    )

                    # Store themes as knowledge entries
                    expires = (datetime.utcnow() + timedelta(hours=_TTL["trend"])).isoformat()
                    for theme in themes[:3]:
                        self.db.log_knowledge(
                            project=proj_name, category="trend",
                            topic=theme, content=f"Trending in r/{sub}: {theme}",
                            source=f"r/{sub}", expires_at=expires,
                        )

                    # Store questions as knowledge entries
                    for q in questions[:3]:
                        self.db.log_knowledge(
                            project=proj_name, category="trend",
                            topic=q[:50], content=f"Common question in r/{sub}: {q}",
                            source=f"r/{sub}", expires_at=expires,
                        )

                time.sleep(random.uniform(2, 4))

            except Exception as e:
                logger.debug(f"Trend analysis for r/{sub} failed: {e}")

    def _parse_trends(self, text: str):
        """Parse LLM trend output."""
        themes = []
        questions = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("THEMES:"):
                items = line.split(":", 1)[1].strip().split(",")
                themes = [t.strip() for t in items if t.strip()]
            elif line.upper().startswith("QUESTIONS:"):
                items = line.split(":", 1)[1].strip().split("|")
                questions = [q.strip() for q in items if q.strip()]
        return themes, questions

    # ── Industry News ─────────────────────────────────────────────

    def _scan_industry_news(self, project: Dict):
        """Scan news headlines and summarize into talking points."""
        if not self.curator or not self.llm:
            return

        proj_name = project.get("project", {}).get("name", "unknown")
        proj = project.get("project", project)
        description = proj.get("description", "")

        # Build search topics from keywords
        keywords = project.get("reddit", {}).get("keywords", [])
        topics = keywords[:3] if keywords else [description[:50]]

        articles = []
        for topic in topics:
            try:
                headlines = self.curator.scrape_news_headlines([topic])
                articles.extend(headlines[:3])
            except Exception:
                pass
            time.sleep(1)

        if not articles:
            return

        # Deduplicate
        seen = set()
        unique = []
        for a in articles:
            title = a.get("title", "")
            if title not in seen:
                seen.add(title)
                unique.append(a)

        # Summarize top articles via LLM
        article_text = "\n".join(
            f"- {a.get('title', '')} (from {a.get('source', 'unknown')})"
            for a in unique[:5]
        )

        prompt = (
            f"These are recent news articles relevant to '{description}':\n"
            f"{article_text}\n\n"
            f"For each article, give a one-sentence casual takeaway "
            f"that someone could reference in a Reddit conversation.\n"
            f"Format: one takeaway per line, no numbering."
        )

        try:
            result = self.llm.generate(
                prompt=prompt,
                system_prompt="You summarize tech news into casual talking points. Be brief.",
                max_tokens=300,
                task="analytical",
            )

            expires = (datetime.utcnow() + timedelta(hours=_TTL["news"])).isoformat()
            for line in result.strip().split("\n"):
                line = line.strip().lstrip("- •")
                if line and len(line) > 20:
                    self.db.log_knowledge(
                        project=proj_name, category="news",
                        topic=line[:50], content=line,
                        source="news_scan", expires_at=expires,
                    )
        except Exception as e:
            logger.debug(f"News synthesis failed: {e}")

    # ── Insight Synthesis ─────────────────────────────────────────

    def _synthesize_insights(self, project: Dict):
        """Use LLM to generate actionable talking points from research data."""
        if not self.llm:
            return

        proj_name = project.get("project", {}).get("name", "unknown")

        # Gather recent knowledge
        trends = self.db.get_knowledge(proj_name, category="trend", limit=10)
        news = self.db.get_knowledge(proj_name, category="news", limit=5)

        if not trends and not news:
            return

        context_items = []
        for t in trends[:5]:
            context_items.append(f"Trend: {t['content']}")
        for n in news[:3]:
            context_items.append(f"News: {n['content']}")

        prompt = (
            f"Based on this recent research for '{proj_name}':\n"
            + "\n".join(context_items)
            + "\n\nGenerate 3-5 casual talking points the bot can reference "
            f"naturally in Reddit conversations. Each should be a natural "
            f"factoid or observation — NOT promotional.\n"
            f"Format: one per line, no numbering."
        )

        try:
            result = self.llm.generate(
                prompt=prompt,
                system_prompt="You create natural conversation topics from research data. Be casual.",
                max_tokens=300,
                task="analytical",
            )

            expires = (datetime.utcnow() + timedelta(hours=_TTL["talking_point"])).isoformat()
            for line in result.strip().split("\n"):
                line = line.strip().lstrip("- •")
                if line and len(line) > 15:
                    self.db.log_knowledge(
                        project=proj_name, category="talking_point",
                        topic=line[:50], content=line,
                        source="synthesis", expires_at=expires,
                    )
        except Exception as e:
            logger.debug(f"Insight synthesis failed: {e}")

    # ── Query (used by content_gen) ───────────────────────────────

    def get_context_for_topic(
        self, project_name: str, topic: str,
    ) -> str:
        """Get research context to inject into LLM prompt.

        Returns a formatted string or empty string if no context available.
        """
        entries = []

        # Get recent trends
        trends = self.db.get_knowledge(
            project_name, category="trend", limit=3, max_age_hours=72,
        )
        for t in trends:
            entries.append(f"- Trend: {t['content']}")
            self.db.mark_knowledge_used(t["id"])

        # Get recent news
        news = self.db.get_knowledge(
            project_name, category="news", limit=2, max_age_hours=96,
        )
        for n in news:
            entries.append(f"- News: {n['content']}")
            self.db.mark_knowledge_used(n["id"])

        # Get talking points
        points = self.db.get_knowledge(
            project_name, category="talking_point", limit=2, max_age_hours=168,
        )
        for p in points:
            entries.append(f"- Insight: {p['content']}")
            self.db.mark_knowledge_used(p["id"])

        if not entries:
            return ""

        # Limit to 5 entries to keep prompt small
        entries = entries[:5]
        return (
            "CURRENT CONTEXT (reference naturally if relevant, don't force it):\n"
            + "\n".join(entries)
        )

    def get_trending_topics(self, project_name: str) -> List[str]:
        """Get current trending topics for seed post generation."""
        trends = self.db.get_knowledge(
            project_name, category="trend", limit=10, max_age_hours=72,
        )
        return [t["topic"] for t in trends]
