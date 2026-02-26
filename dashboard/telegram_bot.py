"""Telegram dashboard bot for monitoring and control."""

import asyncio
import logging
import random
from datetime import datetime
from typing import Dict, List, Optional

import requests as http_requests
import yaml
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from core.database import Database

logger = logging.getLogger(__name__)

# ── Human-like message variations ──────────────────────────────────

_SCAN_DONE_MSGS = [
    "Finished scanning — found {n} opportunities worth looking at",
    "Just scanned everything. {n} leads in the pipeline",
    "Scan done. {n} potential targets queued up",
    "Wrapped up the scan. Sitting on {n} opportunities right now",
]

_ACTION_DONE_MSGS = [
    "Dropped a comment on r/{sub} — \"{title}\"",
    "Just replied in r/{sub}: \"{title}\"",
    "Posted in r/{sub} on the thread \"{title}\"",
    "Left a comment in r/{sub} about \"{title}\"",
]

_TWITTER_ACTION_MSGS = [
    "Replied to a tweet about \"{title}\"",
    "Just dropped a reply on Twitter — \"{title}\"",
    "Engaged on Twitter: \"{title}\"",
]

_ENGAGE_REDDIT_MSGS = [
    "Did some Reddit housekeeping for {proj} — subscribed to {sub} subs, upvoted {up} posts, saved {sv}",
    "Warmed up the Reddit account for {proj}: {sub} subs, {up} upvotes, {sv} saves",
    "Reddit engagement round for {proj}: +{sub} subs, +{up} upvotes, +{sv} bookmarks",
]

_ENGAGE_TWITTER_MSGS = [
    "Twitter warmup for {proj}: liked {lk}, followed {fl}, retweeted {rt}",
    "Did a round of Twitter engagement for {proj} — {lk} likes, {fl} follows, {rt} RTs",
    "Warmed up Twitter for {proj}: {lk} likes, {fl} new follows, {rt} retweets",
]

_LEARN_MSGS = [
    "Finished learning. Best performing subs: {subs}\nCurrent promo ratio: {ratio}\nNew discoveries: {disc}",
    "Learning cycle done. Top subs right now: {subs}\nPromo/organic split: {ratio}\n{disc} new targets found",
    "Updated my strategy. {subs} are the top performers\nRunning at {ratio} promo ratio\nDiscovered {disc} new leads",
]

_SEED_MSGS = [
    "Planted a seed post in r/{sub}\n{url}",
    "Created a discussion post in r/{sub}\n{url}",
    "Dropped a new thread in r/{sub} to build presence\n{url}",
]

_COMMENT_CHECK_MSGS = [
    "Checked {n} recent comments — {r} got removed by Reddit",
    "Comment audit: {n} verified, {r} taken down",
    "Reviewed {n} comments. {r} were removed (adjusting strategy)",
]

_SHADOWBAN_MSGS = [
    "Heads up — u/{user} might be shadowbanned. Signs: {signs}",
    "Warning: possible shadowban on u/{user}. Detected: {signs}",
    "u/{user} looks suspicious — Reddit might have flagged it. Indicators: {signs}",
]

_YOUTUBE_SHARE_MSGS = [
    "Shared a relevant video in r/{sub}: {title}",
    "Posted a YouTube find in r/{sub} — {title}",
    "Dropped a video in r/{sub}: {title}",
]

_NEWS_SHARE_MSGS = [
    "Shared a trending article in r/{sub}: {title}",
    "Posted a news piece to r/{sub} — {title}",
]

_INTEL_MSGS = [
    "Subreddit intel done — found {n} high-opportunity communities",
    "Analyzed {total} subreddits, {n} scored above 7/10",
    "Intel cycle complete: {total} subs scanned, {n} promising leads",
]

_PRESENCE_MSGS = [
    "Maintained presence in {n} subreddits (light engagement)",
    "Kept active in {n} communities — upvotes, saves, light touch",
    "Community warmup: engaged in {n} neglected subreddits",
]

_RESEARCH_MSGS = [
    "Research cycle done — updated knowledge base with fresh context",
    "Finished research: trends and news added to the knowledge base",
    "Knowledge updated — ready to reference current trends in comments",
]

def _pick(templates: list, **kwargs) -> str:
    """Pick a random template and format it."""
    return random.choice(templates).format(**kwargs)


