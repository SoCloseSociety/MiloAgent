"""Telegram Group Engagement Bot — scans groups, replies to messages using Telethon.

Uses a real Telegram user account (not a bot) to participate in groups naturally.
Telethon handles authentication via phone + SMS code, persisted in session files.

Usage:
    python miloagent.py login telegram     # First-time auth (SMS code)
    python miloagent.py test telegram      # Verify connection
    python miloagent.py run                # Auto scan + act in groups
"""

import asyncio
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from platforms.base_platform import BasePlatform
from core.database import Database
from core.content_gen import ContentGenerator

logger = logging.getLogger(__name__)


# ── Persistent event loop for Telethon ────────────────────────────────
# Same pattern as TwitterBot: dedicated thread with a persistent loop
# to avoid "bound to a different event loop" errors.

_tg_loop: Optional[asyncio.AbstractEventLoop] = None
_tg_loop_lock = threading.Lock()
_tg_loop_thread: Optional[threading.Thread] = None


def _get_tg_loop() -> asyncio.AbstractEventLoop:
    """Get or create the persistent event loop for Telegram operations."""
    global _tg_loop, _tg_loop_thread
    with _tg_loop_lock:
        if _tg_loop is None or _tg_loop.is_closed():
            _tg_loop = asyncio.new_event_loop()

            def _run_loop(loop):
                asyncio.set_event_loop(loop)
                loop.run_forever()

            _tg_loop_thread = threading.Thread(
                target=_run_loop, args=(_tg_loop,), daemon=True
            )
            _tg_loop_thread.start()
    return _tg_loop


def _run_tg_async(coro, timeout: int = 120):
    """Run an async coroutine on the persistent Telegram loop."""
    loop = _get_tg_loop()
    if loop.is_closed():
        global _tg_loop
        _tg_loop = None
        loop = _get_tg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


