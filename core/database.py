"""SQLite database layer for logging all bot actions and opportunities.

Thread-safe: each thread gets its own SQLite connection via thread-local storage.
WAL mode allows concurrent reads across threads. Writes serialized via lock.
Auto-maintenance: WAL checkpoint + old data cleanup.
"""

import json
import os
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_HOURS = 6


class Database:
    """SQLite logger for all bot actions, opportunities, and account health.

    Thread-safe via thread-local connections + write lock.
    Each thread gets its own SQLite connection (WAL mode allows concurrent reads).
    Writes are serialized with a threading.Lock to prevent WAL contention.
    """

    SCHEMA_VERSION = 4

    def __init__(self, db_path: str = "data/miloagent.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

        self._lock = threading.Lock()
        self._local = threading.local()
        self._last_cleanup = datetime.utcnow()
        self._closed = False

        # Initialize tables on the main thread connection
        self._init_tables()

    def _make_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection configured for WAL mode."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=100")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Thread-local connection — each thread gets its own.

        This eliminates 'database is locked' errors caused by multiple
        threads sharing a single connection object.
        """
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._make_connection()
        return self._local.conn

    def _init_tables(self):
        """Create tables if they don't exist."""
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    platform TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    account TEXT NOT NULL,
                    project TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    content TEXT,
                    metadata TEXT,
                    success INTEGER DEFAULT 1,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    platform TEXT NOT NULL,
                    target_id TEXT NOT NULL UNIQUE,
                    title TEXT,
                    subreddit_or_query TEXT,
                    score REAL DEFAULT 0.0,
                    project TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    metadata TEXT
                );

                CREATE TABLE IF NOT EXISTS account_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    platform TEXT NOT NULL,
                    account TEXT NOT NULL,
                    status TEXT DEFAULT 'healthy',
                    action_count_1h INTEGER DEFAULT 0,
                    action_count_24h INTEGER DEFAULT 0,
                    last_action TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    action_id INTEGER REFERENCES actions(id),
                    metric_type TEXT NOT NULL,
                    value REAL,
                    metadata TEXT
                );

                -- Learning: tracks performance of each action for self-improvement
                CREATE TABLE IF NOT EXISTS performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id INTEGER REFERENCES actions(id),
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    platform TEXT NOT NULL,
                    project TEXT NOT NULL,
                    subreddit_or_query TEXT,
                    keyword TEXT,
                    action_type TEXT,
                    was_promotional INTEGER DEFAULT 0,
                    engagement_score REAL DEFAULT 0.0,
                    upvotes INTEGER DEFAULT 0,
                    replies INTEGER DEFAULT 0,
                    impressions INTEGER DEFAULT 0,
                    was_removed INTEGER DEFAULT 0,
                    content_length INTEGER DEFAULT 0,
                    tone_style TEXT,
                    metadata TEXT
                );

                -- Learning: tracks which subreddits/keywords are most effective
                CREATE TABLE IF NOT EXISTS learned_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    project TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    sample_count INTEGER DEFAULT 0,
                    avg_engagement REAL DEFAULT 0.0,
                    UNIQUE(category, key, project)
                );

                -- Learning: discovered subreddits/keywords not in original config
                CREATE TABLE IF NOT EXISTS discoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    platform TEXT NOT NULL,
                    project TEXT NOT NULL,
                    discovery_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT,
                    score REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'candidate',
                    UNIQUE(platform, project, discovery_type, value)
                );

                -- Phase 5: Subreddit Intelligence
                CREATE TABLE IF NOT EXISTS subreddit_intel (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subreddit TEXT NOT NULL,
                    project TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    subscribers INTEGER DEFAULT 0,
                    active_users INTEGER DEFAULT 0,
                    created_utc REAL DEFAULT 0,
                    description TEXT DEFAULT '',
                    subreddit_type TEXT DEFAULT 'public',
                    over18 INTEGER DEFAULT 0,
                    posts_per_day REAL DEFAULT 0.0,
                    avg_hours_between_posts REAL DEFAULT 0.0,
                    median_post_score REAL DEFAULT 0.0,
                    avg_comments_per_post REAL DEFAULT 0.0,
                    mod_count INTEGER DEFAULT -1,
                    active_mod_count INTEGER DEFAULT -1,
                    opportunity_score REAL DEFAULT 0.0,
                    relevance_score REAL DEFAULT 0.0,
                    metadata TEXT,
                    UNIQUE(subreddit, project)
                );

                -- Phase 5: Community Presence
                CREATE TABLE IF NOT EXISTS community_presence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subreddit TEXT NOT NULL,
                    project TEXT NOT NULL,
                    account TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    total_comments INTEGER DEFAULT 0,
                    total_posts INTEGER DEFAULT 0,
                    total_upvotes_given INTEGER DEFAULT 0,
                    total_saves INTEGER DEFAULT 0,
                    subscribed INTEGER DEFAULT 0,
                    karma_earned INTEGER DEFAULT 0,
                    comments_removed INTEGER DEFAULT 0,
                    comments_surviving INTEGER DEFAULT 0,
                    avg_comment_score REAL DEFAULT 0.0,
                    total_replies_received INTEGER DEFAULT 0,
                    first_activity TEXT,
                    last_activity TEXT,
                    days_active INTEGER DEFAULT 0,
                    warmth_score REAL DEFAULT 0.0,
                    reputation_score REAL DEFAULT 0.0,
                    stage TEXT DEFAULT 'new',
                    UNIQUE(subreddit, project, account)
                );

                -- Phase 5: Research Knowledge Base
                CREATE TABLE IF NOT EXISTS knowledge_base (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    project TEXT NOT NULL,
                    category TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT,
                    relevance_score REAL DEFAULT 1.0,
                    expires_at TEXT,
                    used_count INTEGER DEFAULT 0,
                    metadata TEXT
                );

                -- Phase 5: Subreddit Trends
                CREATE TABLE IF NOT EXISTS subreddit_trends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    subreddit TEXT NOT NULL,
                    project TEXT NOT NULL,
                    top_themes TEXT,
                    recurring_questions TEXT,
                    avg_score REAL DEFAULT 0.0,
                    hot_post_count INTEGER DEFAULT 0,
                    sample_period_hours INTEGER DEFAULT 24,
                    metadata TEXT
                );

                -- Phase 5: A/B Experiments
                CREATE TABLE IF NOT EXISTS ab_experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    project TEXT NOT NULL,
                    experiment_name TEXT NOT NULL,
                    variable TEXT NOT NULL,
                    variant_a TEXT NOT NULL,
                    variant_b TEXT NOT NULL,
                    status TEXT DEFAULT 'running',
                    min_samples INTEGER DEFAULT 10,
                    concluded_at TEXT,
                    winner TEXT,
                    metadata TEXT
                );

                -- Phase 5: A/B Results
                CREATE TABLE IF NOT EXISTS ab_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id INTEGER REFERENCES ab_experiments(id),
                    action_id INTEGER REFERENCES actions(id),
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    variant TEXT NOT NULL,
                    engagement_score REAL DEFAULT 0.0,
                    upvotes INTEGER DEFAULT 0,
                    replies INTEGER DEFAULT 0,
                    was_removed INTEGER DEFAULT 0,
                    metadata TEXT
                );

                -- Phase 5: Time Performance
                CREATE TABLE IF NOT EXISTS time_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    project TEXT NOT NULL,
                    subreddit TEXT NOT NULL,
                    hour_of_day INTEGER NOT NULL,
                    day_of_week INTEGER NOT NULL,
                    action_count INTEGER DEFAULT 0,
                    avg_engagement REAL DEFAULT 0.0,
                    avg_upvotes REAL DEFAULT 0.0,
                    total_removed INTEGER DEFAULT 0,
                    UNIQUE(project, subreddit, hour_of_day, day_of_week)
                );

                -- Phase 5: Failure Patterns
                CREATE TABLE IF NOT EXISTS failure_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    project TEXT NOT NULL,
                    subreddit TEXT NOT NULL,
                    failure_type TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    frequency INTEGER DEFAULT 1,
                    last_seen TEXT,
                    avoidance_rule TEXT,
                    metadata TEXT
                );

                -- Phase 6: Relationship Building
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT,
                    bio TEXT,
                    karma INTEGER DEFAULT 0,
                    account_age_days INTEGER DEFAULT 0,
                    interests TEXT,
                    subreddits_active TEXT,
                    followers INTEGER DEFAULT 0,
                    first_seen TEXT DEFAULT (datetime('now')),
                    last_updated TEXT DEFAULT (datetime('now')),
                    metadata TEXT,
                    UNIQUE(platform, username)
                );

                CREATE TABLE IF NOT EXISTS relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    username TEXT NOT NULL,
                    our_account TEXT NOT NULL,
                    project TEXT NOT NULL,
                    stage TEXT DEFAULT 'noticed',
                    first_interaction TEXT DEFAULT (datetime('now')),
                    last_interaction TEXT DEFAULT (datetime('now')),
                    public_interactions INTEGER DEFAULT 0,
                    dms_sent INTEGER DEFAULT 0,
                    dms_received INTEGER DEFAULT 0,
                    sentiment REAL DEFAULT 0.0,
                    trust_score REAL DEFAULT 0.0,
                    notes TEXT,
                    next_action TEXT,
                    next_action_after TEXT,
                    is_blocked INTEGER DEFAULT 0,
                    UNIQUE(platform, username, our_account)
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    relationship_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    timestamp TEXT DEFAULT (datetime('now')),
                    subject TEXT,
                    content TEXT NOT NULL,
                    message_id TEXT,
                    read INTEGER DEFAULT 0,
                    replied INTEGER DEFAULT 0,
                    FOREIGN KEY (relationship_id) REFERENCES relationships(id)
                );

                CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_actions_target ON actions(target_id);
                CREATE INDEX IF NOT EXISTS idx_actions_platform ON actions(platform);
                CREATE INDEX IF NOT EXISTS idx_actions_account ON actions(account);
                CREATE INDEX IF NOT EXISTS idx_actions_success_ts ON actions(success, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_actions_acct_plat_ts ON actions(account, platform, success, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);
                CREATE INDEX IF NOT EXISTS idx_opportunities_platform ON opportunities(platform);
                CREATE INDEX IF NOT EXISTS idx_opps_pending_score ON opportunities(status, score DESC, platform, project);
                CREATE INDEX IF NOT EXISTS idx_account_health_account ON account_health(account);
                CREATE INDEX IF NOT EXISTS idx_account_health_plat_acct ON account_health(platform, account);
                CREATE INDEX IF NOT EXISTS idx_performance_project ON performance(project);
                CREATE INDEX IF NOT EXISTS idx_performance_subreddit ON performance(subreddit_or_query);
                CREATE INDEX IF NOT EXISTS idx_learned_weights_cat ON learned_weights(category, project);
                CREATE INDEX IF NOT EXISTS idx_discoveries_status ON discoveries(status, platform);

                -- Phase 5 indexes
                CREATE INDEX IF NOT EXISTS idx_subreddit_intel_score ON subreddit_intel(project, opportunity_score DESC);
                CREATE INDEX IF NOT EXISTS idx_subreddit_intel_updated ON subreddit_intel(updated_at);
                CREATE INDEX IF NOT EXISTS idx_community_presence_sub ON community_presence(subreddit, project);
                CREATE INDEX IF NOT EXISTS idx_community_presence_stage ON community_presence(stage, project);
                CREATE INDEX IF NOT EXISTS idx_community_presence_warmth ON community_presence(warmth_score DESC);
                CREATE INDEX IF NOT EXISTS idx_community_presence_last ON community_presence(last_activity);
                CREATE INDEX IF NOT EXISTS idx_kb_project_cat ON knowledge_base(project, category);
                CREATE INDEX IF NOT EXISTS idx_kb_expires ON knowledge_base(expires_at);
                CREATE INDEX IF NOT EXISTS idx_kb_topic ON knowledge_base(topic);
                CREATE INDEX IF NOT EXISTS idx_trends_sub ON subreddit_trends(subreddit, project);
                CREATE INDEX IF NOT EXISTS idx_ab_status ON ab_experiments(status, project);
                CREATE INDEX IF NOT EXISTS idx_ab_results_exp ON ab_results(experiment_id);
                CREATE INDEX IF NOT EXISTS idx_time_perf ON time_performance(project, subreddit);
                CREATE INDEX IF NOT EXISTS idx_failure_proj ON failure_patterns(project, subreddit);

                -- Phase 6 indexes
                CREATE INDEX IF NOT EXISTS idx_user_profiles_plat ON user_profiles(platform, username);
                CREATE INDEX IF NOT EXISTS idx_relationships_stage ON relationships(stage, project);
                CREATE INDEX IF NOT EXISTS idx_relationships_user ON relationships(platform, username);
                CREATE INDEX IF NOT EXISTS idx_relationships_action ON relationships(next_action_after);
                CREATE INDEX IF NOT EXISTS idx_conversations_rel ON conversations(relationship_id);
                CREATE INDEX IF NOT EXISTS idx_conversations_ts ON conversations(timestamp);

                -- Phase 7: Per-account subreddit authority tracking
                CREATE TABLE IF NOT EXISTS account_subreddit_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    subreddit TEXT NOT NULL,
                    actions_count INTEGER DEFAULT 0,
                    avg_score REAL DEFAULT 0.0,
                    last_activity TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(account, platform, subreddit)
                );
                CREATE INDEX IF NOT EXISTS idx_acc_sub_stats ON account_subreddit_stats(account, platform);
            """)

            # Add columns to performance table if missing (v2 -> v3 migration)
            try:
                self.conn.execute("SELECT hour_of_day FROM performance LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute("ALTER TABLE performance ADD COLUMN hour_of_day INTEGER DEFAULT -1")
                self.conn.execute("ALTER TABLE performance ADD COLUMN day_of_week INTEGER DEFAULT -1")
                self.conn.execute("ALTER TABLE performance ADD COLUMN experiment_id INTEGER DEFAULT NULL")

            # Add post_type column to performance (v3 -> v4 migration)
            try:
                self.conn.execute("SELECT post_type FROM performance LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute("ALTER TABLE performance ADD COLUMN post_type TEXT DEFAULT ''")

            # Reply sentiment tracking (v4 -> v5)
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS reply_sentiment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    action_id INTEGER,
                    project TEXT NOT NULL,
                    subreddit TEXT NOT NULL,
                    tone_style TEXT NOT NULL DEFAULT '',
                    post_type TEXT NOT NULL DEFAULT '',
                    sentiment_score REAL DEFAULT 0.0,
                    reply_count_analyzed INTEGER DEFAULT 0,
                    positive_signals TEXT,
                    negative_signals TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sentiment_proj
                    ON reply_sentiment(project, subreddit);
                CREATE INDEX IF NOT EXISTS idx_sentiment_tone
                    ON reply_sentiment(tone_style, project);

                -- Prompt evolution audit trail (v5)
                CREATE TABLE IF NOT EXISTS prompt_evolution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    project TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    change_description TEXT,
                    performance_before REAL DEFAULT 0.0,
                    performance_after REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'active'
                );
                CREATE INDEX IF NOT EXISTS idx_prompt_evo
                    ON prompt_evolution_log(project, template_name);
            """)

            # Phase 8: Decision visibility (rejection_reason + decision_log)
            try:
                self.conn.execute("SELECT rejection_reason FROM opportunities LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(
                    "ALTER TABLE opportunities ADD COLUMN rejection_reason TEXT"
                )

            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS decision_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    decision_type TEXT NOT NULL,
                    platform TEXT,
                    project TEXT,
                    account TEXT,
                    target_id TEXT,
                    details TEXT,
                    outcome TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_decision_log_ts
                    ON decision_log(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_decision_log_type
                    ON decision_log(decision_type, timestamp DESC);
            """)

            self.conn.commit()
        logger.debug("Database tables initialized")

    # ── Write helpers (locked) ────────────────────────────────────────

    def _execute_write(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write query with thread lock."""
        with self._lock:
            cursor = self.conn.execute(query, params)
            self.conn.commit()
            return cursor

    # ── Actions ──────────────────────────────────────────────────────

    def log_action(
        self,
        platform: str,
        action_type: str,
        account: str,
        project: str,
        target_id: str,
        content: str = "",
        metadata: Optional[Dict] = None,
        success: bool = True,
        error_message: str = "",
    ) -> int:
        """Log a bot action."""
        cursor = self._execute_write(
            """INSERT INTO actions
               (platform, action_type, account, project, target_id,
                content, metadata, success, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                platform, action_type, account, project, target_id,
                content,
                json.dumps(metadata) if metadata else None,
                1 if success else 0,
                error_message,
            ),
        )
        self._maybe_cleanup()
        return cursor.lastrowid

    def update_action_metadata(self, action_id: int, metadata: Dict) -> None:
        """Update the metadata JSON of an existing action."""
        self._execute_write(
            "UPDATE actions SET metadata = ? WHERE id = ?",
            (json.dumps(metadata), action_id),
        )

    def get_recent_actions_by_type(
        self,
        action_type: str,
        hours: int = 48,
        platform: Optional[str] = None,
    ) -> List[Dict]:
        """Get recent actions of a specific type."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        query = "SELECT * FROM actions WHERE action_type = ? AND timestamp > ?"
        params: list = [action_type, since]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY timestamp DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ── Reply Sentiment & Prompt Evolution ─────────────────────────

    def log_reply_sentiment(
        self, action_id: int, project: str, subreddit: str,
        tone_style: str, post_type: str,
        sentiment_score: float, reply_count: int,
        positive_signals: str, negative_signals: str,
    ):
        """Log sentiment analysis of replies to our content."""
        self._execute_write(
            """INSERT INTO reply_sentiment
               (action_id, project, subreddit, tone_style, post_type,
                sentiment_score, reply_count_analyzed,
                positive_signals, negative_signals)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_id, project, subreddit, tone_style, post_type,
             sentiment_score, reply_count, positive_signals, negative_signals),
        )

    def get_sentiment_by_tone(
        self, project: str, days: int = 30,
    ) -> List[Dict]:
        """Get average sentiment grouped by tone_style."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """SELECT tone_style, AVG(sentiment_score) as avg_sentiment,
                      COUNT(*) as count, SUM(reply_count_analyzed) as total_replies
               FROM reply_sentiment
               WHERE project = ? AND tone_style != '' AND timestamp > ?
               GROUP BY tone_style
               HAVING count >= ?""",
            (project, since, 3),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_sentiment_by_subreddit(
        self, project: str, days: int = 30,
    ) -> List[Dict]:
        """Get average sentiment grouped by subreddit."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """SELECT subreddit, AVG(sentiment_score) as avg_sentiment,
                      COUNT(*) as count, SUM(reply_count_analyzed) as total_replies
               FROM reply_sentiment
               WHERE project = ? AND timestamp > ?
               GROUP BY subreddit
               HAVING count >= ?""",
            (project, since, 3),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_evolved_prompt(
        self, project: str, template_name: str,
    ) -> Optional[str]:
        """Get the latest active evolved prompt template."""
        row = self.conn.execute(
            """SELECT content FROM knowledge_base
               WHERE project = ? AND category = 'evolved_prompt' AND topic = ?
               AND (expires_at IS NULL OR expires_at > datetime('now'))
               ORDER BY relevance_score DESC, timestamp DESC LIMIT 1""",
            (project, template_name),
        ).fetchone()
        return row["content"] if row else None

    def log_prompt_evolution(
        self, project: str, template_name: str, version: int,
        change_description: str, perf_before: float, perf_after: float,
    ):
        """Log a prompt evolution event."""
        self._execute_write(
            """INSERT INTO prompt_evolution_log
               (project, template_name, version, change_description,
                performance_before, performance_after)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project, template_name, version, change_description,
             perf_before, perf_after),
        )

    def revert_prompt_evolution(self, project: str, template_name: str):
        """Deactivate evolved prompt, revert to file default."""
        self._execute_write(
            """DELETE FROM knowledge_base
               WHERE project = ? AND category = 'evolved_prompt'
               AND topic = ?""",
            (project, template_name),
        )
        self._execute_write(
            """UPDATE prompt_evolution_log SET status = 'reverted'
               WHERE project = ? AND template_name = ? AND status = 'active'""",
            (project, template_name),
        )

    def get_recent_actions(
        self,
        hours: int = 24,
        platform: Optional[str] = None,
        account: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """Get recent actions within the last N hours."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        query = "SELECT * FROM actions WHERE timestamp > ?"
        params: list = [since]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        if account:
            query += " AND account = ?"
            params.append(account)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_action_count(
        self,
        hours: int = 1,
        account: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> int:
        """Count actions in the last N hours."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        query = "SELECT COUNT(*) FROM actions WHERE timestamp > ? AND success = 1"
        params: list = [since]
        if account:
            query += " AND account = ?"
            params.append(account)
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        return self.conn.execute(query, params).fetchone()[0]

    def was_target_acted_on(self, target_id: str) -> bool:
        """Check if we already acted on this target."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM actions WHERE target_id = ? AND success = 1",
            (target_id,),
        ).fetchone()
        return row[0] > 0

    # ── Opportunities ────────────────────────────────────────────────

    def log_opportunity(
        self,
        platform: str,
        target_id: str,
        title: str,
        subreddit_or_query: str,
        score: float,
        project: str,
        status: str = "pending",
        metadata: Optional[Dict] = None,
    ) -> int:
        """Log a discovered opportunity."""
        cursor = self._execute_write(
            """INSERT OR REPLACE INTO opportunities
               (platform, target_id, title, subreddit_or_query,
                score, project, status, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                platform, target_id, title, subreddit_or_query,
                score, project, status,
                json.dumps(metadata) if metadata else None,
            ),
        )
        return cursor.lastrowid

    def get_pending_opportunities(
        self,
        platform: Optional[str] = None,
        project: Optional[str] = None,
        min_score: float = 0.0,
        limit: int = 20,
    ) -> List[Dict]:
        """Get pending opportunities sorted by score."""
        query = "SELECT * FROM opportunities WHERE status = 'pending' AND score >= ?"
        params: list = [min_score]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_opportunity_status(
        self, target_id: str, status: str, rejection_reason: str = "",
    ):
        """Update opportunity status with optional rejection reason."""
        if rejection_reason:
            self._execute_write(
                "UPDATE opportunities SET status = ?, rejection_reason = ? WHERE target_id = ?",
                (status, rejection_reason, target_id),
            )
        else:
            self._execute_write(
                "UPDATE opportunities SET status = ? WHERE target_id = ?",
                (status, target_id),
            )

    def get_rejected_opportunities(
        self, hours: int = 24, limit: int = 50,
    ) -> List[Dict]:
        """Get recently rejected/skipped opportunities with reasons."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            """SELECT target_id, platform, project, title, subreddit_or_query,
                      score, status, rejection_reason, timestamp
               FROM opportunities
               WHERE status IN ('skipped', 'failed')
               AND timestamp > ?
               ORDER BY timestamp DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Decision Log ──────────────────────────────────────────────────

    def log_decision(
        self,
        decision_type: str,
        platform: str = "",
        project: str = "",
        account: str = "",
        target_id: str = "",
        details: str = "",
        outcome: str = "",
    ):
        """Log a bot decision for audit trail.

        decision_type: select_opp, skip_opp, select_account, decide_promo,
                       rate_limited, dedup_blocked, resource_low, etc.
        """
        self._execute_write(
            """INSERT INTO decision_log
               (decision_type, platform, project, account, target_id, details, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (decision_type, platform, project, account, target_id, details, outcome),
        )

    def get_recent_decisions(
        self, hours: int = 2, decision_type: str = "", limit: int = 30,
    ) -> List[Dict]:
        """Get recent decisions for debugging."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        query = "SELECT * FROM decision_log WHERE timestamp > ?"
        params: list = [since]
        if decision_type:
            query += " AND decision_type = ?"
            params.append(decision_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def purge_low_quality_opportunities(
        self, min_score: float = 3.0, max_age_hours: int = 48
    ) -> int:
        """Remove pending opportunities that are low-score or stale.

        Returns number of rows cleaned up.
        """
        try:
            cur = self.conn.execute(
                """DELETE FROM opportunities
                   WHERE status = 'pending'
                   AND (score < ? OR timestamp < datetime('now', ?))""",
                (min_score, f"-{max_age_hours} hours"),
            )
            self.conn.commit()
            count = cur.rowcount
            if count:
                logger.info(
                    f"Purged {count} low-quality/stale pending opportunities"
                )
            return count
        except Exception as e:
            logger.debug(f"Opportunity purge failed: {e}")
            return 0

    # ── Account Health ───────────────────────────────────────────────

    def update_account_health(
        self, platform: str, account: str,
        status: str = "healthy", notes: str = "",
    ):
        """Update or insert account health record (one row per account).

        Uses a single transaction (DELETE + INSERT) to avoid double lock/commit.
        """
        action_1h = self.get_action_count(hours=1, account=account, platform=platform)
        action_24h = self.get_action_count(hours=24, account=account, platform=platform)
        with self._lock:
            self.conn.execute(
                "DELETE FROM account_health WHERE platform = ? AND account = ?",
                (platform, account),
            )
            self.conn.execute(
                """INSERT INTO account_health
                   (platform, account, status, action_count_1h,
                    action_count_24h, last_action, notes)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                (platform, account, status, action_1h, action_24h, notes),
            )
            self.conn.commit()

    def get_account_health(self, platform: Optional[str] = None) -> List[Dict]:
        """Get health status for all accounts."""
        query = "SELECT * FROM account_health"
        params: list = []
        if platform:
            query += " WHERE platform = ?"
            params.append(platform)
        query += " ORDER BY timestamp DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ── Analytics ────────────────────────────────────────────────────

    def log_analytics(
        self, action_id: int, metric_type: str,
        value: float, metadata: Optional[Dict] = None,
    ):
        """Log an analytics metric."""
        self._execute_write(
            """INSERT INTO analytics (action_id, metric_type, value, metadata)
               VALUES (?, ?, ?, ?)""",
            (action_id, metric_type, value, json.dumps(metadata) if metadata else None),
        )

    def get_stats_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get aggregated stats for the last N hours."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        stats = {}

        rows = self.conn.execute(
            """SELECT platform, action_type, COUNT(*) as count
               FROM actions WHERE timestamp > ? AND success = 1
               GROUP BY platform, action_type""",
            (since,),
        ).fetchall()
        actions_by_platform = {}
        for row in rows:
            p = row["platform"]
            if p not in actions_by_platform:
                actions_by_platform[p] = {}
            actions_by_platform[p][row["action_type"]] = row["count"]
        stats["actions"] = actions_by_platform

        rows = self.conn.execute(
            """SELECT status, COUNT(*) as count
               FROM opportunities WHERE timestamp > ?
               GROUP BY status""",
            (since,),
        ).fetchall()
        stats["opportunities"] = {row["status"]: row["count"] for row in rows}

        row = self.conn.execute(
            "SELECT AVG(score) as avg_score FROM opportunities WHERE timestamp > ?",
            (since,),
        ).fetchone()
        stats["avg_opportunity_score"] = round(row["avg_score"] or 0, 1)
        return stats

    # ── Utilities ────────────────────────────────────────────────────

    def get_last_action_time(self, account: str, platform: str) -> Optional[datetime]:
        """Get timestamp of the last action for an account."""
        row = self.conn.execute(
            """SELECT timestamp FROM actions
               WHERE account = ? AND platform = ? AND success = 1
               ORDER BY timestamp DESC LIMIT 1""",
            (account, platform),
        ).fetchone()
        if row:
            return datetime.fromisoformat(row["timestamp"])
        return None

    def get_last_action_in_subreddit(self, account: str, subreddit: str) -> Optional[datetime]:
        """Get timestamp of last action in a specific subreddit."""
        row = self.conn.execute(
            """SELECT a.timestamp FROM actions a
               JOIN opportunities o ON a.target_id = o.target_id
               WHERE a.account = ? AND o.subreddit_or_query = ?
               AND a.success = 1
               ORDER BY a.timestamp DESC LIMIT 1""",
            (account, subreddit),
        ).fetchone()
        if row:
            return datetime.fromisoformat(row["timestamp"])
        return None

    # ── Learning / Performance ─────────────────────────────────────

    def log_performance(
        self,
        action_id: int,
        platform: str,
        project: str,
        subreddit_or_query: str = "",
        keyword: str = "",
        action_type: str = "comment",
        was_promotional: bool = False,
        engagement_score: float = 0.0,
        upvotes: int = 0,
        replies: int = 0,
        was_removed: bool = False,
        content_length: int = 0,
        tone_style: str = "",
        post_type: str = "",
        metadata: Optional[Dict] = None,
    ):
        """Log performance data for a bot action."""
        self._execute_write(
            """INSERT INTO performance
               (action_id, platform, project, subreddit_or_query, keyword,
                action_type, was_promotional, engagement_score, upvotes,
                replies, was_removed, content_length, tone_style, post_type,
                metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action_id, platform, project, subreddit_or_query, keyword,
                action_type, 1 if was_promotional else 0, engagement_score,
                upvotes, replies, 1 if was_removed else 0, content_length,
                tone_style, post_type,
                json.dumps(metadata) if metadata else None,
            ),
        )

    def get_performance_stats(
        self, project: str = "", platform: str = "", days: int = 30,
    ) -> List[Dict]:
        """Get aggregated performance stats grouped by subreddit/keyword."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        query = """
            SELECT subreddit_or_query, keyword, action_type,
                   COUNT(*) as count,
                   AVG(engagement_score) as avg_engagement,
                   SUM(upvotes) as total_upvotes,
                   SUM(replies) as total_replies,
                   SUM(was_removed) as removed_count,
                   AVG(was_promotional) as promo_ratio
            FROM performance WHERE timestamp > ?
        """
        params: list = [since]
        if project:
            query += " AND project = ?"
            params.append(project)
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " GROUP BY subreddit_or_query, keyword ORDER BY avg_engagement DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_post_type_stats(
        self, project: str, days: int = 30,
    ) -> List[Dict]:
        """Get aggregated performance stats grouped by post_type."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """SELECT post_type, COUNT(*) as count,
                      AVG(engagement_score) as avg_engagement,
                      SUM(upvotes) as total_upvotes,
                      SUM(was_removed) as removed_count
               FROM performance
               WHERE project = ? AND post_type != '' AND timestamp > ?
               GROUP BY post_type
               HAVING count >= ?""",
            (project, since, 3),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_learned_weight(
        self, category: str, key: str, project: str,
        weight: float, sample_count: int, avg_engagement: float,
    ):
        """Update or insert a learned weight."""
        self._execute_write(
            """INSERT INTO learned_weights
               (category, key, project, weight, sample_count, avg_engagement)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(category, key, project)
               DO UPDATE SET weight=?, sample_count=?, avg_engagement=?,
                            updated_at=datetime('now')""",
            (category, key, project, weight, sample_count, avg_engagement,
             weight, sample_count, avg_engagement),
        )

    def get_learned_weights(
        self, category: str, project: str = "",
    ) -> List[Dict]:
        """Get learned weights for a category."""
        query = "SELECT * FROM learned_weights WHERE category = ?"
        params: list = [category]
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY weight DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def log_discovery(
        self, platform: str, project: str,
        discovery_type: str, value: str,
        source: str = "", score: float = 0.0,
    ):
        """Log a discovered subreddit/keyword/trend."""
        self._execute_write(
            """INSERT OR IGNORE INTO discoveries
               (platform, project, discovery_type, value, source, score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (platform, project, discovery_type, value, source, score),
        )

    def get_discoveries(
        self, platform: str = "", project: str = "",
        status: str = "candidate", limit: int = 20,
    ) -> List[Dict]:
        """Get discovered items."""
        query = "SELECT * FROM discoveries WHERE status = ?"
        params: list = [status]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_discovery_status(self, discovery_id: int, status: str):
        """Update discovery status (candidate/approved/rejected)."""
        self._execute_write(
            "UPDATE discoveries SET status = ? WHERE id = ?",
            (status, discovery_id),
        )

    # ── Subreddit Intelligence ────────────────────────────────────────

    def upsert_subreddit_intel(self, subreddit: str, project: str, data: Dict):
        """Insert or update subreddit intelligence data."""
        self._execute_write(
            """INSERT INTO subreddit_intel
               (subreddit, project, subscribers, active_users, created_utc,
                description, subreddit_type, over18, posts_per_day,
                avg_hours_between_posts, median_post_score, avg_comments_per_post,
                mod_count, active_mod_count, opportunity_score, relevance_score,
                metadata, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(subreddit, project) DO UPDATE SET
                subscribers=?, active_users=?, posts_per_day=?,
                avg_hours_between_posts=?, median_post_score=?,
                avg_comments_per_post=?, mod_count=?, active_mod_count=?,
                opportunity_score=?, relevance_score=?, metadata=?,
                updated_at=datetime('now')""",
            (
                subreddit, project,
                data.get("subscribers", 0), data.get("active_users", 0),
                data.get("created_utc", 0), data.get("description", ""),
                data.get("subreddit_type", "public"), data.get("over18", 0),
                data.get("posts_per_day", 0.0), data.get("avg_hours_between_posts", 0.0),
                data.get("median_post_score", 0.0), data.get("avg_comments_per_post", 0.0),
                data.get("mod_count", -1), data.get("active_mod_count", -1),
                data.get("opportunity_score", 0.0), data.get("relevance_score", 0.0),
                json.dumps(data.get("metadata")) if data.get("metadata") else None,
                # ON CONFLICT UPDATE values:
                data.get("subscribers", 0), data.get("active_users", 0),
                data.get("posts_per_day", 0.0), data.get("avg_hours_between_posts", 0.0),
                data.get("median_post_score", 0.0), data.get("avg_comments_per_post", 0.0),
                data.get("mod_count", -1), data.get("active_mod_count", -1),
                data.get("opportunity_score", 0.0), data.get("relevance_score", 0.0),
                json.dumps(data.get("metadata")) if data.get("metadata") else None,
            ),
        )

    def get_subreddit_intel(
        self, project: str = "", min_score: float = 0.0, limit: int = 20,
    ) -> List[Dict]:
        """Get subreddit intelligence records sorted by opportunity_score."""
        query = "SELECT * FROM subreddit_intel WHERE opportunity_score >= ?"
        params: list = [min_score]
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY opportunity_score DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_subreddit_intel_single(self, subreddit: str) -> Optional[Dict]:
        """Get intel for a single subreddit (most recent)."""
        row = self.conn.execute(
            "SELECT * FROM subreddit_intel WHERE subreddit = ? ORDER BY updated_at DESC LIMIT 1",
            (subreddit,),
        ).fetchone()
        return dict(row) if row else None

    def get_stale_subreddits(self, hours: int = 24, project: str = "") -> List[str]:
        """Get subreddits whose intel is older than N hours or missing."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        query = "SELECT subreddit FROM subreddit_intel WHERE updated_at < ?"
        params: list = [cutoff]
        if project:
            query += " AND project = ?"
            params.append(project)
        rows = self.conn.execute(query, params).fetchall()
        return [row["subreddit"] for row in rows]

    # ── Community Presence ─────────────────────────────────────────

    def upsert_community_presence(
        self, subreddit: str, project: str, account: str, updates: Dict,
    ):
        """Insert or update community presence metrics."""
        existing = self.get_presence_for_subreddit(subreddit, project, account)
        if existing:
            # Build dynamic SET clause
            set_parts = ["updated_at = datetime('now')"]
            params = []
            for key, val in updates.items():
                set_parts.append(f"{key} = ?")
                params.append(val)
            params.extend([subreddit, project, account])
            self._execute_write(
                f"UPDATE community_presence SET {', '.join(set_parts)} "
                f"WHERE subreddit = ? AND project = ? AND account = ?",
                tuple(params),
            )
        else:
            cols = ["subreddit", "project", "account"]
            vals = [subreddit, project, account]
            for key, val in updates.items():
                cols.append(key)
                vals.append(val)
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            self._execute_write(
                f"INSERT INTO community_presence ({col_names}) VALUES ({placeholders})",
                tuple(vals),
            )

    def get_community_presence(
        self, project: str = "", account: str = "",
        stage: str = "", limit: int = 50,
    ) -> List[Dict]:
        """Get community presence records with optional filters."""
        query = "SELECT * FROM community_presence WHERE 1=1"
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if account:
            query += " AND account = ?"
            params.append(account)
        if stage:
            query += " AND stage = ?"
            params.append(stage)
        query += " ORDER BY warmth_score DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_presence_for_subreddit(
        self, subreddit: str, project: str, account: str,
    ) -> Optional[Dict]:
        """Get presence data for a specific subreddit."""
        row = self.conn.execute(
            "SELECT * FROM community_presence WHERE subreddit = ? AND project = ? AND account = ?",
            (subreddit, project, account),
        ).fetchone()
        return dict(row) if row else None

    def get_neglected_subreddits(
        self, project: str, account: str, hours: int = 48,
    ) -> List[Dict]:
        """Get subreddits where last_activity is older than N hours."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            """SELECT * FROM community_presence
               WHERE project = ? AND account = ?
               AND (last_activity < ? OR last_activity IS NULL)
               ORDER BY warmth_score DESC""",
            (project, account, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Knowledge Base ─────────────────────────────────────────────

    def log_knowledge(
        self, project: str, category: str, topic: str, content: str,
        source: str = "", relevance_score: float = 1.0,
        expires_at: Optional[str] = None, metadata: Optional[Dict] = None,
    ):
        """Insert a knowledge base entry."""
        self._execute_write(
            """INSERT INTO knowledge_base
               (project, category, topic, content, source, relevance_score,
                expires_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project, category, topic, content, source, relevance_score,
             expires_at, json.dumps(metadata) if metadata else None),
        )

    def get_knowledge(
        self, project: str, category: str = "", topic: str = "",
        limit: int = 10, max_age_hours: int = 0,
    ) -> List[Dict]:
        """Query knowledge base with optional filters."""
        query = "SELECT * FROM knowledge_base WHERE project = ?"
        params: list = [project]
        if category:
            query += " AND category = ?"
            params.append(category)
        if topic:
            query += " AND topic LIKE ?"
            params.append(f"%{topic}%")
        if max_age_hours > 0:
            cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
            query += " AND timestamp > ?"
            params.append(cutoff)
        # Filter out expired entries
        query += " AND (expires_at IS NULL OR expires_at > datetime('now'))"
        query += " ORDER BY relevance_score DESC, timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_knowledge_used(self, knowledge_id: int):
        """Increment used_count for a knowledge entry."""
        self._execute_write(
            "UPDATE knowledge_base SET used_count = used_count + 1 WHERE id = ?",
            (knowledge_id,),
        )

    def cleanup_expired_knowledge(self):
        """Delete expired knowledge base entries."""
        with self._lock:
            self.conn.execute(
                "DELETE FROM knowledge_base WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
            )
            self.conn.commit()

    # ── Subreddit Trends ──────────────────────────────────────────

    def log_subreddit_trends(
        self, subreddit: str, project: str,
        themes: List[str], questions: List[str],
        avg_score: float = 0.0, hot_count: int = 0,
    ):
        """Insert subreddit trend analysis."""
        self._execute_write(
            """INSERT INTO subreddit_trends
               (subreddit, project, top_themes, recurring_questions,
                avg_score, hot_post_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (subreddit, project, json.dumps(themes), json.dumps(questions),
             avg_score, hot_count),
        )

    def get_subreddit_trends(
        self, subreddit: str = "", project: str = "", days: int = 7,
    ) -> List[Dict]:
        """Get recent subreddit trends."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        query = "SELECT * FROM subreddit_trends WHERE timestamp > ?"
        params: list = [cutoff]
        if subreddit:
            query += " AND subreddit = ?"
            params.append(subreddit)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY timestamp DESC LIMIT 50"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ── A/B Testing ───────────────────────────────────────────────

    def create_experiment(
        self, project: str, name: str, variable: str,
        variant_a: str, variant_b: str, min_samples: int = 10,
    ) -> int:
        """Create a new A/B experiment."""
        cursor = self._execute_write(
            """INSERT INTO ab_experiments
               (project, experiment_name, variable, variant_a, variant_b, min_samples)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project, name, variable, variant_a, variant_b, min_samples),
        )
        return cursor.lastrowid

    def log_ab_result(
        self, experiment_id: int, action_id: int, variant: str,
        engagement: float = 0.0, upvotes: int = 0, replies: int = 0,
        was_removed: bool = False,
    ):
        """Log a result for an A/B experiment."""
        self._execute_write(
            """INSERT INTO ab_results
               (experiment_id, action_id, variant, engagement_score,
                upvotes, replies, was_removed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (experiment_id, action_id, variant, engagement, upvotes, replies,
             1 if was_removed else 0),
        )

    def get_experiment_results(self, experiment_id: int) -> Dict:
        """Get aggregated results per variant for an experiment."""
        rows = self.conn.execute(
            """SELECT variant, COUNT(*) as count,
                      AVG(engagement_score) as avg_eng,
                      AVG(upvotes) as avg_up,
                      SUM(was_removed) as removed
               FROM ab_results WHERE experiment_id = ?
               GROUP BY variant""",
            (experiment_id,),
        ).fetchall()
        return {row["variant"]: dict(row) for row in rows}

    def get_running_experiments(self, project: str = "") -> List[Dict]:
        """Get active experiments."""
        query = "SELECT * FROM ab_experiments WHERE status = 'running'"
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def conclude_experiment(self, experiment_id: int, winner: str):
        """Mark an experiment as concluded."""
        self._execute_write(
            "UPDATE ab_experiments SET status = 'concluded', winner = ?, concluded_at = datetime('now') WHERE id = ?",
            (winner, experiment_id),
        )

    # ── Time Performance ──────────────────────────────────────────

    def log_time_performance(
        self, project: str, subreddit: str,
        hour: int, day: int, engagement: float,
        upvotes: float = 0.0, removed: int = 0,
    ):
        """Upsert time performance data."""
        self._execute_write(
            """INSERT INTO time_performance
               (project, subreddit, hour_of_day, day_of_week,
                action_count, avg_engagement, avg_upvotes, total_removed)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(project, subreddit, hour_of_day, day_of_week)
               DO UPDATE SET
                action_count = action_count + 1,
                avg_engagement = (avg_engagement * action_count + ?) / (action_count + 1),
                avg_upvotes = (avg_upvotes * action_count + ?) / (action_count + 1),
                total_removed = total_removed + ?,
                timestamp = datetime('now')""",
            (project, subreddit, hour, day, engagement, upvotes, removed,
             engagement, upvotes, removed),
        )

    def get_best_posting_times(
        self, project: str, subreddit: str = "_all", limit: int = 5,
    ) -> List[Dict]:
        """Get top posting time slots ranked by engagement."""
        rows = self.conn.execute(
            """SELECT * FROM time_performance
               WHERE project = ? AND subreddit = ? AND action_count >= 3
               ORDER BY avg_engagement DESC LIMIT ?""",
            (project, subreddit, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Failure Patterns ──────────────────────────────────────────

    def log_failure_pattern(
        self, project: str, subreddit: str,
        failure_type: str, pattern: str, avoidance_rule: str = "",
    ):
        """Insert or update a failure pattern."""
        self._execute_write(
            """INSERT INTO failure_patterns
               (project, subreddit, failure_type, pattern, avoidance_rule,
                last_seen, frequency)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 1)""",
            (project, subreddit, failure_type, pattern, avoidance_rule),
        )

    def get_failure_patterns(
        self, project: str, subreddit: str = "",
    ) -> List[Dict]:
        """Get active failure patterns."""
        query = "SELECT * FROM failure_patterns WHERE project = ?"
        params: list = [project]
        if subreddit:
            query += " AND subreddit = ?"
            params.append(subreddit)
        query += " ORDER BY frequency DESC, timestamp DESC LIMIT 10"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ── Performance Stats (extended) ──────────────────────────────

    def get_performance_stats_range(
        self, project: str, days_ago_start: int, days_ago_end: int,
    ) -> List[Dict]:
        """Get performance stats for a specific date range."""
        since = (datetime.utcnow() - timedelta(days=days_ago_start)).isoformat()
        until = (datetime.utcnow() - timedelta(days=days_ago_end)).isoformat()
        rows = self.conn.execute(
            """SELECT subreddit_or_query, keyword, action_type,
                      COUNT(*) as count,
                      AVG(engagement_score) as avg_engagement,
                      SUM(upvotes) as total_upvotes,
                      SUM(replies) as total_replies,
                      SUM(was_removed) as removed_count
               FROM performance
               WHERE project = ? AND timestamp > ? AND timestamp <= ?
               GROUP BY subreddit_or_query, keyword
               ORDER BY avg_engagement DESC""",
            (project, since, until),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Relationships & Conversations (Phase 6) ─────────────────────

    def upsert_user_profile(self, platform: str, username: str, **fields):
        """Create or update a user profile."""
        existing = self.conn.execute(
            "SELECT id FROM user_profiles WHERE platform = ? AND username = ?",
            (platform, username),
        ).fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in fields)
            if sets:
                vals = list(fields.values()) + [platform, username]
                self._execute_write(
                    f"UPDATE user_profiles SET {sets}, last_updated = datetime('now') "
                    f"WHERE platform = ? AND username = ?",
                    tuple(vals),
                )
        else:
            cols = ["platform", "username"] + list(fields.keys())
            placeholders = ", ".join("?" for _ in cols)
            vals = [platform, username] + list(fields.values())
            self._execute_write(
                f"INSERT OR IGNORE INTO user_profiles ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(vals),
            )

    def get_user_profile(self, platform: str, username: str) -> Optional[Dict]:
        """Get a user profile."""
        row = self.conn.execute(
            "SELECT * FROM user_profiles WHERE platform = ? AND username = ?",
            (platform, username),
        ).fetchone()
        return dict(row) if row else None

    def upsert_relationship(
        self, platform: str, username: str, our_account: str, project: str, **fields
    ):
        """Create or update a relationship."""
        existing = self.conn.execute(
            "SELECT id FROM relationships WHERE platform = ? AND username = ? AND our_account = ?",
            (platform, username, our_account),
        ).fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in fields)
            if sets:
                vals = list(fields.values()) + [platform, username, our_account]
                self._execute_write(
                    f"UPDATE relationships SET {sets}, last_interaction = datetime('now') "
                    f"WHERE platform = ? AND username = ? AND our_account = ?",
                    tuple(vals),
                )
            return existing["id"]
        else:
            cols = ["platform", "username", "our_account", "project"] + list(fields.keys())
            placeholders = ", ".join("?" for _ in cols)
            vals = [platform, username, our_account, project] + list(fields.values())
            cursor = self._execute_write(
                f"INSERT OR IGNORE INTO relationships ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(vals),
            )
            return cursor.lastrowid

    def get_relationship(
        self, platform: str, username: str, our_account: str,
    ) -> Optional[Dict]:
        """Get a specific relationship."""
        row = self.conn.execute(
            "SELECT * FROM relationships WHERE platform = ? AND username = ? AND our_account = ?",
            (platform, username, our_account),
        ).fetchone()
        return dict(row) if row else None

    def get_relationships_by_stage(
        self, project: str, stage: str, platform: str = "",
    ) -> List[Dict]:
        """Get relationships at a specific stage."""
        query = "SELECT * FROM relationships WHERE project = ? AND stage = ? AND is_blocked = 0"
        params: list = [project, stage]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY trust_score DESC LIMIT 50"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_relationships_needing_action(
        self, project: str, platform: str = "",
    ) -> List[Dict]:
        """Get relationships with scheduled actions that are due."""
        query = (
            "SELECT * FROM relationships "
            "WHERE project = ? AND is_blocked = 0 "
            "AND next_action IS NOT NULL "
            "AND (next_action_after IS NULL OR next_action_after <= datetime('now'))"
        )
        params: list = [project]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY trust_score DESC LIMIT 20"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def advance_relationship_stage(self, rel_id: int, new_stage: str):
        """Advance a relationship to a new stage."""
        self._execute_write(
            "UPDATE relationships SET stage = ?, last_interaction = datetime('now') WHERE id = ?",
            (new_stage, rel_id),
        )

    def log_conversation(
        self, relationship_id: int, platform: str, direction: str,
        content: str, subject: str = "", message_id: str = "",
    ):
        """Log a DM/message in conversation history."""
        self._execute_write(
            """INSERT INTO conversations
               (relationship_id, platform, direction, content, subject, message_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (relationship_id, platform, direction, content, subject, message_id),
        )
        # Update relationship DM counts
        if direction == "sent":
            self._execute_write(
                "UPDATE relationships SET dms_sent = dms_sent + 1, last_interaction = datetime('now') WHERE id = ?",
                (relationship_id,),
            )
        elif direction == "received":
            self._execute_write(
                "UPDATE relationships SET dms_received = dms_received + 1, last_interaction = datetime('now') WHERE id = ?",
                (relationship_id,),
            )

    def get_conversation_history(
        self, relationship_id: int, limit: int = 20,
    ) -> List[Dict]:
        """Get conversation history for a relationship."""
        rows = self.conn.execute(
            "SELECT * FROM conversations WHERE relationship_id = ? ORDER BY timestamp DESC LIMIT ?",
            (relationship_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_unread_messages(self, platform: str = "") -> List[Dict]:
        """Get unread received messages."""
        query = "SELECT c.*, r.username, r.our_account, r.project FROM conversations c JOIN relationships r ON c.relationship_id = r.id WHERE c.direction = 'received' AND c.read = 0"
        params: list = []
        if platform:
            query += " AND c.platform = ?"
            params.append(platform)
        query += " ORDER BY c.timestamp DESC LIMIT 50"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_message_read(self, conv_id: int):
        """Mark a message as read."""
        self._execute_write(
            "UPDATE conversations SET read = 1 WHERE id = ?", (conv_id,)
        )

    def get_dm_count_today(self, platform: str, our_account: str) -> int:
        """Count DMs sent today for rate limiting."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM conversations c
               JOIN relationships r ON c.relationship_id = r.id
               WHERE c.direction = 'sent'
               AND c.platform = ?
               AND r.our_account = ?
               AND c.timestamp > datetime('now', '-1 day')""",
            (platform, our_account),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_relationship_stats(self, project: str) -> Dict[str, int]:
        """Get counts of relationships by stage."""
        rows = self.conn.execute(
            "SELECT stage, COUNT(*) as cnt FROM relationships WHERE project = ? AND is_blocked = 0 GROUP BY stage",
            (project,),
        ).fetchall()
        return {row["stage"]: row["cnt"] for row in rows}

    def get_recent_conversations(self, limit: int = 20) -> List[Dict]:
        """Get recent conversations with relationship context."""
        rows = self.conn.execute(
            """SELECT c.*, r.username, r.platform, r.stage, r.our_account, r.project
               FROM conversations c
               JOIN relationships r ON c.relationship_id = r.id
               ORDER BY c.timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_stale_relationships(self, days: int = 60):
        """Remove inactive relationships older than N days."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._lock:
            # Delete conversations for stale noticed/engaged relationships
            self.conn.execute(
                """DELETE FROM conversations WHERE relationship_id IN (
                       SELECT id FROM relationships
                       WHERE stage IN ('noticed', 'engaged')
                       AND last_interaction < ?
                   )""",
                (cutoff,),
            )
            self.conn.execute(
                "DELETE FROM relationships WHERE stage IN ('noticed', 'engaged') AND last_interaction < ?",
                (cutoff,),
            )
            self.conn.commit()

    # ── Per-Account Subreddit Stats (Authority Building) ────────────

    def update_subreddit_stats(
        self, account: str, platform: str, subreddit: str
    ):
        """Increment activity count for an account in a subreddit."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO account_subreddit_stats
                       (account, platform, subreddit, actions_count, last_activity, updated_at)
                   VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))
                   ON CONFLICT(account, platform, subreddit) DO UPDATE SET
                       actions_count = actions_count + 1,
                       last_activity = datetime('now'),
                       updated_at = datetime('now')""",
                (account, platform, subreddit),
            )
            self.conn.commit()

    def get_top_subreddits_for_account(
        self, account: str, platform: str, limit: int = 5
    ) -> List[Dict]:
        """Get the subreddits where an account has most activity."""
        rows = self.conn.execute(
            """SELECT subreddit, actions_count, last_activity
               FROM account_subreddit_stats
               WHERE account = ? AND platform = ?
               ORDER BY actions_count DESC
               LIMIT ?""",
            (account, platform, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Maintenance ──────────────────────────────────────────────────

    def _maybe_cleanup(self):
        """Periodic cleanup: remove old data, checkpoint WAL."""
        elapsed = (datetime.utcnow() - self._last_cleanup).total_seconds()
        if elapsed < _CLEANUP_INTERVAL_HOURS * 3600:
            return
        self._last_cleanup = datetime.utcnow()
        try:
            self._cleanup_old_data()
            self._wal_checkpoint()
        except Exception as e:
            logger.warning(f"DB cleanup error: {e}")

    def _cleanup_old_data(self):
        """Remove old data to keep DB small."""
        cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
        cutoff_7d = (datetime.utcnow() - timedelta(days=7)).isoformat()
        with self._lock:
            self.conn.execute("DELETE FROM actions WHERE timestamp < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM opportunities WHERE status != 'pending' AND timestamp < ?",
                (cutoff,),
            )
            self.conn.execute("DELETE FROM analytics WHERE timestamp < ?", (cutoff,))
            # Phase 5: cleanup intel (stale after 7 days), expired knowledge, old trends
            self.conn.execute("DELETE FROM subreddit_intel WHERE updated_at < ?", (cutoff_7d,))
            self.conn.execute("DELETE FROM knowledge_base WHERE expires_at IS NOT NULL AND expires_at < datetime('now')")
            self.conn.execute("DELETE FROM subreddit_trends WHERE timestamp < ?", (cutoff,))
            self.conn.execute("DELETE FROM failure_patterns WHERE timestamp < ?", (cutoff,))
            self.conn.execute("DELETE FROM ab_results WHERE timestamp < ?", (cutoff,))
            # Phase 6: clean stale relationships + old conversations
            self.conn.execute(
                "DELETE FROM conversations WHERE timestamp < ?", (cutoff,)
            )
            self.conn.execute(
                "DELETE FROM relationships WHERE stage IN ('noticed', 'engaged') AND last_interaction < ?",
                (cutoff,),
            )
            self.conn.execute(
                "DELETE FROM user_profiles WHERE last_updated < ?", (cutoff,)
            )
            self.conn.commit()
        logger.info("DB cleanup: removed data older than 30 days")

    def _wal_checkpoint(self):
        """Force WAL checkpoint to keep WAL file small."""
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.debug("WAL checkpoint completed")

    def force_maintenance(self):
        """Manually trigger cleanup + checkpoint."""
        self._cleanup_old_data()
        self._wal_checkpoint()

    def get_db_size_mb(self) -> float:
        """Get database file size in MB (including WAL)."""
        try:
            size = os.path.getsize(self.db_path)
            wal_path = self.db_path + "-wal"
            if os.path.exists(wal_path):
                size += os.path.getsize(wal_path)
            return size / (1024 * 1024)
        except OSError:
            return 0.0

    def close(self):
        """Close all thread-local database connections cleanly."""
        if self._closed:
            return
        self._closed = True
        try:
            with self._lock:
                conn = self.conn
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                self._local.conn = None
            logger.debug("Database connection closed")
        except Exception as e:
            logger.warning(f"Error closing DB: {e}")
