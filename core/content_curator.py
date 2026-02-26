"""Content Curator — finds and curates shareable content from the web.

Capabilities:
- YouTube: search relevant videos, extract metadata for sharing
- Web scraping: grab trending articles/posts from niche blogs
- Content mixing: provide fresh links/videos the bot can share organically
"""

import json
import logging
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus

import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

# Timeout for all HTTP requests (seconds)
_HTTP_TIMEOUT = 15
# Timeout for yt-dlp subprocess
_YTDLP_TIMEOUT = 30

# User agents for scraping
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


class ContentCurator:
    """Finds fresh content from YouTube and the web for organic sharing."""

    def __init__(self, cache_dir: str = "data/curator_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        })
        # Track shared URLs to avoid duplication
        self._shared_cache_file = self.cache_dir / "shared_urls.json"
        self._shared_urls = self._load_shared_urls()

    def _load_shared_urls(self) -> set:
        try:
            if self._shared_cache_file.exists():
                data = json.loads(self._shared_cache_file.read_text())
                return set(data[-500:])  # Keep last 500
        except Exception:
            pass
        return set()

    def _mark_shared(self, url: str):
        self._shared_urls.add(url)
        try:
            urls = list(self._shared_urls)[-500:]
            self._shared_cache_file.write_text(json.dumps(urls))
        except Exception:
            pass

    # ── YouTube ───────────────────────────────────────────────────────

    def find_youtube_videos(
        self,
        query: str,
        max_results: int = 5,
        max_age_days: int = 30,
    ) -> List[Dict]:
        """Search YouTube for relevant videos via yt-dlp.

        Returns list of dicts with: title, url, channel, views, duration, description
        """
        try:
            cmd = [
                "yt-dlp",
                f"ytsearch{max_results}:{query}",
                "--dump-json",
                "--flat-playlist",
                "--no-download",
                "--no-warnings",
                "--quiet",
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_YTDLP_TIMEOUT,
            )

            if result.returncode != 0:
                logger.debug(f"yt-dlp search failed: {result.stderr[:200]}")
                return []

            videos = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    url = data.get("url") or f"https://www.youtube.com/watch?v={data.get('id', '')}"
                    if url in self._shared_urls:
                        continue
                    videos.append({
                        "title": data.get("title", ""),
                        "url": url,
                        "channel": data.get("channel") or data.get("uploader", ""),
                        "views": data.get("view_count", 0),
                        "duration": data.get("duration", 0),
                        "description": (data.get("description") or "")[:300],
                    })
                except (json.JSONDecodeError, KeyError):
                    continue

            # Sort by views (most popular first)
            videos.sort(key=lambda v: v.get("views", 0), reverse=True)
            return videos

        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp search timed out")
            return []
        except FileNotFoundError:
            logger.warning("yt-dlp not installed")
            return []
        except Exception as e:
            logger.error(f"YouTube search failed: {e}")
            return []

    def get_trending_youtube(
        self,
        topics: List[str],
        max_per_topic: int = 3,
    ) -> List[Dict]:
        """Find trending YouTube videos across multiple topics."""
        all_videos = []
        for topic in topics[:5]:  # Limit topics
            videos = self.find_youtube_videos(
                query=topic,
                max_results=max_per_topic,
            )
            all_videos.extend(videos)
            if videos:
                time.sleep(random.uniform(1, 3))
        return all_videos

    def format_youtube_for_reddit(self, video: Dict, context: str = "") -> Dict:
        """Format a YouTube video as a Reddit post (link post or discussion).

        Returns dict with: title, body, url (for link posts)
        """
        title = video.get("title", "")
        url = video.get("url", "")
        channel = video.get("channel", "")
        duration = video.get("duration", 0)

        dur_str = ""
        if duration:
            mins = duration // 60
            secs = duration % 60
            dur_str = f" ({mins}:{secs:02d})"

        # Create a discussion-style post (more organic than just dropping a link)
        body = f"Found this interesting video{dur_str}"
        if channel:
            body += f" by {channel}"
        body += f":\n\n{url}\n\n"
        if context:
            body += f"{context}\n\n"
        body += "What do you guys think? Would love to hear your thoughts."

        return {
            "title": title,
            "body": body,
            "url": url,
            "type": "link_discussion",
        }

    def format_youtube_for_twitter(self, video: Dict) -> str:
        """Format a YouTube video as a tweet."""
        title = video.get("title", "")
        url = video.get("url", "")
        channel = video.get("channel", "")

        # Keep it casual and short
        templates = [
            f"This is really well done -- {title}\n{url}",
            f"Worth watching: {title}\n{url}",
            f"Came across this and it's actually great\n{url}",
            f"{title}\n\nGreat breakdown by {channel}\n{url}" if channel else f"{title}\n{url}",
        ]
        tweet = random.choice(templates)

        # Ensure under 280 chars
        if len(tweet) > 280:
            tweet = f"{title[:180]}\n{url}"
        return tweet

    # ── Web Scraping ──────────────────────────────────────────────────

    def scrape_subreddit_hot(
        self,
        subreddit: str,
        limit: int = 10,
    ) -> List[Dict]:
        """Scrape hot posts from a subreddit (no auth needed, JSON API)."""
        try:
            url = f"https://old.reddit.com/r/{subreddit}/hot.json?limit={limit}"
            resp = self._session.get(url, timeout=_HTTP_TIMEOUT)
            if resp.status_code != 200:
                return []

            data = resp.json()
            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                posts.append({
                    "title": post.get("title", ""),
                    "url": f"https://reddit.com{post.get('permalink', '')}",
                    "score": post.get("score", 0),
                    "comments": post.get("num_comments", 0),
                    "author": post.get("author", ""),
                    "subreddit": subreddit,
                    "is_self": post.get("is_self", True),
                    "external_url": post.get("url", "") if not post.get("is_self") else "",
                })
            return posts

        except Exception as e:
            logger.debug(f"Subreddit scrape failed for r/{subreddit}: {e}")
            return []

    def scrape_news_headlines(
        self,
        topics: List[str],
        max_results: int = 10,
    ) -> List[Dict]:
        """Scrape recent news/blog headlines from Google News RSS.

        Returns list of: title, url, source, snippet
        """
        all_articles = []
        for topic in topics[:3]:
            try:
                rss_url = f"https://news.google.com/rss/search?q={quote_plus(topic)}&hl=en"
                resp = self._session.get(rss_url, timeout=_HTTP_TIMEOUT)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                for item in soup.find_all("item")[:max_results]:
                    title = item.find("title")
                    link = item.find("link")
                    source = item.find("source")
                    if title and link:
                        article_url = link.get_text(strip=True) if link.string else str(link.next_sibling).strip()
                        if article_url in self._shared_urls:
                            continue
                        all_articles.append({
                            "title": title.get_text(strip=True),
                            "url": article_url,
                            "source": source.get_text(strip=True) if source else "",
                            "topic": topic,
                        })

                time.sleep(random.uniform(1, 2))
            except Exception as e:
                logger.debug(f"News scrape failed for '{topic}': {e}")

        return all_articles

    def scrape_page_content(self, url: str, max_chars: int = 2000) -> Optional[str]:
        """Scrape and extract main text content from a URL.

        Useful for feeding article content into LLM for summarization.
        """
        try:
            resp = self._session.get(url, timeout=_HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove noise
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            # Try article tag first, then main, then body
            content = soup.find("article") or soup.find("main") or soup.find("body")
            if not content:
                return None

            text = content.get_text(separator="\n", strip=True)
            # Clean up whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:max_chars]

        except Exception as e:
            logger.debug(f"Page scrape failed for {url}: {e}")
            return None

    # ── Content Ideas ─────────────────────────────────────────────────

    def get_content_ideas(
        self,
        project: Dict,
        platform: str = "reddit",
    ) -> List[Dict]:
        """Get a mix of shareable content ideas for a project.

        Returns list of content ideas with type, title, body/url.
        Types: youtube_share, news_discussion, trending_topic
        """
        proj = project.get("project", {})
        keywords = project.get("reddit", {}).get("keywords", [])
        name = proj.get("name", "")
        description = proj.get("description", "")

        # Build search queries from project keywords
        search_topics = keywords[:4] if keywords else [description[:50]]

        ideas = []

        # YouTube videos
        try:
            videos = self.get_trending_youtube(search_topics, max_per_topic=2)
            for v in videos[:3]:
                if platform == "reddit":
                    formatted = self.format_youtube_for_reddit(v)
                else:
                    formatted = {"title": self.format_youtube_for_twitter(v), "url": v["url"]}
                ideas.append({
                    "type": "youtube_share",
                    "content": formatted,
                    "source_url": v["url"],
                })
        except Exception as e:
            logger.debug(f"YouTube content ideas failed: {e}")

        # News headlines
        try:
            articles = self.scrape_news_headlines(search_topics)
            for a in articles[:3]:
                ideas.append({
                    "type": "news_discussion",
                    "content": {
                        "title": a["title"],
                        "url": a["url"],
                        "source": a.get("source", ""),
                    },
                    "source_url": a["url"],
                })
        except Exception as e:
            logger.debug(f"News content ideas failed: {e}")

        random.shuffle(ideas)
        return ideas
