"""Content deduplication — prevents duplicate or too-similar content."""

import logging
from typing import Optional

from core.database import Database

logger = logging.getLogger(__name__)


class ContentDeduplicator:
    """Prevents duplicate or too-similar content.

    Checks:
    - Exact target ID match (already acted on)
    - Same project mentioned in same subreddit too recently
    - Simple text similarity (word overlap)
    """

    def __init__(self, db: Database):
        self.db = db

    def is_target_already_hit(self, target_id: str) -> bool:
        """Check if we already acted on this target."""
        return self.db.was_target_acted_on(target_id)

    def is_duplicate_content(
        self,
        content: str,
        platform: str,
        hours: int = 24,
        similarity_threshold: float = 0.6,
    ) -> bool:
        """Check if content is too similar to recent posts.

        Uses simple word-overlap (Jaccard similarity).
        Fail-safe: returns True (blocks posting) on DB errors to prevent spam.
        """
        try:
            recent = self.db.get_recent_actions(hours=hours, platform=platform)
        except Exception as e:
            logger.error(f"Dedup DB query failed, blocking as safety: {e}")
            return True  # Fail-safe: treat as duplicate to prevent spam

        content_words = set(content.lower().split())

        for action in recent:
            prev_content = action.get("content", "")
            if not prev_content:
                continue
            prev_words = set(prev_content.lower().split())

            if not content_words or not prev_words:
                continue

            # Jaccard similarity
            intersection = content_words & prev_words
            union = content_words | prev_words
            similarity = len(intersection) / len(union) if union else 0

            if similarity >= similarity_threshold:
                logger.warning(
                    f"Content too similar to recent action "
                    f"(similarity={similarity:.2f})"
                )
                return True

        return False

    def was_thread_recently_hit(
        self, target_id: str, hours: int = 6
    ) -> bool:
        """Check if ANY of our accounts already attempted this thread (success or fail)."""
        recent = self.db.get_recent_actions(hours=hours)
        for action in recent:
            if action.get("target_id") == target_id:
                return True
        return False

    def was_project_mentioned_in_subreddit(
        self,
        project_name: str,
        subreddit: str,
        hours: int = 24,
    ) -> bool:
        """Check if project was already mentioned in a subreddit recently."""
        recent = self.db.get_recent_actions(
            hours=hours, platform="reddit"
        )
        for action in recent:
            if action.get("project", "").lower() != project_name.lower():
                continue
            metadata = action.get("metadata", "")
            if isinstance(metadata, str) and subreddit.lower() in metadata.lower():
                # Check if it was promotional
                if '"promotional": true' in metadata.lower():
                    logger.info(
                        f"Project {project_name} already promoted in "
                        f"r/{subreddit} within {hours}h"
                    )
                    return True
        return False
