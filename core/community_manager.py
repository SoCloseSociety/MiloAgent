"""Community Manager — Full lifecycle management for owned subreddits.

Manages: creation → setup → moderation → growth → takeover.

Setup pipeline for new subreddits:
1. Configure settings (sidebar, description, submission rules)
2. Create community rules (4-6 rules)
3. Create flair templates (Discussion, Question, Tool, Guide, News)
4. Configure AutoModerator (anti-spam, domain whitelist)
5. Create and pin welcome post (slot 1)
6. Create and pin rules/about post (slot 2)

Ongoing management:
- Auto-moderate the mod queue (LLM-assisted)
- Refresh pinned posts weekly
- Distinguish mod comments
- Cross-promote between owned subs

Takeover:
- Find abandoned subreddits in our niche
- Score them for takeover potential
- Submit r/redditrequest claims
- Track request status
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default flair templates per subreddit
DEFAULT_FLAIRS = [
    {"text": "Discussion", "css_class": "discussion"},
    {"text": "Question", "css_class": "question"},
    {"text": "Tool / Resource", "css_class": "tool"},
    {"text": "Guide / Tutorial", "css_class": "guide"},
    {"text": "News", "css_class": "news"},
]

# Safety limits
MAX_MOD_ACTIONS_PER_CYCLE = 20
MAX_RULES_PER_SUB = 10
TAKEOVER_REQUEST_COOLDOWN_DAYS = 15  # Reddit's minimum between r/redditrequest posts


class CommunityManager:
    """Manages owned subreddit lifecycle: creation → setup → moderation → growth."""

    def __init__(self, db, llm, content_gen, hub_manager, intel):
        self.db = db
        self.llm = llm
        self.content_gen = content_gen
        self.hub_manager = hub_manager
        self.intel = intel
        self._ensure_tables()

    def _ensure_tables(self):
        """Create tracking tables if not exists."""
        try:
            with self.db._lock:
                self.db.conn.executescript("""
                    CREATE TABLE IF NOT EXISTS community_setup_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subreddit TEXT NOT NULL,
                        project TEXT NOT NULL,
                        step TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        completed_at TEXT,
                        details TEXT,
                        UNIQUE(subreddit, step)
                    );

                    CREATE TABLE IF NOT EXISTS subreddit_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subreddit TEXT NOT NULL,
                        project TEXT NOT NULL,
                        account TEXT NOT NULL,
                        request_post_url TEXT,
                        submitted_at TEXT DEFAULT (datetime('now')),
                        status TEXT DEFAULT 'pending',
                        checked_at TEXT,
                        reason TEXT,
                        takeover_score REAL,
                        metadata TEXT,
                        UNIQUE(subreddit, account)
                    );

                    CREATE INDEX IF NOT EXISTS idx_setup_sub
                        ON community_setup_log(subreddit);
                    CREATE INDEX IF NOT EXISTS idx_requests_status
                        ON subreddit_requests(status);
                """)
        except Exception as e:
            logger.debug(f"Community tables init: {e}")

        # Extend subreddit_hubs table with new columns (safe ALTER)
        new_columns = [
            ("setup_complete", "INTEGER DEFAULT 0"),
            ("automod_configured", "INTEGER DEFAULT 0"),
            ("rules_count", "INTEGER DEFAULT 0"),
            ("flair_count", "INTEGER DEFAULT 0"),
            ("sticky_post_1", "TEXT"),
            ("sticky_post_2", "TEXT"),
            ("fullname", "TEXT"),
            ("ownership_type", "TEXT DEFAULT 'created'"),
            ("mod_queue_last_checked", "TEXT"),
        ]
        for col_name, col_type in new_columns:
            try:
                self.db.conn.execute(
                    f"ALTER TABLE subreddit_hubs ADD COLUMN {col_name} {col_type}"
                )
            except Exception:
                pass  # Column already exists

    # ── Subreddit Setup Pipeline ─────────────────────────────────────

    def get_setup_status(self, subreddit: str) -> Dict:
        """Check which setup steps have been completed for a subreddit."""
        steps = {}
        try:
            rows = self.db.conn.execute(
                "SELECT step, status FROM community_setup_log WHERE subreddit = ?",
                (subreddit,),
            ).fetchall()
            for row in rows:
                steps[row["step"]] = row["status"]
        except Exception:
            pass
        return steps

    def _mark_step(self, subreddit: str, project: str, step: str,
                   status: str = "completed", details: str = ""):
        """Mark a setup step as completed or failed."""
        try:
            self.db._execute_write(
                """INSERT INTO community_setup_log (subreddit, project, step, status, completed_at, details)
                   VALUES (?, ?, ?, ?, datetime('now'), ?)
                   ON CONFLICT(subreddit, step) DO UPDATE SET
                   status=excluded.status, completed_at=excluded.completed_at, details=excluded.details""",
                (subreddit, project, step, status, details),
            )
        except Exception as e:
            logger.debug(f"Mark step error: {e}")

    def setup_new_subreddit(self, reddit_bot, subreddit: str, project: Dict) -> bool:
        """Full setup of a newly created subreddit.

        Runs the complete pipeline: settings → rules → flairs → automod → stickies.
        Skips already-completed steps (idempotent).
        """
        proj_name = project.get("project", {}).get("name", "unknown")
        status = self.get_setup_status(subreddit)
        logger.info(f"Setting up r/{subreddit} for {proj_name} (done: {list(status.keys())})")

        # Generate config via LLM
        config = self._generate_subreddit_config(subreddit, project)
        if not config:
            logger.error(f"Failed to generate config for r/{subreddit}")
            return False

        success_count = 0

        # Step 1: Update settings (sidebar, description)
        if status.get("settings") != "completed":
            if self._apply_settings(reddit_bot, subreddit, config, proj_name):
                success_count += 1
            time.sleep(random.uniform(3, 8))

        # Step 2: Create rules
        if status.get("rules") != "completed":
            if self._apply_rules(reddit_bot, subreddit, config, proj_name):
                success_count += 1
            time.sleep(random.uniform(3, 8))

        # Step 3: Create flairs
        if status.get("flairs") != "completed":
            if self._apply_flairs(reddit_bot, subreddit, config, proj_name):
                success_count += 1
            time.sleep(random.uniform(3, 8))

        # Step 4: Configure AutoModerator
        if status.get("automod") != "completed":
            if self._apply_automod(reddit_bot, subreddit, config, project, proj_name):
                success_count += 1
            time.sleep(random.uniform(3, 8))

        # Step 5: Create and pin welcome post
        if status.get("welcome_post") != "completed":
            if self._create_welcome_post(reddit_bot, subreddit, config, project, proj_name):
                success_count += 1
            time.sleep(random.uniform(5, 15))

        # Step 6: Create and pin rules post
        if status.get("rules_post") != "completed":
            if self._create_rules_post(reddit_bot, subreddit, config, project, proj_name):
                success_count += 1

        # Mark overall setup complete
        all_done = all(
            self.get_setup_status(subreddit).get(s) == "completed"
            for s in ("settings", "rules", "flairs", "automod", "welcome_post", "rules_post")
        )
        if all_done:
            try:
                self.db._execute_write(
                    "UPDATE subreddit_hubs SET setup_complete = 1 WHERE subreddit = ?",
                    (subreddit,),
                )
            except Exception:
                pass
            logger.info(f"r/{subreddit} setup complete!")

        return all_done

    def _generate_subreddit_config(self, subreddit: str, project: Dict) -> Optional[Dict]:
        """Use LLM to generate appropriate settings for a new subreddit."""
        proj_info = project.get("project", {})
        proj_name = proj_info.get("name", "")
        desc = proj_info.get("description", "")
        audiences = proj_info.get("target_audiences", [])
        reddit_cfg = project.get("reddit", {})
        owned = reddit_cfg.get("owned_subreddits", [])

        # Find matching config for this subreddit
        sub_config = {}
        for s in owned:
            if s.get("name", "").lower() == subreddit.lower():
                sub_config = s
                break

        niche = sub_config.get("niche", desc)
        title = sub_config.get("title", f"r/{subreddit}")

        prompt = f"""You are setting up a new Reddit community: r/{subreddit}
