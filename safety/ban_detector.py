"""Shadowban and restriction detection."""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine safely from any context.

    Works whether called from a thread with no loop, or inside a running loop.
    Never crashes with 'event loop already running'.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)
    else:
        return asyncio.run(coro)


class BanDetector:
    """Detects shadowbans and restrictions on Reddit and Twitter."""

    def check_reddit_shadowban(self, reddit_instance, username: str) -> Dict:
        """Check for Reddit shadowban indicators."""
        result = {
            "is_shadowbanned": False,
            "indicators": [],
            "confidence": "low",
        }

        try:
            user = reddit_instance.redditor(username)
            comments = list(user.comments.new(limit=5))
            if not comments:
                result["indicators"].append("No recent comments found")

            low_score_count = sum(1 for c in comments if c.score <= 1)
            if comments and low_score_count == len(comments):
                result["indicators"].append(
                    "All recent comments have score <= 1"
                )

            for comment in comments[:3]:
                try:
                    comment.refresh()
                except Exception:
                    result["indicators"].append(
                        f"Comment {comment.id} may be hidden"
                    )

            if len(result["indicators"]) >= 2:
                result["is_shadowbanned"] = True
                result["confidence"] = "medium"
            if len(result["indicators"]) >= 3:
                result["confidence"] = "high"

        except Exception as e:
            logger.error(f"Shadowban check failed for u/{username}: {e}")
            result["indicators"].append(f"Check failed: {e}")

        return result

    def check_twitter_restriction(self, client, username: str) -> Dict:
        """Check for Twitter account restrictions. Safe for sync/async contexts."""
        result = {
            "is_restricted": False,
            "indicators": [],
        }

        try:
            import asyncio

            async def _check():
                tweets = await client.search_tweet(
                    f"from:{username}", product="Latest"
                )
                return list(tweets) if tweets else []

            tweets = _run_async(_check())

            if not tweets:
                result["indicators"].append("No own tweets found in search")
                result["is_restricted"] = True

        except Exception as e:
            logger.error(f"Twitter restriction check failed for @{username}: {e}")
            result["indicators"].append(f"Check failed: {e}")

        return result