class TelegramGroupBot(BasePlatform):
    """Telegram group scanner + engagement bot using Telethon.

    Inherits BasePlatform and implements scan(), act(), test_connection().
    Uses a real user account to participate in groups naturally.
    """

    def __init__(
        self,
        db: Database,
        content_gen: ContentGenerator,
        account_config: Dict,
    ):
        super().__init__(db, content_gen, account_config)
        self.account_config = account_config
        self.client = None  # Lazy init in authenticate()
        self._phone = account_config.get("phone", "unknown")
        self._username = self._phone  # Used for logging/account tracking
        self._authenticated = False
        self._session_file = account_config.get(
            "session_file", "data/sessions/telegram_user1.session"
        )
        self._api_id = account_config.get("api_id", "")
        self._api_hash = account_config.get("api_hash", "")
        self._max_messages_per_hour = account_config.get(
            "max_messages_per_hour", 5
        )
        self._send_timestamps: List[float] = []  # Track sends for rate limiting

        # Ensure session directory exists
        os.makedirs(os.path.dirname(self._session_file), exist_ok=True)

    # ── Authentication ─────────────────────────────────────────────────

    async def authenticate(self):
        """Connect Telethon client and load session.

        Session files persist authentication so SMS code is only needed once.
        """
        if self._authenticated and self.client and self.client.is_connected():
            return

        try:
            from telethon import TelegramClient
        except ImportError:
            raise ImportError(
                "Telethon not installed. Run:\n"
                "  pip install telethon"
            )

        if not self._api_id or not self._api_hash:
            raise ValueError(
                "Telegram api_id and api_hash not configured.\n"
                "  1. Go to https://my.telegram.org\n"
                "  2. Create an app to get api_id and api_hash\n"
                "  3. Add them to config/telegram_user_accounts.yaml"
            )

        if self.client is None:
            self.client = TelegramClient(
                self._session_file,
                int(self._api_id),
                self._api_hash,
            )

        if not self.client.is_connected():
            await self.client.connect()

        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram session not authorized. Run:\n"
                "  python miloagent.py login telegram"
            )

        me = await self.client.get_me()
        self._username = me.username or me.phone or self._phone
        self._authenticated = True
        logger.info(f"Telegram connected as @{self._username}")

    # ── Group Discovery & Auto-Join ─────────────────────────────────────

    async def _get_joined_groups(self) -> List[Dict]:
        """List all groups/supergroups the account is currently in."""
        await self.authenticate()
        from telethon.tl.types import Channel, Chat

        groups = []
        async for dialog in self.client.iter_dialogs(limit=200):
            entity = dialog.entity
            # Supergroups and megagroups (Channel with megagroup=True)
            if isinstance(entity, Channel) and not entity.broadcast:
                groups.append({
                    "id": entity.id,
                    "title": dialog.name,
                    "username": entity.username or "",
                    "participants": getattr(entity, "participants_count", 0),
                })
            # Small groups (Chat type)
            elif isinstance(entity, Chat):
                groups.append({
                    "id": entity.id,
                    "title": dialog.name,
                    "username": "",
                    "participants": getattr(entity, "participants_count", 0),
                })
        return groups

    async def discover_groups_async(self, keywords: List[str], max_results: int = 10) -> List[Dict]:
        """Search Telegram for public groups matching keywords.

        Uses Telegram's global search to find relevant groups to join.
        Returns list of group info dicts (not yet joined).
        """
        await self.authenticate()
        from telethon.tl.functions.contacts import SearchRequest
        from telethon.tl.types import Channel

        discovered = []
        already_joined = {g["id"] for g in await self._get_joined_groups()}

        for kw in keywords[:5]:  # Limit keyword searches to avoid rate limits
            try:
                result = await self.client(SearchRequest(q=kw, limit=20))
                for chat in result.chats:
                    if not isinstance(chat, Channel):
                        continue
                    if chat.broadcast:  # Skip channels (broadcasts), keep groups
                        continue
                    if chat.id in already_joined:
                        continue
                    # Only public groups (have username)
                    if not chat.username:
                        continue
                    discovered.append({
                        "id": chat.id,
                        "title": chat.title,
                        "username": chat.username,
                        "participants": getattr(chat, "participants_count", 0),
                    })
                await asyncio.sleep(random.uniform(2, 5))
            except Exception as e:
                logger.debug(f"Telegram search error for '{kw}': {e}")

        # Deduplicate by id
        seen = set()
        unique = []
        for g in discovered:
            if g["id"] not in seen:
                seen.add(g["id"])
                unique.append(g)

        unique.sort(key=lambda g: g.get("participants", 0), reverse=True)
        return unique[:max_results]

    async def auto_join_group(self, group_username: str) -> bool:
        """Join a public Telegram group by username."""
        await self.authenticate()
        from telethon.tl.functions.channels import JoinChannelRequest

        try:
            entity = await self.client.get_entity(group_username)
            await self.client(JoinChannelRequest(entity))
            logger.info(f"Joined Telegram group: @{group_username} ({entity.title})")
            try:
                self.db.log_action(
                    platform="telegram",
                    action_type="join_group",
                    account=self._username,
                    project="discovery",
                    target_id=str(entity.id),
                    content=f"Joined @{group_username}: {entity.title}",
                )
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning(f"Failed to join @{group_username}: {e}")
            return False

    async def discover_and_join_async(self, project: Dict, max_join: int = 3) -> Dict:
        """Full autonomous cycle: discover relevant groups and join them.

        Returns stats dict.
        """
        await self.authenticate()
        telegram_config = project.get("telegram", {})
        keywords = telegram_config.get("keywords", [])
        project_name = project.get("project", {}).get("name", "unknown")

        if not keywords:
            return {"discovered": 0, "joined": 0}

        # Discover groups
        discovered = await self.discover_groups_async(keywords, max_results=10)
        logger.info(
            f"Telegram discovery for {project_name}: "
            f"found {len(discovered)} candidate groups"
        )

        # Join top groups (by member count)
        joined = 0
        for group in discovered[:max_join]:
            if await self.auto_join_group(group["username"]):
                joined += 1
                # Don't spam joins — wait between each
                await asyncio.sleep(random.uniform(10, 30))

        return {"discovered": len(discovered), "joined": joined}

    def discover_and_join(self, project: Dict) -> Dict:
        """Sync wrapper for discover_and_join_async."""
        return _run_tg_async(self.discover_and_join_async(project))

    # ── Scan ───────────────────────────────────────────────────────────

    async def scan_async(self, project: Dict) -> List[Dict]:
        """Scan Telegram groups for engagement opportunities.

        Auto-mode: if target_groups is empty or set to ["auto"], scan ALL
        joined groups that match project keywords. No manual config needed.
        """
        await self.authenticate()

        opportunities = []
        telegram_config = project.get("telegram", {})
        if not telegram_config.get("enabled", False):
            return opportunities

        try:
            return await self._scan_groups(project, telegram_config)
        except Exception as e:
            import traceback
            logger.error(
                f"[TG-SCAN] Fatal scan error: {e}\n{traceback.format_exc()}"
            )
            return opportunities

    async def _scan_groups(self, project: Dict, telegram_config: Dict) -> List[Dict]:
        """Inner scan logic — isolated from DB contention errors."""
        opportunities = []

        keywords = telegram_config.get("keywords", [])
        exclude_keywords = telegram_config.get("exclude_keywords", [])
        target_groups_cfg = telegram_config.get("target_groups", [])
        project_name = project.get("project", {}).get("name", "unknown")
        max_age_minutes = telegram_config.get("max_message_age_minutes", 120)
        max_groups_per_scan = telegram_config.get("max_groups_per_scan", 8)
        seen_ids = set()

        # Pre-load acted IDs into memory (avoids per-message DB query)
        acted_ids = set()
        try:
            rows = self.db.conn.execute(
                "SELECT target_id FROM actions WHERE platform = 'telegram' AND success = 1"
            ).fetchall()
            acted_ids = {row[0] for row in rows}
        except Exception:
            pass
        logger.debug(f"[TG-SCAN] Loaded {len(acted_ids)} acted IDs")

        me = await self.client.get_me()
        logger.debug(f"[TG-SCAN] Got me: @{me.username or me.phone}")

        # Resolve which groups to scan
        groups_to_scan = []
        auto_mode = (
            not target_groups_cfg
            or target_groups_cfg == ["auto"]
            or "auto" in target_groups_cfg
        )

        if auto_mode:
            # Auto-discover: scan all joined groups (skip tiny chats)
            all_groups = await self._get_joined_groups()
            # Filter to groups with at least 10 members
            groups_to_scan = [
                g for g in all_groups
                if g.get("participants", 0) >= 10
            ]
            # Randomize to spread scanning across cycles
            random.shuffle(groups_to_scan)
            groups_to_scan = groups_to_scan[:max_groups_per_scan]
            logger.info(
                f"Telegram auto-scan: {len(groups_to_scan)} groups "
                f"(of {len(all_groups)} joined)"
            )
        else:
            # Manual mode: resolve each target_group
            for group_identifier in target_groups_cfg:
                try:
                    entity = await self.client.get_entity(group_identifier)
                    groups_to_scan.append({
                        "id": entity.id,
                        "title": getattr(entity, "title", str(group_identifier)),
                        "username": getattr(entity, "username", ""),
                    })
                except Exception as e:
                    logger.warning(
                        f"Cannot resolve Telegram group '{group_identifier}': {e}"
                    )

        for group_info in groups_to_scan:
            try:
                entity = await self.client.get_entity(group_info["id"])
                group_name = group_info.get("title", str(group_info["id"]))

                # Get recent messages
                messages = await self.client.get_messages(entity, limit=50)
                if not messages:
                    continue

                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=max_age_minutes
                )

                for msg in messages:
                    if not msg.text:
                        continue

                    # Skip old messages
                    if msg.date and msg.date < cutoff:
                        continue

                    # Build unique target_id
                    target_id = f"tg:{group_info['id']}:{msg.id}"
                    if target_id in seen_ids:
                        continue
                    seen_ids.add(target_id)

                    if target_id in acted_ids:
                        continue

                    text_lower = msg.text.lower()

                    # Check exclude keywords
                    if any(ek.lower() in text_lower for ek in exclude_keywords):
                        continue

                    # Check keyword relevance
                    keyword_match = ""
                    for kw in keywords:
                        if kw.lower() in text_lower:
                            keyword_match = kw
                            break

                    # Also include messages with question/help signals
                    has_intent = any(
                        sig in text_lower
                        for sig in ["?", "help", "how to", "looking for",
                                    "recommend", "suggest", "anyone know"]
                    )

                    if not keyword_match and not has_intent:
                        continue

                    # Get sender info
                    sender = await msg.get_sender()
                    author_name = ""
                    author_id = ""
                    if sender:
                        author_name = (
                            getattr(sender, "username", "")
                            or getattr(sender, "first_name", "")
                            or ""
                        )
                        author_id = str(getattr(sender, "id", ""))

                    # Skip our own messages
                    if sender and getattr(sender, "id", None) == me.id:
                        continue

                    # Get reply count (approximate via replies attribute)
                    reply_count = 0
                    if hasattr(msg, "replies") and msg.replies:
                        reply_count = getattr(msg.replies, "replies", 0)

                    opp = {
                        "platform": "telegram",
                        "target_id": target_id,
                        "title": msg.text[:100],
                        "body": msg.text,
                        "text": msg.text,
                        "group_id": str(group_info["id"]),
                        "group_name": group_name,
                        "message_id": msg.id,
                        "author_name": author_name,
                        "author_id": author_id,
                        "reply_count": reply_count,
                        "keyword": keyword_match,
                        "subreddit_or_query": group_name,
                    }

                    opp["relevance_score"] = self._score_opportunity(
                        opp, project
                    )

                    # Only keep opportunities above minimum score
                    if opp["relevance_score"] < 3.0:
                        continue

                    opportunities.append(opp)

            except Exception as e:
                logger.error(
                    f"Telegram scan error for group '{group_info.get('title', '?')}': {e}"
                )

            # Delay between groups to avoid rate limits
            await asyncio.sleep(random.uniform(2, 5))

        opportunities.sort(
            key=lambda x: x.get("relevance_score", 0), reverse=True
        )

        # Write opportunities to DB (thread-local connections prevent lock issues)
        logged = 0
        for opp in opportunities:
            try:
                self.db.log_opportunity(
                    platform="telegram",
                    target_id=opp["target_id"],
                    title=opp.get("title", "")[:100],
                    subreddit_or_query=opp.get("group_name", ""),
                    score=opp["relevance_score"],
                    project=project_name,
                    metadata={
                        "keyword": opp.get("keyword", ""),
                        "author_name": opp.get("author_name", ""),
                        "author_id": opp.get("author_id", ""),
                        "group_id": opp.get("group_id", ""),
                        "group_name": opp.get("group_name", ""),
                        "message_id": opp.get("message_id"),
                        "reply_count": opp.get("reply_count", 0),
                        "body": opp.get("body", "")[:500],
                        "text": opp.get("text", "")[:500],
                    },
                )
                logged += 1
            except Exception as e:
                logger.debug(f"DB write skip for {opp['target_id']}: {e}")

        logger.info(
            f"Telegram scan for {project_name}: "
            f"found {len(opportunities)} opportunities (logged {logged})"
        )
        return opportunities

    def _score_opportunity(self, opp: Dict, project: Dict) -> float:
        """Score a Telegram group opportunity 0-10."""
        score = 0.0
        text_lower = opp.get("text", "").lower()
        telegram_config = project.get("telegram", {})
        keywords = telegram_config.get("keywords", [])

        # Keyword matches (0-3)
        kw_score = 0.0
        for kw in keywords:
            if kw.lower() in text_lower:
                kw_score += 1.0
        score += min(kw_score, 3.0)

        # Question/help signals (0-2)
        question_signals = [
            "?", "how do", "how to", "what is", "which",
            "anyone know", "recommend", "looking for",
            "suggest", "alternative", "best way",
        ]
        if any(sig in text_lower for sig in question_signals):
            score += 1.0

        help_signals = [
            "struggling", "stuck", "doesn't work", "help",
            "need a tool", "what tool", "what app",
        ]
        if any(sig in text_lower for sig in help_signals):
            score += 1.0

        # Engagement level (0-1.5) — active threads have more visibility
        reply_count = opp.get("reply_count", 0) or 0
        if reply_count >= 5:
            score += 1.0
        elif reply_count >= 2:
            score += 0.5

        # Low competition bonus (0-1.5) — easier to stand out + get seen
        if reply_count <= 1:
            score += 1.5
        elif reply_count <= 3:
            score += 1.0
        elif reply_count <= 5:
            score += 0.5

        # Message length bonus (longer = more context to work with)
        text_len = len(opp.get("text", ""))
        if text_len >= 200:
            score += 0.5
        elif text_len >= 50:
            score += 0.25

        return min(score, 10.0)

    # ── Act ─────────────────────────────────────────────────────────────

    async def _act_async(self, opportunity: Dict, project: Dict) -> bool:
        """Generate reply and send it to the Telegram group."""
        await self.authenticate()
        project_name = project.get("project", {}).get("name", "unknown")

        try:
            is_promo = self.content_gen._should_be_promotional()

            # Use project-level persona (from telegram config), fallback to account-level
            tg_config = project.get("telegram", {})
            persona = tg_config.get("persona") or tg_config.get("message_style") or self.account_config.get("persona", "helpful_casual")
            if persona == "auto":
                persona = "helpful_casual"  # Default when auto

            reply_text = self.content_gen.generate_telegram_reply(
                message_text=opportunity.get("text", ""),
                group_name=opportunity.get("group_name", ""),
                project=project,
                author_name=opportunity.get("author_name", ""),
                persona=persona,
                is_promotional=is_promo,
            )

            if not reply_text:
                logger.warning("Empty Telegram reply generated, skipping")
                return False

            # Validate content if validator available
            reply_text = self._validate_content(reply_text, project)
            if not reply_text:
                logger.warning("Telegram content validation failed, skipping")
                return False

            # Enforce max_messages_per_hour
            now = time.time()
            self._send_timestamps = [
                t for t in self._send_timestamps if now - t < 3600
            ]
            if len(self._send_timestamps) >= self._max_messages_per_hour:
                logger.info(
                    f"Telegram rate limit: {len(self._send_timestamps)}/"
                    f"{self._max_messages_per_hour} msgs/hour — skipping"
                )
                return False
            self._send_timestamps.append(now)

            # Human-like reading + typing delay (Telegram is more sensitive)
            delay = self._human_delay(
                opportunity.get("text", ""), reply_text
            )
            logger.debug(f"Telegram human delay: {delay:.1f}s")
            await asyncio.sleep(delay)

            # Send reply to the group
            raw_gid = opportunity.get("group_id")
            if not raw_gid:
                logger.error("Telegram act: missing group_id in opportunity")
                return False
            group_id = int(raw_gid)
            message_id = opportunity.get("message_id")

            from telethon.errors import FloodWaitError

            try:
                await self.client.send_message(
                    group_id,
                    reply_text,
                    reply_to=message_id,
                )
            except FloodWaitError as e:
                logger.warning(
                    f"Telegram FloodWait: sleeping {e.seconds}s + jitter"
                )
                await asyncio.sleep(e.seconds + random.uniform(10, 30))
                # Retry once after flood wait
                await self.client.send_message(
                    group_id,
                    reply_text,
                    reply_to=message_id,
                )

            # Log success
            self.db.log_action(
                platform="telegram",
                action_type="reply",
                account=self._username,
                project=project_name,
                target_id=opportunity["target_id"],
                content=reply_text,
                metadata={
                    "group_name": opportunity.get("group_name", ""),
                    "group_id": opportunity.get("group_id", ""),
                    "author": opportunity.get("author_name", ""),
                },
            )
            self.db.update_opportunity_status(
                opportunity["target_id"], "acted"
            )
            logger.info(
                f"Telegram reply sent in {opportunity.get('group_name', '?')}: "
                f"{reply_text[:60]}..."
            )
            return True

        except Exception as e:
            logger.error(f"Telegram action failed: {e}")
            self.db.log_action(
                platform="telegram",
                action_type="reply",
                account=self._username,
                project=project_name,
                target_id=opportunity.get("target_id", "failed"),
                content="",
                success=False,
                error_message=str(e),
            )
            return False

    def _validate_content(
        self, content: str, project: Dict
    ) -> Optional[str]:
        """Validate generated content before sending."""
        try:
            from core.content_validator import ContentValidator
            validator = ContentValidator()
            is_valid, score, issues = validator.validate(
                content, project, "telegram"
            )
            if is_valid:
                return content
            logger.info(
                f"Telegram validation: score={score:.2f}, issues={issues}"
            )
            if score >= 0.4:
                return content
            return None
        except Exception:
            return content

    def _human_delay(self, original_text: str, reply_text: str) -> float:
        """Calculate human-like delay for reading + typing in Telegram.

        Telegram users typically respond slower than Twitter, faster than Reddit.
        """
        # Reading time: ~250 wpm casual reading, 60% speed for scanning
        read_words = len(original_text.split())
        read_time = (read_words / 250) * 60 * 0.6

        # Think time
        think_time = random.uniform(5, 20)

        # Typing time: ~50 wpm with variance
        reply_words = len(reply_text.split())
        type_time = (reply_words / 50) * 60 * random.uniform(0.7, 1.3)

        base_delay = read_time + think_time + type_time
        delay = base_delay * random.uniform(0.8, 1.4)

        # Telegram: 30s minimum, 120s maximum
        return max(30.0, min(delay, 120.0))

    # ── Warm-up ────────────────────────────────────────────────────────

    async def warm_up_async(self, project: Dict) -> Dict:
        """Warm up: read messages, view media, react with emoji.

        Builds account history before posting to appear natural.
        Uses joined groups automatically.
        """
        await self.authenticate()
        stats = {"viewed": 0, "reacted": 0}

        # Use joined groups instead of manual config
        all_groups = await self._get_joined_groups()
        if not all_groups:
            return stats

        groups_to_warm = random.sample(
            all_groups, min(3, len(all_groups))
        )

        for group_info in groups_to_warm:
            try:
                entity = await self.client.get_entity(group_info["id"])
                messages = await self.client.get_messages(entity, limit=20)
                if not messages:
                    continue

                # "Read" messages (mark as read)
                try:
                    await self.client.send_read_acknowledge(
                        entity, messages[0]
                    )
                    stats["viewed"] += len(messages)
                except Exception:
                    pass

                # React to 1-2 messages with emoji (15% chance per message)
                for msg in messages[:10]:
                    if random.random() < 0.15 and msg.text:
                        try:
                            from telethon.tl.functions.messages import (
                                SendReactionRequest,
                            )
                            from telethon.tl.types import ReactionEmoji

                            emoji = random.choice(
                                ["\U0001f44d", "\U0001f525", "\u2764\ufe0f",
                                 "\U0001f4af", "\U0001f44f"]
                            )
                            await self.client(
                                SendReactionRequest(
                                    peer=entity,
                                    msg_id=msg.id,
                                    reaction=[ReactionEmoji(emoticon=emoji)],
                                )
                            )
                            stats["reacted"] += 1
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(2, 6))

            except Exception as e:
                logger.debug(f"Telegram warm-up error: {e}")

            await asyncio.sleep(random.uniform(5, 15))

        logger.info(
            f"Telegram warm-up: viewed={stats['viewed']}, "
            f"reacted={stats['reacted']}"
        )
        return stats

    def warm_up(self, project: Dict) -> Dict:
        """Sync wrapper for warm_up_async."""
        return _run_tg_async(self.warm_up_async(project))

    # ── Sync Wrappers ──────────────────────────────────────────────────

    def scan(self, project: Dict) -> List[Dict]:
        """Sync wrapper for scan_async."""
        return _run_tg_async(self.scan_async(project))

    def act(self, opportunity: Dict, project: Dict) -> bool:
        """Sync wrapper for _act_async."""
        return _run_tg_async(self._act_async(opportunity, project))

    def test_connection(self) -> bool:
        """Verify Telegram credentials work."""
        return _run_tg_async(self._test_async())

    async def _test_async(self) -> bool:
        """Test authentication and list joined groups."""
        try:
            await self.authenticate()
            # List dialogs to verify access
            from telethon.tl.types import Channel
            groups = []
            async for dialog in self.client.iter_dialogs(limit=50):
                if dialog.is_group or isinstance(dialog.entity, Channel):
                    groups.append(dialog.name)

            logger.info(
                f"Telegram connected as @{self._username}, "
                f"{len(groups)} groups accessible"
            )
            return True
        except Exception as e:
            logger.error(f"Telegram connection failed: {e}")
            return False

    async def disconnect(self):
        """Disconnect the Telethon client."""
        if self.client and self.client.is_connected():
            try:
                await self.client.disconnect()
            except Exception:
                pass
