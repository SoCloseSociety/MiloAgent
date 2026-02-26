"""Relationship Engine — build genuine connections and manage DMs.

Tracks per-user relationship stages:
  noticed → engaged → warm → friend → advocate

Safety:
  - Max 3 DMs per platform per day
  - Never DM cold — only after public interactions
  - Min 48h between DMs to same user
  - No promo on first contact
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.database import Database
from core.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

MAX_DMS_PER_PLATFORM_PER_DAY = 3
MIN_HOURS_BETWEEN_DMS = 48
STAGE_ORDER = ["noticed", "engaged", "warm", "friend", "advocate"]


class RelationshipEngine:
    """Manages user relationships and DM conversations.

    Usage:
        engine = RelationshipEngine(db, llm, content_gen)
        engine.run_relationship_cycle(project, reddit_bot, twitter_bot)
    """

    def __init__(self, db: Database, llm: LLMProvider, content_gen=None):
        self.db = db
        self.llm = llm
        self.content_gen = content_gen
        self._prompts: Dict[str, str] = {}

    def _load_prompt(self, name: str) -> str:
        """Load a prompt template by name."""
        if name not in self._prompts:
            try:
                import os
                path = os.path.join("prompts", name)
                with open(path) as f:
                    self._prompts[name] = f.read()
            except FileNotFoundError:
                logger.warning(f"Prompt template not found: {name}")
                self._prompts[name] = ""
        return self._prompts[name]

    # ── Target Identification ────────────────────────────────────────

    def identify_targets(
        self, project: Dict, platform: str = "reddit",
    ) -> List[str]:
        """Find users worth building relationships with.

        Sources:
        1. Authors of posts we commented on (from recent actions)
        2. Users who replied to our comments (from verification data)
        """
        proj_name = project.get("project", {}).get("name", "unknown")
        new_targets = []

        # Get recent actions to find post authors we interacted with
        try:
            recent = self.db.get_recent_actions(
                hours=72, platform=platform, limit=30,
            )
            for action in recent:
                if action.get("action_type") != "comment":
                    continue
                if not action.get("success"):
                    continue

                # Parse metadata for post author
                meta = {}
                metadata_raw = action.get("metadata", "")
                if isinstance(metadata_raw, str) and metadata_raw:
                    try:
                        meta = json.loads(metadata_raw)
                    except Exception:
                        continue

                author = meta.get("post_author", "")
                if not author or author == "[deleted]" or author == "AutoModerator":
                    continue

                account = action.get("account", "")
                if author.lower() == account.lower():
                    continue  # Don't befriend ourselves

                # Check if relationship already exists
                existing = self.db.get_relationship(platform, author, account)
                if existing:
                    # Update interaction count
                    self.db.upsert_relationship(
                        platform, author, account, proj_name,
                        public_interactions=existing.get("public_interactions", 0) + 1,
                    )
                    continue

                # Create new relationship
                self.db.upsert_relationship(
                    platform, author, account, proj_name,
                    stage="noticed",
                    public_interactions=1,
                )
                new_targets.append(author)

        except Exception as e:
            logger.debug(f"Target identification failed: {e}")

        if new_targets:
            logger.info(
                f"Relationships: identified {len(new_targets)} new targets "
                f"on {platform} for {proj_name}"
            )

        return new_targets

    def fetch_user_profile(
        self, platform: str, username: str, bot=None,
    ):
        """Fetch and cache a user profile from the platform."""
        # Check if we already have a recent profile
        existing = self.db.get_user_profile(platform, username)
        if existing:
            last = existing.get("last_updated", "")
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now() - last_dt).days < 7:
                    return existing  # Fresh enough
            except Exception:
                pass

        if not bot:
            return existing

        try:
            if platform == "reddit" and hasattr(bot, "get_user_about"):
                info = bot.get_user_about(username)
                if info:
                    self.db.upsert_user_profile(
                        platform, username,
                        display_name=info.get("name", username),
                        bio=info.get("subreddit", {}).get("public_description", ""),
                        karma=info.get("link_karma", 0) + info.get("comment_karma", 0),
                        account_age_days=max(0, int(
                            (time.time() - info.get("created_utc", time.time())) / 86400
                        )),
                    )
            elif platform == "twitter" and hasattr(bot, "get_user_by_name"):
                info = bot.get_user_by_name(username)
                if info:
                    self.db.upsert_user_profile(
                        platform, username,
                        display_name=info.get("name", username),
                        bio=info.get("bio", ""),
                        followers=info.get("followers_count", 0),
                    )
        except Exception as e:
            logger.debug(f"Failed to fetch profile for {username}: {e}")

        return self.db.get_user_profile(platform, username)

    # ── Stage Advancement ────────────────────────────────────────────

    def advance_relationships(self, project: Dict):
        """Check all relationships and advance stages where criteria are met."""
        proj_name = project.get("project", {}).get("name", "unknown")

        for stage in STAGE_ORDER[:-1]:  # Don't iterate on 'advocate'
            rels = self.db.get_relationships_by_stage(proj_name, stage)
            for rel in rels:
                new_stage = self._check_advancement(rel)
                if new_stage and new_stage != rel["stage"]:
                    self.db.advance_relationship_stage(rel["id"], new_stage)
                    logger.info(
                        f"Relationship advanced: {rel['username']} "
                        f"{rel['stage']} -> {new_stage}"
                    )
                    # Schedule next action for newly warm relationships
                    if new_stage == "warm":
                        next_time = (
                            datetime.now() + timedelta(hours=random.randint(12, 48))
                        ).isoformat()
                        self.db.upsert_relationship(
                            rel["platform"], rel["username"],
                            rel["our_account"], proj_name,
                            next_action="dm_first",
                            next_action_after=next_time,
                        )

    def _check_advancement(self, rel: Dict) -> Optional[str]:
        """Check if a relationship should advance to the next stage."""
        stage = rel.get("stage", "noticed")
        public = rel.get("public_interactions", 0)
        dms_sent = rel.get("dms_sent", 0)
        dms_received = rel.get("dms_received", 0)
        first = rel.get("first_interaction", "")

        # Calculate days known
        days_known = 0
        try:
            first_dt = datetime.fromisoformat(first)
            days_known = (datetime.now() - first_dt).days
        except Exception:
            pass

        if stage == "noticed" and public >= 2:
            return "engaged"
        elif stage == "engaged" and public >= 4 and days_known >= 7:
            return "warm"
        elif stage == "warm" and dms_sent >= 1 and dms_received >= 1 and days_known >= 14:
            return "friend"
        elif stage == "friend" and dms_sent >= 3 and dms_received >= 2 and days_known >= 30:
            return "advocate"

        return None

    # ── DM Decision & Generation ─────────────────────────────────────

    def should_dm(self, rel: Dict, platform: str) -> bool:
        """Check if we should DM this user now."""
        if rel.get("is_blocked"):
            return False

        stage = rel.get("stage", "noticed")
        if stage not in ("warm", "friend", "advocate"):
            return False

        # Check daily cap
        our_account = rel.get("our_account", "")
        today_count = self.db.get_dm_count_today(platform, our_account)
        if today_count >= MAX_DMS_PER_PLATFORM_PER_DAY:
            return False

        # Check minimum interval between DMs to this user
        history = self.db.get_conversation_history(rel["id"], limit=1)
        if history:
            last_sent = [m for m in history if m.get("direction") == "sent"]
            if last_sent:
                try:
                    last_ts = datetime.fromisoformat(last_sent[-1]["timestamp"])
                    hours_since = (datetime.now() - last_ts).total_seconds() / 3600
                    if hours_since < MIN_HOURS_BETWEEN_DMS:
                        return False
                except Exception:
                    pass

        return True

    def get_pending_dms(
        self, project: Dict, platform: str,
    ) -> List[Dict]:
        """Get relationships that are due for a DM."""
        proj_name = project.get("project", {}).get("name", "unknown")
        due = self.db.get_relationships_needing_action(proj_name, platform)
        return [
            r for r in due
            if r.get("next_action", "").startswith("dm_")
            and self.should_dm(r, platform)
        ]

    def generate_dm(
        self, rel: Dict, user_profile: Optional[Dict],
        project: Dict, platform: str = "reddit",
    ) -> Tuple[str, str]:
        """Generate a DM for a relationship.

        Returns (subject, body) for Reddit or ("", body) for Twitter.
        """
        stage = rel.get("stage", "warm")
        history = self.db.get_conversation_history(rel["id"], limit=10)

        # Build context
        their_interests = ""
        their_subs = ""
        their_bio = ""
        if user_profile:
            their_interests = user_profile.get("interests", "") or user_profile.get("bio", "")
            their_subs = user_profile.get("subreddits_active", "")
            their_bio = user_profile.get("bio", "")

        # Build interaction summary
        interaction_summary = f"{rel.get('public_interactions', 0)} public interactions"
        if rel.get("dms_sent", 0) > 0:
            interaction_summary += f", {rel['dms_sent']} DMs exchanged"

        # Build conversation history text
        conv_text = ""
        if history:
            for msg in history[-5:]:  # Last 5 messages
                who = "You" if msg["direction"] == "sent" else f"u/{rel['username']}"
                conv_text += f"{who}: {msg['content']}\n\n"

        # Build business context
        business_context = ""
        if self.content_gen and hasattr(self.content_gen, '_build_business_context'):
            try:
                business_context = self.content_gen._build_business_context(project)
            except Exception:
                pass

        # Choose prompt template
        promo_instruction = ""
        if platform == "reddit":
            if not history or stage == "warm":
                template = self._load_prompt("reddit_dm_first.txt")
            else:
                template = self._load_prompt("reddit_dm_followup.txt")
                if stage == "advocate":
                    promo_instruction = (
                        "You can casually mention the product if it fits "
                        "naturally in the conversation. Don't force it."
                    )
        else:
            template = self._load_prompt("twitter_dm.txt")
            if stage == "advocate":
                promo_instruction = (
                    "You can casually mention the product if it fits "
                    "naturally in the conversation."
                )

        if not template:
            return ("", "Hey! Just wanted to say I enjoyed our conversation.")

        # Fill template
        prompt = template.format(
            target_user=rel.get("username", ""),
            their_subreddits=their_subs or "various tech subs",
            their_interests=their_interests or "unknown",
            their_bio=their_bio or "",
            interaction_summary=interaction_summary,
            conversation_history=conv_text or "(first contact)",
            stage=stage,
            business_context=business_context,
            promotional_instruction=promo_instruction,
        )

        # Generate via LLM
        try:
            response = self.llm.generate(
                prompt, max_tokens=300, temperature=0.8,
                task="creative",
            )
            if not response:
                return ("", "")

            # Parse response
            if platform == "reddit" and "SUBJECT:" in response and "BODY:" in response:
                parts = response.split("BODY:", 1)
                subject = parts[0].replace("SUBJECT:", "").strip()
                body = parts[1].strip()
                return (subject, body)
            else:
                # Twitter or fallback
                cleaned = response.strip()
                # Remove any meta text
                for prefix in ["Here's", "Here is", "DM:", "Message:"]:
                    if cleaned.startswith(prefix):
                        cleaned = cleaned[len(prefix):].strip()
                return ("", cleaned)

        except Exception as e:
            logger.error(f"DM generation failed: {e}")
            return ("", "")

    # ── Inbox Processing ─────────────────────────────────────────────

    def process_inbox(
        self, platform: str, messages: List[Dict],
        our_account: str, project: str,
    ):
        """Process incoming DMs/messages from a platform inbox."""
        for msg in messages:
            author = msg.get("author", "")
            if not author or author.lower() == our_account.lower():
                continue

            # Find or create relationship
            rel = self.db.get_relationship(platform, author, our_account)
            if not rel:
                # New person DMing us — create relationship
                rel_id = self.db.upsert_relationship(
                    platform, author, our_account, project,
                    stage="engaged",
                    public_interactions=0,
                    dms_received=1,
                )
                rel = self.db.get_relationship(platform, author, our_account)
            else:
                rel_id = rel["id"]

            if not rel:
                continue

            # Check if we already logged this message
            history = self.db.get_conversation_history(rel["id"], limit=5)
            msg_id = msg.get("id", "")
            already_logged = any(
                h.get("message_id") == msg_id for h in history if msg_id
            )
            if already_logged:
                continue

            # Log the message
            self.db.log_conversation(
                relationship_id=rel["id"],
                platform=platform,
                direction="received",
                content=msg.get("body", msg.get("text", "")),
                subject=msg.get("subject", ""),
                message_id=msg_id,
            )

            # Schedule reply
            reply_time = (
                datetime.now() + timedelta(hours=random.randint(2, 12))
            ).isoformat()
            self.db.upsert_relationship(
                platform, author, our_account, project,
                next_action="dm_reply",
                next_action_after=reply_time,
            )

            logger.info(
                f"Received DM from {author} on {platform}: "
                f"{msg.get('body', '')[:50]}"
            )

    def generate_reply(
        self, rel: Dict, project: Dict, platform: str,
    ) -> str:
        """Generate a reply to an incoming DM."""
        user_profile = self.db.get_user_profile(platform, rel["username"])
        _, body = self.generate_dm(rel, user_profile, project, platform)
        return body

    # ── Main Cycle ───────────────────────────────────────────────────

    def run_relationship_cycle(
        self, project: Dict,
        reddit_bot=None, twitter_bot=None,
    ) -> Dict[str, int]:
        """Main relationship building cycle.

        Returns stats dict with counts of actions taken.
        """
        proj_name = project.get("project", {}).get("name", "unknown")
        stats = {
            "targets_found": 0,
            "advanced": 0,
            "dms_sent": 0,
            "replies_sent": 0,
            "inbox_processed": 0,
        }

        # 1. Identify new targets
        for platform, bot in [("reddit", reddit_bot), ("twitter", twitter_bot)]:
            if bot:
                targets = self.identify_targets(project, platform)
                stats["targets_found"] += len(targets)

                # Fetch profiles for new targets (limit to 5)
                for username in targets[:5]:
                    self.fetch_user_profile(platform, username, bot)
                    time.sleep(random.uniform(1, 3))

        # 2. Advance relationship stages
        self.advance_relationships(project)

        # 3. Check inbox on both platforms
        if reddit_bot and hasattr(reddit_bot, "check_inbox"):
            try:
                messages = reddit_bot.check_inbox(limit=15)
                if messages:
                    account = getattr(reddit_bot, "username", "")
                    self.process_inbox("reddit", messages, account, proj_name)
                    stats["inbox_processed"] += len(messages)
            except Exception as e:
                logger.debug(f"Reddit inbox check failed: {e}")

        if twitter_bot and hasattr(twitter_bot, "check_dms"):
            try:
                messages = twitter_bot.check_dms(limit=15)
                if messages:
                    account = getattr(twitter_bot, "username", "")
                    self.process_inbox("twitter", messages, account, proj_name)
                    stats["inbox_processed"] += len(messages)
            except Exception as e:
                logger.debug(f"Twitter DM check failed: {e}")

        # 4. Send pending DMs (max 3 per platform)
        for platform, bot in [("reddit", reddit_bot), ("twitter", twitter_bot)]:
            if not bot:
                continue

            pending = self.get_pending_dms(project, platform)
            sent = 0

            for rel in pending:
                if sent >= MAX_DMS_PER_PLATFORM_PER_DAY:
                    break

                user_profile = self.db.get_user_profile(platform, rel["username"])
                action = rel.get("next_action", "")

                if action == "dm_reply":
                    body = self.generate_reply(rel, project, platform)
                    subject = ""
                else:
                    subject, body = self.generate_dm(
                        rel, user_profile, project, platform
                    )

                if not body:
                    continue

                # Send the DM
                success = False
                try:
                    if platform == "reddit" and hasattr(bot, "send_dm"):
                        if action == "dm_reply":
                            # Get the message ID to reply to
                            history = self.db.get_conversation_history(rel["id"], limit=3)
                            received = [m for m in history if m["direction"] == "received"]
                            if received:
                                thing_id = received[-1].get("message_id", "")
                                if thing_id and hasattr(bot, "reply_to_dm"):
                                    success = bot.reply_to_dm(thing_id, body)
                        if not success:
                            success = bot.send_dm(
                                rel["username"], subject or "Hey!", body
                            )
                    elif platform == "twitter" and hasattr(bot, "send_dm"):
                        # Need user_id for Twitter
                        user_id = ""
                        if user_profile and user_profile.get("metadata"):
                            try:
                                meta = json.loads(user_profile["metadata"])
                                user_id = meta.get("user_id", "")
                            except Exception:
                                pass
                        if user_id:
                            success = bot.send_dm(user_id, body)

                except Exception as e:
                    logger.error(f"Failed to send DM to {rel['username']}: {e}")

                if success:
                    # Log conversation
                    self.db.log_conversation(
                        relationship_id=rel["id"],
                        platform=platform,
                        direction="sent",
                        content=body,
                        subject=subject,
                    )
                    # Clear next_action
                    self.db.upsert_relationship(
                        platform, rel["username"], rel["our_account"], proj_name,
                        next_action=None,
                        next_action_after=None,
                    )
                    sent += 1
                    stats["dms_sent"] += 1
                    logger.info(f"Sent DM to {rel['username']} on {platform}")

                    # Human-like delay between DMs
                    time.sleep(random.uniform(30, 120))

        # 5. Cleanup stale relationships
        try:
            self.db.cleanup_stale_relationships(days=60)
        except Exception:
            pass

        return stats
