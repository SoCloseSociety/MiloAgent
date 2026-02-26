"""Milo — Rich Terminal UI Dashboard V3.

Full-screen, live-updating dashboard with:
- Reddit/Twitter minimaps (per-subreddit/keyword activity)
- Agent Brain panel (intelligence, learning, discoveries, A/B)
- Per-account stats (comments, likes, posts)
- Performance scoring with improvement points
- Live event log + scheduler view
- Keyboard-driven commands
"""

import os
import sys
import time
import select
import tty
import termios
import json
import logging
import threading
from collections import deque
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich import box

logger = logging.getLogger(__name__)


# ── Live Log Handler ────────────────────────────────────────────────

class _TUILogHandler(logging.Handler):
    """Captures log records into a bounded deque for the TUI live feed."""

    def __init__(self, maxlen: int = 80):
        super().__init__()
        self.records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = record.getMessage()
            if len(msg) > 140:
                msg = msg[:137] + "..."
            with self._lock:
                self.records.append((ts, record.levelno, record.name, msg))
        except Exception:
            pass

    def get_recent(self, n: int = 25) -> list:
        with self._lock:
            return list(self.records)[-n:]


# ── Helpers ─────────────────────────────────────────────────────────

def _ago(iso_str: str) -> str:
    """Convert ISO timestamp to 'Xm ago' / 'Xh ago'."""
    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.utcnow() - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "now"
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m"
        elif secs < 86400:
            return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
        else:
            return f"{secs // 86400}d"
    except Exception:
        return "?"


def _bar(value: float, max_val: float, width: int = 12) -> Text:
    """Render a mini progress bar as Text."""
    ratio = min(value / max(max_val, 1), 1.0)
    filled = int(ratio * width)
    empty = width - filled
    if ratio < 0.5:
        color = "green"
    elif ratio < 0.8:
        color = "yellow"
    else:
        color = "red"
    return Text(f"{'█' * filled}{'░' * empty} {value:.0f}%", style=color)


def _activity_dots(count: int, max_dots: int = 5) -> str:
    """Convert a count to activity dots: ●●●○○."""
    if max_dots <= 0:
        return ""
    filled = min(count, max_dots)
    empty = max_dots - filled
    return "●" * filled + "○" * empty


def _score_grade(score: float) -> tuple:
    """Convert 0-100 score to letter grade + color."""
    if score >= 90:
        return "A+", "bold green"
    elif score >= 80:
        return "A", "green"
    elif score >= 70:
        return "B", "bright_green"
    elif score >= 60:
        return "C", "yellow"
    elif score >= 50:
        return "D", "bright_red"
    else:
        return "F", "red"


