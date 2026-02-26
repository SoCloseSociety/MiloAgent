"""Orchestrator — central coordinator for all bot operations.

SAFETY: Designed to NEVER freeze the system.
- All scans have hard timeouts
- Resource checks before and during every heavy operation
- Limited subreddits/keywords per scan cycle
- No blocking initial scan on startup
- Graceful degradation under memory pressure
- Auto-detects Mac vs Server and adapts thresholds
"""

import os
import sys
import copy
import json
import time
import random
import signal
import logging
import threading
import concurrent.futures
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler

from core.database import Database
from core.environment import detect_environment
from core.llm_provider import LLMProvider
from core.content_gen import ContentGenerator
from core.strategy import StrategyEngine
from core.learning_engine import LearningEngine
from core.business_manager import BusinessManager
from core.resource_monitor import ResourceMonitor
from core.content_curator import ContentCurator
from core.subreddit_intel import SubredditIntelligence
from core.research_engine import ResearchEngine
from core.ab_testing import ABTestingEngine
from core.relationship_engine import RelationshipEngine
from core.subreddit_hub import SubredditHubManager
from core.community_manager import CommunityManager
from platforms.reddit_bot import RedditBot
from platforms.twitter_bot import TwitterBot, _run_async_safe
from platforms.telegram_group_bot import TelegramGroupBot
from safety.rate_limiter import RateLimiter
from safety.account_manager import AccountManager
from safety.content_dedup import ContentDeduplicator
from safety.ban_detector import BanDetector

logger = logging.getLogger(__name__)

# ── Hard Safety Limits ────────────────────────────────────────────
MAX_SUBREDDITS_PER_SCAN = 12    # Never scan more than 12 subs per cycle
MAX_KEYWORDS_PER_SUBREDDIT = 8  # Never search more than 8 keywords per sub
SCAN_TIMEOUT_SECONDS = 900      # Hard timeout for entire scan operation (5 projects × 3 platforms + TG discovery)
ACT_TIMEOUT_SECONDS = 90        # Hard timeout for action operations
LLM_TIMEOUT_SECONDS = 45        # Hard timeout for LLM calls


def load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