Title: {title}
Niche: {niche}
Related project: {proj_name} — {desc}
Target audience: {', '.join(audiences[:5])}

Generate a complete subreddit configuration. Output in this EXACT format:

SIDEBAR:
[Write 200-400 words of sidebar markdown. Include: welcome message, what the community is about, useful links, how to participate. Make it welcoming and professional.]

PUBLIC_DESCRIPTION:
[One sentence, max 200 chars, for the subreddit's public description.]

RULES:
1. [Rule name] | [Brief description of the rule]
2. [Rule name] | [Brief description]
3. [Rule name] | [Brief description]
4. [Rule name] | [Brief description]
5. [Rule name] | [Brief description]

WELCOME_TITLE:
[Title for the pinned welcome post]

WELCOME_BODY:
[2-3 paragraphs welcoming new members, explaining the community purpose, and encouraging participation.]

Guidelines:
- Be genuine and community-focused, not corporate
- Rules should protect quality without being overly strict
- Include a rule that allows product/tool sharing with context (this lets us share our links)
- Include a no-spam rule (but define spam as low-effort, not all links)
- Write in English
"""

        try:
            response = self.llm.generate(prompt, task="creative", max_tokens=2000)
            return self._parse_config_response(response)
        except Exception as e:
            logger.error(f"Config generation failed: {e}")
            return None

    def _parse_config_response(self, response: str) -> Dict:
        """Parse LLM config response into structured dict."""
        config = {
            "sidebar": "",
            "public_description": "",
            "rules": [],
            "welcome_title": "",
            "welcome_body": "",
        }

        current_section = None
        buffer = []

        for line in response.split("\n"):
            stripped = line.strip()

            if stripped.startswith("SIDEBAR:"):
                if current_section and buffer:
                    config[current_section] = "\n".join(buffer).strip()
                current_section = "sidebar"
                buffer = [stripped[8:].strip()] if stripped[8:].strip() else []
            elif stripped.startswith("PUBLIC_DESCRIPTION:"):
                if current_section and buffer:
                    config[current_section] = "\n".join(buffer).strip()
                current_section = "public_description"
                rest = stripped[19:].strip()
                buffer = [rest] if rest else []
            elif stripped.startswith("RULES:"):
                if current_section and buffer:
                    config[current_section] = "\n".join(buffer).strip()
                current_section = "rules_raw"
                buffer = []
            elif stripped.startswith("WELCOME_TITLE:"):
                if current_section == "rules_raw" and buffer:
                    config["rules"] = self._parse_rules(buffer)
                elif current_section and buffer:
                    config[current_section] = "\n".join(buffer).strip()
                current_section = "welcome_title"
                rest = stripped[14:].strip()
                buffer = [rest] if rest else []
            elif stripped.startswith("WELCOME_BODY:"):
                if current_section and buffer:
                    val = "\n".join(buffer).strip()
                    if current_section == "rules_raw":
                        config["rules"] = self._parse_rules(buffer)
                    else:
                        config[current_section] = val
                current_section = "welcome_body"
                buffer = [stripped[13:].strip()] if stripped[13:].strip() else []
            else:
                buffer.append(line)

        # Flush last section
        if current_section and buffer:
            val = "\n".join(buffer).strip()
            if current_section == "rules_raw":
                config["rules"] = self._parse_rules(buffer)
            else:
                config[current_section] = val

        # Fallback rules if none parsed
        if not config["rules"]:
            config["rules"] = [
                {"name": "Be respectful", "desc": "Treat everyone with respect. No personal attacks or harassment."},
                {"name": "Stay on topic", "desc": "Posts should be relevant to the community's focus."},
                {"name": "No low-effort spam", "desc": "No drive-by link dumps. Share links with context and genuine discussion."},
                {"name": "Share tools with context", "desc": "Product/tool recommendations are welcome when accompanied by real experience or comparison."},
                {"name": "Use flairs", "desc": "Tag your posts with the appropriate flair for easy browsing."},
            ]

        return config

    def _parse_rules(self, lines: List[str]) -> List[Dict]:
        """Parse numbered rules from LLM output."""
        rules = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Remove number prefix: "1. " or "1) "
            for prefix in range(1, 11):
                for fmt in (f"{prefix}. ", f"{prefix}) ", f"{prefix}- "):
                    if line.startswith(fmt):
                        line = line[len(fmt):]
                        break

            # Split on " | " or " - " for name|desc
            for sep in (" | ", " — ", " – ", " - "):
                if sep in line:
                    parts = line.split(sep, 1)
                    rules.append({"name": parts[0].strip(), "desc": parts[1].strip()})
                    break
            else:
                if line and len(line) < 100:
                    rules.append({"name": line, "desc": ""})

        return rules[:MAX_RULES_PER_SUB]

    def _apply_settings(self, reddit_bot, subreddit: str, config: Dict,
                        proj_name: str) -> bool:
        """Apply subreddit settings (sidebar, description)."""
        sidebar = config.get("sidebar", "")
        public_desc = config.get("public_description", "")[:500]

        success = reddit_bot.update_subreddit_settings(
            subreddit,
            description=sidebar,
            public_description=public_desc,
            submit_text="Please use appropriate flair and be constructive!",
        )
        if success:
            self._mark_step(subreddit, proj_name, "settings", "completed", sidebar[:200])
            logger.info(f"r/{subreddit}: settings applied")
        else:
            self._mark_step(subreddit, proj_name, "settings", "failed")
        return success

    def _apply_rules(self, reddit_bot, subreddit: str, config: Dict,
                     proj_name: str) -> bool:
        """Create community rules."""
        rules = config.get("rules", [])
        created = 0
        for rule in rules:
            if reddit_bot.add_subreddit_rule(
                subreddit,
                short_name=rule["name"],
                description=rule.get("desc", ""),
                kind="all",
            ):
                created += 1
                time.sleep(random.uniform(1, 3))

        if created > 0:
            self._mark_step(subreddit, proj_name, "rules", "completed",
                            json.dumps({"count": created}))
            try:
                self.db._execute_write(
                    "UPDATE subreddit_hubs SET rules_count = ? WHERE subreddit = ?",
                    (created, subreddit),
                )
            except Exception:
                pass
            logger.info(f"r/{subreddit}: {created} rules created")
            return True
        self._mark_step(subreddit, proj_name, "rules", "failed")
        return False

    def _apply_flairs(self, reddit_bot, subreddit: str, config: Dict,
                      proj_name: str) -> bool:
        """Create flair templates."""
        created = 0
        for flair in DEFAULT_FLAIRS:
            if reddit_bot.set_flair_template(
                subreddit,
                text=flair["text"],
                css_class=flair["css_class"],
                flair_type="LINK_FLAIR",
            ):
                created += 1
                time.sleep(random.uniform(0.5, 2))

        if created > 0:
            self._mark_step(subreddit, proj_name, "flairs", "completed",
                            json.dumps({"count": created}))
            try:
                self.db._execute_write(
                    "UPDATE subreddit_hubs SET flair_count = ? WHERE subreddit = ?",
                    (created, subreddit),
                )
            except Exception:
                pass
            logger.info(f"r/{subreddit}: {created} flairs created")
            return True
        self._mark_step(subreddit, proj_name, "flairs", "failed")
        return False

    def _apply_automod(self, reddit_bot, subreddit: str, config: Dict,
                       project: Dict, proj_name: str) -> bool:
        """Configure AutoModerator rules via wiki page."""
        reddit_cfg = project.get("reddit", {})
        allowed_domains = reddit_cfg.get("allowed_domains", [])

        # Get our account usernames for auto-approve
        from safety.account_manager import AccountManager
        our_accounts = []
        try:
            accs = self.db.conn.execute(
                "SELECT DISTINCT account FROM actions WHERE platform = 'reddit' LIMIT 10"
            ).fetchall()
            our_accounts = [a["account"] for a in accs]
        except Exception:
            pass

        automod_yaml = self._build_automod_config(
            subreddit, our_accounts, allowed_domains, proj_name,
        )

        success = reddit_bot.edit_wiki_page(
            subreddit,
            page="config/automoderator",
            content=automod_yaml,
            reason="Initial AutoModerator configuration",
        )

        if success:
            self._mark_step(subreddit, proj_name, "automod", "completed")
            try:
                self.db._execute_write(
                    "UPDATE subreddit_hubs SET automod_configured = 1 WHERE subreddit = ?",
                    (subreddit,),
                )
            except Exception:
                pass
            logger.info(f"r/{subreddit}: AutoModerator configured")
        else:
            self._mark_step(subreddit, proj_name, "automod", "failed")
        return success

    def _build_automod_config(self, subreddit: str, our_accounts: List[str],
                              allowed_domains: List[str], proj_name: str) -> str:
        """Build AutoModerator YAML config."""
        # Auto-approve posts from our accounts
        accounts_str = ", ".join(f'"{a}"' for a in our_accounts) if our_accounts else ""
        domains_str = ", ".join(f'"{d}"' for d in allowed_domains) if allowed_domains else ""

        config_parts = []

        # Rule 1: Auto-approve our accounts
        if accounts_str:
            config_parts.append(f"""# Auto-approve posts from community managers
author:
    name: [{accounts_str}]
action: approve""")

        # Rule 2: Allow our project domains
        if domains_str:
            config_parts.append(f"""# Allow project-related domains
domain+body+url (includes): [{domains_str}]
action: approve""")

        # Rule 3: Anti-spam for new accounts with links
        config_parts.append("""# Filter potential spam from very new accounts
type: submission
author:
    combined_karma: "< 5"
    account_age: "< 2 days"
action: filter
action_reason: "New account with low karma — held for review"
""")

        # Rule 4: Remove shortened links (common spam)
        config_parts.append("""# Remove shortened URL spam
domain+body+url (includes): ["bit.ly", "tinyurl.com", "t.co", "goo.gl", "shorturl.at"]
action: remove
comment: "Shortened URLs are not allowed. Please use the full URL instead."
""")

        return "\n---\n\n".join(config_parts)

    def _create_welcome_post(self, reddit_bot, subreddit: str, config: Dict,
                             project: Dict, proj_name: str) -> bool:
        """Create and pin the welcome post (sticky slot 1)."""
        title = config.get("welcome_title", f"Welcome to r/{subreddit}!")
        body = config.get("welcome_body", "")
        if not body:
            body = (
                f"Welcome to r/{subreddit}! This is a community for sharing knowledge, "
                f"tools, and experiences. Feel free to ask questions, share resources, "
                f"and help fellow members.\n\n"
                f"**Please read the rules** before posting, and use flairs to tag your posts."
            )

        url = reddit_bot.create_post(subreddit, title, body, project)
        if url:
            # Extract thing_id from URL and pin it
            post_id = self._extract_post_id(url)
            if post_id:
                time.sleep(random.uniform(2, 5))
                reddit_bot.sticky_post(post_id, state=True, num=1)
                reddit_bot.distinguish_comment(post_id, how="yes")
                try:
                    self.db._execute_write(
                        "UPDATE subreddit_hubs SET sticky_post_1 = ? WHERE subreddit = ?",
                        (url, subreddit),
                    )
                except Exception:
                    pass

            self._mark_step(subreddit, proj_name, "welcome_post", "completed", url)
            logger.info(f"r/{subreddit}: welcome post created and pinned")
            return True

        self._mark_step(subreddit, proj_name, "welcome_post", "failed")
        return False

    def _create_rules_post(self, reddit_bot, subreddit: str, config: Dict,
                           project: Dict, proj_name: str) -> bool:
        """Create and pin the rules/about post (sticky slot 2)."""
        rules = config.get("rules", [])
        rules_text = "\n".join(
            f"**{i+1}. {r['name']}** — {r.get('desc', '')}"
            for i, r in enumerate(rules)
        )
        title = f"r/{subreddit} Rules & Guidelines"
        body = (
            f"# Community Rules\n\n{rules_text}\n\n"
            f"---\n\n"
            f"These rules help keep the community productive and welcoming. "
            f"Violations may result in post removal or a temporary ban.\n\n"
            f"*If you have questions about these rules, comment below or message the mods.*"
        )

        url = reddit_bot.create_post(subreddit, title, body, project)
        if url:
            post_id = self._extract_post_id(url)
            if post_id:
                time.sleep(random.uniform(2, 5))
                reddit_bot.sticky_post(post_id, state=True, num=2)
                reddit_bot.distinguish_comment(post_id, how="yes")
                try:
                    self.db._execute_write(
                        "UPDATE subreddit_hubs SET sticky_post_2 = ? WHERE subreddit = ?",
                        (url, subreddit),
                    )
                except Exception:
                    pass

            self._mark_step(subreddit, proj_name, "rules_post", "completed", url)
            logger.info(f"r/{subreddit}: rules post created and pinned")
            return True

        self._mark_step(subreddit, proj_name, "rules_post", "failed")
        return False

    def _extract_post_id(self, url: str) -> Optional[str]:
        """Extract Reddit post fullname (t3_xxx) from a URL.

        URL format: https://www.reddit.com/r/sub/comments/abc123/title/
        """
        if not url:
            return None
        try:
            parts = url.rstrip("/").split("/")
            # Find 'comments' and take the next part
            for i, p in enumerate(parts):
                if p == "comments" and i + 1 < len(parts):
                    return f"t3_{parts[i + 1]}"
        except Exception:
            pass
        return None

    # ── Ongoing Moderation ───────────────────────────────────────────

    def moderate_subreddit(self, reddit_bot, subreddit: str) -> Dict:
        """Review and moderate items in the mod queue.

        Uses LLM to decide: approve / remove / ignore.
        Returns stats {approved, removed, ignored}.
        """
        stats = {"approved": 0, "removed": 0, "ignored": 0}

        queue = reddit_bot.get_mod_queue(subreddit, limit=25)
        if not queue:
            return stats

        logger.info(f"r/{subreddit}: {len(queue)} items in mod queue")

        for item in queue[:MAX_MOD_ACTIONS_PER_CYCLE]:
            decision = self._auto_moderate_item(item, subreddit)

            if decision == "approve":
                if reddit_bot.approve_item(item["id"]):
                    stats["approved"] += 1
            elif decision == "remove":
                if reddit_bot.remove_item(item["id"], spam=False):
                    stats["removed"] += 1
            else:
                stats["ignored"] += 1

            time.sleep(random.uniform(1, 3))

        # Update last checked timestamp
        try:
            self.db._execute_write(
                "UPDATE subreddit_hubs SET mod_queue_last_checked = datetime('now') WHERE subreddit = ?",
                (subreddit,),
            )
        except Exception:
            pass

        if stats["approved"] or stats["removed"]:
            logger.info(
                f"r/{subreddit} moderation: {stats['approved']} approved, "
                f"{stats['removed']} removed, {stats['ignored']} ignored"
            )
        return stats

    def _auto_moderate_item(self, item: Dict, subreddit: str) -> str:
        """Use LLM to decide moderation action for a single item.

        Returns 'approve' | 'remove' | 'ignore'
        """
        author = item.get("author", "unknown")
        title = item.get("title", "")
        body = item.get("body", "")[:500]
        reports = item.get("num_reports", 0)
        content = title + "\n" + body if title else body

        # Quick heuristics before calling LLM
        if not content.strip():
            return "remove"  # Empty content
        if reports >= 3:
            return "remove"  # Multiple reports = likely spam

        prompt = f"""You are moderating r/{subreddit}. Decide what to do with this item.

Author: u/{author}
Content: {content[:400]}
Reports: {reports}
User reports: {item.get('user_reports', [])}

Decide: APPROVE (quality content), REMOVE (spam/off-topic/rule-breaking), or IGNORE (borderline, needs human review).

Reply with exactly one word: APPROVE, REMOVE, or IGNORE."""

        try:
            response = self.llm.generate(prompt, task="analytical", max_tokens=10)
            response = response.strip().upper()
            if "APPROVE" in response:
                return "approve"
            elif "REMOVE" in response:
                return "remove"
            return "ignore"
        except Exception:
            return "ignore"  # Default to not acting if LLM fails

    # ── Content Management ───────────────────────────────────────────

    def refresh_stickied_posts(self, reddit_bot, subreddit: str,
                               project: Dict) -> bool:
        """Refresh pinned posts with new content (weekly rotation).

        Creates a new weekly discussion thread and pins it in slot 1,
        keeping the rules post in slot 2.
        """
        proj_name = project.get("project", {}).get("name", "unknown")
        now = datetime.utcnow()
        week_str = now.strftime("%B %d, %Y")

        title = f"Weekly Discussion Thread — {week_str}"
        body = (
            f"Welcome to this week's discussion thread! Share what you've been working on, "
            f"ask questions, or start a conversation about anything related to our community.\n\n"
            f"---\n\n"
            f"**New here?** Check the pinned rules post and introduce yourself!\n\n"
            f"**Have a tool or resource to share?** Post it with context about your experience."
        )

        url = reddit_bot.create_post(subreddit, title, body, project)
        if url:
            post_id = self._extract_post_id(url)
            if post_id:
                time.sleep(random.uniform(2, 5))
                reddit_bot.sticky_post(post_id, state=True, num=1)
                reddit_bot.distinguish_comment(post_id, how="yes")
                try:
                    self.db._execute_write(
                        "UPDATE subreddit_hubs SET sticky_post_1 = ? WHERE subreddit = ?",
                        (url, subreddit),
                    )
                except Exception:
                    pass
                logger.info(f"r/{subreddit}: weekly discussion thread pinned")
                return True
        return False

    def should_refresh_stickies(self, hub: Dict, refresh_days: int = 7) -> bool:
        """Check if stickied posts need refreshing."""
        sticky_url = hub.get("sticky_post_1", "")
        if not sticky_url:
            return True  # No sticky = needs one

        last_checked = hub.get("mod_queue_last_checked")
        if not last_checked:
            return True

        try:
            last_dt = datetime.fromisoformat(last_checked)
            return (datetime.utcnow() - last_dt) > timedelta(days=refresh_days)
        except Exception:
            return True

    # ── Takeover System ──────────────────────────────────────────────

    def score_takeover_potential(self, reddit_bot, subreddit: str,
                                project: Dict) -> Dict:
        """Score a subreddit for takeover viability (0-10).

        Factors: mod activity, dormancy, subscriber count, niche relevance.
        """
        result = {
            "subreddit": subreddit,
            "score": 0.0,
            "method": "unknown",
            "eligible": False,
            "dormancy_days": 0,
            "reasoning": "",
            "mod_activity": [],
        }

        # Get subreddit info
        about = reddit_bot.get_subreddit_about(subreddit)
        if not about:
            result["reasoning"] = "Cannot access subreddit info"
            return result

        subscribers = about.get("subscribers", 0)
        created_utc = about.get("created_utc", 0)
        sub_type = about.get("subreddit_type", "public")

        if sub_type != "public":
            result["reasoning"] = f"Not public ({sub_type})"
            return result

        # Check mod activity
        mods = reddit_bot.get_subreddit_moderators(subreddit)
        mod_activity = []
        all_inactive = True

        for mod in mods:
            mod_info = reddit_bot.get_user_about(mod["name"])
            if mod_info:
                # Check if account still exists
                if mod_info.get("is_suspended"):
                    mod_activity.append({"name": mod["name"], "status": "suspended"})
                    continue

                # Rough activity check via comment karma changes
                created = mod_info.get("created_utc", 0)
                if created:
                    age_days = (time.time() - created) / 86400
                    # If account exists and isn't suspended, check last activity
                    # Reddit doesn't expose last_active directly, so we estimate
                    mod_activity.append({
                        "name": mod["name"],
                        "status": "active" if age_days < 365 else "unknown",
                        "karma": mod_info.get("link_karma", 0) + mod_info.get("comment_karma", 0),
                    })
                    all_inactive = False
            else:
                mod_activity.append({"name": mod["name"], "status": "deleted_or_suspended"})
            time.sleep(random.uniform(1, 3))

        result["mod_activity"] = mod_activity

        # Score calculation
        score = 0.0
        reasons = []

        # 1. Subscriber value (logarithmic, 20%)
        if subscribers > 0:
            import math
            sub_score = min(10, math.log10(max(subscribers, 1)) * 2.5)
            score += sub_score * 0.20
            reasons.append(f"subscribers={subscribers} ({sub_score:.1f}/10)")

        # 2. Mod situation (30%)
        if len(mods) == 0:
            mod_score = 10.0
            reasons.append("no mods")
        elif all_inactive or all(m.get("status") in ("suspended", "deleted_or_suspended") for m in mod_activity):
            mod_score = 9.0
            reasons.append("all mods inactive/suspended")
        elif len(mods) == 1:
            mod_score = 7.0
            reasons.append("single mod")
        elif len(mods) <= 3:
            mod_score = 4.0
            reasons.append(f"{len(mods)} mods")
        else:
            mod_score = 2.0
            reasons.append(f"{len(mods)} mods (many)")
        score += mod_score * 0.30

        # 3. Niche relevance (30%)
        proj_info = project.get("project", {})
        reddit_cfg = project.get("reddit", {})
        keywords = reddit_cfg.get("keywords", [])
        sub_desc = (about.get("public_description", "") + " " + about.get("title", "")).lower()
        sub_name = subreddit.lower()

        matches = sum(1 for kw in keywords if kw.lower() in sub_desc or kw.lower() in sub_name)
        relevance = min(10, matches * 3)
        score += relevance * 0.30
        reasons.append(f"keyword_matches={matches}")

        # 4. Age bonus — older subs have more SEO value (20%)
        if created_utc:
            age_years = (time.time() - created_utc) / (365.25 * 86400)
            age_score = min(10, age_years * 2)
            score += age_score * 0.20
            reasons.append(f"age={age_years:.1f}y")

        result["score"] = round(min(10, score), 1)
        result["reasoning"] = "; ".join(reasons)

        # Determine method
        if all_inactive or len(mods) == 0:
            result["method"] = "redditrequest"
            result["eligible"] = True
        elif len(mods) == 1:
            result["method"] = "contact_mod"
            result["eligible"] = True
        else:
            result["method"] = "organic_growth"
            result["eligible"] = False

        return result

    def find_takeover_targets(self, reddit_bot, project: Dict,
                              limit: int = 5) -> List[Dict]:
        """Search for abandoned subreddits in the project's niche.

        Uses Reddit search to find relevant subreddits, then scores them.
        """
        proj_info = project.get("project", {})
        reddit_cfg = project.get("reddit", {})
        keywords = reddit_cfg.get("keywords", [])[:10]
        audiences = proj_info.get("target_audiences", [])

        # Build search queries from keywords + audiences
        search_terms = list(set(
            keywords[:5] + [a.replace("_", " ") for a in audiences[:3]]
        ))

        candidates = []
        seen = set()

        for term in search_terms[:5]:
            try:
                resp = reddit_bot.session.get(
                    f"{reddit_bot.REDDIT_BASE if hasattr(reddit_bot, 'REDDIT_BASE') else 'https://www.reddit.com'}"
                    f"/subreddits/search.json?q={term}&limit=10&sort=relevance",
                    headers={"User-Agent": reddit_bot.session.headers.get("User-Agent", ""),
                             "Accept": "application/json"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    for child in data.get("children", []):
                        d = child.get("data", {})
                        name = d.get("display_name", "")
                        if name.lower() not in seen:
                            seen.add(name.lower())
                            subs = d.get("subscribers", 0)
                            # Filter: at least 50 subscribers, not huge (< 100k)
                            if 50 <= subs <= 100000:
                                candidates.append(name)
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                logger.debug(f"Takeover search error for '{term}': {e}")

        # Score each candidate
        scored = []
        for sub in candidates[:limit * 2]:
            try:
                result = self.score_takeover_potential(reddit_bot, sub, project)
                if result["score"] >= 3.0:  # Only track promising ones
                    scored.append(result)
                time.sleep(random.uniform(2, 5))
            except Exception as e:
                logger.debug(f"Takeover scoring error for r/{sub}: {e}")

        # Sort by score descending
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def submit_redditrequest(self, reddit_bot, target_sub: str,
                             project: Dict, account: Dict) -> Optional[str]:
        """Submit a request to r/redditrequest to claim an abandoned subreddit.

        Requirements (Reddit's rules):
        - Account 90+ days old with 300+ combined karma
        - Target sub has inactive mods for 60+ days
        - Max 1 request per 15 days per account

        Returns the post URL or None.
        """
        username = account.get("username", "")
        proj_name = project.get("project", {}).get("name", "unknown")

        # Check if we already have a pending request for this sub
        try:
            existing = self.db.conn.execute(
                "SELECT * FROM subreddit_requests WHERE subreddit = ? AND status = 'pending'",
                (target_sub,),
            ).fetchone()
            if existing:
                logger.info(f"Already have pending request for r/{target_sub}")
                return None
        except Exception:
            pass

        # Check cooldown: 15 days between requests per account
        try:
            recent = self.db.conn.execute(
                """SELECT submitted_at FROM subreddit_requests
                   WHERE account = ? AND submitted_at > datetime('now', '-15 days')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (username,),
            ).fetchone()
            if recent:
                logger.info(f"Account {username} submitted a request recently, waiting")
                return None
        except Exception:
            pass

        # Verify account eligibility
        user_info = reddit_bot.get_user_info()
        if not user_info:
            logger.warning("Cannot verify account eligibility for r/redditrequest")
            return None

        karma = user_info.get("link_karma", 0) + user_info.get("comment_karma", 0)
        created = user_info.get("created_utc", 0)
        age_days = (time.time() - created) / 86400 if created else 0

        if age_days < 90:
            logger.warning(f"Account {username} too young for r/redditrequest ({age_days:.0f} days)")
            return None
        if karma < 300:
            logger.warning(f"Account {username} too low karma for r/redditrequest ({karma})")
            return None

        # Generate request message
        title = f"Requesting r/{target_sub} — inactive moderators, no activity"
        body = self._generate_request_message(target_sub, project)

        # Submit to r/redditrequest
        url = reddit_bot.create_post("redditrequest", title, body, project)
        if url:
            # Track in DB
            try:
                self.db._execute_write(
                    """INSERT INTO subreddit_requests
                       (subreddit, project, account, request_post_url, status, takeover_score)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (target_sub, proj_name, username, url, 0),
                )
            except Exception as e:
                logger.debug(f"Failed to track request: {e}")

            logger.info(f"Submitted r/redditrequest for r/{target_sub}: {url}")
            return url

        return None

    def _generate_request_message(self, target_sub: str, project: Dict) -> str:
        """Generate a genuine-sounding r/redditrequest message."""
        proj_info = project.get("project", {})

        prompt = f"""Write a brief r/redditrequest post body to claim the abandoned subreddit r/{target_sub}.

Context: This subreddit has inactive moderators and no recent activity. I want to revive it as a community for {proj_info.get('description', 'relevant topics')}.

Rules for the request:
- Be genuine and brief (3-5 sentences)
- Mention that moderators are inactive
- State your plans to revive the community (post quality content, enforce rules, grow membership)
- Sound like a real Reddit user, not a business
- Do NOT mention any product or company

Write ONLY the post body (no title)."""

        try:
            return self.llm.generate(prompt, task="creative", max_tokens=200)
        except Exception:
            return (
                f"I'd like to take over r/{target_sub} as the current moderators appear to be inactive. "
                f"I plan to clean up the subreddit, add proper rules and flairs, "
                f"and post quality content regularly to build an active community. "
                f"I have experience moderating communities in this niche."
            )

    def check_request_status(self, reddit_bot, request_post_url: str) -> str:
        """Check if a r/redditrequest has been approved/denied.

        Returns 'pending' | 'approved' | 'denied' | 'unknown'.
        """
        # TODO: Parse the request post for admin responses
        # For now, this is a manual check
        return "pending"

    def get_pending_requests(self) -> List[Dict]:
        """Get all pending r/redditrequest submissions."""
        try:
            rows = self.db.conn.execute(
                "SELECT * FROM subreddit_requests WHERE status = 'pending' ORDER BY submitted_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_all_managed_communities(self) -> List[Dict]:
        """Get all owned/managed communities with their status."""
        try:
            rows = self.db.conn.execute(
                """SELECT h.*,
                   (SELECT COUNT(*) FROM community_setup_log
                    WHERE subreddit = h.subreddit AND status = 'completed') as steps_done
                   FROM subreddit_hubs h
                   WHERE h.status = 'active'
                   ORDER BY h.created_at DESC"""
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