class RichDashboard:
    """Full-screen Rich TUI V3 for monitoring Milo."""

    REFRESH_HZ = 2

    # Loggers that produce excessive noise (retry spam, HTTP traces)
    _NOISY_LOGGERS = (
        "httpx", "httpcore", "openai", "openai._base_client",
        "urllib3", "urllib3.connectionpool", "requests",
        "hpack", "h2",
    )

    _VIEW_MODES = ["main", "accounts", "convos", "opps"]

    def __init__(self, orchestrator):
        self.orch = orchestrator
        # Detect VSCode terminal — it has issues with alternate screen buffer
        self._is_vscode = os.environ.get("TERM_PROGRAM") == "vscode"
        self.console = Console(force_terminal=True)
        self._running = False
        self._key_thread = None
        self._flash_msg = ""
        self._flash_until = 0.0
        self._view_mode = "main"  # main | accounts | convos | opps
        self._show_help = False

        # Command input (vim-style : prefix)
        self._command_buffer = ""
        self._command_mode = False

        # Cached stats (refreshed every 5s to avoid DB hammering)
        self._stats_cache = {}
        self._stats_ts = 0
        self._account_stats_cache = {}
        self._account_stats_ts = 0
        self._opps_cache = []
        self._opps_ts = 0

        # Install live log handler
        self._log_handler = _TUILogHandler(maxlen=80)
        self._log_handler.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(self._log_handler)

        # Saved state for cleanup
        self._saved_stream_handlers = []
        self._saved_logger_levels = {}

    def _suppress_noisy_loggers(self):
        """Suppress noisy third-party loggers and remove root StreamHandlers.

        The openai client retries 429 errors internally and httpx logs every
        attempt at INFO level — thousands of lines per minute when a provider
        is rate-limited.  These go to the root StreamHandler (stderr) which
        prints *below* the Rich Live screen, causing the terminal to scroll.
        """
        # 1. Silence noisy third-party loggers
        for name in self._NOISY_LOGGERS:
            lg = logging.getLogger(name)
            self._saved_logger_levels[name] = lg.level
            lg.setLevel(logging.CRITICAL)

        # 2. Remove console StreamHandlers from root logger while TUI is running
        #    (keep FileHandlers alive for persistent logging)
        root = logging.getLogger()
        for h in root.handlers[:]:
            if isinstance(h, logging.StreamHandler) and h is not self._log_handler and not isinstance(h, logging.FileHandler):
                self._saved_stream_handlers.append(h)
                root.removeHandler(h)

    def _restore_loggers(self):
        """Restore original logger levels and handlers on TUI exit."""
        for name, level in self._saved_logger_levels.items():
            logging.getLogger(name).setLevel(level)
        self._saved_logger_levels.clear()

        root = logging.getLogger()
        for h in self._saved_stream_handlers:
            root.addHandler(h)
        self._saved_stream_handlers.clear()

    def _cleanup_log_handler(self):
        self._restore_loggers()
        root = logging.getLogger()
        root.removeHandler(self._log_handler)

    def _get_stats(self) -> dict:
        """Cached stats refresh every 5 seconds."""
        now = time.time()
        if now - self._stats_ts > 5:
            try:
                self._stats_cache = self.orch.db.get_stats_summary(hours=24)
            except Exception:
                pass
            self._stats_ts = now
        return self._stats_cache

    def _get_account_stats(self) -> dict:
        """Per-account stats cached every 8 seconds."""
        now = time.time()
        if now - self._account_stats_ts > 8:
            result = {}
            try:
                for platform in ("reddit", "twitter"):
                    accounts = self.orch.account_mgr.load_accounts(platform)
                    for acc in accounts:
                        username = acc["username"]
                        key = f"{platform}:{username}"
                        # Get per-action-type counts
                        try:
                            recent = self.orch.db.get_recent_actions(
                                hours=24, account=username, platform=platform, limit=200
                            )
                            type_counts = {}
                            for a in (recent or []):
                                t = a.get("action_type", "unknown")
                                type_counts[t] = type_counts.get(t, 0) + 1
                            total = sum(type_counts.values())
                            result[key] = {
                                "username": username,
                                "platform": platform,
                                "total": total,
                                "types": type_counts,
                                "cookies": os.path.exists(acc.get("cookies_file", "")),
                                "status": self.orch.account_mgr._statuses.get(key, "healthy"),
                            }
                        except Exception:
                            result[key] = {
                                "username": username,
                                "platform": platform,
                                "total": 0,
                                "types": {},
                                "cookies": False,
                                "status": "unknown",
                            }
            except Exception:
                pass
            self._account_stats_cache = result
            self._account_stats_ts = now
        return self._account_stats_cache

    # ── Layout ──────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["left"].split_column(
            Layout(name="minimaps", size=14),
            Layout(name="accounts_overview", size=12),
            Layout(name="event_log"),
        )
        layout["right"].split_column(
            Layout(name="brain", size=16),
            Layout(name="performance", size=8),
            Layout(name="schedule"),
        )
        layout["minimaps"].split_row(
            Layout(name="reddit_map"),
            Layout(name="twitter_map"),
        )
        return layout

    # ── Header ──────────────────────────────────────────────────────

    def _render_header(self) -> Panel:
        paused = self.orch._paused
        status = Text(" PAUSED ", style="bold white on red") if paused else Text(" LIVE ", style="bold white on green")
        mode = self.orch._mode.upper()
        n_proj = len(self.orch.projects)
        proj_names = ", ".join(p.get("project", {}).get("name", "?") for p in self.orch.projects)

        # Uptime
        pid_file = self.orch.settings.get("process", {}).get("pid_file", "data/miloagent.pid")
        uptime = "?"
        try:
            if os.path.exists(pid_file):
                delta = time.time() - os.path.getmtime(pid_file)
                h, m = int(delta // 3600), int((delta % 3600) // 60)
                uptime = f"{h}h{m:02d}m"
        except Exception:
            pass

        # Flash message
        flash = ""
        if self._flash_msg and time.time() < self._flash_until:
            flash = f"  [{self._flash_msg}]"

        # Version from settings
        version = self.orch.settings.get("bot", {}).get("version", "3.0")

        grid = Table.grid(padding=1)
        grid.add_column(ratio=1)
        grid.add_column(justify="center", ratio=2)
        grid.add_column(justify="right", ratio=1)
        grid.add_row(
            Text.assemble((" MILO ", "bold white on bright_blue"), f" v{version} {mode}"),
            Text.assemble(status, f"  {proj_names}", (flash, "bold yellow")),
            Text(f"Up: {uptime}  {n_proj} proj", style="dim"),
        )
        return Panel(grid, box=box.HEAVY, style="bright_blue")

    # ── Reddit Minimap ──────────────────────────────────────────────

    def _render_reddit_map(self) -> Panel:
        """Per-subreddit activity minimap showing engagement level."""
        tbl = Table(box=None, show_header=True, padding=(0, 1), expand=True)
        tbl.add_column("Subreddit", style="bold bright_red", width=16)
        tbl.add_column("Act", width=5, justify="center")
        tbl.add_column("24h", width=3, justify="right")
        tbl.add_column("Stage", width=7)

        try:
            stats = self._get_stats()
            # Get subreddit-level action breakdown
            recent_actions = self.orch.db.get_recent_actions(
                hours=24, platform="reddit", limit=200
            )

            # Count per-subreddit
            sub_counts = {}
            for a in (recent_actions or []):
                meta = a.get("metadata", "")
                if isinstance(meta, str) and meta:
                    try:
                        m = json.loads(meta)
                        sub = m.get("subreddit", "")
                        if sub:
                            sub_counts[sub] = sub_counts.get(sub, 0) + 1
                    except Exception:
                        pass

            # Also get subreddits from config for zero-activity subs
            for proj in self.orch.projects[:1]:
                subs_cfg = proj.get("reddit", {}).get("target_subreddits", {})
                if isinstance(subs_cfg, dict):
                    all_subs = subs_cfg.get("primary", []) + subs_cfg.get("secondary", [])
                else:
                    all_subs = subs_cfg if isinstance(subs_cfg, list) else []

                for s in all_subs:
                    if s not in sub_counts:
                        sub_counts[s] = 0

            # Sort by activity (desc), show top 10
            sorted_subs = sorted(sub_counts.items(), key=lambda x: -x[1])
            for sub_name, count in sorted_subs[:9]:
                dots = _activity_dots(count, 5)
                dot_style = "green" if count >= 3 else ("yellow" if count >= 1 else "dim")

                # Get presence stage
                stage = ""
                try:
                    for proj in self.orch.projects[:1]:
                        pname = proj.get("project", {}).get("name", "")
                        presence = self.orch.db.get_community_presence(pname)
                        for p in (presence or []):
                            if p.get("subreddit") == sub_name:
                                stage = p.get("stage", "new")[:5]
                                break
                except Exception:
                    pass

                stage_style = {
                    "new": "dim", "warmi": "yellow", "estab": "green", "trust": "bright_green",
                }.get(stage, "dim")

                tbl.add_row(
                    f"r/{sub_name[:14]}",
                    Text(dots, style=dot_style),
                    Text(str(count), style="bold" if count > 0 else "dim"),
                    Text(stage or "-", style=stage_style),
                )

            if not sub_counts:
                tbl.add_row("(no data)", "", "", "")

        except Exception:
            tbl.add_row("(error)", "", "", "")

        return Panel(tbl, title="Reddit Map", border_style="bright_red", box=box.ROUNDED)

    # ── Twitter Minimap ─────────────────────────────────────────────

    def _render_twitter_map(self) -> Panel:
        """Twitter activity minimap with keyword activity."""
        tbl = Table(box=None, show_header=True, padding=(0, 1), expand=True)
        tbl.add_column("Keyword/Acc", style="bold bright_cyan", width=16)
        tbl.add_column("Act", width=5, justify="center")
        tbl.add_column("24h", width=3, justify="right")

        try:
            recent_actions = self.orch.db.get_recent_actions(
                hours=24, platform="twitter", limit=200
            )

            # Count per action type
            type_counts = {}
            for a in (recent_actions or []):
                t = a.get("action_type", "unknown")
                type_counts[t] = type_counts.get(t, 0) + 1

            # Show action types with dots
            for atype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                dots = _activity_dots(count, 5)
                dot_style = "green" if count >= 3 else ("yellow" if count >= 1 else "dim")
                tbl.add_row(
                    atype.capitalize()[:16],
                    Text(dots, style=dot_style),
                    Text(str(count), style="bold" if count > 0 else "dim"),
                )

            # Show keywords from config
            if not type_counts:
                for proj in self.orch.projects[:1]:
                    kws = proj.get("twitter", {}).get("keywords", [])
                    for kw in kws[:6]:
                        tbl.add_row(kw[:16], Text("○○○○○", style="dim"), "0")

            # Show X accounts
            tbl.add_row("", "", "")
            x_accs = self.orch.account_mgr.load_accounts("twitter")
            for acc in x_accs:
                username = acc["username"]
                count = self.orch.db.get_action_count(hours=24, account=username, platform="twitter")
                dots = _activity_dots(count, 5)
                status = "●" if os.path.exists(acc.get("cookies_file", "")) else "○"
                tbl.add_row(
                    f"{status} @{username[:14]}",
                    Text(dots, style="green" if count > 0 else "dim"),
                    Text(str(count), style="bold" if count > 0 else "dim"),
                )

        except Exception:
            tbl.add_row("(error)", "", "")

        return Panel(tbl, title="Twitter Map", border_style="bright_cyan", box=box.ROUNDED)

    # ── Per-Account Overview ────────────────────────────────────────

    def _render_accounts_overview(self) -> Panel:
        """Per-account breakdown: comments, likes, posts."""
        tbl = Table(box=box.SIMPLE, padding=(0, 1), expand=True)
        tbl.add_column("Account", style="bold", width=16)
        tbl.add_column("Plat", width=4)
        tbl.add_column("Coms", width=5, justify="right", style="bright_green")
        tbl.add_column("Like", width=5, justify="right", style="bright_yellow")
        tbl.add_column("Post", width=5, justify="right", style="bright_cyan")
        tbl.add_column("Tot", width=4, justify="right", style="bold")
        tbl.add_column("HP", width=8)

        acc_stats = self._get_account_stats()
        for key in sorted(acc_stats.keys()):
            s = acc_stats[key]
            types = s["types"]
            username = s["username"][:16]
            plat = "R" if s["platform"] == "reddit" else "X"

            comments = types.get("comment", 0) + types.get("reply", 0)
            likes = types.get("upvote", 0) + types.get("like", 0)
            posts = types.get("post", 0) + types.get("tweet", 0) + types.get("seed", 0)
            total = s["total"]

            # Health status bar
            status = s["status"]
            hp_map = {"healthy": ("█████", "green"), "cooldown": ("███░░", "yellow"),
                       "warned": ("██░░░", "red"), "banned": ("░░░░░", "bright_red")}
            hp_bar, hp_style = hp_map.get(status, ("?????", "dim"))

            tbl.add_row(
                username, plat,
                str(comments), str(likes), str(posts),
                str(total),
                Text(hp_bar, style=hp_style),
            )

        if not acc_stats:
            tbl.add_row("(loading)", "", "", "", "", "", "")

        return Panel(tbl, title="Account Stats (24h)", border_style="yellow", box=box.ROUNDED)

    # ── Agent Brain ─────────────────────────────────────────────────

    def _render_brain(self) -> Panel:
        """Agent intelligence overview: capabilities, learning, research, A/B."""
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=20, style="dim")
        grid.add_column()

        try:
            # Learning status
            insights = {}
            try:
                insights = self.orch.learning.get_insights()
            except Exception:
                pass

            top_subs = insights.get("top_subreddits", [])
            top_subs_str = ", ".join(f"r/{s['name']}" for s in top_subs[:3]) if top_subs else "(learning...)"
            grid.add_row("Top subs:", Text(top_subs_str, style="bright_green"))

            ratio = insights.get("optimal_promo_ratio", 0.25)
            grid.add_row("Promo ratio:", Text(f"{ratio:.0%} promo / {1-ratio:.0%} organic", style="cyan"))

            best_tone = insights.get("best_tone", "N/A")
            grid.add_row("Best tone:", Text(best_tone, style="white"))

            # Discoveries
            disc = insights.get("pending_discoveries", 0)
            disc_style = "bright_yellow" if disc > 0 else "dim"
            grid.add_row("Discoveries:", Text(f"{disc} pending", style=disc_style))

            # Post-type top performers
            try:
                pt_parts = []
                for proj in self.orch.projects:
                    pname = proj.get("project", {}).get("name", "")
                    pt_stats = self.orch.db.get_post_type_stats(pname, days=30)
                    for pt in (pt_stats or [])[:3]:
                        pt_parts.append(f"{pt['post_type']}({pt['avg_engagement']:.1f})")
                pt_str = ", ".join(pt_parts[:3]) if pt_parts else "(no data)"
                grid.add_row("Top posts:", Text(pt_str, style="bright_cyan"))
            except Exception:
                grid.add_row("Top posts:", Text("-", style="dim"))

            # Sentiment indicator
            try:
                all_sent = []
                for proj in self.orch.projects:
                    pname = proj.get("project", {}).get("name", "")
                    sent = self.orch.db.get_sentiment_by_tone(pname, days=30)
                    all_sent.extend(sent or [])
                if all_sent:
                    avg = sum(s["avg_sentiment"] for s in all_sent) / len(all_sent)
                    total_r = sum(s["total_replies"] for s in all_sent)
                    s_icon = "▲" if avg > 0.1 else ("▼" if avg < -0.1 else "━")
                    s_style = "green" if avg > 0.1 else ("red" if avg < -0.1 else "yellow")
                    grid.add_row("Sentiment:", Text(f"{s_icon} {avg:+.2f} ({total_r} replies)", style=s_style))
                else:
                    grid.add_row("Sentiment:", Text("(no replies yet)", style="dim"))
            except Exception:
                grid.add_row("Sentiment:", Text("-", style="dim"))

            # A/B Tests — with experiment details
            try:
                ab_parts = []
                for proj in self.orch.projects:
                    pname = proj.get("project", {}).get("name", "")
                    exps = self.orch.ab_testing.get_active_experiments(pname)
                    for exp in (exps or []):
                        results = self.orch.db.get_experiment_results(exp["id"])
                        a_n = results.get("a", {}).get("count", 0)
                        b_n = results.get("b", {}).get("count", 0)
                        ab_parts.append(f"{exp['variable']}({a_n}v{b_n})")
                if ab_parts:
                    grid.add_row("A/B Tests:", Text(" | ".join(ab_parts), style="magenta"))
                else:
                    grid.add_row("A/B Tests:", Text("0 active", style="dim"))
            except Exception:
                grid.add_row("A/B Tests:", Text("-", style="dim"))

            # Evolved prompts
            try:
                evo_count = self.orch.db.conn.execute(
                    "SELECT COUNT(*) as c FROM prompt_evolution_log WHERE status='active'"
                ).fetchone()["c"]
                evo_style = "bright_green" if evo_count > 0 else "dim"
                grid.add_row("Evolved:", Text(f"{evo_count} templates", style=evo_style))
            except Exception:
                grid.add_row("Evolved:", Text("-", style="dim"))

            # LLM stats
            try:
                llm_stats = self.orch.llm.get_stats()
                total_calls = llm_stats.get("total_calls", 0)
                total_errors = llm_stats.get("total_errors", 0)
                err_style = "red" if total_errors > 0 else "green"
                grid.add_row("LLM calls:", Text(f"{total_calls} ({total_errors} err)", style=err_style))

                groq = llm_stats.get("groq_rate", {})
                rpd = groq.get("day", 0)
                rpd_limit = groq.get("day_limit", 14400)
                rpd_pct = rpd / max(rpd_limit, 1) * 100
                pct_style = "green" if rpd_pct < 50 else ("yellow" if rpd_pct < 80 else "red")
                grid.add_row("Groq RPD:", Text(f"{rpd}/{rpd_limit} ({rpd_pct:.0f}%)", style=pct_style))

                # Show disabled providers (circuit-breaker)
                disabled = llm_stats.get("disabled_providers", {})
                if disabled:
                    parts = []
                    for pname, secs_left in disabled.items():
                        mins = secs_left // 60
                        parts.append(f"{pname} ({mins}m)")
                    grid.add_row("Disabled:", Text(", ".join(parts), style="bright_red"))

                routing = llm_stats.get("routing", {})
                creative_chain = " > ".join(routing.get("creative", []))
                grid.add_row("Creative:", Text(creative_chain or "-", style="dim"))
            except Exception:
                grid.add_row("LLM:", Text("unavailable", style="dim"))

            # Relationships
            try:
                total_rels = 0
                friends = 0
                for proj in self.orch.projects:
                    pname = proj.get("project", {}).get("name", "")
                    rel_stats = self.orch.db.get_relationship_stats(pname)
                    total_rels += sum(rel_stats.values())
                    friends += rel_stats.get("friend", 0) + rel_stats.get("advocate", 0)
                grid.add_row("Relationships:", Text(f"{total_rels} ({friends} friends)", style="bright_red"))
            except Exception:
                grid.add_row("Relationships:", Text("-", style="dim"))

            # System resources
            state = self.orch.resource_monitor.get_state()
            ram_style = "green" if state.ram_used_percent < 70 else ("yellow" if state.ram_used_percent < 85 else "red")
            grid.add_row(
                "System:",
                Text(f"RAM {state.ram_used_percent:.0f}% | RSS {state.process_rss_mb:.0f}MB | Disk {state.disk_used_percent:.0f}%", style=ram_style),
            )

        except Exception as e:
            grid.add_row("Error:", Text(str(e)[:40], style="red"))

        return Panel(grid, title="Agent Brain", border_style="bright_magenta", box=box.ROUNDED)

    # ── Performance Score ───────────────────────────────────────────

    def _render_performance(self) -> Panel:
        """Performance score + improvement suggestions."""
        try:
            stats = self._get_stats()
            actions = stats.get("actions", {})
            total_actions = sum(sum(t.values()) for t in actions.values())

            # Score components (0-100)
            scores = []

            # Activity score (0-40): are we doing enough?
            max_expected = self.orch.settings.get("bot", {}).get("max_actions_per_hour", 18) * 24
            activity_score = min(40, (total_actions / max(max_expected, 1)) * 40)
            scores.append(activity_score)

            # Platform balance (0-20): Reddit vs Twitter
            r_actions = sum(actions.get("reddit", {}).values())
            t_actions = sum(actions.get("twitter", {}).values())
            total_plat = r_actions + t_actions
            if total_plat > 0:
                balance = 1.0 - abs(r_actions - t_actions) / total_plat
                balance_score = balance * 20
            else:
                balance_score = 0
            scores.append(balance_score)

            # Account usage (0-20): are all accounts active?
            acc_stats = self._get_account_stats()
            active_accs = sum(1 for s in acc_stats.values() if s["total"] > 0)
            total_accs = len(acc_stats)
            usage_score = (active_accs / max(total_accs, 1)) * 20
            scores.append(usage_score)

            # Content diversity (0-20): mix of action types
            all_types = set()
            for plat_types in actions.values():
                all_types.update(plat_types.keys())
            diversity_score = min(20, len(all_types) * 4)
            scores.append(diversity_score)

            total_score = sum(scores)
            grade, grade_style = _score_grade(total_score)

            # Build improvements list
            improvements = []
            if activity_score < 20:
                improvements.append("Low activity")
            if balance_score < 10:
                improvements.append("Platform imbalance")
            if usage_score < 15:
                improvements.append(f"Only {active_accs}/{total_accs} accs active")
            if diversity_score < 12:
                improvements.append("Need more action types")

            tbl = Table.grid(padding=(0, 1))
            tbl.add_column(width=6)
            tbl.add_column()
            tbl.add_row(
                Text(grade, style=grade_style),
                Text(f"{total_score:.0f}/100  Act:{activity_score:.0f} Bal:{balance_score:.0f} Acc:{usage_score:.0f} Div:{diversity_score:.0f}", style="dim"),
            )
            if improvements:
                tbl.add_row("", Text(" | ".join(improvements[:3]), style="bright_yellow"))
            else:
                tbl.add_row("", Text("All systems optimal", style="green"))

        except Exception:
            tbl = Table.grid()
            tbl.add_row(Text("(calculating...)", style="dim"))

        return Panel(tbl, title="Performance", border_style="bright_green", box=box.ROUNDED)

    # ── Schedule ────────────────────────────────────────────────────

    def _render_schedule(self) -> Panel:
        tbl = Table(box=None, show_header=True, padding=(0, 1), expand=True)
        tbl.add_column("Job", style="bold", width=16)
        tbl.add_column("In", justify="right", width=8)
        tbl.add_column("Every", width=6)

        try:
            jobs = self.orch.scheduler.get_jobs()
            for job in sorted(jobs, key=lambda j: (j.next_run_time is None, str(j.next_run_time or ""))):
                next_run = "[dim]off[/]"
                if job.next_run_time:
                    delta = (job.next_run_time.replace(tzinfo=None) - datetime.now()).total_seconds()
                    if delta < 0:
                        next_run = "[red]now![/]"
                    elif delta < 60:
                        next_run = f"[green]{int(delta)}s[/]"
                    elif delta < 3600:
                        next_run = f"{int(delta // 60)}m"
                    else:
                        next_run = f"{int(delta // 3600)}h{int((delta % 3600) // 60):02d}m"

                interval = "-"
                try:
                    trigger = job.trigger
                    if hasattr(trigger, "interval"):
                        secs = trigger.interval.total_seconds()
                        interval = f"{secs / 3600:.0f}h" if secs >= 3600 else f"{secs / 60:.0f}m"
                    elif hasattr(trigger, "fields"):
                        interval = "cron"
                except Exception:
                    pass

                name = job.id.replace("_safe", "").replace("_", " ").title()[:16]
                tbl.add_row(name, next_run, interval)
        except Exception:
            tbl.add_row("error", "-", "-")

        return Panel(tbl, title="Schedule", border_style="cyan", box=box.ROUNDED)

    # ── Event Log ───────────────────────────────────────────────────

    def _render_event_log(self) -> Panel:
        """Real-time event log from Python logging."""
        records = self._log_handler.get_recent(16)

        tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True)
        tbl.add_column("time", style="dim", width=8)
        tbl.add_column("msg")

        level_styles = {
            logging.DEBUG: "dim",
            logging.INFO: "white",
            logging.WARNING: "yellow",
            logging.ERROR: "red",
            logging.CRITICAL: "bold red",
        }

        for ts, level, name, msg in records:
            style = level_styles.get(level, "white")
            prefix = ""
            if "scan" in name.lower() or "scan" in msg.lower():
                prefix = "[bright_blue]SCAN[/] "
            elif "action" in msg.lower() or "acting" in msg.lower() or "comment" in msg.lower():
                prefix = "[bright_green]ACT[/]  "
            elif "learn" in msg.lower():
                prefix = "[bright_magenta]LEARN[/]"
            elif "telegram" in name.lower():
                prefix = "[bright_cyan]TG[/]   "
            elif "error" in msg.lower() or level >= logging.ERROR:
                prefix = "[red]ERR[/]  "
            elif "relationship" in msg.lower() or "dm" in msg.lower():
                prefix = "[bright_red]REL[/]  "
            elif "engage" in msg.lower() or "warm" in msg.lower():
                prefix = "[yellow]ENG[/]  "
            elif "research" in msg.lower() or "intel" in msg.lower():
                prefix = "[magenta]RES[/]  "
            elif "presence" in msg.lower():
                prefix = "[cyan]PRES[/] "

            short_msg = msg
            for strip in ["core.", "platforms.", "dashboard.", "safety."]:
                short_msg = short_msg.replace(strip, "")

            tbl.add_row(ts, Text.from_markup(f"{prefix} [{style}]{short_msg}[/]"))

        if not records:
            tbl.add_row("", Text("Waiting for events...", style="dim italic"))

        return Panel(tbl, title="Live Events", border_style="bright_yellow", box=box.ROUNDED)

    # ── Footer ──────────────────────────────────────────────────────

    def _render_footer(self) -> Panel:
        if self._command_mode:
            cmd_text = Text.assemble(
                (":", "bold green"),
                (self._command_buffer, "white"),
                ("_", "blink white"),
            )
            return Panel(cmd_text, box=box.HEAVY, style="bright_green")

        # View indicator + shortcuts
        view_parts = []
        for i, mode in enumerate(self._VIEW_MODES, 1):
            if mode == self._view_mode:
                view_parts.append((f" {i}:{mode.upper()} ", "bold white on bright_blue"))
            else:
                view_parts.append((f" {i}:{mode} ", "dim"))

        keys = Text.assemble(
            *view_parts,
            ("  TAB", "bold bright_blue"), ("=next ", "dim"),
            ("  s", "bold cyan"), ("=scan ", "dim"),
            ("  a", "bold cyan"), ("=act ", "dim"),
            ("  p", "bold cyan"), ("=pause ", "dim"),
            ("  :", "bold green"), ("=cmd ", "dim"),
            ("  ?", "bold yellow"), ("=help ", "dim"),
            ("  q", "bold cyan"), ("=quit ", "dim"),
        )
        return Panel(keys, box=box.HEAVY, style="bright_blue")

    # ── Main Render ─────────────────────────────────────────────────

    def _render(self) -> Layout:
        if self._show_help:
            return self._render_help_view()

        if self._view_mode == "main":
            return self._render_main_view()
        elif self._view_mode == "accounts":
            return self._render_accounts_detail_view()
        elif self._view_mode == "convos":
            return self._render_convos_view()
        elif self._view_mode == "opps":
            return self._render_opps_view()
        return self._render_main_view()

    def _render_main_view(self) -> Layout:
        """Original main dashboard view."""
        layout = self._build_layout()
        layout["header"].update(self._render_header())
        layout["reddit_map"].update(self._render_reddit_map())
        layout["twitter_map"].update(self._render_twitter_map())
        layout["accounts_overview"].update(self._render_accounts_overview())
        layout["event_log"].update(self._render_event_log())
        layout["brain"].update(self._render_brain())
        layout["performance"].update(self._render_performance())
        layout["schedule"].update(self._render_schedule())
        layout["footer"].update(self._render_footer())
        return layout

    # ── Accounts Detail View ─────────────────────────────────────

    def _render_accounts_detail_view(self) -> Layout:
        """Detailed per-account view."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["header"].update(self._render_header())
        layout["footer"].update(self._render_footer())

        tbl = Table(
            box=box.SIMPLE_HEAVY, padding=(0, 1), expand=True,
            title="Account Details (24h)",
        )
        tbl.add_column("Account", style="bold", width=18)
        tbl.add_column("Plat", width=4)
        tbl.add_column("Status", width=9)
        tbl.add_column("Coms", width=5, justify="right", style="bright_green")
        tbl.add_column("Likes", width=5, justify="right", style="bright_yellow")
        tbl.add_column("Posts", width=5, justify="right", style="bright_cyan")
        tbl.add_column("Seeds", width=5, justify="right", style="magenta")
        tbl.add_column("Total", width=5, justify="right", style="bold")
        tbl.add_column("Health", width=10)
        tbl.add_column("Persona", width=14, style="dim")
        tbl.add_column("Cookie", width=6, justify="center")

        acc_stats = self._get_account_stats()
        for key in sorted(acc_stats.keys()):
            s = acc_stats[key]
            types = s["types"]
            plat = "Reddit" if s["platform"] == "reddit" else "X"

            comments = types.get("comment", 0) + types.get("reply", 0)
            likes = types.get("upvote", 0) + types.get("like", 0)
            posts = types.get("post", 0) + types.get("tweet", 0)
            seeds = types.get("seed", 0)

            status = s["status"]
            status_style = {"healthy": "green", "cooldown": "yellow",
                           "warned": "red", "banned": "bright_red"}.get(status, "dim")
            hp_bar = {"healthy": "█████", "cooldown": "███░░",
                      "warned": "██░░░", "banned": "░░░░░"}.get(status, "?????")

            # Get persona from config
            persona = ""
            try:
                accs = self.orch.account_mgr.load_accounts(s["platform"])
                for acc in accs:
                    if acc["username"] == s["username"]:
                        persona = acc.get("persona", "")[:14]
                        break
            except Exception:
                pass

            cookie_status = Text("OK", style="green") if s.get("cookies") else Text("NO", style="red")

            tbl.add_row(
                s["username"][:18], plat,
                Text(status.upper(), style=status_style),
                str(comments), str(likes), str(posts), str(seeds),
                str(s["total"]),
                Text(hp_bar, style=status_style),
                persona,
                cookie_status,
            )

        if not acc_stats:
            tbl.add_row(*["(loading)"] + [""] * 10)

        layout["body"].update(Panel(tbl, border_style="yellow", box=box.ROUNDED))
        return layout

    # ── Conversations View ───────────────────────────────────────

    def _render_convos_view(self) -> Layout:
        """DM conversations + Telegram alert log."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="dms", ratio=1),
            Layout(name="telegram", ratio=1),
        )
        layout["header"].update(self._render_header())
        layout["footer"].update(self._render_footer())

        # Left: DM Conversations
        dm_tbl = Table(box=None, show_header=True, padding=(0, 1), expand=True)
        dm_tbl.add_column("Time", style="dim", width=8)
        dm_tbl.add_column("Dir", width=4)
        dm_tbl.add_column("User", style="bold", width=16)
        dm_tbl.add_column("Platform", width=6)
        dm_tbl.add_column("Message", ratio=1)

        try:
            convos = self.orch.db.conn.execute(
                """SELECT c.timestamp, c.direction, r.username, r.platform, c.content
                   FROM conversations c
                   JOIN relationships r ON c.relationship_id = r.id
                   ORDER BY c.timestamp DESC LIMIT 20"""
            ).fetchall()

            for row in convos:
                ts = _ago(row["timestamp"]) if row["timestamp"] else "?"
                direction = Text(">>", style="bright_green") if row["direction"] == "sent" else Text("<<", style="bright_cyan")
                content = (row["content"] or "")[:60]
                if len(row["content"] or "") > 60:
                    content += "..."
                dm_tbl.add_row(
                    ts, direction, row["username"][:16],
                    row["platform"][:6], content,
                )
        except Exception:
            pass

        if not dm_tbl.rows:
            dm_tbl.add_row("", "", "", "", Text("No conversations yet", style="dim italic"))

        layout["dms"].update(
            Panel(dm_tbl, title="DM Conversations", border_style="bright_red", box=box.ROUNDED)
        )

        # Right: Telegram Alerts
        tg_tbl = Table(box=None, show_header=True, padding=(0, 1), expand=True)
        tg_tbl.add_column("Time", style="dim", width=8)
        tg_tbl.add_column("Message", ratio=1)

        try:
            alerts = list(self.orch._alert_log)[-25:]
            for ts_iso, msg in reversed(alerts):
                ts = _ago(ts_iso)
                short_msg = msg[:80]
                if len(msg) > 80:
                    short_msg += "..."
                tg_tbl.add_row(ts, short_msg)
        except Exception:
            pass

        if not tg_tbl.rows:
            tg_tbl.add_row("", Text("No Telegram alerts yet", style="dim italic"))

        layout["telegram"].update(
            Panel(tg_tbl, title="Telegram Alerts", border_style="bright_cyan", box=box.ROUNDED)
        )

        return layout

    # ── Opportunities View ───────────────────────────────────────

    def _get_opportunities(self) -> list:
        """Cached pending opportunities (refresh every 10s)."""
        now = time.time()
        if now - self._opps_ts > 10:
            try:
                self._opps_cache = self.orch.db.conn.execute(
                    """SELECT platform, subreddit_or_query, title, relevance_score,
                              timestamp, status
                       FROM opportunities
                       WHERE status = 'pending'
                       ORDER BY relevance_score DESC
                       LIMIT 30"""
                ).fetchall()
            except Exception:
                self._opps_cache = []
            self._opps_ts = now
        return self._opps_cache

    def _render_opps_view(self) -> Layout:
        """Pending opportunities with scores."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["header"].update(self._render_header())
        layout["footer"].update(self._render_footer())

        tbl = Table(
            box=box.SIMPLE_HEAVY, padding=(0, 1), expand=True,
            title="Pending Opportunities",
        )
        tbl.add_column("Plat", width=6)
        tbl.add_column("Subreddit/KW", style="bold", width=18)
        tbl.add_column("Title", ratio=1)
        tbl.add_column("Score", width=6, justify="right")
        tbl.add_column("Age", width=6, justify="right")
        tbl.add_column("Status", width=8)

        opps = self._get_opportunities()
        for opp in opps:
            plat = opp["platform"][:6]
            sub = (opp["subreddit_or_query"] or "")[:18]
            title = (opp["title"] or "")[:50]
            if len(opp["title"] or "") > 50:
                title += "..."

            score = opp["relevance_score"] or 0
            if score >= 7:
                score_style = "bold bright_green"
            elif score >= 5:
                score_style = "yellow"
            else:
                score_style = "dim"

            age = _ago(opp["timestamp"]) if opp["timestamp"] else "?"
            status = opp["status"] or "pending"

            tbl.add_row(
                plat, sub, title,
                Text(f"{score:.1f}", style=score_style),
                age, status,
            )

        if not opps:
            tbl.add_row("", "", Text("No pending opportunities", style="dim italic"), "", "", "")

        layout["body"].update(Panel(tbl, border_style="bright_green", box=box.ROUNDED))
        return layout

    # ── Help Overlay ─────────────────────────────────────────────

    def _render_help_view(self) -> Layout:
        """Full-screen help overlay."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["header"].update(self._render_header())
        layout["footer"].update(Panel(
            Text.assemble(("  Press ", "dim"), ("?", "bold yellow"), (" to close help", "dim")),
            box=box.HEAVY, style="bright_yellow",
        ))

        help_grid = Table.grid(padding=(0, 2))
        help_grid.add_column(style="bold cyan", width=16)
        help_grid.add_column()

        help_grid.add_row("", Text("KEYBOARD SHORTCUTS", style="bold underline"))
        help_grid.add_row("", "")
        help_grid.add_row("TAB", "Cycle through views (Main > Accounts > Convos > Opps)")
        help_grid.add_row("1 / 2 / 3 / 4", "Jump to Main / Accounts / Conversations / Opportunities")
        help_grid.add_row(":", "Enter command mode (vim-style)")
        help_grid.add_row("?", "Toggle this help screen")
        help_grid.add_row("", "")
        help_grid.add_row("s", "Trigger manual scan")
        help_grid.add_row("a", "Trigger manual action")
        help_grid.add_row("l", "Trigger learning cycle")
        help_grid.add_row("e", "Trigger engagement cycle")
        help_grid.add_row("r", "Trigger research cycle")
        help_grid.add_row("d", "Trigger DM/relationship cycle")
        help_grid.add_row("p", "Pause / Resume bot")
        help_grid.add_row("q", "Quit dashboard")
        help_grid.add_row("", "")
        help_grid.add_row("", Text("COMMANDS (type : then command)", style="bold underline"))
        help_grid.add_row("", "")
        help_grid.add_row(":scan", "Trigger scan")
        help_grid.add_row(":act", "Trigger action")
        help_grid.add_row(":learn", "Trigger learning")
        help_grid.add_row(":engage", "Trigger engagement")
        help_grid.add_row(":research", "Trigger research")
        help_grid.add_row(":dm", "Trigger DM cycle")
        help_grid.add_row(":pause / :resume", "Pause or resume bot")
        help_grid.add_row(":reload", "Hot-reload account configs")
        help_grid.add_row(":view <name>", "Switch view (main/accounts/convos/opps)")
        help_grid.add_row(":quit", "Quit dashboard")

        layout["body"].update(Panel(help_grid, title="Help", border_style="bright_yellow", box=box.DOUBLE))
        return layout

    # ── Keyboard ────────────────────────────────────────────────────

    def _flash(self, msg: str, duration: float = 3.0):
        self._flash_msg = msg
        self._flash_until = time.time() + duration

    def _handle_key(self, key: str):
        # Command mode: accumulate input
        if self._command_mode:
            if key == "\n" or key == "\r":
                self._exec_command(self._command_buffer.strip())
                self._command_buffer = ""
                self._command_mode = False
            elif key == "\x1b":  # Escape
                self._command_buffer = ""
                self._command_mode = False
            elif key == "\x7f" or key == "\x08":  # Backspace
                self._command_buffer = self._command_buffer[:-1]
            else:
                self._command_buffer += key
            return

        # Normal mode shortcuts
        if key == ":":
            self._command_mode = True
            self._command_buffer = ""
        elif key == "\t":
            idx = self._VIEW_MODES.index(self._view_mode) if self._view_mode in self._VIEW_MODES else 0
            self._view_mode = self._VIEW_MODES[(idx + 1) % len(self._VIEW_MODES)]
            self._flash(f"View: {self._view_mode.upper()}")
        elif key == "?":
            self._show_help = not self._show_help
        elif key == "q":
            self._running = False
        elif key == "p":
            self.orch._paused = not self.orch._paused
            state = "PAUSED" if self.orch._paused else "RESUMED"
            self._flash(state)
            logger.info(f"Milo {state} via TUI")
        elif key == "s":
            threading.Thread(target=self.orch._scan_all_safe, daemon=True).start()
            self._flash("Scan started")
            logger.info("Manual scan triggered via TUI")
        elif key == "a":
            threading.Thread(target=self.orch._act_on_best_safe, daemon=True).start()
            self._flash("Action started")
            logger.info("Manual action triggered via TUI")
        elif key == "l":
            threading.Thread(target=self.orch._learn, daemon=True).start()
            self._flash("Learning started")
            logger.info("Manual learn triggered via TUI")
        elif key == "e":
            threading.Thread(target=self.orch._engage_safe, daemon=True).start()
            self._flash("Engagement started")
            logger.info("Manual engagement triggered via TUI")
        elif key == "r":
            threading.Thread(target=self.orch._research_safe, daemon=True).start()
            self._flash("Research started")
            logger.info("Manual research triggered via TUI")
        elif key == "d":
            threading.Thread(target=self.orch._build_relationships_safe, daemon=True).start()
            self._flash("DM cycle started")
            logger.info("Manual DM/relationship cycle triggered via TUI")
        elif key == "1":
            self._view_mode = "main"
            self._flash("View: MAIN")
        elif key == "2":
            self._view_mode = "accounts"
            self._flash("View: ACCOUNTS")
        elif key == "3":
            self._view_mode = "convos"
            self._flash("View: CONVERSATIONS")
        elif key == "4":
            self._view_mode = "opps"
            self._flash("View: OPPORTUNITIES")

    def _exec_command(self, cmd: str):
        """Execute a :command."""
        cmd_lower = cmd.lower().strip()
        if not cmd_lower:
            return

        cmd_map = {
            "scan": ("Scan started", lambda: threading.Thread(target=self.orch._scan_all_safe, daemon=True).start()),
            "act": ("Action started", lambda: threading.Thread(target=self.orch._act_on_best_safe, daemon=True).start()),
            "learn": ("Learning started", lambda: threading.Thread(target=self.orch._learn, daemon=True).start()),
            "engage": ("Engagement started", lambda: threading.Thread(target=self.orch._engage_safe, daemon=True).start()),
            "research": ("Research started", lambda: threading.Thread(target=self.orch._research_safe, daemon=True).start()),
            "dm": ("DM cycle started", lambda: threading.Thread(target=self.orch._build_relationships_safe, daemon=True).start()),
            "pause": ("PAUSED", lambda: setattr(self.orch, "_paused", True)),
            "resume": ("RESUMED", lambda: setattr(self.orch, "_paused", False)),
            "quit": ("Quitting...", lambda: setattr(self, "_running", False)),
            "q": ("Quitting...", lambda: setattr(self, "_running", False)),
        }

        if cmd_lower in cmd_map:
            msg, action = cmd_map[cmd_lower]
            action()
            self._flash(msg)
            logger.info(f"Command :{cmd_lower} executed via TUI")
        elif cmd_lower.startswith("view "):
            view_name = cmd_lower[5:].strip()
            view_aliases = {"main": "main", "accounts": "accounts", "acc": "accounts",
                           "convos": "convos", "conversations": "convos", "conv": "convos",
                           "opps": "opps", "opportunities": "opps", "opp": "opps"}
            if view_name in view_aliases:
                self._view_mode = view_aliases[view_name]
                self._flash(f"View: {self._view_mode.upper()}")
            else:
                self._flash(f"Unknown view: {view_name}")
        elif cmd_lower == "reload":
            self.orch.account_mgr.reload()
            self._flash("Accounts reloaded")
            logger.info("Manual account reload via TUI")
        elif cmd_lower == "help":
            self._show_help = not self._show_help
        else:
            self._flash(f"Unknown: :{cmd_lower}")

    def _key_listener(self):
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
        except (termios.error, ValueError, OSError):
            logger.debug("No terminal - keyboard shortcuts disabled")
            return

        try:
            tty.setcbreak(fd)
            while self._running:
                if select.select([sys.stdin], [], [], 0.3)[0]:
                    ch = sys.stdin.read(1)
                    if ch:
                        # In command mode, pass raw char; otherwise lowercase
                        if self._command_mode:
                            self._handle_key(ch)
                        else:
                            self._handle_key(ch.lower() if ch.isprintable() else ch)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    # ── Main Loop ───────────────────────────────────────────────────

    def run(self):
        self._running = True

        # Suppress noisy loggers BEFORE starting Live screen
        self._suppress_noisy_loggers()

        self._key_thread = threading.Thread(target=self._key_listener, daemon=True)
        self._key_thread.start()

        # VSCode terminal doesn't support alternate screen buffer properly.
        # Use screen=False in VSCode so the TUI renders inline (scrolling mode).
        use_alt_screen = not self._is_vscode

        try:
            with Live(
                self._render(),
                console=self.console,
                refresh_per_second=self.REFRESH_HZ,
                screen=use_alt_screen,
            ) as live:
                while self._running:
                    time.sleep(1.0 / self.REFRESH_HZ)
                    try:
                        live.update(self._render())
                    except Exception:
                        pass  # render errors silently ignored
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            logger.error(f"TUI crashed: {exc}", exc_info=True)
            return
        finally:
            self._running = False
            self._cleanup_log_handler()
            logger.info("TUI dashboard stopped")
