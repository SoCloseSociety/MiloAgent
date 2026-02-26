"""Base platform interface for all social media bots."""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional

from core.database import Database
from core.content_gen import ContentGenerator


class BasePlatform(ABC):
    """Common interface for all platform bots (Reddit, Twitter, etc.)."""

    def __init__(
        self,
        db: Database,
        content_gen: ContentGenerator,
        config: Dict,
    ):
        self.db = db
        self.content_gen = content_gen
        self.config = config

    @abstractmethod
    def scan(self, project: Dict) -> List[Dict]:
        """Scan for opportunities. Returns list of opportunity dicts."""
        ...

    @abstractmethod
    def act(self, opportunity: Dict, project: Dict) -> bool:
        """Execute action on an opportunity. Returns success."""
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        """Verify credentials and connectivity."""
        ...

    def _already_acted(self, target_id: str) -> bool:
        """Check if we already acted on this target."""
        return self.db.was_target_acted_on(target_id)