class Orchestrator:
    """Central coordinator for all bot operations.

    SAFETY GUARANTEES:
    - Scans limited to MAX_SUBREDDITS_PER_SCAN subreddits per cycle
    - Keywords limited to MAX_KEYWORDS_PER_SUBREDDIT per subreddit
    - Hard timeout of SCAN_TIMEOUT_SECONDS on all scan operations
    - Resource check before AND during every scan/action
    - No blocking initial scan on startup (delayed by 2 minutes)
    - Thread pool limited to 2 workers
    """

    def __init__(self, config_dir: str = "config/"):
        self.config_dir = config_dir
        self.settings = load_yaml(f"{config_dir}/settings.yaml")
        self._bot_settings = self.settings.get("bot", {})
        self._mode = self._bot_settings.get("mode", "background")

        # Auto-detect mode if configured
        if self._mode == "auto":
            env = detect_environment()
            self._mode = env["recommended_mode"]
            logger.info(
                f"Auto-detected mode: {self._mode} "
                f"(os={env['os']}, docker={env['is_docker']}, "
                f"headless={env['is_headless']})"
            )

        # Core components
        self.db = Database(self.settings["database"]["path"])
        self.llm = LLMProvider(f"{config_dir}/llm.yaml")
        self.content_gen = ContentGenerator(
            self.llm,
            organic_ratio=self._bot_settings.get("organic_promo_ratio", 0.8),
        )

        # Safety
        self.rate_limiter = RateLimiter(self.db, self.settings)
        self.account_mgr = AccountManager(self.db, config_dir)
        self.dedup = ContentDeduplicator(self.db)
        self.ban_detector = BanDetector()

        # Strategy + Learning
        self.strategy = StrategyEngine(self.db, self.settings)
        self.learning = LearningEngine(self.db, self.llm)
        self.strategy.set_learning_engine(self.learning)

        # Scheduler (controlled thread pool)
        self.scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
            executors={"default": {"type": "threadpool", "max_workers": 3}},
        )

        # Content curator (YouTube, web scraping, news)
        self.curator = ContentCurator()

        # Phase 5: Intelligence + Self-Improvement
        self.subreddit_intel = SubredditIntelligence(self.db)
        self.research = ResearchEngine(self.db, self.llm, self.curator)
        self.ab_testing = ABTestingEngine(self.db)
        self.content_gen._ab_engine = self.ab_testing
        self.content_gen._db = self.db
        self.strategy.set_subreddit_intel(self.subreddit_intel)
        self.relationships = RelationshipEngine(self.db, self.llm, self.content_gen)
        self.hub_manager = SubredditHubManager(self.db, self.llm, self.content_gen)

        # Community Manager (subreddit lifecycle: setup, moderation, takeover)
        self.community_manager = CommunityManager(
            self.db, self.llm, self.content_gen,
            self.hub_manager, self.subreddit_intel,
        )
        self.hub_manager._community_manager = self.community_manager

        # Business manager with hot-reload
        self.business_mgr = BusinessManager()
        self.business_mgr.on_reload(self._on_projects_reloaded)

        # Telegram dashboard (optional)
        self.telegram: Optional[object] = None
        self._telegram_thread: Optional[threading.Thread] = None
        self._paused = False
        self._running = False
        self._scan_running = False  # Guard against concurrent scans

        # Platform bots (initialized per-account on demand)
        self._reddit_bots: Dict[str, RedditBot] = {}
        self._twitter_bots: Dict[str, TwitterBot] = {}
        self._telegram_group_bots: Dict[str, TelegramGroupBot] = {}

        # Alert log for TUI conversations view
        self._alert_log: deque = deque(maxlen=100)

        # Subreddit rotation index (for round-robin across cycles)
        self._sub_rotation_index: int = 0

        # Platform rotation (thread-safe)
        self._platform_turn: int = 0
        self._state_lock = threading.Lock()

        # Resource monitor (Mac-aware, checks every 30s)
        self.resource_monitor = ResourceMonitor(check_interval=30)
        self.resource_monitor.on_threshold(self._on_resource_event)

        logger.info(
            f"Orchestrator initialized: mode={self._mode}, "
            f"projects={[p['project']['name'] for p in self.projects]}"
        )

    @property
    def projects(self) -> List[Dict]:
        """Live project list from BusinessManager."""
        return self.business_mgr.projects

    def _send_telegram_alert(self, message: str):
        """Send a Telegram alert safely from any thread."""
        # Always buffer for TUI conversations view
        self._alert_log.append((datetime.now().isoformat(), message))

        if not self.telegram:
            return
        try:
            self.telegram.send_alert_sync(message)
        except Exception as e:
            logger.debug(f"Telegram alert failed: {e}")

    def _on_projects_reloaded(self, projects: List[Dict]):
        """Called when project files change on disk."""
        names = [p["project"]["name"] for p in projects]
        logger.info(f"Projects hot-reloaded: {names}")
        self._send_telegram_alert(
            f"Picked up config changes — now working on {len(projects)} projects: "
            f"{', '.join(names)}"
        )

    def _on_resource_event(self, event: str, state):
        """Handle resource monitor threshold events."""
        if event == "ram_critical":
            self._paused = True
            logger.warning(
                f"AUTO-PAUSED: RAM at {state.ram_used_percent:.0f}% "
                f"({state.ram_available_gb:.1f}GB free)"
            )
            self._send_telegram_alert(
                f"Had to pause — your Mac is running low on RAM "
                f"({state.ram_used_percent:.0f}% used, "
                f"only {state.ram_available_gb:.1f}GB free). "
                f"I'll resume automatically when it clears up."
            )
        elif event == "ram_warn":
            logger.warning(
                f"RAM high: {state.ram_used_percent:.0f}%, "
                f"throttling to {self.resource_monitor.throttle_factor}x"
            )
        elif event == "disk_critical":
            self._paused = True
            logger.warning(f"AUTO-PAUSED: Disk at {state.disk_used_percent:.0f}%")
            self.db.force_maintenance()
        elif event == "disk_warn":
            logger.warning(f"Disk usage high: {state.disk_used_percent:.0f}%")
            self.db.force_maintenance()
        elif event == "recovered":
            if self._paused:
                self._paused = False
                logger.info("Resources recovered, resuming bot")
                self._send_telegram_alert("System resources are back to normal — getting back to work.")
        elif event == "process_memory_warn":
            logger.warning(
                f"Process RSS high: {state.process_rss_mb:.0f}MB, "
                f"running garbage collection"
            )

    def _check_resources(self) -> bool:
        """Quick resource check. Returns False if we should abort."""
        if self._paused:
            return False
        return self.resource_monitor.is_safe_to_proceed()

    def _get_reddit_bot(self, account: Dict):
        """Get or create a Reddit bot for an account."""
        username = account["username"]
        if username not in self._reddit_bots:
            reddit_cfg = load_yaml(f"{self.config_dir}/reddit_accounts.yaml")
            auth_mode = reddit_cfg.get("auth_mode", "web")
            if auth_mode == "api" and account.get("client_id"):
                self._reddit_bots[username] = RedditBot(
                    self.db, self.content_gen, account
                )
            else:
                from platforms.reddit_web import RedditWebBot
                self._reddit_bots[username] = RedditWebBot(
                    self.db, self.content_gen, account
                )
        return self._reddit_bots[username]

    def _get_twitter_bot(self, account: Dict) -> TwitterBot:
        """Get or create a Twitter bot for an account."""
        username = account["username"]
        if username not in self._twitter_bots:
            # Resolve proxy: per-account > twitter-specific > global
            http_cfg = self.settings.get("http", {})
            proxy = (
                account.get("proxy")
                or http_cfg.get("twitter_proxy")
                or http_cfg.get("proxy")
            )
            self._twitter_bots[username] = TwitterBot(
                self.db, self.content_gen, account, proxy=proxy
            )
        return self._twitter_bots[username]

    def _get_telegram_group_bot(self, account: Dict) -> TelegramGroupBot:
        """Get or create a Telegram group bot for an account."""
        username = account.get("username") or account.get("phone", "unknown")
        if username not in self._telegram_group_bots:
            self._telegram_group_bots[username] = TelegramGroupBot(
                self.db, self.content_gen, account
            )
        return self._telegram_group_bots[username]

    def _init_telegram(self):
        """Initialize Telegram dashboard if configured."""
        try:
            tg_config = load_yaml(f"{self.config_dir}/telegram.yaml")
            token = tg_config.get("bot_token", "")
            if token and not token.startswith("YOUR_"):
                from dashboard.telegram_bot import TelegramDashboard

                self.telegram = TelegramDashboard(tg_config, self.db)
                self.telegram.set_account_manager(self.account_mgr)
                self.telegram.set_orchestrator(self)
                self.telegram.build()
                logger.info("Telegram dashboard initialized")
            else:
                logger.info("Telegram not configured, skipping dashboard")
        except Exception as e:
            logger.warning(f"Failed to init Telegram dashboard: {e}")

    # ── Scheduling ───────────────────────────────────────────────────

    def start(self, nonblocking: bool = False):
        """Start the bot in background mode.

        Args:
            nonblocking: If True, return after starting all jobs instead of
                         blocking on signal.pause(). Used by the TUI dashboard.
        """
        self._running = True
        self._write_pid()
        self._set_nice_priority()
        self._setup_signal_handlers()
        self._init_telegram()

        # Start account file watcher for hot-reload
        self.account_mgr.start_watching(interval=10.0)
        self.account_mgr.on_reload(self._on_accounts_reloaded)

        # Start resource monitor FIRST
        self.resource_monitor.start()
        state = self.resource_monitor.get_state()
        logger.info(
            f"System: {state.cpu_cores} cores, "
            f"{state.ram_total_gb:.0f}GB RAM "
            f"({state.ram_used_percent:.0f}% used), "
            f"{state.disk_free_gb:.0f}GB disk free"
        )

        # Purge stale/low-quality opportunities on startup
        self.db.purge_low_quality_opportunities(min_score=3.0, max_age_hours=48)

        # SAFETY: Abort startup if resources already critical
        if state.ram_used_percent >= 85:
            logger.warning(
                f"RAM already at {state.ram_used_percent:.0f}% — "
                f"starting in reduced mode (scan-only, no initial scan)"
            )

        # Get mode-specific intervals
        scheduling = self.settings.get("scheduling", {}).get(
            self._mode, {}
        )
        scan_interval = scheduling.get(
            "scan_interval_minutes",
            self._bot_settings.get("scan_interval_minutes", 30),
        )
        action_interval = scheduling.get(
            "action_interval_minutes",
            self._bot_settings.get("action_interval_minutes", 10),
        )

        # Schedule jobs — NONE run immediately (next_run_time=None where needed)
        from datetime import datetime, timedelta

        # First scan delayed by 2 minutes (let system settle)
        first_scan_time = datetime.now() + timedelta(minutes=2)

        self.scheduler.add_job(
            self._scan_all_safe, "interval",
            minutes=scan_interval, id="scan_all",
            next_run_time=first_scan_time,
        )
        self.scheduler.add_job(
            self._act_on_best_safe, "interval",
            minutes=action_interval, id="act_best",
            next_run_time=datetime.now() + timedelta(minutes=3),
        )
        self.scheduler.add_job(
            self._health_check, "interval",
            minutes=60, id="health_check",
            next_run_time=None,
        )
        self.scheduler.add_job(
            self._learn, "interval",
            hours=6, id="learn",
            next_run_time=datetime.now() + timedelta(minutes=30),
        )
        self.scheduler.add_job(
            self._verify_comments, "interval",
            hours=3, id="verify_comments",
            next_run_time=None,
        )
        self.scheduler.add_job(
            self._seed_content, "interval",
            hours=6, id="seed_content",
            next_run_time=datetime.now() + timedelta(minutes=15),
        )
        # Tweet cycle DISABLED: Twitter blocked on server (code 226)
        # self.scheduler.add_job(
        #     self._tweet_cycle_safe, "interval",
        #     hours=4, id="tweet_cycle",
        #     next_run_time=datetime.now() + timedelta(minutes=8),
        # )
        self.scheduler.add_job(
            self._engage_safe, "interval",
            hours=2, id="engagement",
            next_run_time=datetime.now() + timedelta(minutes=5),
        )
        self.scheduler.add_job(
            self._curate_and_share, "interval",
            hours=8, id="curate_content",
            next_run_time=datetime.now() + timedelta(minutes=20),
        )

        # Phase 5: Intelligence + Self-Improvement jobs
        self.scheduler.add_job(
            self._analyze_subreddits_safe, "interval",
            hours=12, id="subreddit_intel",
            next_run_time=datetime.now() + timedelta(minutes=45),
        )
        self.scheduler.add_job(
            self._maintain_presence_safe, "interval",
            hours=6, id="community_presence",
            next_run_time=datetime.now() + timedelta(minutes=15),
        )
        self.scheduler.add_job(
            self._research_safe, "interval",
            hours=12, id="research",
            next_run_time=datetime.now() + timedelta(minutes=50),
        )
        self.scheduler.add_job(
            self._send_weekly_report, "cron",
            day_of_week="sun", hour=20, id="weekly_report",
        )
        self.scheduler.add_job(
            self._build_relationships_safe, "interval",
            hours=8, id="relationships",
            next_run_time=datetime.now() + timedelta(minutes=60),
        )
        self.scheduler.add_job(
            self._animate_hubs_safe, "interval",
            hours=8, id="hub_animation",
            next_run_time=datetime.now() + timedelta(minutes=90),
        )
        # Community management: setup, moderation, stickies (every 4h)
        self.scheduler.add_job(
            self._manage_communities_safe, "interval",
            hours=4, id="community_management",
            next_run_time=datetime.now() + timedelta(minutes=100),
        )
        # Takeover scan: find abandoned subreddits (daily)
        self.scheduler.add_job(
            self._scan_takeover_targets_safe, "interval",
            hours=24, id="takeover_scan",
            next_run_time=datetime.now() + timedelta(hours=3),
        )
        self.scheduler.add_job(
            self._auto_improve_safe, "interval",
            hours=12, id="auto_improve",
            next_run_time=datetime.now() + timedelta(minutes=120),
        )
        # Periodic opportunity cleanup (every 6h)
        self.scheduler.add_job(
            lambda: self.db.purge_low_quality_opportunities(
                min_score=3.0, max_age_hours=48
            ),
            "interval", hours=6, id="opportunity_purge",
            next_run_time=datetime.now() + timedelta(minutes=30),
        )

        # Daily report
        notifications = {}
        try:
            tg_config = load_yaml(f"{self.config_dir}/telegram.yaml")
            notifications = tg_config.get("notifications", {})
        except Exception:
            pass

        if notifications.get("daily_report"):
            report_hour = notifications.get("daily_report_hour", 22)
            self.scheduler.add_job(
                self._send_daily_report, "cron",
                hour=report_hour, id="daily_report",
            )

        self.scheduler.start()

        # Start project file watcher (low overhead)
        self.business_mgr.start_watching(interval=10.0)

        logger.info(
            f"Milo started in {self._mode} mode. "
            f"First scan in 2min, act in 3min. "
            f"Scan every {scan_interval}min, act every {action_interval}min."
        )

        # Start Telegram polling in a separate thread (if configured)
        if self.telegram and hasattr(self.telegram, 'app') and self.telegram.app:
            self._telegram_thread = threading.Thread(
                target=self.telegram.start_polling, daemon=True
            )
            self._telegram_thread.start()
            logger.info("Telegram polling started in background thread")

        # Keep main thread alive (loop: signal.pause() returns after ONE signal)
        if nonblocking:
            return  # Caller manages the main loop (e.g. TUI dashboard)

        try:
            while self._running:
                signal.pause()
        except AttributeError:
            # Windows fallback (no signal.pause)
            while self._running:
                time.sleep(1)

    def stop(self):
        """Graceful shutdown — close all resources. Idempotent (safe to call twice)."""
        if not self._running and not hasattr(self, '_stopping'):
            return  # Already stopped
        self._stopping = True
        logger.info("Shutting down Milo...")
        self._running = False
        self.resource_monitor.stop()
        self.business_mgr.stop_watching()
        try:
            # wait=True gives running jobs up to ~5s to finish before force-killing
            self.scheduler.shutdown(wait=True)
        except Exception:
            pass  # Already shut down

        # Stop Telegram polling
        if self.telegram and hasattr(self.telegram, 'stop_polling'):
            try:
                self.telegram.stop_polling()
            except Exception:
                pass

        # Close all Reddit bot sessions
        for bot in self._reddit_bots.values():
            if hasattr(bot, "close"):
                try:
                    bot.close()
                except Exception:
                    pass

        # Disconnect Telegram group bots
        for bot in self._telegram_group_bots.values():
            try:
                from platforms.telegram_group_bot import _run_tg_async
                _run_tg_async(bot.disconnect(), timeout=5)
            except Exception:
                pass

        self._reddit_bots.clear()
        self._twitter_bots.clear()
        self._telegram_group_bots.clear()

        # Shutdown LLM thread pool
        if hasattr(self.llm, "shutdown"):
            self.llm.shutdown()

        # Brief pause to let any lingering threads finish DB writes
        time.sleep(0.5)
        self.db.close()
        self._remove_pid()
        logger.info("Milo stopped.")

    # ── Safe Wrappers (with timeout + resource check) ────────────────

    def _scan_all_safe(self):
        """Wrapper: run _scan_all with a hard timeout."""
        if not self._check_resources():
            logger.info("Scan skipped: resources too low")
            return

        # Run scan in a thread with hard timeout
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._scan_all)
            try:
                future.result(timeout=SCAN_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    f"Scan ABORTED: exceeded {SCAN_TIMEOUT_SECONDS}s timeout"
                )
                future.cancel()
            except Exception as e:
                logger.error(f"Scan error: {e}")

    def _act_on_best_safe(self):
        """Wrapper: run _act_on_best with a hard timeout."""
        if not self._check_resources():
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._act_on_best)
            try:
                future.result(timeout=ACT_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    f"Action ABORTED: exceeded {ACT_TIMEOUT_SECONDS}s timeout"
                )
                future.cancel()
            except Exception as e:
                logger.error(f"Action error: {e}")

    # ── Scheduled Jobs ───────────────────────────────────────────────

    def _scan_all(self):
        """Scan platforms for all projects.

        SAFETY LIMITS:
        - Max MAX_SUBREDDITS_PER_SCAN subreddits per cycle (round-robin)
        - Max MAX_KEYWORDS_PER_SUBREDDIT keywords per subreddit
        - Resource check between each subreddit
        - Overall timeout enforced by _scan_all_safe()
        """
        if self._scan_running:
            logger.info("Scan already in progress, skipping")
            return
        if self._paused:
            logger.debug("Bot is paused, skipping scan")
            return
        if not self.rate_limiter.is_active_hours():
            logger.debug("Outside active hours, skipping scan")
            return
        if self.rate_limiter.should_take_random_break():
            logger.info("Taking a random break (human simulation)")
            return
        self._scan_running = True
        try:
            self.__scan_all_inner()
        finally:
            self._scan_running = False

    def __scan_all_inner(self):
        """Inner scan logic — always called with _scan_running=True."""
        # Skip if system is heavily throttled
        throttle = self.resource_monitor.throttle_factor
        if throttle >= 5.0:
            logger.info("System resources critical, skipping scan")
            return

        logger.info("Starting scan cycle...")

        # Per-cycle skip flags — disable broken platforms without breaking scan loop
        _skip_twitter = True   # Twitter disabled: server IP blocked (code 226)
        _skip_telegram = False

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")

            # Inject expanded targets from learning
            scan_project = self._expand_project_targets(project)

            # SAFETY: Limit subreddits per scan via round-robin
            scan_project = self._limit_scan_targets(scan_project)

            # Scan Reddit
            account = self.account_mgr.get_next_account("reddit")
            if account:
                try:
                    # Resource check before scan
                    if not self._check_resources():
                        logger.info("Aborting scan: resources low")
                        return

                    bot = self._get_reddit_bot(account)
                    opps = bot.scan(scan_project)
                    logger.info(
                        f"Reddit scan for {proj_name}: "
                        f"{len(opps)} opportunities"
                    )
                except Exception as e:
                    logger.error(f"Reddit scan error for {proj_name}: {e}")

            # Scan Twitter (only if resources allow and not blocked)
            if self._check_resources() and not _skip_twitter:
                account = self.account_mgr.get_next_account("twitter")
                if account:
                    try:
                        bot = self._get_twitter_bot(account)
                        opps = bot.scan(scan_project)
                        logger.info(
                            f"Twitter scan for {proj_name}: "
                            f"{len(opps)} opportunities"
                        )
                    except Exception as e:
                        err_str = str(e)
                        # Cloudflare block — log once, disable for this cycle
                        if "blocked" in err_str.lower() or "403" in err_str[:10]:
                            if not getattr(self, "_twitter_blocked_warned", False):
                                logger.warning(
                                    "Twitter blocked by Cloudflare — server IP banned. "
                                    "Set http.twitter_proxy in settings.yaml"
                                )
                                self._twitter_blocked_warned = True
                            _skip_twitter = True
                        else:
                            logger.error(f"Twitter scan error for {proj_name}: {e}")

            # Scan Telegram groups (only if project has telegram config and not disabled)
            if self._check_resources() and not _skip_telegram:
                tg_config = project.get("telegram", {})
                if tg_config.get("enabled", False):
                    account = self.account_mgr.get_next_account("telegram")
                    if account:
                        try:
                            bot = self._get_telegram_group_bot(account)

                            # Auto-discover and join new groups (once per day)
                            if tg_config.get("auto_discover", False):
                                last_discover = getattr(self, "_tg_last_discover", {})
                                last_time = last_discover.get(proj_name, 0)
                                if time.time() - last_time > 86400:  # 24h
                                    try:
                                        stats = bot.discover_and_join(project)
                                        if not hasattr(self, "_tg_last_discover"):
                                            self._tg_last_discover = {}
                                        self._tg_last_discover[proj_name] = time.time()
                                        if stats.get("joined", 0) > 0:
                                            logger.info(
                                                f"Telegram auto-discovery for {proj_name}: "
                                                f"discovered={stats['discovered']}, joined={stats['joined']}"
                                            )
                                    except Exception as e:
                                        logger.debug(f"Telegram discovery error: {e}")

                            opps = bot.scan(project)
                            logger.info(
                                f"Telegram scan for {proj_name}: "
                                f"{len(opps)} opportunities"
                            )
                        except RuntimeError as e:
                            if "not authorized" in str(e).lower():
                                if not getattr(self, "_tg_auth_warned", False):
                                    logger.warning(
                                        "Telegram session not authorized — "
                                        "run: python miloagent.py login telegram"
                                    )
                                    self._tg_auth_warned = True
                                _skip_telegram = True
                            else:
                                logger.error(f"Telegram scan error for {proj_name}: {e}")
                        except Exception as e:
                            logger.error(f"Telegram scan error for {proj_name}: {e}")

        # Notify Telegram (one message per cycle)
        total_opps = self.db.get_pending_opportunities(limit=100)
        if total_opps:
            from dashboard.telegram_bot import _SCAN_DONE_MSGS, _pick
            self._send_telegram_alert(_pick(_SCAN_DONE_MSGS, n=len(total_opps)))
        logger.info("Scan cycle complete")

    def _limit_scan_targets(self, project: Dict) -> Dict:
        """Limit subreddits and keywords per scan cycle (round-robin rotation).

        This prevents scanning ALL subreddits every cycle, which would
        make 200+ HTTP requests and freeze the system.
        """
        limited = copy.deepcopy(project)
        reddit_config = limited.get("reddit", {})

        # Collect all subreddits
        subs = reddit_config.get("target_subreddits", {})
        if isinstance(subs, dict):
            all_subs = subs.get("primary", []) + subs.get("secondary", [])
        elif isinstance(subs, list):
            all_subs = subs
        else:
            all_subs = []

        if len(all_subs) > MAX_SUBREDDITS_PER_SCAN:
            # Round-robin: pick the next batch
            start = self._sub_rotation_index % len(all_subs)
            selected = []
            for i in range(MAX_SUBREDDITS_PER_SCAN):
                idx = (start + i) % len(all_subs)
                selected.append(all_subs[idx])
            self._sub_rotation_index = (start + MAX_SUBREDDITS_PER_SCAN) % len(all_subs)

            # Update config with limited subs
            if isinstance(subs, dict):
                reddit_config["target_subreddits"] = {
                    "primary": selected, "secondary": []
                }
            else:
                reddit_config["target_subreddits"] = selected

            logger.debug(
                f"Scan limited to {len(selected)}/{len(all_subs)} subreddits: "
                f"{selected}"
            )

        # Limit keywords
        keywords = reddit_config.get("keywords", [])
        if len(keywords) > MAX_KEYWORDS_PER_SUBREDDIT:
            # Rotate through keywords too
            import random
            selected_kw = random.sample(keywords, MAX_KEYWORDS_PER_SUBREDDIT)
            reddit_config["keywords"] = selected_kw
            logger.debug(
                f"Scan limited to {len(selected_kw)}/{len(keywords)} keywords"
            )

        return limited

    def _expand_project_targets(self, project: Dict) -> Dict:
        """Merge discovered subreddits/keywords into project config for scanning."""
        expanded = copy.deepcopy(project)

        try:
            all_subs = self.strategy.get_expanded_subreddits(project)
            reddit_config = expanded.get("reddit", {})
            subs = reddit_config.get("target_subreddits", {})
            if isinstance(subs, dict):
                existing = set(subs.get("primary", []) + subs.get("secondary", []))
                discovered = [s for s in all_subs if s not in existing]
                if discovered:
                    subs.setdefault("secondary", []).extend(discovered[:3])

            all_kw = self.strategy.get_expanded_keywords(project)
            existing_kw = set(reddit_config.get("keywords", []))
            discovered_kw = [k for k in all_kw if k not in existing_kw]
            if discovered_kw:
                reddit_config.setdefault("keywords", []).extend(discovered_kw[:3])
        except Exception as e:
            logger.debug(f"Target expansion failed: {e}")

        return expanded

    def _act_on_best(self):
        """Pick the best pending opportunity and act on it.

        Platform rotation: alternates starting platform each cycle so
        Twitter gets equal chances to act (not always behind Reddit).
        """
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        if self.telegram and hasattr(self.telegram, 'paused') and self.telegram.paused:
            self._paused = True
            logger.info("Paused via Telegram dashboard")
            return

        project = self.strategy.select_project(self.projects)
        if not project:
            logger.debug("No project selected")
            return

        proj_name = project.get("project", {}).get("name", "unknown")

        # Rotate starting platform each cycle (thread-safe)
        platforms = ["reddit", "telegram"]  # Twitter disabled: server IP blocked (code 226)
        with self._state_lock:
            start = self._platform_turn % len(platforms)
            self._platform_turn += 1
        platforms = platforms[start:] + platforms[:start]

        for platform in platforms:
            # Telegram gets lower min_score because its metrics
            # produce lower scores than Reddit (no upvote counts in scan).
            min_score = 3.0 if platform == "telegram" else 5.0
            pending = self.db.get_pending_opportunities(
                platform=platform, project=proj_name, min_score=min_score, limit=10
            )
            if not pending:
                continue

            account = self.account_mgr.get_next_account(platform)
            if not account:
                continue

            # Expertise focus: prefer opportunities in this account's focus subreddits
            if platform == "reddit" and len(pending) > 1:
                preferred = self.account_mgr.get_preferred_subreddits(
                    account["username"], platform
                )
                if preferred:
                    def _safe_score(o):
                        s = o.get("relevance_score") or o.get("score") or 0
                        return float(s) if isinstance(s, (int, float)) else 0.0
                    pending.sort(key=lambda o: (
                        0 if o.get("subreddit_or_query", "") in preferred else 1,
                        -_safe_score(o),
                    ))

            opp = pending[0]

            # Map DB fields to what platform bots expect
            if "subreddit_or_query" in opp and "subreddit" not in opp:
                opp["subreddit"] = opp["subreddit_or_query"]
            if "body" not in opp:
                opp["body"] = ""

            # Unpack metadata JSON into top-level fields
            # (DB stores group_id, message_id, keyword etc. inside metadata blob)
            metadata_raw = opp.get("metadata")
            if metadata_raw and isinstance(metadata_raw, str):
                try:
                    metadata = json.loads(metadata_raw)
                    if isinstance(metadata, dict):
                        for key in ("group_id", "message_id", "group_name",
                                    "keyword", "author_name", "author",
                                    "user", "reply_count", "post_score",
                                    "num_comments", "body", "text",
                                    "favorites", "retweets", "followers"):
                            if key in metadata and key not in opp:
                                opp[key] = metadata[key]
                except (json.JSONDecodeError, TypeError):
                    pass

            # Map title → text for Twitter (scan stores tweet text as title)
            if platform == "twitter" and "text" not in opp and "title" in opp:
                opp["text"] = opp["title"]
            # Map title → text for Telegram
            if platform == "telegram" and "text" not in opp and "title" in opp:
                opp["text"] = opp["title"]

            allowed, reason = self.rate_limiter.can_act(
                account["username"], platform,
                subreddit_or_query=opp.get("subreddit_or_query"),
                cooldown_minutes=account.get("cooldown_minutes", 15),
            )
            if not allowed:
                logger.debug(f"Rate limited: {reason}")
                self.db.log_decision(
                    "rate_limited", platform, proj_name,
                    account["username"], opp["target_id"],
                    details=reason, outcome="skipped",
                )
                continue

            if self.dedup.is_target_already_hit(opp["target_id"]):
                self.db.update_opportunity_status(
                    opp["target_id"], "skipped", rejection_reason="already_acted",
                )
                self.db.log_decision(
                    "dedup_blocked", platform, proj_name,
                    account["username"], opp["target_id"],
                    details="Target already acted on", outcome="skipped",
                )
                continue

            # Thread-awareness: skip if any account already commented
            if self.dedup.was_thread_recently_hit(opp["target_id"], hours=6):
                logger.debug(
                    f"Thread already hit by another account: {opp['target_id']}"
                )
                self.db.update_opportunity_status(
                    opp["target_id"], "skipped",
                    rejection_reason="thread_already_hit",
                )
                self.db.log_decision(
                    "dedup_blocked", platform, proj_name,
                    account["username"], opp["target_id"],
                    details="Thread already hit by another account",
                    outcome="skipped",
                )
                continue

            # SAFETY: Resource check before LLM call + posting
            if not self._check_resources():
                logger.info("Action skipped: resources too low")
                self.db.log_decision(
                    "resource_low", platform, proj_name,
                    details="System resources too low", outcome="aborted",
                )
                return

            # Smart scheduling: delay if current time slot is bad
            if platform == "reddit":
                delay = self.strategy.should_delay_action(project)
                if delay and delay > 0:
                    logger.info(
                        f"Delaying action by ~{delay}min (better time slot soon)"
                    )
                    self.db.log_decision(
                        "delayed", platform, proj_name,
                        target_id=opp["target_id"],
                        details=f"Better time slot in ~{delay}min",
                        outcome="delayed",
                    )
                    continue

            # Inject community stage for stage-aware promotion
            if platform == "reddit":
                sub = opp.get("subreddit_or_query", opp.get("subreddit", ""))
                try:
                    presence = self.db.get_presence_for_subreddit(
                        sub, proj_name, account["username"]
                    )
                    if presence:
                        stage = self.strategy.determine_stage(presence)
                    else:
                        stage = "new"
                except Exception:
                    stage = "new"
                opp["_community_stage"] = stage

            # Inject research context for content enrichment
            try:
                topic = opp.get("title", "") or opp.get("keyword", "")
                ctx = self.research.get_context_for_topic(proj_name, topic)
                self.content_gen._research_context = ctx or ""

                # Inject failure avoidance rules
                sub = opp.get("subreddit_or_query", opp.get("subreddit", ""))
                if sub:
                    patterns = self.db.get_failure_patterns(proj_name, sub)
                    if patterns:
                        rules = "\n".join(
                            f"- {p['avoidance_rule']}"
                            for p in patterns[:5]
                            if p.get("avoidance_rule")
                        )
                        self.content_gen._failure_rules = rules
                    else:
                        self.content_gen._failure_rules = ""
                else:
                    self.content_gen._failure_rules = ""
            except Exception as e:
                logger.debug(f"Research context injection failed: {e}")
                self.content_gen._research_context = ""
                self.content_gen._failure_rules = ""

            score = opp.get("score") or opp.get("relevance_score") or 0
            logger.info(
                f"Acting on {platform} opportunity for {proj_name}: "
                f"{opp.get('title', '')[:50]} (score={score})"
            )
            self.db.log_decision(
                "select_opp", platform, proj_name,
                account["username"], opp["target_id"],
                details=f"score={score}, sub={opp.get('subreddit_or_query', '')}",
                outcome="acting",
            )

            try:
                if platform == "reddit":
                    bot = self._get_reddit_bot(account)
                elif platform == "twitter":
                    bot = self._get_twitter_bot(account)
                elif platform == "telegram":
                    bot = self._get_telegram_group_bot(account)
                else:
                    continue

                success = bot.act(opp, project)

                if success:
                    self.rate_limiter.record_action(
                        account["username"], platform
                    )
                    self.account_mgr.mark_healthy(
                        platform, account["username"]
                    )

                    try:
                        recent = self.db.get_recent_actions(
                            hours=1, account=account["username"],
                            platform=platform, limit=1,
                        )
                        if recent:
                            self.learning.record_outcome(
                                action_id=recent[0].get("id", 0),
                                platform=platform,
                                project=proj_name,
                                subreddit_or_query=opp.get("subreddit_or_query", ""),
                                keyword=opp.get("keyword", ""),
                                action_type="comment" if platform == "reddit" else "reply",
                                engagement_score=1.0,
                            )
                    except Exception as e:
                        logger.debug(f"Failed to record learning outcome: {e}")

                    # Update community presence after successful action
                    if platform == "reddit":
                        self._update_presence(
                            opp, proj_name, account["username"], "comment"
                        )
                        # Track relationship with post author
                        self._notice_relationship(
                            opp, proj_name, account["username"], platform
                        )
                        # Track per-account subreddit stats for authority building
                        sub = opp.get("subreddit_or_query", opp.get("subreddit", ""))
                        if sub:
                            try:
                                self.db.update_subreddit_stats(
                                    account["username"], "reddit", sub
                                )
                            except Exception:
                                pass

                    sub = opp.get("subreddit_or_query", "")
                    title_short = opp.get("title", "")[:40]
                    from dashboard.telegram_bot import (
                        _ACTION_DONE_MSGS, _TWITTER_ACTION_MSGS, _pick,
                    )
                    if platform == "reddit":
                        self._send_telegram_alert(
                            _pick(_ACTION_DONE_MSGS, sub=sub, title=title_short)
                        )
                    elif platform == "telegram":
                        group = opp.get("group_name", sub)
                        self._send_telegram_alert(
                            f"Replied in Telegram group {group}: {title_short}"
                        )
                    else:
                        self._send_telegram_alert(
                            _pick(_TWITTER_ACTION_MSGS, title=title_short)
                        )

                    return  # One action per cycle
                else:
                    # Mark opportunity as failed to prevent retry spam
                    self.db.update_opportunity_status(opp["target_id"], "failed")
                    # Graduated cooldown based on error type
                    cooldown = self._graduated_cooldown(
                        platform, account["username"]
                    )
                    self.account_mgr.mark_cooldown(
                        platform, account["username"], minutes=cooldown
                    )

            except Exception as e:
                logger.error(f"Action failed: {e}")
                # Mark opportunity as failed to prevent retry loops
                try:
                    self.db.update_opportunity_status(opp["target_id"], "failed")
                except Exception:
                    pass
                # Graduated cooldown from exception context
                cooldown = self._error_cooldown_from_exception(str(e))
                self.account_mgr.mark_cooldown(
                    platform, account["username"], minutes=cooldown
                )

    # ── Graduated Cooldown ──────────────────────────────────────
    def _graduated_cooldown(self, platform: str, account: str) -> int:
        """Determine cooldown minutes based on recent error type and frequency.

        Graduated response:
        - Content/validation failure: 3min (try again soon with new content)
        - Rate limit (429): 8min (Reddit clears quickly)
        - Auth/403 error: 15min (session needs refresh)
        - Suspicious/ban signal: 60min (serious warning)
        - Escalates on repeated failures: 2+ in 1h → 15min min, 4+ → 30min min
        """
        try:
            recent = self.db.get_recent_actions(
                hours=1, account=account, platform=platform, limit=5
            )
            recent_failures = sum(
                1 for a in recent if not a.get("success", True)
            )

            # Find the last error message for error-type detection
            last_error = ""
            for a in recent:
                if not a.get("success", True) and a.get("error_message"):
                    last_error = a["error_message"].lower()
                    break

            base = self._error_cooldown_from_message(last_error)

            # Escalate on repeated failures
            if recent_failures >= 4:
                base = max(base, 30)
            elif recent_failures >= 2:
                base = max(base, 15)

            return base
        except Exception:
            return 10  # Safe fallback

    @staticmethod
    def _error_cooldown_from_message(error_msg: str) -> int:
        """Map error message content to a cooldown duration in minutes."""
        msg = error_msg.lower() if error_msg else ""
        if "banned" in msg or "suspended" in msg or "shadowban" in msg:
            return 60
        if "daily limit" in msg or "daily_limit" in msg:
            return 120  # Twitter daily limit — back off 2 hours
        if "403" in msg or "forbidden" in msg:
            return 15
        if "429" in msg or "rate limit" in msg or "ratelimit" in msg:
            return 8
        if "circuit breaker" in msg:
            return 30
        # Content validation, auth refresh, generic failures
        return 3

    @staticmethod
    def _error_cooldown_from_exception(exc_str: str) -> int:
        """Map exception string to cooldown duration."""
        msg = exc_str.lower()
        if "banned" in msg or "suspended" in msg:
            return 60
        if "daily limit" in msg or "daily_limit" in msg:
            return 120  # Twitter daily limit — back off 2 hours
        if "403" in msg or "forbidden" in msg:
            return 15
        if "429" in msg or "rate" in msg:
            return 8
        if "timeout" in msg or "timed out" in msg:
            return 5
        return 5  # Generic exception → short cooldown

    def _engage_safe(self):
        """Wrapper: run engagement with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._engage)
            try:
                future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                logger.warning("Engagement ABORTED: exceeded 120s timeout")
            except Exception as e:
                logger.error(f"Engagement error: {e}")

    def _engage(self):
        """Run organic engagement: upvote, subscribe, like, follow.

        Keeps accounts looking natural between actions.
        """
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        logger.info("Running engagement cycle...")

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")

            # Reddit engagement
            account = self.account_mgr.get_next_account("reddit")
            if account and self._check_resources():
                try:
                    bot = self._get_reddit_bot(account)
                    if hasattr(bot, "warm_up"):
                        stats = bot.warm_up(project)
                        if any(v > 0 for v in stats.values()):
                            # Record engagement so rate limiter knows about it
                            self.rate_limiter.record_action(
                                account["username"], "reddit"
                            )
                            from dashboard.telegram_bot import _ENGAGE_REDDIT_MSGS, _pick
                            self._send_telegram_alert(_pick(
                                _ENGAGE_REDDIT_MSGS,
                                proj=proj_name,
                                sub=stats['subscribed'],
                                up=stats['upvoted'],
                                sv=stats['saved'],
                            ))
                except Exception as e:
                    logger.error(f"Reddit engagement error: {e}")

            # Twitter engagement — DISABLED: server IP blocked (code 226)

            time.sleep(random.uniform(5, 15))

        logger.info("Engagement cycle complete")

    def _curate_and_share(self):
        """Find and share curated content (YouTube videos, news articles).

        Makes the account look like a real human who shares interesting
        stuff, not just a bot that only promotes products.
        """
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return
        if not self._check_resources():
            return

        logger.info("Running content curation cycle...")
        shared = 0

        for project in self.projects:
            if shared >= 2:  # Max 2 curated posts per cycle
                break

            proj_name = project.get("project", {}).get("name", "unknown")
            account = self.account_mgr.get_next_account("reddit")
            if not account:
                continue

            allowed, reason = self.rate_limiter.can_act(
                account["username"], "reddit",
                cooldown_minutes=account.get("cooldown_minutes", 15),
            )
            if not allowed:
                continue

            try:
                ideas = self.curator.get_content_ideas(project, platform="reddit")
                if not ideas:
                    continue

                # Pick the best idea (YouTube > news)
                idea = ideas[0]
                content = idea.get("content", {})

                # Get a relevant subreddit
                subs = project.get("reddit", {}).get("target_subreddits", {})
                if isinstance(subs, dict):
                    all_subs = subs.get("primary", []) + subs.get("secondary", [])
                else:
                    all_subs = subs if isinstance(subs, list) else []

                if not all_subs:
                    continue

                sub = random.choice(all_subs)
                bot = self._get_reddit_bot(account)

                if idea["type"] == "youtube_share" and hasattr(bot, "create_post"):
                    title = content.get("title", "")[:290]
                    body = content.get("body", "")
                    url = bot.create_post(sub, title, body, project)
                    if url:
                        self.curator._mark_shared(idea["source_url"])
                        self.rate_limiter.record_action(account["username"], "reddit")
                        from dashboard.telegram_bot import _YOUTUBE_SHARE_MSGS, _pick
                        self._send_telegram_alert(
                            _pick(_YOUTUBE_SHARE_MSGS, sub=sub, title=title[:50])
                        )
                        shared += 1

                elif idea["type"] == "news_discussion" and hasattr(bot, "create_post"):
                    title = f"{content.get('title', '')}"[:290]
                    source = content.get("source", "")
                    article_url = content.get("url", "")
                    body = f"Interesting article"
                    if source:
                        body += f" from {source}"
                    body += f":\n\n{article_url}\n\nThoughts?"

                    url = bot.create_post(sub, title, body, project)
                    if url:
                        self.curator._mark_shared(idea["source_url"])
                        self.rate_limiter.record_action(account["username"], "reddit")
                        from dashboard.telegram_bot import _NEWS_SHARE_MSGS, _pick
                        self._send_telegram_alert(
                            _pick(_NEWS_SHARE_MSGS, sub=sub, title=title[:50])
                        )
                        shared += 1

                time.sleep(random.uniform(10, 30))

            except Exception as e:
                logger.error(f"Content curation failed for {proj_name}: {e}")

        if shared:
            logger.info(f"Content curation: shared {shared} pieces")
        else:
            logger.debug("Content curation: nothing shared this cycle")

    def _learn(self):
        """Run the learning cycle — analyze performance and adapt.

        Self-improvement loop:
        1. Learn weights from past outcomes
        2. Auto-approve high-quality discoveries (new subreddits/keywords)
        3. Adapt organic/promo ratio based on what works
        4. Report insights via Telegram
        """
        if self._paused:
            return
        if not self._check_resources():
            return
        try:
            logger.info("Running learning cycle...")
            self.learning.learn()
            insights = self.learning.get_insights()

            # --- Auto-approve discoveries ---
            self._auto_approve_discoveries()

            # --- Adapt promo ratio from learning ---
            self._adapt_promo_ratio(insights)

            # --- A/B Testing ---
            try:
                self.ab_testing.evaluate_experiments()
                for proj in self.projects:
                    self.ab_testing.auto_create_experiments(proj)
            except Exception as e:
                logger.debug(f"A/B testing evaluation failed: {e}")

            # --- Report ---
            top_subs = insights.get("top_subreddits", [])
            if top_subs:
                sub_names = ", ".join(f"r/{s['name']}" for s in top_subs[:3])
                ratio = insights.get("optimal_promo_ratio", 0.2)
                logger.info(
                    f"Learning insights: top subs = {sub_names}, "
                    f"promo ratio = {ratio:.0%}"
                )
                pending = insights.get("pending_discoveries", 0)
                from dashboard.telegram_bot import _LEARN_MSGS, _pick
                self._send_telegram_alert(_pick(
                    _LEARN_MSGS,
                    subs=sub_names,
                    ratio=f"{ratio:.0%}",
                    disc=pending,
                ))
            pending = insights.get("pending_discoveries", 0)
            if pending > 0:
                logger.info(f"Learning: {pending} new targets discovered")

            # --- Adaptive learning interval ---
            try:
                new_interval = self._get_adaptive_learning_interval()
                job = self.scheduler.get_job("learn")
                if job:
                    current_hours = 6
                    try:
                        current_hours = job.trigger.interval.total_seconds() / 3600
                    except Exception:
                        pass
                    if abs(new_interval - current_hours) >= 1:
                        self.scheduler.reschedule_job(
                            "learn", trigger="interval", hours=new_interval
                        )
                        logger.info(
                            f"Adaptive learning: interval changed "
                            f"{current_hours:.0f}h -> {new_interval:.0f}h"
                        )
            except Exception as e:
                logger.debug(f"Adaptive learning interval failed: {e}")

        except Exception as e:
            logger.error(f"Learning cycle failed: {e}")

    def _auto_approve_discoveries(self):
        """Auto-approve discovered subreddits/keywords with score >= 5.

        This closes the self-improvement loop: discoveries from LLM
        get added to scan targets automatically without manual review.
        """
        try:
            for proj in self.projects:
                proj_name = proj.get("project", {}).get("name", "unknown")
                candidates = self.db.get_discoveries(
                    project=proj_name, status="candidate",
                )
                approved = 0
                for disc in candidates:
                    if disc.get("score", 0) >= 5.0:
                        self.db.update_discovery_status(
                            disc["id"], "approved"
                        )
                        approved += 1
                if approved:
                    logger.info(
                        f"Auto-approved {approved} discoveries for {proj_name}"
                    )
        except Exception as e:
            logger.debug(f"Auto-approve discoveries failed: {e}")

    def _adapt_promo_ratio(self, insights: dict):
        """Dynamically adjust organic/promo ratio based on learning.

        If promotional posts get removed more, the ratio goes down.
        If they get good engagement, the ratio goes up.
        """
        try:
            learned_ratio = insights.get("optimal_promo_ratio")
            if learned_ratio is not None and learned_ratio > 0:
                current = self.content_gen.organic_ratio
                # organic_ratio = 1 - promo_ratio
                new_organic = 1.0 - learned_ratio
                new_organic = max(0.6, min(new_organic, 0.95))  # clamp

                if abs(new_organic - current) > 0.03:
                    self.content_gen.organic_ratio = new_organic
                    logger.info(
                        f"Adapted promo ratio: "
                        f"{1-current:.0%} -> {1-new_organic:.0%} promo "
                        f"(from learning)"
                    )
        except Exception as e:
            logger.debug(f"Promo ratio adaptation failed: {e}")

    def _verify_comments(self):
        """Verify recent comments weren't removed by mods/spam filter."""
        if self._paused:
            return
        if not self._check_resources():
            return

        logger.info("Verifying recent comments...")
        verified = 0
        removed = 0

        recent = self.db.get_recent_actions(
            hours=12, platform="reddit", limit=10  # Reduced from 20
        )
        for action in recent:
            if action.get("action_type") != "comment":
                continue

            # Resource check between each verification
            if not self._check_resources():
                logger.info("Comment verification paused: resources low")
                break

            metadata = action.get("metadata", "")
            if isinstance(metadata, str):
                try:
                    meta = json.loads(metadata) if metadata else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            else:
                meta = metadata or {}

            comment_id = meta.get("comment_id", "")
            if not comment_id or comment_id == "unknown":
                continue

            account = self.account_mgr.get_next_account("reddit")
            if not account:
                break

            try:
                bot = self._get_reddit_bot(account)
                if hasattr(bot, "verify_comment"):
                    result = bot.verify_comment(comment_id)
                    verified += 1

                    if result.get("removed"):
                        removed += 1
                        logger.warning(
                            f"Comment {comment_id} was REMOVED"
                        )
                        try:
                            self.learning.record_outcome(
                                action_id=action.get("id", 0),
                                platform="reddit",
                                project=action.get("project", ""),
                                subreddit_or_query=meta.get("subreddit", ""),
                                was_removed=True,
                                engagement_score=0.0,
                                post_type=meta.get("post_type", ""),
                            )
                        except Exception:
                            pass
                        # Update presence: mark removal
                        self._update_presence_reputation(
                            meta.get("subreddit", ""),
                            action.get("project", ""),
                            action.get("account", ""),
                            removed=True,
                        )
                    elif result.get("exists"):
                        try:
                            self.learning.record_outcome(
                                action_id=action.get("id", 0),
                                platform="reddit",
                                project=action.get("project", ""),
                                subreddit_or_query=meta.get("subreddit", ""),
                                upvotes=result.get("upvotes", 0),
                                replies=result.get("replies", 0),
                                engagement_score=max(1.0,
                                    result.get("upvotes", 0) * 0.5
                                    + result.get("replies", 0) * 2.0
                                ),
                                post_type=meta.get("post_type", ""),
                            )
                        except Exception:
                            pass

                        # Sentiment analysis on replies
                        try:
                            reply_bodies = result.get("reply_bodies", [])
                            if reply_bodies:
                                sentiment = self.learning.analyze_reply_sentiment(
                                    reply_bodies
                                )
                                if abs(sentiment["score"]) > 0.1:
                                    self.db.log_reply_sentiment(
                                        action_id=action.get("id", 0),
                                        project=action.get("project", ""),
                                        subreddit=meta.get("subreddit", ""),
                                        tone_style=meta.get("tone", ""),
                                        post_type=meta.get("post_type", ""),
                                        sentiment_score=sentiment["score"],
                                        reply_count=len(reply_bodies),
                                        positive_signals=json.dumps(
                                            sentiment["positive"]
                                        ),
                                        negative_signals=json.dumps(
                                            sentiment["negative"]
                                        ),
                                    )
                        except Exception:
                            pass

                        # Update presence: mark surviving + karma
                        self._update_presence_reputation(
                            meta.get("subreddit", ""),
                            action.get("project", ""),
                            action.get("account", ""),
                            removed=False,
                            upvotes=result.get("upvotes", 0),
                            replies=result.get("replies", 0),
                        )

                    time.sleep(2)

            except Exception as e:
                logger.debug(f"Comment verification failed: {e}")

        if verified > 0:
            logger.info(
                f"Comment verification: {verified} checked, "
                f"{removed} removed"
            )
            if removed > 0:
                from dashboard.telegram_bot import _COMMENT_CHECK_MSGS, _pick
                self._send_telegram_alert(
                    _pick(_COMMENT_CHECK_MSGS, n=verified, r=removed)
                )

    def _seed_content(self):
        """Create autonomous user-style posts in target subreddits.

        Evolved from simple seed posts to diverse content types that
        simulate real user behavior: tips, questions, tutorials,
        comparisons, experience reports, discovery posts, etc.
        """
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return
        if not self._check_resources():
            return

        for project in self.projects:
            account = self.account_mgr.get_next_account("reddit")
            if not account:
                continue

            # New strategy: smart decision based on stage, limits, and post type
            decision = self.strategy.should_create_user_post(
                project, account["username"]
            )
            if not decision:
                # Fallback: old seed behavior
                sub = self.strategy.should_seed_subreddit(project)
                if sub:
                    decision = {
                        "subreddit": sub,
                        "post_type": "tip",
                        "is_promotional": False,
                        "trend_context": "",
                    }
                else:
                    continue

            allowed, reason = self.rate_limiter.can_act(
                account["username"], "reddit",
                cooldown_minutes=account.get("cooldown_minutes", 15),
            )
            if not allowed:
                continue

            try:
                bot = self._get_reddit_bot(account)
                proj_name = project.get("project", {}).get("name", "unknown")
                ptype = decision["post_type"]
                sub = decision["subreddit"]

                # Downgrade trend_react if no context
                trend_ctx = decision.get("trend_context", "")
                if ptype == "trend_react" and not trend_ctx:
                    ptype = "tip"

                # A/B testing: override post_type if experiment running
                ab_post_type_info = None
                ab_length_info = None
                try:
                    exp_id, variant, value = self.ab_testing.get_variant(
                        proj_name, "post_type"
                    )
                    if variant and value:
                        ptype = value
                        ab_post_type_info = (exp_id, variant)
                except Exception:
                    pass

                # A/B testing: content length experiment
                target_length = ""
                try:
                    exp_id2, variant2, value2 = self.ab_testing.get_variant(
                        proj_name, "content_length"
                    )
                    if variant2 and value2:
                        target_length = value2
                        ab_length_info = (exp_id2, variant2)
                except Exception:
                    pass

                logger.info(
                    f"Creating user post ({ptype}) in r/{sub} for {proj_name}"
                )

                # Generate diverse post content
                post_data = self.content_gen.generate_user_post(
                    subreddit=sub,
                    project=project,
                    post_type=ptype,
                    is_promotional=decision["is_promotional"],
                    trend_context=trend_ctx,
                    target_length=target_length,
                )

                if not post_data.get("title") or not post_data.get("body"):
                    logger.warning("User post generation returned empty content")
                    continue

                # Dedup check
                if self.dedup.is_duplicate_content(
                    post_data["body"][:200], "reddit",
                ):
                    logger.info("User post skipped: too similar to recent content")
                    continue

                url = bot.create_post(
                    subreddit=sub,
                    title=post_data["title"],
                    body=post_data["body"],
                    project=project,
                )

                if url:
                    self.rate_limiter.record_action(
                        account["username"], "reddit"
                    )
                    promo_label = "promo" if decision["is_promotional"] else "organic"

                    # Log with post_type metadata for learning
                    action_id = self.db.log_action(
                        platform="reddit",
                        action_type="user_post",
                        account=account["username"],
                        project=proj_name,
                        target_id=f"user_post_{sub}",
                        content=f"{post_data['title']}\n\n{post_data['body'][:300]}",
                        metadata=json.dumps({
                            "subreddit": sub,
                            "post_type": ptype,
                            "promotional": decision["is_promotional"],
                            "url": url,
                        }),
                    )

                    # Record outcome for post-type learning
                    try:
                        self.learning.record_outcome(
                            action_id=action_id,
                            platform="reddit",
                            project=proj_name,
                            subreddit_or_query=sub,
                            action_type="user_post",
                            was_promotional=decision["is_promotional"],
                            engagement_score=1.0,
                            post_type=ptype,
                        )
                    except Exception:
                        pass

                    # Record A/B experiment results
                    try:
                        if ab_post_type_info:
                            self.ab_testing.record_result(
                                ab_post_type_info[0], action_id,
                                ab_post_type_info[1], engagement=1.0,
                            )
                        if ab_length_info:
                            self.ab_testing.record_result(
                                ab_length_info[0], action_id,
                                ab_length_info[1], engagement=1.0,
                            )
                    except Exception:
                        pass

                    self._send_telegram_alert(
                        f"User post ({ptype}/{promo_label}) in r/{sub} "
                        f"for {proj_name}:\n{post_data['title'][:80]}\n{url}"
                    )

            except Exception as e:
                logger.error(f"User post failed: {e}")

    def _tweet_cycle_safe(self):
        """Wrapper: run tweet creation with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._tweet_cycle)
            try:
                future.result(timeout=90)
            except concurrent.futures.TimeoutError:
                logger.warning("Tweet cycle ABORTED: exceeded 90s timeout")
            except Exception as e:
                logger.error(f"Tweet cycle error: {e}")

    def _tweet_cycle(self):
        """Create original tweets for each project.

        DISABLED: Twitter blocked on server IP (code 226).
        """
        return  # Twitter disabled
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return
        if not self._check_resources():
            return

        tweeted = 0

        for project in self.projects:
            if tweeted >= 2:
                break

            proj_name = project.get("project", {}).get("name", "unknown")
            account = self.account_mgr.get_next_account("twitter")
            if not account:
                continue

            allowed, reason = self.rate_limiter.can_act(
                account["username"], "twitter",
                cooldown_minutes=account.get("cooldown_minutes", 20),
            )
            if not allowed:
                continue

            try:
                bot = self._get_twitter_bot(account)

                # Build tweet context from research + project
                proj_info = project.get("project", {})
                desc = proj_info.get("description", "")
                audiences = proj_info.get("target_audiences", [])

                # Decide promotional or organic
                is_promo = self.content_gen._should_be_promotional()

                context = f"Project: {proj_name} — {desc}\nAudience: {', '.join(audiences[:3])}"

                # Add trending context if available
                try:
                    topic = proj_info.get("name", "")
                    research_ctx = self.research.get_context_for_topic(proj_name, topic)
                    if research_ctx:
                        context += f"\nTrending context: {research_ctx[:200]}"
                except Exception:
                    pass

                tweet_text = self.content_gen.generate_twitter_tweet(
                    context=context,
                    project=project,
                    persona=account.get("persona", "tech_enthusiast"),
                    is_promotional=is_promo,
                )

                if not tweet_text or len(tweet_text) > 280:
                    continue

                success = _run_async_safe(bot.post_tweet_async(tweet_text, proj_name))

                if success:
                    tweeted += 1
                    self.rate_limiter.record_action(account["username"], "twitter")
                    self.account_mgr.mark_healthy("twitter", account["username"])

                    label = "promo" if is_promo else "organic"
                    logger.info(f"Tweet posted ({label}) for {proj_name}: {tweet_text[:50]}")
                    self._send_telegram_alert(
                        f"Tweet ({label}) for {proj_name}:\n{tweet_text[:100]}"
                    )
                    time.sleep(random.uniform(10, 30))

            except Exception as e:
                logger.error(f"Tweet cycle error for {proj_name}: {e}")

        if tweeted:
            logger.info(f"Tweet cycle complete: {tweeted} tweets posted")

        # Cross-platform: share recent Reddit user posts as tweets
        cross_cfg = self.settings.get("user_posts", {})
        if cross_cfg.get("cross_platform_share", True) and tweeted < 2:
            try:
                self._cross_platform_share(tweeted)
            except Exception as e:
                logger.error(f"Cross-platform share error: {e}")

    def _cross_platform_share(self, already_tweeted: int = 0):
        """Share recent Reddit user posts as tweets for cross-platform presence."""
        max_shares = 2 - already_tweeted
        if max_shares <= 0:
            return

        # Find recent Reddit user posts not yet shared to Twitter
        recent_posts = self.db.get_recent_actions_by_type(
            action_type="user_post", hours=48, platform="reddit"
        )
        if not recent_posts:
            return

        shared = 0
        for action in recent_posts:
            if shared >= max_shares:
                break

            # Parse metadata to check if already shared
            try:
                meta = json.loads(action.get("metadata", "{}") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            if meta.get("shared_to_twitter"):
                continue

            reddit_url = meta.get("url", "")
            if not reddit_url:
                continue

            # Find a Twitter account
            account = self.account_mgr.get_next_account("twitter")
            if not account:
                break

            allowed, reason = self.rate_limiter.can_act(
                account["username"], "twitter",
                cooldown_minutes=account.get("cooldown_minutes", 20),
            )
            if not allowed:
                continue

            try:
                # Find project for this post
                proj_name = action.get("project", "")
                project = None
                for p in self.projects:
                    if p.get("project", {}).get("name", "") == proj_name:
                        project = p
                        break
                if not project:
                    continue

                bot = self._get_twitter_bot(account)
                post_type = meta.get("post_type", "tip")

                tweet_text = self.content_gen.generate_user_tweet(
                    project=project,
                    tweet_type=post_type,
                    is_promotional=meta.get("promotional", False),
                    trend_context="",
                    reddit_url=reddit_url,
                )

                if not tweet_text or len(tweet_text) > 280:
                    continue

                success = _run_async_safe(bot.post_tweet_async(tweet_text, proj_name))

                if success:
                    shared += 1
                    self.rate_limiter.record_action(account["username"], "twitter")

                    # Mark original Reddit post as shared
                    meta["shared_to_twitter"] = True
                    self.db.update_action_metadata(action["id"], meta)

                    logger.info(
                        f"Cross-shared Reddit post to Twitter for {proj_name}: "
                        f"{tweet_text[:50]}"
                    )
                    self._send_telegram_alert(
                        f"Cross-share tweet for {proj_name}:\n"
                        f"{tweet_text[:100]}\nSource: {reddit_url}"
                    )
                    time.sleep(random.uniform(10, 30))

            except Exception as e:
                logger.error(f"Cross-platform share failed for {proj_name}: {e}")

        if shared:
            logger.info(f"Cross-platform sharing: {shared} Reddit posts shared to Twitter")

    def _health_check(self):
        """Run periodic health checks on all accounts."""
        if self._paused:
            return
        if not self._check_resources():
            return

        logger.info("Running health check...")

        for platform in ("reddit", "twitter"):
            accounts = self.account_mgr.load_accounts(platform)
            for account in accounts:
                self.db.update_account_health(
                    platform, account["username"]
                )

        # Shadowban checks
        for account in self.account_mgr.load_accounts("reddit"):
            username = account["username"]
            try:
                bot = self._get_reddit_bot(account)
                result = self.ban_detector.check_reddit_shadowban(
                    bot.reddit, username
                )
                if result["is_shadowbanned"]:
                    self.account_mgr.mark_warned(
                        "reddit", username,
                        f"Possible shadowban: {result['indicators']}"
                    )
                    from dashboard.telegram_bot import _SHADOWBAN_MSGS, _pick
                    self._send_telegram_alert(_pick(
                        _SHADOWBAN_MSGS,
                        user=username,
                        signs=", ".join(result['indicators']),
                    ))
            except Exception as e:
                logger.error(f"Health check failed for u/{username}: {e}")

        logger.info("Health check complete")

    # ── Phase 5: Intelligence + Self-Improvement ────────────────────

    def _analyze_subreddits_safe(self):
        """Wrapper: run subreddit intelligence with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._analyze_subreddits)
            try:
                future.result(timeout=180)
            except concurrent.futures.TimeoutError:
                logger.warning("Subreddit intel ABORTED: exceeded 180s timeout")
            except Exception as e:
                logger.error(f"Subreddit intel error: {e}")

    def _analyze_subreddits(self):
        """Analyze subreddits for all projects — discover high-opportunity communities."""
        if self._paused:
            return
        if not self._check_resources():
            return

        logger.info("Running subreddit intelligence cycle...")
        total_analyzed = 0

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")

            # Collect subreddits to analyze
            subs = project.get("reddit", {}).get("target_subreddits", {})
            if isinstance(subs, dict):
                all_subs = subs.get("primary", []) + subs.get("secondary", [])
            elif isinstance(subs, list):
                all_subs = subs
            else:
                all_subs = []

            # Also include discovered/approved subs
            try:
                expanded = self.strategy.get_expanded_subreddits(project)
                for s in expanded:
                    if s not in all_subs:
                        all_subs.append(s)
            except Exception:
                pass

            # Get stale ones first (not analyzed recently)
            try:
                stale = self.db.get_stale_subreddits(proj_name, hours=24)
                stale_names = {s["subreddit"] for s in stale}
                # Prioritize stale subs
                prioritized = [s for s in all_subs if s in stale_names]
                rest = [s for s in all_subs if s not in stale_names]
                all_subs = prioritized + rest
            except Exception:
                pass

            # Analyze up to 10 per project per cycle
            for sub in all_subs[:10]:
                if not self._check_resources():
                    break

                try:
                    intel = self.subreddit_intel.analyze_subreddit(sub)
                    if intel:
                        score = self.subreddit_intel.score_subreddit_opportunity(
                            intel, project
                        )
                        self.subreddit_intel.store_intel(
                            sub, proj_name, intel, score
                        )
                        total_analyzed += 1

                        # Alert on high-opportunity discoveries
                        if score > 7.0:
                            self._send_telegram_alert(
                                f"High-opportunity subreddit found: r/{sub} "
                                f"(score: {score:.1f}/10, "
                                f"{intel.get('subscribers', 0):,} members)"
                            )
                except Exception as e:
                    logger.debug(f"Failed to analyze r/{sub}: {e}")

        logger.info(f"Subreddit intel: analyzed {total_analyzed} subreddits")

    def _maintain_presence_safe(self):
        """Wrapper: run community presence maintenance with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._maintain_presence)
            try:
                future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                logger.warning("Presence maintenance ABORTED: exceeded 120s timeout")
            except Exception as e:
                logger.error(f"Presence maintenance error: {e}")

    def _maintain_presence(self):
        """Maintain community presence in neglected subreddits.

        Upvote, save, subscribe — light engagement to stay active.
        """
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        logger.info("Running community presence maintenance...")
        actions_taken = 0

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")
            account = self.account_mgr.get_next_account("reddit")
            if not account:
                continue

            # Find subreddits needing activity
            try:
                neglected = self.strategy.get_subreddits_needing_activity(
                    project, account["username"]
                )
            except Exception:
                neglected = []

            for sub in neglected[:5]:  # Max 5 subs per cycle
                if not self._check_resources():
                    break
                if actions_taken >= 5:
                    break

                try:
                    bot = self._get_reddit_bot(account)
                    if hasattr(bot, "warm_up_subreddit"):
                        stats = bot.warm_up_subreddit(sub)
                        if stats and any(v > 0 for v in stats.values()):
                            actions_taken += 1
                            # Update presence record
                            self._update_presence(
                                {"subreddit_or_query": sub, "subreddit": sub},
                                proj_name,
                                account["username"],
                                "engagement",
                            )
                    elif hasattr(bot, "warm_up"):
                        # Fallback to general warm_up
                        stats = bot.warm_up(project)
                        if stats and any(v > 0 for v in stats.values()):
                            actions_taken += 1
                except Exception as e:
                    logger.debug(f"Presence maintenance failed for r/{sub}: {e}")

                time.sleep(random.uniform(3, 8))

        if actions_taken:
            logger.info(f"Presence maintenance: {actions_taken} subreddits engaged")

    def _research_safe(self):
        """Wrapper: run research cycle with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._run_research)
            try:
                future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                logger.warning("Research ABORTED: exceeded 120s timeout")
            except Exception as e:
                logger.error(f"Research error: {e}")

    def _run_research(self):
        """Run research cycle for all projects — build knowledge base."""
        if self._paused:
            return
        if not self._check_resources():
            return

        logger.info("Running research cycle...")

        for project in self.projects:
            try:
                self.research.run_research(project)
            except Exception as e:
                proj_name = project.get("project", {}).get("name", "unknown")
                logger.error(f"Research failed for {proj_name}: {e}")

        logger.info("Research cycle complete")

    def _send_weekly_report(self):
        """Send weekly performance report via Telegram."""
        if not self.telegram:
            return

        try:
            report = "Weekly Performance Report\n\n"

            for project in self.projects:
                proj_name = project.get("project", {}).get("name", "unknown")

                # Get benchmark comparison
                try:
                    bench = self.learning.get_performance_benchmark(proj_name)
                    report += f"--- {proj_name} ---\n"
                    report += (
                        f"Actions: {bench.get('this_week_actions', 0)} "
                        f"(vs {bench.get('last_week_actions', 0)} last week)\n"
                    )
                    eng_delta = bench.get("engagement_delta", 0)
                    direction = "up" if eng_delta > 0 else "down"
                    report += (
                        f"Engagement: {direction} {abs(eng_delta):.0%}\n"
                    )
                    report += (
                        f"Removal rate: {bench.get('removal_rate', 0):.0%}\n"
                    )
                except Exception:
                    report += f"--- {proj_name} ---\n(no data yet)\n"

                # Active experiments
                try:
                    experiments = self.ab_testing.get_active_experiments(proj_name)
                    if experiments:
                        report += f"Active A/B tests: {len(experiments)}\n"
                        for exp in experiments:
                            report += (
                                f"  - {exp['experiment_name']}: "
                                f"{exp['variant_a']} vs {exp['variant_b']}\n"
                            )
                except Exception:
                    pass

                # Top subreddits
                try:
                    top = self.subreddit_intel.get_top_opportunities(proj_name, 3)
                    if top:
                        names = ", ".join(f"r/{t['subreddit']}" for t in top)
                        report += f"Top opportunities: {names}\n"
                except Exception:
                    pass

                report += "\n"

            report += "Cost: $0.00 (all free-tier)"
            self._send_telegram_alert(report)
            logger.info("Weekly report sent")

        except Exception as e:
            logger.error(f"Weekly report failed: {e}")

    def _update_presence(
        self, opp: Dict, project: str, account: str, action_type: str,
    ):
        """Update community presence after a successful action."""
        try:
            sub = opp.get("subreddit_or_query", opp.get("subreddit", ""))
            if not sub:
                return

            # Get current presence
            presence = self.db.get_presence_for_subreddit(sub, project, account)
            from datetime import datetime
            now = datetime.now().isoformat()

            if presence:
                updates = {"last_activity": now}
                if action_type == "comment":
                    updates["total_comments"] = presence.get("total_comments", 0) + 1
                elif action_type == "post":
                    updates["total_posts"] = presence.get("total_posts", 0) + 1
                elif action_type == "engagement":
                    updates["total_upvotes_given"] = presence.get("total_upvotes_given", 0) + 1

                # Recompute days_active
                first = presence.get("first_activity", now)
                try:
                    from datetime import datetime as dt
                    first_dt = dt.fromisoformat(first)
                    days = (dt.now() - first_dt).days
                    updates["days_active"] = max(1, days)
                except Exception:
                    pass

                # Recompute warmth + stage
                merged = {**presence, **updates}
                warmth = self.strategy.compute_warmth_score(merged)
                stage = self.strategy.determine_stage(merged)
                updates["warmth_score"] = warmth
                updates["stage"] = stage

                self.db.upsert_community_presence(
                    sub, project, account, **updates
                )
            else:
                # First interaction — create new record
                self.db.upsert_community_presence(
                    sub, project, account,
                    total_comments=1 if action_type == "comment" else 0,
                    total_posts=1 if action_type == "post" else 0,
                    total_upvotes_given=1 if action_type == "engagement" else 0,
                    first_activity=now,
                    last_activity=now,
                    days_active=1,
                    warmth_score=0.0,
                    stage="new",
                )

        except Exception as e:
            logger.debug(f"Presence update failed: {e}")

    def _update_presence_reputation(
        self, subreddit: str, project: str, account: str,
        removed: bool = False, upvotes: int = 0, replies: int = 0,
    ):
        """Update presence reputation after comment verification."""
        try:
            if not subreddit or not project or not account:
                return

            presence = self.db.get_presence_for_subreddit(
                subreddit, project, account
            )
            if not presence:
                return

            updates = {}
            if removed:
                updates["comments_removed"] = presence.get("comments_removed", 0) + 1
            else:
                updates["comments_surviving"] = presence.get("comments_surviving", 0) + 1
                updates["karma_earned"] = presence.get("karma_earned", 0) + max(0, upvotes)
                updates["total_replies_received"] = (
                    presence.get("total_replies_received", 0) + replies
                )

                # Recalculate avg comment score
                total_surviving = updates.get(
                    "comments_surviving",
                    presence.get("comments_surviving", 0),
                )
                total_karma = updates.get(
                    "karma_earned", presence.get("karma_earned", 0)
                )
                if total_surviving > 0:
                    updates["avg_comment_score"] = total_karma / total_surviving

            # Recalculate reputation
            surviving = updates.get("comments_surviving", presence.get("comments_surviving", 0))
            removed_count = updates.get("comments_removed", presence.get("comments_removed", 0))
            total = surviving + removed_count
            if total > 0:
                updates["reputation_score"] = surviving / total * 10.0

            # Recompute warmth + stage
            merged = {**presence, **updates}
            warmth = self.strategy.compute_warmth_score(merged)
            stage = self.strategy.determine_stage(merged)
            updates["warmth_score"] = warmth
            updates["stage"] = stage

            self.db.upsert_community_presence(
                subreddit, project, account, **updates
            )

        except Exception as e:
            logger.debug(f"Presence reputation update failed: {e}")

    def _get_adaptive_learning_interval(self) -> int:
        """Calculate adaptive learning interval based on recent activity.

        More actions → learn more frequently.
        Returns hours between learning cycles.
        """
        try:
            recent = self.db.get_recent_actions(hours=24, limit=100)
            count = len(recent) if recent else 0

            if count >= 20:
                return 1  # Very active: learn every hour
            elif count >= 10:
                return 2  # Active: every 2 hours
            elif count >= 5:
                return 4  # Moderate: every 4 hours
            else:
                return 6  # Low activity: default 6 hours
        except Exception:
            return 6

    # ── Phase 6: Relationship Building ──────────────────────────────

    def _build_relationships_safe(self):
        """Wrapper: run relationship building with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._build_relationships)
            try:
                future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                logger.warning("Relationship building ABORTED: exceeded 120s timeout")
            except Exception as e:
                logger.error(f"Relationship building error: {e}")

    def _build_relationships(self):
        """Run relationship building cycle for all projects."""
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        logger.info("Running relationship building cycle...")
        total_stats = {"targets_found": 0, "dms_sent": 0, "inbox_processed": 0}

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")

            # Get bots for this project (Twitter disabled: server IP blocked)
            reddit_bot = None
            twitter_bot = None

            account = self.account_mgr.get_next_account("reddit")
            if account:
                reddit_bot = self._get_reddit_bot(account)

            try:
                stats = self.relationships.run_relationship_cycle(
                    project, reddit_bot, twitter_bot,
                )
                for k, v in stats.items():
                    total_stats[k] = total_stats.get(k, 0) + v
            except Exception as e:
                logger.error(f"Relationship cycle failed for {proj_name}: {e}")

        # Report via Telegram
        if any(v > 0 for v in total_stats.values()):
            self._send_telegram_alert(
                f"Relationship cycle: {total_stats['targets_found']} new contacts, "
                f"{total_stats['dms_sent']} DMs sent, "
                f"{total_stats['inbox_processed']} inbox messages processed"
            )

        logger.info(f"Relationship cycle complete: {total_stats}")

    def _notice_relationship(self, opp: Dict, project: str, account: str, platform: str):
        """Create a 'noticed' relationship when we interact with someone's post."""
        try:
            author = opp.get("author", "")
            if not author or author == "[deleted]" or author == "AutoModerator":
                return
            if author.lower() == account.lower():
                return

            existing = self.db.get_relationship(platform, author, account)
            if existing:
                self.db.upsert_relationship(
                    platform, author, account, project,
                    public_interactions=existing.get("public_interactions", 0) + 1,
                )
            else:
                self.db.upsert_relationship(
                    platform, author, account, project,
                    stage="noticed",
                    public_interactions=1,
                )
        except Exception as e:
            logger.debug(f"Relationship notice failed: {e}")

    # ── Phase 7: Hub Animation + Auto-Improvement ─────────────────

    def _animate_hubs_safe(self):
        """Wrapper: run hub animation with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._animate_hubs)
            try:
                future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                logger.warning("Hub animation ABORTED: exceeded 120s timeout")
            except Exception as e:
                logger.error(f"Hub animation error: {e}")

    def _animate_hubs(self):
        """Post content to owned subreddit hubs."""
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        logger.info("Running hub animation cycle...")
        total_posts = 0

        for project in self.projects:
            account = self.account_mgr.get_next_account("reddit")
            if not account:
                continue

            try:
                bot = self._get_reddit_bot(account)
                stats = self.hub_manager.run_hub_cycle(project, bot)
                total_posts += stats.get("posts_created", 0)
            except Exception as e:
                proj_name = project.get("project", {}).get("name", "unknown")
                logger.error(f"Hub animation failed for {proj_name}: {e}")

        if total_posts:
            self._send_telegram_alert(
                f"Hub animation: posted {total_posts} piece(s) to owned subreddits"
            )
            logger.info(f"Hub animation: {total_posts} posts created")

    # ── Community Management ─────────────────────────────────────────

    def _manage_communities_safe(self):
        """Wrapper: run community management with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._manage_communities)
            try:
                future.result(timeout=300)
            except concurrent.futures.TimeoutError:
                logger.warning("Community management ABORTED: exceeded 300s timeout")
            except Exception as e:
                logger.error(f"Community management error: {e}")

    def _manage_communities(self):
        """Manage owned subreddits: setup incomplete ones, moderate, refresh stickies."""
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        cm_settings = self.settings.get("community_management", {})
        if not cm_settings.get("enabled", True):
            return

        logger.info("Running community management cycle...")
        max_subs = cm_settings.get("max_subs_per_cycle", 5)
        stats = {"setup": 0, "moderated": 0, "stickies_refreshed": 0}

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")
            hubs = self.hub_manager.get_hubs(proj_name)

            for hub in hubs[:max_subs]:
                account = self.account_mgr.get_next_account("reddit")
                if not account:
                    continue
                bot = self._get_reddit_bot(account)

                try:
                    # 1. Complete setup for new hubs
                    if not hub.get("setup_complete"):
                        self.community_manager.setup_new_subreddit(
                            bot, hub["subreddit"], project,
                        )
                        stats["setup"] += 1
                        continue  # Setup takes many actions, skip other ops

                    # 2. Moderate (check mod queue)
                    if cm_settings.get("auto_moderate", True):
                        mod_stats = self.community_manager.moderate_subreddit(
                            bot, hub["subreddit"],
                        )
                        stats["moderated"] += mod_stats.get("approved", 0) + mod_stats.get("removed", 0)

                    # 3. Refresh stickied posts if needed
                    refresh_days = cm_settings.get("sticky_refresh_days", 7)
                    if self.community_manager.should_refresh_stickies(hub, refresh_days):
                        self.community_manager.refresh_stickied_posts(
                            bot, hub["subreddit"], project,
                        )
                        stats["stickies_refreshed"] += 1

                except Exception as e:
                    logger.error(f"Community management error for r/{hub['subreddit']}: {e}")

                time.sleep(random.uniform(10, 30))

        if any(v > 0 for v in stats.values()):
            self._send_telegram_alert(
                f"Community management: {stats['setup']} setups, "
                f"{stats['moderated']} moderated, {stats['stickies_refreshed']} stickies refreshed"
            )
            logger.info(f"Community management cycle: {stats}")

    def _scan_takeover_targets_safe(self):
        """Wrapper: scan for takeover targets with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._scan_takeover_targets)
            try:
                future.result(timeout=300)
            except concurrent.futures.TimeoutError:
                logger.warning("Takeover scan ABORTED: exceeded 300s timeout")
            except Exception as e:
                logger.error(f"Takeover scan error: {e}")

    def _scan_takeover_targets(self):
        """Find abandoned subreddits in our niches and alert for takeover."""
        if self._paused:
            return
        if not self.rate_limiter.is_active_hours():
            return

        cm_settings = self.settings.get("community_management", {})
        takeover_cfg = cm_settings.get("takeover", {})
        if not takeover_cfg.get("enabled", True):
            return

        logger.info("Scanning for takeover targets...")
        min_score = takeover_cfg.get("min_takeover_score", 7.0)
        all_targets = []

        for project in self.projects:
            proj_name = project.get("project", {}).get("name", "unknown")
            account = self.account_mgr.get_next_account("reddit")
            if not account:
                continue

            try:
                bot = self._get_reddit_bot(account)
                targets = self.community_manager.find_takeover_targets(
                    bot, project, limit=3,
                )
                for t in targets:
                    t["project"] = proj_name
                all_targets.extend(targets)
            except Exception as e:
                logger.error(f"Takeover scan failed for {proj_name}: {e}")

            time.sleep(random.uniform(5, 15))

        # Alert about high-score targets
        high_value = [t for t in all_targets if t["score"] >= min_score]
        if high_value:
            alert_lines = [f"Takeover targets found ({len(high_value)}):"]
            for t in high_value[:5]:
                alert_lines.append(
                    f"  r/{t['subreddit']} — score {t['score']}/10 "
                    f"({t['method']}, {t['reasoning'][:60]})"
                )
            alert = "\n".join(alert_lines)
            self._send_telegram_alert(alert)
            logger.info(alert)
        else:
            logger.info(f"Takeover scan: {len(all_targets)} candidates, none above {min_score}")

    def _auto_improve_safe(self):
        """Wrapper: run auto-improvement with timeout."""
        if not self._check_resources():
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._auto_improve)
            try:
                future.result(timeout=180)
            except concurrent.futures.TimeoutError:
                logger.warning("Auto-improve ABORTED: exceeded 180s timeout")
            except Exception as e:
                logger.error(f"Auto-improve error: {e}")

    def _auto_improve(self):
        """Analyze performance gaps and auto-execute improvements.

        Scoring dimensions:
        1. Activity level (are we posting enough?)
        2. Platform balance (Reddit vs Twitter)
        3. Account utilization (are all accounts active?)
        4. Content diversity (mix of action types)
        5. Removal rate (are our posts surviving?)

        For each weakness, the engine schedules corrective actions.
        """
        if self._paused:
            return

        logger.info("Running auto-improvement analysis...")

        try:
            stats = self.db.get_stats_summary(hours=24)
            actions = stats.get("actions", {})
            total_actions = sum(sum(t.values()) for t in actions.values())

            improvements = []

            # 1. Activity check
            max_hourly = self.settings.get("bot", {}).get("max_actions_per_hour", 18)
            expected_daily = max_hourly * 16  # 16 active hours
            activity_pct = total_actions / max(expected_daily, 1) * 100
            if activity_pct < 30:
                improvements.append({
                    "issue": "Low activity",
                    "detail": f"Only {total_actions} actions in 24h ({activity_pct:.0f}% of capacity)",
                    "fix": "reduce_scan_interval",
                })

            # 2. Platform balance (Twitter disabled — only check Reddit + Telegram)
            r_total = sum(actions.get("reddit", {}).values())
            tg_total = sum(actions.get("telegram", {}).values())
            if r_total == 0 and tg_total > 0:
                improvements.append({
                    "issue": "Reddit inactive",
                    "detail": "Zero Reddit actions in 24h",
                    "fix": "boost_reddit",
                })

            # 3. Account utilization
            all_accounts = {}
            for platform in ("reddit", "telegram"):
                accs = self.account_mgr.load_accounts(platform)
                for acc in accs:
                    count = self.db.get_action_count(
                        hours=24, account=acc["username"], platform=platform
                    )
                    all_accounts[f"{platform}:{acc['username']}"] = count

            inactive = [k for k, v in all_accounts.items() if v == 0]
            if inactive and len(inactive) > len(all_accounts) * 0.5:
                improvements.append({
                    "issue": "Underused accounts",
                    "detail": f"{len(inactive)}/{len(all_accounts)} accounts idle",
                    "fix": "rotate_accounts",
                })

            # 4. Content diversity
            all_types = set()
            for plat_types in actions.values():
                all_types.update(plat_types.keys())
            if len(all_types) <= 2:
                improvements.append({
                    "issue": "Low diversity",
                    "detail": f"Only {len(all_types)} action types used",
                    "fix": "diversify_actions",
                })

            # 5. Removal rate
            try:
                recent_perf = self.db.conn.execute(
                    """SELECT COUNT(*) as total,
                              SUM(CASE WHEN was_removed = 1 THEN 1 ELSE 0 END) as removed
                       FROM performance
                       WHERE timestamp > datetime('now', '-7 days')"""
                ).fetchone()
                if recent_perf and recent_perf["total"] > 5:
                    removed = recent_perf["removed"] or 0
                    removal_rate = removed / recent_perf["total"]
                    if removal_rate > 0.3:
                        improvements.append({
                            "issue": "High removal rate",
                            "detail": f"{removal_rate:.0%} of posts removed",
                            "fix": "adjust_tone",
                        })
            except Exception:
                pass

            # 6. Evolved prompt health check
            try:
                for proj in self.projects:
                    proj_name = proj.get("project", {}).get("name", "")
                    if not proj_name:
                        continue
                    evolutions = self.db.conn.execute(
                        """SELECT template_name, performance_after
                           FROM prompt_evolution_log
                           WHERE project = ? AND status = 'active'
                           AND timestamp > datetime('now', '-14 days')""",
                        (proj_name,),
                    ).fetchall()
                    for evo in evolutions:
                        template = evo["template_name"]
                        post_type = template.replace("reddit_user_", "")
                        current_perf = self.db.conn.execute(
                            """SELECT AVG(engagement_score) as avg_eng
                               FROM performance
                               WHERE project = ? AND post_type = ?
                               AND timestamp > datetime('now', '-7 days')""",
                            (proj_name, post_type),
                        ).fetchone()
                        if (
                            current_perf
                            and current_perf["avg_eng"] is not None
                            and evo["performance_after"] > 0
                            and current_perf["avg_eng"]
                            < evo["performance_after"] * 0.7
                        ):
                            self.db.revert_prompt_evolution(
                                proj_name, template
                            )
                            improvements.append({
                                "issue": f"Prompt {template} reverted",
                                "detail": "Performance dropped after evolution",
                                "fix": "none",
                            })
                            logger.warning(
                                f"Reverted prompt: {template} for {proj_name}"
                            )
            except Exception:
                pass

            # Execute improvements
            executed = []
            for imp in improvements:
                fix = imp["fix"]
                try:
                    if fix == "reduce_scan_interval":
                        job = self.scheduler.get_job("scan_all")
                        if job:
                            self.scheduler.reschedule_job(
                                "scan_all", trigger="interval", minutes=10
                            )
                            executed.append("Scan interval reduced to 10min")

                    elif fix == "boost_reddit":
                        self._platform_turn = 0  # Even = Reddit first
                        executed.append("Reddit prioritized for next cycles")

                    elif fix == "diversify_actions":
                        # Trigger engagement cycle
                        threading.Thread(
                            target=self._engage_safe, daemon=True
                        ).start()
                        executed.append("Triggered engagement diversification")

                    elif fix == "adjust_tone":
                        # Make content more organic
                        current = self.content_gen.organic_ratio
                        new_ratio = min(0.95, current + 0.1)
                        self.content_gen.organic_ratio = new_ratio
                        executed.append(
                            f"Organic ratio increased: {current:.0%} -> {new_ratio:.0%}"
                        )

                except Exception as e:
                    logger.debug(f"Auto-fix '{fix}' failed: {e}")

            # Report
            if improvements:
                report_lines = ["Auto-improvement analysis:"]
                for imp in improvements:
                    report_lines.append(f"  - {imp['issue']}: {imp['detail']}")
                if executed:
                    report_lines.append("Actions taken:")
                    for ex in executed:
                        report_lines.append(f"  + {ex}")
                report = "\n".join(report_lines)
                logger.info(report)
                self._send_telegram_alert(report)
            else:
                logger.info("Auto-improvement: all metrics healthy")

        except Exception as e:
            logger.error(f"Auto-improvement failed: {e}")

    def _send_daily_report(self):
        """Send daily report via Telegram."""
        if self.telegram:
            try:
                self.telegram.send_daily_report_sync()
                logger.info("Daily report sent via Telegram")
            except Exception as e:
                logger.error(f"Failed to send daily report: {e}")

    # ── Process Management ───────────────────────────────────────────

    def _write_pid(self):
        """Write PID file for single-instance enforcement."""
        pid_file = self.settings.get("process", {}).get(
            "pid_file", "data/miloagent.pid"
        )
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)

        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)
                msg = (
                    f"Another instance is running (PID {old_pid}). "
                    f"Stop it first or delete {pid_file}"
                )
                logger.error(msg)
                raise RuntimeError(msg)
            except (OSError, ValueError):
                pass  # Stale PID file — will be overwritten

        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
        logger.debug(f"PID file written: {pid_file}")

    def _remove_pid(self):
        """Remove PID file."""
        pid_file = self.settings.get("process", {}).get(
            "pid_file", "data/miloagent.pid"
        )
        try:
            os.remove(pid_file)
        except OSError:
            pass

    def _set_nice_priority(self):
        """Set process to low priority for background operation."""
        try:
            priority = self.settings.get("process", {}).get("nice_priority", 10)
            os.nice(priority)
            logger.debug(f"Process nice priority set to {priority}")
        except (OSError, AttributeError):
            pass

    def _setup_signal_handlers(self):
        """Handle SIGTERM/SIGINT for graceful shutdown, SIGUSR1 for hot-reload."""
        def _handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down...")
            self.stop()
            sys.exit(0)

        def _reload_handler(signum, frame):
            logger.info("Received SIGUSR1: hot-reloading account configs")
            self._on_accounts_reloaded()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        try:
            signal.signal(signal.SIGUSR1, _reload_handler)
        except (AttributeError, OSError):
            pass  # SIGUSR1 not available on all platforms

    def _on_accounts_reloaded(self):
        """Clear cached bot instances so they rebuild with new accounts."""
        old_reddit = len(self._reddit_bots)
        old_twitter = len(self._twitter_bots)
        old_telegram = len(self._telegram_group_bots)
        self._reddit_bots.clear()
        self._twitter_bots.clear()
        self._telegram_group_bots.clear()
        logger.info(
            f"Account caches invalidated: {old_reddit} reddit, "
            f"{old_twitter} twitter, {old_telegram} telegram bots cleared"
        )
        self._send_telegram_alert("Account configs hot-reloaded")