class TelegramDashboard:
    """Telegram bot for monitoring and controlling Milo.

    Human-like notifications + full remote control.
    """

    def __init__(self, config: Dict, db: Database):
        self.config = config
        self.db = db
        self.admin_ids = config.get("admin_chat_ids", [])
        self.app: Optional[Application] = None
        self.paused = False
        self._account_manager = None
        self._orchestrator = None

    def set_account_manager(self, account_manager):
        self._account_manager = account_manager

    def set_orchestrator(self, orchestrator):
        self._orchestrator = orchestrator

    def build(self):
        token = self.config.get("bot_token", "")
        if not token or token.startswith("YOUR_"):
            logger.warning("Telegram bot token not configured")
            return

        self.app = Application.builder().token(token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_help))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("report", self._cmd_report))
        self.app.add_handler(CommandHandler("insights", self._cmd_insights))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("health", self._cmd_health))
        self.app.add_handler(CommandHandler("last", self._cmd_last))
        self.app.add_handler(CommandHandler("scan", self._cmd_scan))
        self.app.add_handler(CommandHandler("post", self._cmd_post))
        self.app.add_handler(CommandHandler("projects", self._cmd_projects))
        self.app.add_handler(CommandHandler("learn", self._cmd_learn))
        self.app.add_handler(CommandHandler("messages", self._cmd_messages))
        self.app.add_handler(CommandHandler("intel", self._cmd_intel))
        self.app.add_handler(CommandHandler("presence", self._cmd_presence))
        self.app.add_handler(CommandHandler("research", self._cmd_research))
        self.app.add_handler(CommandHandler("friends", self._cmd_friends))
        self.app.add_handler(CommandHandler("conversations", self._cmd_conversations))
        self.app.add_handler(CommandHandler("accounts", self._cmd_accounts))
        self.app.add_handler(CommandHandler("addreddit", self._cmd_add_reddit))
        self.app.add_handler(CommandHandler("addtwitter", self._cmd_add_twitter))
        self.app.add_handler(CommandHandler("removeaccount", self._cmd_remove_account))
        self.app.add_handler(CommandHandler("llm", self._cmd_llm))
        self.app.add_handler(CommandHandler("hubs", self._cmd_hubs))
        self.app.add_handler(CommandHandler("performance", self._cmd_performance))
        self.app.add_handler(CommandHandler("debug", self._cmd_debug))

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    # ── Command Handlers ─────────────────────────────────────────────

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        text = (
            "Hey! Here's what I can do:\n\n"
            "-- Check on me --\n"
            "/status — How am I doing right now\n"
            "/stats — What I've done in the last 24h\n"
            "/report — Full daily breakdown\n"
            "/last N — My last N actions (full text)\n"
            "/messages N — All messages posted in last N hours\n"
            "/health — Are my accounts OK\n"
            "/insights — What I've learned so far\n"
            "/projects — Which projects I'm working on\n\n"
            "-- Intelligence --\n"
            "/intel — Subreddit opportunity analysis\n"
            "/presence — Community presence & trust stages\n"
            "/research — What's trending right now\n"
            "/friends — Relationship stats\n"
            "/conversations — Recent DM conversations\n"
            "/hubs — Owned subreddit hubs status\n"
            "/performance — Performance score & improvements\n"
            "/llm — Dual-LLM stats (Groq + Ollama)\n\n"
            "-- Accounts --\n"
            "/accounts — List all accounts\n"
            "/addreddit user pass — Add Reddit account\n"
            "/addtwitter user email pass — Add X account\n"
            "/removeaccount platform user — Disable account\n\n"
            "-- Debugging --\n"
            "/debug — Why am I not acting? Show recent skipped decisions\n\n"
            "-- Tell me what to do --\n"
            "/scan — Go scan for opportunities now\n"
            "/post — Post something right now\n"
            "/learn — Analyze my performance and adapt\n"
            "/pause — Take a break\n"
            "/resume — Get back to work"
        )
        await update.message.reply_text(text)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        paused_str = "Taking a break" if self.paused else "Working"
        stats = self.db.get_stats_summary(hours=24)
        total_actions = sum(
            sum(t.values()) for t in stats.get("actions", {}).values()
        )
        avg_score = stats.get("avg_opportunity_score", 0)

        text = f"I'm currently: {paused_str}\n"
        text += f"Actions today: {total_actions}\n"

        if avg_score:
            text += f"Avg opportunity quality: {avg_score}/10\n"

        for platform, types in stats.get("actions", {}).items():
            counts = ", ".join(f"{v} {k}s" for k, v in types.items())
            emoji = "Reddit" if platform == "reddit" else "Twitter"
            text += f"\n{emoji}: {counts}"

        if not stats.get("actions"):
            text += "\nNothing done yet today. Still warming up."

        await update.message.reply_text(text)

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        stats = self.db.get_stats_summary(hours=24)

        text = "Here's my last 24 hours:\n\n"

        actions = stats.get("actions", {})
        if actions:
            for platform, types in actions.items():
                name = "Reddit" if platform == "reddit" else "Twitter"
                items = ", ".join(f"{v} {k}s" for k, v in types.items())
                text += f"{name}: {items}\n"
        else:
            text += "No actions yet — I'm getting started.\n"

        opps = stats.get("opportunities", {})
        if opps:
            total = sum(opps.values())
            pending = opps.get("pending", 0)
            text += f"\nOpportunities: {total} total, {pending} still pending"

        text += "\n\nCost: $0.00 (running free)"

        await update.message.reply_text(text)

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        report = self._generate_daily_report()
        await update.message.reply_text(report)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        self.paused = True
        responses = [
            "Alright, taking a break. Hit /resume when you need me.",
            "Paused. I'll be here when you're ready.",
            "Got it, going quiet. /resume to get me back.",
        ]
        await update.message.reply_text(random.choice(responses))
        logger.info("Bot paused via Telegram")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        self.paused = False
        responses = [
            "Back at it. Let's go.",
            "Resuming operations. I'll keep you posted.",
            "On it. I'll send updates as things happen.",
        ]
        await update.message.reply_text(random.choice(responses))
        logger.info("Bot resumed via Telegram")

    async def _cmd_insights(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        try:
            from core.learning_engine import LearningEngine
            engine = LearningEngine(self.db)
            insights = engine.get_insights()

            text = "Here's what I've figured out so far:\n\n"

            top_subs = insights.get("top_subreddits", [])
            if top_subs:
                text += "Best subreddits for us:\n"
                for i, s in enumerate(top_subs[:5], 1):
                    bar = "=" * max(1, int(s["score"] * 3))
                    text += f"  {i}. r/{s['name']} [{bar}] ({s['samples']} posts)\n"
            else:
                text += "Haven't collected enough data yet to rank subreddits.\n"

            top_kw = insights.get("top_keywords", [])
            if top_kw:
                text += "\nKeywords that work:\n"
                for k in top_kw[:5]:
                    text += f"  - \"{k['name']}\" (score: {k['score']:.1f})\n"

            tone = insights.get("best_tone", "helpful_casual")
            text += f"\nBest tone so far: {tone}"
            ratio = insights.get("optimal_promo_ratio", 0.2)
            text += f"\nSweet spot: {ratio:.0%} promo / {1-ratio:.0%} organic"

            # Post-type performance
            pt_stats = self.db.get_post_type_stats(days=30)
            if pt_stats:
                text += "\n\nPost types that work best:"
                for pt in pt_stats[:5]:
                    removal = (pt["removed_count"] or 0) / max(pt["count"], 1)
                    text += (
                        f"\n  {pt['post_type']}: "
                        f"eng={pt['avg_engagement']:.2f} "
                        f"(n={pt['count']}, {removal:.0%} removed)"
                    )

            # Sentiment from replies
            sentiment = self.db.get_sentiment_by_tone(days=30)
            if sentiment:
                text += "\n\nReply sentiment by tone:"
                for s in sentiment:
                    score = s["avg_sentiment"]
                    icon = "👍" if score > 0.1 else ("👎" if score < -0.1 else "➖")
                    text += (
                        f"\n  {icon} {s['tone_style']}: {score:+.2f} "
                        f"({s['total_replies']} replies)"
                    )

            # A/B experiments
            from core.ab_testing import ABTestingEngine
            ab = ABTestingEngine(self.db)
            experiments = ab.get_active_experiments()
            if experiments:
                text += "\n\nA/B experiments running:"
                for exp in experiments:
                    results = self.db.get_experiment_results(exp["id"])
                    a_d = results.get("a", {})
                    b_d = results.get("b", {})
                    text += (
                        f"\n  {exp['variable']}: "
                        f"{exp['variant_a']}({a_d.get('avg_eng', 0):.1f}) vs "
                        f"{exp['variant_b']}({b_d.get('avg_eng', 0):.1f})"
                    )

            # Evolved prompts
            try:
                evos = self.db.conn.execute(
                    """SELECT template_name, status FROM prompt_evolution_log
                       WHERE status='active' ORDER BY timestamp DESC LIMIT 3"""
                ).fetchall()
                if evos:
                    names = ", ".join(e["template_name"] for e in evos)
                    text += f"\n\nEvolved prompts: {names}"
            except Exception:
                pass

            pending = insights.get("pending_discoveries", 0)
            if pending:
                text += f"\n\nI also found {pending} new potential targets to explore"

        except Exception as e:
            text = f"Couldn't load insights right now: {e}"

        await update.message.reply_text(text)

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        text = "Account status:\n\n"

        if self._account_manager:
            health = self._account_manager.get_all_health()
            for acc in health:
                status_word = {
                    "healthy": "Good",
                    "cooldown": "Cooling down",
                    "warned": "Flagged",
                    "banned": "BANNED",
                }.get(acc["status"], "Unknown")
                platform = "Reddit" if acc["platform"] == "reddit" else "Twitter"
                text += (
                    f"{platform} @{acc['username']}: {status_word} "
                    f"({acc['actions_24h']} actions today)\n"
                )
                if acc.get("cooldown_until"):
                    text += f"  Back online: {acc['cooldown_until'][:16]}\n"
        else:
            text += "Can't check right now."

        await update.message.reply_text(text)

    async def _cmd_last(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show last N actions with FULL content — each as a separate message."""
        if not self._is_admin(update.effective_user.id):
            return

        n = 5
        if context.args:
            try:
                n = min(int(context.args[0]), 20)
            except ValueError:
                pass

        recent = self.db.get_recent_actions(hours=48, limit=n)

        if not recent:
            await update.message.reply_text("Nothing yet. I haven't taken any actions.")
            return

        await update.message.reply_text(f"Last {len(recent)} actions:")

        for action in recent:
            ts = action["timestamp"][11:16]
            date = action["timestamp"][:10]
            ok = "OK" if action["success"] else "FAILED"
            platform = "Reddit" if action["platform"] == "reddit" else "X"

            # Parse metadata for subreddit/URL
            meta = {}
            metadata_raw = action.get("metadata", "")
            if isinstance(metadata_raw, str) and metadata_raw:
                try:
                    import json
                    meta = json.loads(metadata_raw)
                except Exception:
                    pass

            sub = meta.get("subreddit", "")
            url = meta.get("comment_url", meta.get("post_url", ""))

            header = f"[{date} {ts}] {platform} {action['action_type']} ({ok})"
            header += f"\nAccount: @{action['account']} | Project: {action['project']}"
            if sub:
                header += f" | r/{sub}"
            if url:
                header += f"\n{url}"

            # Full content
            content = action.get("content", "")
            if content:
                msg = f"{header}\n\n{content}"
            else:
                msg = header

            # Telegram max message = 4096 chars
            if len(msg) > 4096:
                msg = msg[:4090] + "\n[...]"

            await update.message.reply_text(msg)

    async def _cmd_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all Reddit+X messages posted, grouped by platform."""
        if not self._is_admin(update.effective_user.id):
            return

        hours = 24
        if context.args:
            try:
                hours = min(int(context.args[0]), 168)  # Max 7 days
            except ValueError:
                pass

        recent = self.db.get_recent_actions(hours=hours, limit=50)

        if not recent:
            await update.message.reply_text(f"No actions in the last {hours}h.")
            return

        # Group by platform
        reddit_actions = [a for a in recent if a["platform"] == "reddit"]
        twitter_actions = [a for a in recent if a["platform"] == "twitter"]

        summary = (
            f"All interactions — last {hours}h\n"
            f"Reddit: {len(reddit_actions)} | X: {len(twitter_actions)}\n"
            f"Sending each one below..."
        )
        await update.message.reply_text(summary)

        for action in recent:
            ts = action["timestamp"][11:16]
            date = action["timestamp"][:10]
            ok = "OK" if action["success"] else "FAIL"
            plat = "Reddit" if action["platform"] == "reddit" else "X"

            meta = {}
            metadata_raw = action.get("metadata", "")
            if isinstance(metadata_raw, str) and metadata_raw:
                try:
                    import json
                    meta = json.loads(metadata_raw)
                except Exception:
                    pass

            sub = meta.get("subreddit", "")
            url = meta.get("comment_url", meta.get("post_url", ""))

            header = f"{plat} | {date} {ts} | {action['action_type']} | {ok}"
            if sub:
                header += f" | r/{sub}"
            header += f"\n@{action['account']} for {action['project']}"
            if url:
                header += f"\n{url}"

            content = action.get("content", "(no content)")
            msg = f"{header}\n{'—' * 30}\n{content}"

            if len(msg) > 4096:
                msg = msg[:4090] + "\n[...]"

            await update.message.reply_text(msg)


    # ── Remote Control ───────────────────────────────────────────────

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        if not self._orchestrator:
            await update.message.reply_text("I'm not connected to the main engine right now.")
            return

        await update.message.reply_text("On it — scanning all platforms now. I'll let you know what I find.")
        try:
            import threading
            t = threading.Thread(target=self._orchestrator._scan_all_safe, daemon=True)
            t.start()
        except Exception as e:
            await update.message.reply_text(f"Something went wrong: {e}")

    async def _cmd_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        if not self._orchestrator:
            await update.message.reply_text("Not connected to the engine.")
            return
        if self.paused:
            await update.message.reply_text("I'm paused right now. /resume me first.")
            return

        await update.message.reply_text("Looking for the best opportunity to act on...")
        try:
            import threading
            t = threading.Thread(target=self._orchestrator._act_on_best_safe, daemon=True)
            t.start()
        except Exception as e:
            await update.message.reply_text(f"Couldn't do it: {e}")

    async def _cmd_projects(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        if self._orchestrator:
            projects = self._orchestrator.projects
        else:
            from pathlib import Path
            import yaml as _yaml
            projects = []
            for f in Path("projects/").glob("*.yaml"):
                try:
                    with open(f) as fh:
                        data = _yaml.safe_load(fh)
                    if data and data.get("project", {}).get("enabled", True):
                        projects.append(data)
                except Exception:
                    pass

        if not projects:
            await update.message.reply_text("No projects set up yet.")
            return

        text = f"I'm working on {len(projects)} project(s):\n\n"
        for p in projects:
            proj = p.get("project", {})
            name = proj.get("name", "?")
            url = proj.get("url", "")
            weight = proj.get("weight", 1.0)
            desc = proj.get("description", "")[:80]
            text += f"  {name}\n  {url}\n  Priority: {weight} | {desc}\n\n"

        await update.message.reply_text(text)

    async def _cmd_learn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        if not self._orchestrator:
            await update.message.reply_text("Not connected.")
            return

        await update.message.reply_text("Analyzing my performance and adapting... Give me a sec.")
        try:
            import threading
            t = threading.Thread(target=self._orchestrator._learn, daemon=True)
            t.start()
        except Exception as e:
            await update.message.reply_text(f"Learning failed: {e}")

    # ── Intelligence Commands ─────────────────────────────────────────

    async def _cmd_intel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        try:
            text = "Subreddit Intelligence\n\n"
            if self._orchestrator:
                for proj in self._orchestrator.projects:
                    proj_name = proj.get("project", {}).get("name", "unknown")
                    top = self._orchestrator.subreddit_intel.get_top_opportunities(
                        proj_name, limit=10
                    )
                    if top:
                        text += f"--- {proj_name} ---\n"
                        for i, s in enumerate(top, 1):
                            score = s.get("opportunity_score", 0)
                            subs = s.get("subscribers", 0)
                            bar = "=" * max(1, int(score))
                            text += (
                                f"  {i}. r/{s['subreddit']} [{bar}] "
                                f"{score:.1f}/10 ({subs:,} members)\n"
                            )
                        text += "\n"
                    else:
                        text += f"--- {proj_name} ---\nNo intel yet. Run /scan first.\n\n"
            else:
                text += "Not connected to the engine."
        except Exception as e:
            text = f"Couldn't load intel: {e}"

        await update.message.reply_text(text)

    async def _cmd_presence(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        try:
            text = "Community Presence\n\n"
            if self._orchestrator:
                for proj in self._orchestrator.projects:
                    proj_name = proj.get("project", {}).get("name", "unknown")
                    presences = self.db.get_community_presence(proj_name)
                    if presences:
                        text += f"--- {proj_name} ---\n"
                        for p in presences[:15]:
                            stage = p.get("stage", "new")
                            warmth = p.get("warmth_score", 0)
                            comments = p.get("total_comments", 0)
                            stage_icon = {
                                "new": "[NEW]",
                                "warming": "[WARM]",
                                "established": "[EST]",
                                "trusted": "[TRUST]",
                            }.get(stage, "[?]")
                            text += (
                                f"  r/{p['subreddit']} {stage_icon} "
                                f"warmth={warmth:.1f} "
                                f"({comments} comments)\n"
                            )
                        text += "\n"
                    else:
                        text += f"--- {proj_name} ---\nNo presence data yet.\n\n"
            else:
                text += "Not connected to the engine."
        except Exception as e:
            text = f"Couldn't load presence: {e}"

        await update.message.reply_text(text)

    async def _cmd_research(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return

        try:
            text = "Research & Trends\n\n"
            if self._orchestrator:
                for proj in self._orchestrator.projects:
                    proj_name = proj.get("project", {}).get("name", "unknown")

                    # Knowledge base entries
                    knowledge = self.db.get_knowledge(proj_name, limit=10)
                    if knowledge:
                        text += f"--- {proj_name} ---\n"
                        for k in knowledge:
                            cat = k.get("category", "?")
                            topic = k.get("topic", "")
                            used = k.get("used_count", 0)
                            text += f"  [{cat}] {topic} (used {used}x)\n"
                        text += "\n"

                    # Subreddit trends
                    subs = proj.get("reddit", {}).get("target_subreddits", {})
                    if isinstance(subs, dict):
                        all_subs = subs.get("primary", [])[:3]
                    elif isinstance(subs, list):
                        all_subs = subs[:3]
                    else:
                        all_subs = []

                    for sub in all_subs:
                        trends = self.db.get_subreddit_trends(sub, proj_name)
                        if trends:
                            t = trends[0]
                            themes = t.get("top_themes", "")
                            if themes:
                                text += f"  r/{sub} trending: {themes[:100]}\n"

                    if not knowledge and not all_subs:
                        text += f"--- {proj_name} ---\nNo research data yet.\n"
                    text += "\n"
            else:
                text += "Not connected to the engine."
        except Exception as e:
            text = f"Couldn't load research: {e}"

        await update.message.reply_text(text)

    # ── Relationship Commands ─────────────────────────────────────────

    async def _cmd_friends(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show relationship stats by stage."""
        if not self._is_admin(update.effective_user.id):
            return

        try:
            text = "Relationships\n\n"
            if self._orchestrator:
                for proj in self._orchestrator.projects:
                    proj_name = proj.get("project", {}).get("name", "unknown")
                    stats = self.db.get_relationship_stats(proj_name)
                    if stats:
                        text += f"--- {proj_name} ---\n"
                        for stage in ["noticed", "engaged", "warm", "friend", "advocate"]:
                            count = stats.get(stage, 0)
                            if count > 0:
                                text += f"  {stage.capitalize()}: {count}\n"
                        text += "\n"
                    else:
                        text += f"--- {proj_name} ---\nNo relationships yet.\n\n"

                # DMs sent today
                for platform in ("reddit", "twitter"):
                    accounts = self._orchestrator.account_mgr.load_accounts(platform)
                    for acc in accounts:
                        count = self.db.get_dm_count_today(platform, acc["username"])
                        if count > 0:
                            plat = "Reddit" if platform == "reddit" else "X"
                            text += f"{plat} @{acc['username']}: {count} DMs today\n"
            else:
                text += "Not connected to the engine."
        except Exception as e:
            text = f"Couldn't load relationships: {e}"

        await update.message.reply_text(text)

    async def _cmd_conversations(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent DM conversations."""
        if not self._is_admin(update.effective_user.id):
            return

        try:
            recent = self.db.get_recent_conversations(limit=15)
            if not recent:
                await update.message.reply_text("No conversations yet.")
                return

            text = "Recent conversations:\n\n"
            for conv in recent:
                plat = "Reddit" if conv["platform"] == "reddit" else "X"
                direction = "->" if conv["direction"] == "sent" else "<-"
                stage = conv.get("stage", "?")
                ts = conv["timestamp"][5:16]  # MM-DD HH:MM
                preview = conv["content"][:60].replace("\n", " ")
                text += (
                    f"  {plat} {direction} @{conv['username']} [{stage}] {ts}\n"
                    f"    \"{preview}\"\n\n"
                )

            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # ── Hub & Performance Commands ──────────────────────────────────

    async def _cmd_hubs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show owned subreddit hubs and their stats."""
        if not self._is_admin(update.effective_user.id):
            return

        try:
            if not self._orchestrator:
                await update.message.reply_text("Not connected to the engine.")
                return

            hubs = self._orchestrator.hub_manager.get_hubs()
            if not hubs:
                await update.message.reply_text(
                    "No subreddit hubs registered yet.\n\n"
                    "To register a hub, use the hub manager in the code "
                    "or create subreddits via the CLI."
                )
                return

            text = f"Owned Subreddit Hubs ({len(hubs)})\n\n"
            for hub in hubs:
                text += f"r/{hub['subreddit']} [{hub['status']}]\n"
                text += f"  Posts: {hub['total_posts']} ({hub['organic_posts']} organic, {hub['promo_posts']} promo)\n"
                text += f"  Subscribers: {hub.get('subscribers', '?')}\n"
                if hub.get('last_post_at'):
                    text += f"  Last post: {hub['last_post_at'][:16]}\n"
                text += "\n"

            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show performance score and improvement suggestions."""
        if not self._is_admin(update.effective_user.id):
            return

        try:
            stats = self.db.get_stats_summary(hours=24)
            actions = stats.get("actions", {})
            total_actions = sum(sum(t.values()) for t in actions.values())

            # Score components
            max_expected = 18 * 24
            activity = min(40, (total_actions / max(max_expected, 1)) * 40)

            r_act = sum(actions.get("reddit", {}).values())
            t_act = sum(actions.get("twitter", {}).values())
            total_plat = r_act + t_act
            balance = (1.0 - abs(r_act - t_act) / max(total_plat, 1)) * 20 if total_plat else 0

            all_types = set()
            for pt in actions.values():
                all_types.update(pt.keys())
            diversity = min(20, len(all_types) * 4)

            total_score = activity + balance + diversity + 20  # +20 baseline

            if total_score >= 80:
                grade = "A"
            elif total_score >= 60:
                grade = "B"
            elif total_score >= 40:
                grade = "C"
            else:
                grade = "D"

            text = f"Performance Score: {grade} ({total_score:.0f}/100)\n\n"
            text += f"Activity: {activity:.0f}/40\n"
            text += f"Platform Balance: {balance:.0f}/20\n"
            text += f"Diversity: {diversity:.0f}/20\n\n"

            # Improvements
            issues = []
            if activity < 20:
                issues.append("Increase posting frequency")
            if balance < 10:
                weak = "Twitter" if r_act > t_act else "Reddit"
                issues.append(f"Boost {weak} activity")
            if diversity < 12:
                issues.append("Diversify action types (comment, post, engage)")

            if issues:
                text += "Improvements needed:\n"
                for iss in issues:
                    text += f"  - {iss}\n"
            else:
                text += "All metrics healthy!"

            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # ── Debug ────────────────────────────────────────────────────────

    async def _cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent skipped decisions — why the bot is NOT acting."""
        if not self._is_admin(update.effective_user.id):
            return

        try:
            # Get recent decisions from the decision_log table
            decisions = self.db.get_recent_decisions(hours=2, limit=15)

            if not decisions:
                await update.message.reply_text(
                    "No decision log entries in the last 2 hours.\n"
                    "The bot may be outside active hours or paused."
                )
                return

            text = "Recent Decisions (last 2h):\n\n"
            for d in decisions:
                ts = d.get("timestamp", "")[-8:]  # HH:MM:SS
                dtype = d.get("decision_type", "?")
                platform = d.get("platform", "")
                outcome = d.get("outcome", "")
                details = d.get("details", "")[:60]

                icon = {
                    "select_opp": "+",
                    "rate_limited": "~",
                    "dedup_blocked": "x",
                    "resource_low": "!",
                    "delayed": ".",
                }.get(dtype, "?")

                text += f"[{ts}] {icon} {dtype}"
                if platform:
                    text += f" ({platform})"
                if details:
                    text += f": {details}"
                text += "\n"

            # Also show rejected opportunities summary
            rejected = self.db.get_rejected_opportunities(hours=2, limit=5)
            if rejected:
                text += "\nRecent Rejections:\n"
                for r in rejected:
                    reason = r.get("rejection_reason", "unknown")
                    title = (r.get("title") or "")[:30]
                    score = r.get("score", 0)
                    text += f"  - {title}... (score={score:.1f}) -> {reason}\n"

            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Debug error: {e}")

    # ── LLM Stats ──────────────────────────────────────────────────

    async def _cmd_llm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show dual-LLM system stats (Groq + Ollama parallel routing)."""
        if not self._is_admin(update.effective_user.id):
            return

        try:
            if not self._orchestrator:
                await update.message.reply_text("Not connected to the engine.")
                return

            stats = self._orchestrator.llm.get_stats()
            providers = stats.get("providers", {})
            groq_rate = stats.get("groq_rate", {})
            routing = stats.get("routing", {})

            text = "Dual-LLM System\n\n"

            # Routing info
            creative = routing.get("creative", [])
            analytical = routing.get("analytical", [])
            text += f"Creative tasks: {' -> '.join(creative)}\n"
            text += f"Analytical tasks: {' -> '.join(analytical)}\n\n"

            # Per-provider stats
            for name, s in providers.items():
                calls = s.get("calls", 0)
                errors = s.get("errors", 0)
                tokens = s.get("tokens", 0)
                avg_ms = s.get("avg_ms", 0)

                label = name.upper()
                if name == "groq":
                    label += " (cloud, 70B)"
                elif name == "ollama":
                    label += " (local, 3B)"
                elif name == "gemini":
                    label += " (cloud, flash)"

                text += f"--- {label} ---\n"
                text += f"  Calls: {calls}"
                if errors:
                    text += f" ({errors} errors)"
                text += "\n"
                if calls > 0:
                    text += f"  Tokens: {tokens:,}\n"
                    text += f"  Avg latency: {avg_ms}ms\n"
                text += "\n"

            # Groq rate limits
            if groq_rate:
                rpm = groq_rate.get("minute", 0)
                rpd = groq_rate.get("day", 0)
                rpm_limit = groq_rate.get("minute_limit", 30)
                rpd_limit = groq_rate.get("day_limit", 14400)
                pct_min = rpm / max(rpm_limit, 1) * 100
                pct_day = rpd / max(rpd_limit, 1) * 100
                text += "Groq Rate Usage:\n"
                text += f"  This minute: {rpm}/{rpm_limit} ({pct_min:.0f}%)\n"
                text += f"  Today: {rpd}/{rpd_limit} ({pct_day:.0f}%)\n\n"

            # Totals
            text += f"Total LLM calls: {stats.get('total_calls', 0)}\n"
            text += f"Total errors: {stats.get('total_errors', 0)}\n"
            text += "Cost: $0.00 (Groq free + Ollama local)"

            await update.message.reply_text(text)

        except Exception as e:
            await update.message.reply_text(f"Error loading LLM stats: {e}")

    # ── Account Management ─────────────────────────────────────────

    async def _cmd_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all configured accounts."""
        if not self._is_admin(update.effective_user.id):
            return

        if not self._account_manager:
            await update.message.reply_text("Account manager not available.")
            return

        try:
            accounts = self._account_manager.list_all_accounts()
            if not accounts:
                await update.message.reply_text("No accounts configured yet.")
                return

            text = f"All accounts ({len(accounts)}):\n\n"
            for acc in accounts:
                plat = "Reddit" if acc["platform"] == "reddit" else "X"
                status = "ON" if acc["enabled"] else "OFF"
                cookies = "cookies OK" if acc["has_cookies"] else "no cookies"
                projects = ", ".join(acc["projects"]) if acc["projects"] else "none"
                text += (
                    f"  {plat} @{acc['username']} [{status}]\n"
                    f"    Persona: {acc['persona']} | {cookies}\n"
                    f"    Projects: {projects}\n\n"
                )

            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_add_reddit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a Reddit account. Usage: /addreddit username password [projects]

        Example: /addreddit myuser mypass my_project,second_project
        """
        if not self._is_admin(update.effective_user.id):
            return
        if not self._account_manager:
            await update.message.reply_text("Account manager not available.")
            return

        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /addreddit username password [project1,project2]\n"
                "Example: /addreddit cooluser123 myP@ss my_project,second_project"
            )
            return

        username = args[0]
        password = args[1]
        projects = args[2].split(",") if len(args) > 2 else None

        try:
            result = self._account_manager.add_account(
                platform="reddit",
                username=username,
                password=password,
                projects=projects,
            )
            await update.message.reply_text(
                f"{result}\n\n"
                f"The bot will auto-login with web cookies on next action.\n"
                f"Account is ready to use."
            )
        except Exception as e:
            await update.message.reply_text(f"Failed to add account: {e}")

    async def _cmd_add_twitter(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a Twitter/X account. Usage: /addtwitter username email password [projects]

        Example: /addtwitter myuser user@mail.com mypass my_project
        """
        if not self._is_admin(update.effective_user.id):
            return
        if not self._account_manager:
            await update.message.reply_text("Account manager not available.")
            return

        args = context.args or []
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /addtwitter username email password [project1,project2]\n"
                "Example: /addtwitter myuser user@mail.com myP@ss my_project"
            )
            return

        username = args[0]
        email = args[1]
        password = args[2]
        projects = args[3].split(",") if len(args) > 3 else None

        try:
            result = self._account_manager.add_account(
                platform="twitter",
                username=username,
                password=password,
                email=email,
                projects=projects,
            )
            await update.message.reply_text(
                f"{result}\n\n"
                f"The bot will try to login on next action.\n"
                f"If 2FA is needed, add totp_secret in the YAML."
            )
        except Exception as e:
            await update.message.reply_text(f"Failed to add account: {e}")

    async def _cmd_remove_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disable an account. Usage: /removeaccount reddit username"""
        if not self._is_admin(update.effective_user.id):
            return
        if not self._account_manager:
            await update.message.reply_text("Account manager not available.")
            return

        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /removeaccount platform username\n"
                "Example: /removeaccount reddit cooluser123\n"
                "Example: /removeaccount twitter myuser"
            )
            return

        platform = args[0].lower()
        if platform == "x":
            platform = "twitter"
        username = args[1]

        try:
            result = self._account_manager.remove_account(platform, username)
            await update.message.reply_text(result)
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

    # ── Reports ──────────────────────────────────────────────────────

    def _generate_daily_report(self) -> str:
        now = datetime.now()
        stats = self.db.get_stats_summary(hours=24)

        report = f"Daily recap — {now.strftime('%B %d, %Y')}\n\n"

        # Reddit
        reddit_actions = stats.get("actions", {}).get("reddit", {})
        if reddit_actions:
            items = ", ".join(f"{v} {k}(s)" for k, v in reddit_actions.items())
            report += f"Reddit: {items}\n"
        else:
            report += "Reddit: quiet day, no actions\n"

        # Twitter
        twitter_actions = stats.get("actions", {}).get("twitter", {})
        if twitter_actions:
            items = ", ".join(f"{v} {k}(s)" for k, v in twitter_actions.items())
            report += f"Twitter: {items}\n"
        else:
            report += "Twitter: nothing today\n"

        # Cost
        report += "\nTotal cost: $0.00 (all free-tier)\n"

        # Opportunities
        opps = stats.get("opportunities", {})
        pending = opps.get("pending", 0)
        report += f"\nOpportunities in queue: {pending}"
        avg = stats.get("avg_opportunity_score", 0)
        if avg:
            report += f" (avg quality: {avg}/10)"

        # Health
        if self._account_manager:
            report += "\n\nAccounts:\n"
            for acc in self._account_manager.get_all_health():
                status = "OK" if acc["status"] == "healthy" else acc["status"].upper()
                platform = "Reddit" if acc["platform"] == "reddit" else "Twitter"
                report += f"  {platform} @{acc['username']}: {status} ({acc['actions_24h']} today)\n"

        # Learning
        try:
            from core.learning_engine import LearningEngine
            engine = LearningEngine(self.db)
            insights = engine.get_insights()
            top_subs = insights.get("top_subreddits", [])
            if top_subs:
                names = ", ".join(f"r/{s['name']}" for s in top_subs[:3])
                report += f"\nTop performers: {names}"
                ratio = insights.get("optimal_promo_ratio", 0.2)
                report += f"\nPromo ratio: {ratio:.0%}"
                disc = insights.get("pending_discoveries", 0)
                if disc:
                    report += f"\nNew targets discovered: {disc}"
        except Exception:
            pass

        return report

    # ── Alerts ────────────────────────────────────────────────────────

    def send_alert_sync(self, message: str):
        """Send alert to all admins (thread-safe, plain HTTP)."""
        token = self.config.get("bot_token", "")
        if not token or token.startswith("YOUR_"):
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for chat_id in self.admin_ids:
            try:
                resp = http_requests.post(
                    url,
                    json={"chat_id": chat_id, "text": message},
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.debug(f"Telegram alert failed ({resp.status_code})")
            except Exception as e:
                logger.error(f"Telegram alert error: {e}")

    async def send_alert(self, message: str):
        self.send_alert_sync(message)

    def send_daily_report_sync(self):
        report = self._generate_daily_report()
        self.send_alert_sync(report)

    async def send_daily_report(self):
        self.send_daily_report_sync()

    # ── Polling ──────────────────────────────────────────────────────

    def start_polling(self):
        if not self.app:
            self.build()
        if not self.app:
            logger.error("Cannot start Telegram bot — not configured")
            return
        logger.info("Starting Telegram dashboard...")

        loop = asyncio.new_event_loop()
        # NOTE: Do NOT call asyncio.set_event_loop(loop) here — it would
        # overwrite the global default loop and break Twitter's persistent
        # event loop running in another daemon thread.
        try:
            loop.run_until_complete(self._async_polling())
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
        finally:
            loop.close()

    async def _async_polling(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram polling active")

        self._polling_event = asyncio.Event()
        await self._polling_event.wait()

        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    def stop_polling(self):
        if hasattr(self, '_polling_event') and self._polling_event:
            self._polling_event.set()
