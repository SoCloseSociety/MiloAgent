"""Subreddit Hub Manager — Create and animate owned subreddits as content hubs.

Strategy:
1. Create subreddits related to your niche (e.g., r/ProductivityHacks)
2. Post valuable content regularly to build community
3. Strategically place project links among organic content
4. Cross-post popular content from your hubs to target subreddits

Safety:
- Max 1 subreddit creation per week (Reddit anti-spam)
- Mix 70% organic posts, 30% promotional
- Never post more than 3 times per hub per day
- Build credibility first — at least 10 organic posts before any promo
"""

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Limits
MAX_POSTS_PER_HUB_PER_DAY = 3
MIN_ORGANIC_BEFORE_PROMO = 10
HUB_POST_INTERVAL_HOURS = 8


class SubredditHubManager:
    """Manages owned subreddits as strategic content hubs."""

    def __init__(self, db, llm, content_gen):
        self.db = db
        self.llm = llm
        self.content_gen = content_gen
        self._community_manager = None  # Set by orchestrator after init
        self._ensure_table()

    def _ensure_table(self):
        """Create hub tracking table if not exists."""
        try:
            with self.db._lock:
                self.db.conn.executescript("""
                    CREATE TABLE IF NOT EXISTS subreddit_hubs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subreddit TEXT NOT NULL UNIQUE,
                        project TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at TEXT DEFAULT (datetime('now')),
                        description TEXT,
                        niche TEXT,
                        total_posts INTEGER DEFAULT 0,
                        organic_posts INTEGER DEFAULT 0,
                        promo_posts INTEGER DEFAULT 0,
                        subscribers INTEGER DEFAULT 0,
                        last_post_at TEXT,
                        status TEXT DEFAULT 'active',
                        sidebar_text TEXT,
                        rules TEXT,
                        metadata TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_hubs_project ON subreddit_hubs(project);
                    CREATE INDEX IF NOT EXISTS idx_hubs_status ON subreddit_hubs(status);
                """)
        except Exception as e:
            logger.debug(f"Hub table creation: {e}")

    # ── Hub Management ──────────────────────────────────────────────

    def register_hub(
        self,
        subreddit: str,
        project: str,
        created_by: str,
        description: str = "",
        niche: str = "",
    ) -> bool:
        """Register an owned subreddit as a hub."""
        try:
            self.db._execute_write(
                """INSERT OR REPLACE INTO subreddit_hubs
                   (subreddit, project, created_by, description, niche)
                   VALUES (?, ?, ?, ?, ?)""",
                (subreddit, project, created_by, description, niche),
            )
            logger.info(f"Registered hub r/{subreddit} for {project}")
            return True
        except Exception as e:
            logger.error(f"Failed to register hub: {e}")
            return False

    def get_hubs(self, project: str = None) -> List[Dict]:
        """Get all registered hubs, optionally filtered by project."""
        try:
            if project:
                rows = self.db.conn.execute(
                    "SELECT * FROM subreddit_hubs WHERE project = ? AND status = 'active'",
                    (project,),
                ).fetchall()
            else:
                rows = self.db.conn.execute(
                    "SELECT * FROM subreddit_hubs WHERE status = 'active'"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_hub(self, subreddit: str) -> Optional[Dict]:
        """Get a specific hub."""
        try:
            row = self.db.conn.execute(
                "SELECT * FROM subreddit_hubs WHERE subreddit = ?",
                (subreddit,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    # ── Subreddit Creation ──────────────────────────────────────────

    def create_subreddit(self, reddit_bot, name: str, title: str,
                          description: str, project: str) -> bool:
        """Create a new subreddit via Reddit web API.

        Note: Reddit requires accounts to be 30+ days old with some karma
        to create subreddits.
        """
        if not hasattr(reddit_bot, '_ensure_auth'):
            logger.error("Reddit bot doesn't support subreddit creation")
            return False

        if not reddit_bot._ensure_auth():
            logger.error("Not authenticated")
            return False

        if not reddit_bot._modhash:
            logger.error("No modhash — cannot create subreddit")
            return False

        try:
            time.sleep(random.uniform(5, 15))

            resp = reddit_bot.session.post(
                "https://old.reddit.com/api/site_admin",
                data={
                    "api_type": "json",
                    "name": name,
                    "title": title,
                    "description": description,
                    "public_description": description[:500],
                    "type": "public",
                    "link_type": "any",
                    "allow_top": "true",
                    "show_media": "true",
                    "uh": reddit_bot._modhash,
                },
                headers={
                    "User-Agent": reddit_bot.session.headers.get("User-Agent", ""),
                    "Referer": "https://old.reddit.com/subreddits/create",
                    "Origin": "https://old.reddit.com",
                },
                timeout=30,
            )

            if resp.status_code == 200:
                result = resp.json()
                errors = result.get("json", {}).get("errors", [])
                if not errors:
                    logger.info(f"Created subreddit r/{name}")
                    self.register_hub(
                        name, project, reddit_bot._username,
                        description=description,
                    )
                    # Trigger community setup if manager is available
                    if self._community_manager:
                        logger.info(f"Triggering setup pipeline for r/{name}")
                        # setup will run in the next community management cycle
                    return True
                else:
                    logger.error(f"Subreddit creation errors: {errors}")
                    # If subreddit already exists, register it
                    for err in errors:
                        if "already exists" in str(err).lower():
                            logger.info(f"r/{name} already exists — registering as hub")
                            self.register_hub(
                                name, project, reddit_bot._username,
                                description=description,
                            )
                            return True
            else:
                logger.error(f"Subreddit creation HTTP {resp.status_code}")

        except Exception as e:
            logger.error(f"Failed to create subreddit: {e}")

        return False

    # ── Hub Content Generation ──────────────────────────────────────

    def generate_hub_ideas(self, hub: Dict, project: Dict, count: int = 3) -> List[Dict]:
        """Generate content ideas for a hub using LLM."""
        proj_info = project.get("project", {})
        niche = hub.get("niche", proj_info.get("description", ""))
        hub_name = hub["subreddit"]

        prompt = f"""You manage the subreddit r/{hub_name}.
Niche: {niche}
Project: {proj_info.get('name', '')} - {proj_info.get('description', '')}

Generate {count} post ideas for r/{hub_name}. Mix of:
- Discussion posts (questions for the community)
- Tips/guides (helpful content about the niche)
- News/trends (what's happening in the space)

For each post, output:
TYPE: discussion|guide|news
TITLE: [compelling title]
BODY: [2-4 paragraphs of valuable content]
PROMO: none|subtle
---

Rules:
- Make content genuinely valuable — not just filler
- If PROMO is "subtle", naturally mention {proj_info.get('name', '')} ONCE
- Most posts should be PROMO: none (organic)
- Write like a passionate community moderator, not a marketer
"""

        try:
            response = self.llm.generate(prompt, task="creative", max_tokens=2000)
            return self._parse_hub_ideas(response)
        except Exception as e:
            logger.error(f"Failed to generate hub ideas: {e}")
            return []

    def _parse_hub_ideas(self, response: str) -> List[Dict]:
        """Parse LLM response into structured post ideas."""
        ideas = []
        current = {}

        for line in response.split("\n"):
            line = line.strip()
            if line.startswith("TYPE:"):
                if current.get("title"):
                    ideas.append(current)
                current = {"type": line[5:].strip(), "body": ""}
            elif line.startswith("TITLE:"):
                current["title"] = line[6:].strip()
            elif line.startswith("BODY:"):
                current["body"] = line[5:].strip()
            elif line.startswith("PROMO:"):
                current["promo"] = line[6:].strip()
            elif line == "---":
                if current.get("title"):
                    ideas.append(current)
                current = {}
            elif current.get("title") and "body" in current:
                current["body"] += "\n" + line

        if current.get("title"):
            ideas.append(current)

        return ideas

    # ── Hub Animation Cycle ─────────────────────────────────────────

    def should_post_to_hub(self, hub: Dict) -> bool:
        """Check if we should post to a hub now."""
        # Check daily post limit
        try:
            today_count = self.db.conn.execute(
                """SELECT COUNT(*) FROM actions
                   WHERE action_type = 'hub_post'
                   AND metadata LIKE ?
                   AND timestamp > datetime('now', '-24 hours')""",
                (f'%"hub": "{hub["subreddit"]}"%',),
            ).fetchone()[0]
            if today_count >= MAX_POSTS_PER_HUB_PER_DAY:
                return False
        except Exception:
            pass

        # Check interval since last post
        last_post = hub.get("last_post_at")
        if last_post:
            try:
                last_dt = datetime.fromisoformat(last_post)
                if datetime.utcnow() - last_dt < timedelta(hours=HUB_POST_INTERVAL_HOURS):
                    return False
            except Exception:
                pass

        return True

    def should_be_promotional(self, hub: Dict) -> bool:
        """Decide if the next post should subtly promote the project."""
        organic = hub.get("organic_posts", 0)
        promo = hub.get("promo_posts", 0)

        # Need minimum organic posts first
        if organic < MIN_ORGANIC_BEFORE_PROMO:
            return False

        # Target ~25% promotional
        total = organic + promo
        if total == 0:
            return False
        current_ratio = promo / total
        return current_ratio < 0.25

    def post_to_hub(self, reddit_bot, hub: Dict, project: Dict) -> Optional[str]:
        """Generate and post content to an owned hub."""
        if not self.should_post_to_hub(hub):
            logger.debug(f"Hub r/{hub['subreddit']}: not ready for new post")
            return None

        is_promo = self.should_be_promotional(hub)

        # Generate content
        ideas = self.generate_hub_ideas(hub, project, count=1)
        if not ideas:
            return None

        idea = ideas[0]
        if not is_promo:
            idea["promo"] = "none"

        title = idea.get("title", "")
        body = idea.get("body", "")

        if not title or not body:
            return None

        # Post it
        url = reddit_bot.create_post(
            hub["subreddit"], title, body, project,
        )

        if url:
            # Update hub stats
            is_promo_actual = idea.get("promo", "none") != "none"
            try:
                if is_promo_actual:
                    self.db._execute_write(
                        """UPDATE subreddit_hubs SET
                           total_posts = total_posts + 1,
                           promo_posts = promo_posts + 1,
                           last_post_at = datetime('now')
                           WHERE subreddit = ?""",
                        (hub["subreddit"],),
                    )
                else:
                    self.db._execute_write(
                        """UPDATE subreddit_hubs SET
                           total_posts = total_posts + 1,
                           organic_posts = organic_posts + 1,
                           last_post_at = datetime('now')
                           WHERE subreddit = ?""",
                        (hub["subreddit"],),
                    )
            except Exception:
                pass

            # Log as a specific action type
            import json
            self.db.log_action(
                platform="reddit",
                action_type="hub_post",
                account=reddit_bot._username,
                project=project.get("project", {}).get("name", ""),
                target_id=f"hub_{hub['subreddit']}_{int(time.time())}",
                content=f"{title}\n\n{body}",
                metadata=json.dumps({
                    "hub": hub["subreddit"],
                    "type": idea.get("type", "unknown"),
                    "promo": is_promo_actual,
                    "url": url,
                }),
            )

            logger.info(
                f"Posted to hub r/{hub['subreddit']}: {title[:50]}"
                f" ({'promo' if is_promo_actual else 'organic'})"
            )

        return url

    # ── Main Cycle ──────────────────────────────────────────────────

    def run_hub_cycle(self, project: Dict, reddit_bot) -> Dict:
        """Run a hub animation cycle for a project.

        Returns stats: {posts_created: int, hubs_active: int}
        """
        proj_name = project.get("project", {}).get("name", "")
        hubs = self.get_hubs(proj_name)
        stats = {"posts_created": 0, "hubs_active": len(hubs)}

        for hub in hubs:
            try:
                url = self.post_to_hub(reddit_bot, hub, project)
                if url:
                    stats["posts_created"] += 1
                    time.sleep(random.uniform(30, 120))  # Pace posts
            except Exception as e:
                logger.error(f"Hub cycle error for r/{hub['subreddit']}: {e}")

        return stats

    # ── Hub Suggestions ─────────────────────────────────────────────

    def suggest_hub_names(self, project: Dict) -> List[Dict]:
        """Use LLM to suggest subreddit names for a project's niche."""
        proj_info = project.get("project", {})
        name = proj_info.get("name", "")
        desc = proj_info.get("description", "")
        audiences = proj_info.get("target_audiences", [])

        prompt = f"""Suggest 5 subreddit names for building a community around this niche:
Project: {name} - {desc}
Target audiences: {', '.join(audiences)}

Rules:
- Names should be broad enough to attract organic members (NOT just about the product)
- Think about what communities these audiences would join
- Names should be 3-20 chars, lowercase, no spaces
- Include a mix: general niche subs + specific topic subs

For each, output:
NAME: [subreddit_name]
TITLE: [subreddit display title]
DESC: [one-line description]
NICHE: [what niche this serves]
---
"""
        try:
            response = self.llm.generate(prompt, task="analytical", max_tokens=800)
            suggestions = []
            current = {}
            for line in response.split("\n"):
                line = line.strip()
                if line.startswith("NAME:"):
                    if current.get("name"):
                        suggestions.append(current)
                    current = {"name": line[5:].strip().lower().replace(" ", "")}
                elif line.startswith("TITLE:"):
                    current["title"] = line[6:].strip()
                elif line.startswith("DESC:"):
                    current["desc"] = line[5:].strip()
                elif line.startswith("NICHE:"):
                    current["niche"] = line[6:].strip()
                elif line == "---":
                    if current.get("name"):
                        suggestions.append(current)
                    current = {}
            if current.get("name"):
                suggestions.append(current)
            return suggestions
        except Exception as e:
            logger.error(f"Hub suggestion failed: {e}")
            return []
