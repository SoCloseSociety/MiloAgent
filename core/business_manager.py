"""Business (project) manager with hot-reload and CRUD operations."""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Callable

import yaml

logger = logging.getLogger(__name__)


class BusinessManager:
    """Manages project YAML files with hot-reload capability.

    Features:
    - Load all projects from projects/ directory
    - Watch for file changes (add/edit/delete) via mtime polling
    - CRUD operations for projects
    - Thread-safe access to projects list
    - Callbacks for reload notification
    """

    def __init__(self, projects_dir: str = "projects/"):
        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._projects: List[Dict] = []
        self._file_mtimes: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._watcher_thread: Optional[threading.Thread] = None
        self._watching = False
        self._on_reload_callbacks: List[Callable] = []

        # Initial load
        self.reload()

    @property
    def projects(self) -> List[Dict]:
        """Thread-safe access to current projects list."""
        with self._lock:
            return list(self._projects)

    def reload(self):
        """Reload all projects from disk."""
        projects = []
        new_mtimes = {}

        for f in self.projects_dir.glob("*.yaml"):
            try:
                new_mtimes[str(f)] = f.stat().st_mtime
                with open(f) as fh:
                    data = yaml.safe_load(fh) or {}
                if data and data.get("project", {}).get("enabled", True):
                    projects.append(data)
            except Exception as e:
                logger.error(f"Error loading project file {f}: {e}")

        projects.sort(
            key=lambda p: p.get("project", {}).get("weight", 1.0),
            reverse=True,
        )

        with self._lock:
            old_names = {p["project"]["name"] for p in self._projects}
            new_names = {p["project"]["name"] for p in projects}
            self._projects = projects
            self._file_mtimes = new_mtimes

        # Log changes
        added = new_names - old_names
        removed = old_names - new_names
        if added:
            logger.info(f"Projects added: {added}")
        if removed:
            logger.info(f"Projects removed: {removed}")
        if not added and not removed and old_names:
            logger.info(f"Projects reloaded: {[p['project']['name'] for p in projects]}")
        elif not old_names:
            logger.info(f"Loaded {len(projects)} projects: {[p['project']['name'] for p in projects]}")

        # Notify callbacks
        for cb in self._on_reload_callbacks:
            try:
                cb(projects)
            except Exception as e:
                logger.error(f"Reload callback error: {e}")

    # ── File Watcher ──────────────────────────────────────────────────

    def start_watching(self, interval: float = 5.0):
        """Start file watcher thread (daemon, polls every interval seconds)."""
        if self._watching:
            return
        self._watching = True
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), daemon=True
        )
        self._watcher_thread.start()
        logger.info(f"Project file watcher started (interval={interval}s)")

    def stop_watching(self):
        """Stop file watcher thread."""
        self._watching = False

    def _watch_loop(self, interval: float):
        """Poll for file changes in projects/ directory."""
        while self._watching:
            time.sleep(interval)
            try:
                current_mtimes = {}
                for f in self.projects_dir.glob("*.yaml"):
                    current_mtimes[str(f)] = f.stat().st_mtime

                if current_mtimes != self._file_mtimes:
                    logger.info("Project files changed, reloading...")
                    self.reload()
            except Exception as e:
                logger.error(f"File watcher error: {e}")

    def on_reload(self, callback: Callable):
        """Register a callback for when projects are reloaded.

        Callback receives: callback(projects: List[Dict])
        """
        self._on_reload_callbacks.append(callback)

    # ── CRUD Operations ───────────────────────────────────────────────

    def add_project(
        self,
        name: str,
        url: str,
        description: str,
        project_type: str = "SaaS",
        **kwargs,
    ) -> str:
        """Create a new project YAML file.

        Returns the filepath of the created file.
        Raises ValueError if file already exists.
        """
        slug = name.lower().replace(" ", "_").replace("-", "_")
        filepath = self.projects_dir / f"{slug}.yaml"

        if filepath.exists():
            raise ValueError(f"Project file already exists: {filepath}")

        data = {
            "project": {
                "name": name,
                "url": url,
                "type": project_type,
                "description": description,
                "tagline": kwargs.get("tagline", ""),
                "weight": kwargs.get("weight", 1.0),
                "enabled": True,
                "selling_points": kwargs.get("selling_points", []),
                "target_audiences": kwargs.get("target_audiences", []),
                "business_profile": {
                    "socials": {
                        "twitter": "",
                        "website": url,
                    },
                    "features": [],
                    "pricing": {
                        "model": "unknown",
                        "free_tier": "",
                        "paid_plans": [],
                    },
                    "faqs": [],
                    "competitors": [],
                    "rules": {
                        "never_say": [],
                        "always_accurate": [
                            f"Product name is exactly '{name}'",
                            f"URL is {url}",
                        ],
                    },
                },
            },
            "reddit": {
                "target_subreddits": {"primary": [], "secondary": []},
                "keywords": [],
                "min_post_score": 3,
                "max_post_age_hours": 24,
            },
            "twitter": {
                "keywords": [],
                "hashtags": [],
            },
            "tone": {
                "style": "helpful_casual",
                "language": "en",
                "formality": "casual",
            },
        }

        with open(filepath, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        self.reload()
        return str(filepath)

    def delete_project(self, name: str) -> bool:
        """Delete a project by name. Returns True if found and deleted."""
        for f in self.projects_dir.glob("*.yaml"):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh) or {}
                if data.get("project", {}).get("name", "").lower() == name.lower():
                    f.unlink()
                    self.reload()
                    return True
            except Exception:
                continue
        return False

    def get_project(self, name: str) -> Optional[Dict]:
        """Get a project by name (case-insensitive)."""
        for p in self.projects:
            if p.get("project", {}).get("name", "").lower() == name.lower():
                return p
        return None

    def list_projects(self) -> List[str]:
        """List all project names."""
        return [p["project"]["name"] for p in self.projects]

    def get_project_filepath(self, name: str) -> Optional[str]:
        """Get the YAML file path for a project."""
        for f in self.projects_dir.glob("*.yaml"):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh) or {}
                if data.get("project", {}).get("name", "").lower() == name.lower():
                    return str(f)
            except Exception:
                continue
        return None
