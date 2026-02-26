#!/usr/bin/env python3
"""Milo — AI Growth Agent for social media automation."""

import os
import sys
import time
import logging
from pathlib import Path

import click
import yaml

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_yaml(path: str) -> dict:
    """Load a YAML config file."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_projects(projects_dir: str = "projects/") -> list:
    """Auto-discover all YAML project files."""
    projects = []
    pdir = Path(projects_dir)
    if not pdir.exists():
        return projects
    for f in pdir.glob("*.yaml"):
        data = load_yaml(str(f))
        if data and data.get("project", {}).get("enabled", True):
            projects.append(data)
    projects.sort(
        key=lambda p: p.get("project", {}).get("weight", 1.0), reverse=True
    )
    return projects


def find_project(projects: list, name: str) -> dict:
    """Find a project by name (case-insensitive)."""
    for p in projects:
        if p.get("project", {}).get("name", "").lower() == name.lower():
            return p
    return {}


def setup_logging(level: str = "INFO"):
    """Configure logging with console + rotating file output."""
    from logging.handlers import RotatingFileHandler

    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler (for TUI dashboard)
    if not root.handlers:
        console = logging.StreamHandler()
        console.setLevel(log_level)
        console.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        root.addHandler(console)

    # Persistent file handler (5MB, keep 3 rotated files)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "miloagent.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_handler)

    # Suppress noisy third-party loggers that flood the log
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
    logging.getLogger("telethon.extensions.messagepacker").setLevel(logging.WARNING)


def check_config_placeholders(config: dict, name: str) -> list:
    """Check for placeholder values in config. Returns list of warnings."""
    warnings = []

    def _check(d, path=""):
        if isinstance(d, dict):
            for k, v in d.items():
                _check(v, f"{path}.{k}" if path else k)
        elif isinstance(d, list):
            for i, v in enumerate(d):
                _check(v, f"{path}[{i}]")
        elif isinstance(d, str) and d.startswith("YOUR_"):
            warnings.append(f"  {name}: {path} = {d}")

    _check(config)
    return warnings


# ─── CLI ─────────────────────────────────────────────────────────────


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, verbose):
    """Milo — AI Growth Agent."""
    ctx.ensure_object(dict)
    os.chdir(PROJECT_ROOT)
    setup_logging("DEBUG" if verbose else "INFO")
    ctx.obj["settings"] = load_yaml("config/settings.yaml")
    ctx.obj["projects"] = load_projects()

    # Lazy-init business manager (only created if needed)
    ctx.obj["_business_mgr"] = None


def _get_business_mgr():
    """Get or create the BusinessManager singleton."""
    from core.business_manager import BusinessManager
    return BusinessManager()


# ─── BUSINESS MANAGEMENT ─────────────────────────────────────────────


@cli.group()
def business():
    """Manage businesses/projects (add, remove, list, show)."""
    pass


@business.command(name="list")
@click.pass_context
def business_list(ctx):
    """List all configured businesses."""
    mgr = _get_business_mgr()
    projects = mgr.projects
    if not projects:
        click.echo("No projects configured. Add one with: business add")
        return

    click.echo(f"\n=== {len(projects)} Business(es) ===\n")
    for p in projects:
        proj = p["project"]
        enabled = click.style("enabled", fg="green") if proj.get("enabled", True) else click.style("disabled", fg="red")
        url = proj.get("url", "")
        weight = proj.get("weight", 1.0)
        has_profile = "+" if proj.get("business_profile") else "-"
        click.echo(f"  {proj['name']} ({url}) [{enabled}] weight={weight} profile={has_profile}")

    click.echo("")


@business.command(name="add")
@click.option("--name", required=True, prompt="Business name")
@click.option("--url", required=True, prompt="Website URL")
@click.option("--description", required=True, prompt="Short description")
@click.option("--type", "project_type", default="SaaS", help="Business type")
def business_add(name, url, description, project_type):
    """Add a new business/project."""
    mgr = _get_business_mgr()
    try:
        filepath = mgr.add_project(name, url, description, project_type)
        click.echo(click.style(f"\nCreated: {filepath}", fg="green"))
        click.echo("Next steps:")
        click.echo(f"  1. Edit {filepath} to add keywords, subreddits, and business profile")
        click.echo(f"  2. Run: python miloagent.py business show {name}")
        click.echo(f"  3. Run: python miloagent.py scan reddit -p {name.lower()}")
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"))


@business.command(name="remove")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to delete this project?")
def business_remove(name):
    """Remove a business/project by name."""
    mgr = _get_business_mgr()
    if mgr.delete_project(name):
        click.echo(click.style(f"Removed project: {name}", fg="green"))
    else:
        click.echo(click.style(f"Project '{name}' not found.", fg="red"))
        available = mgr.list_projects()
        if available:
            click.echo(f"Available: {', '.join(available)}")


@business.command(name="show")
@click.argument("name")
def business_show(name):
    """Show detailed info for a business."""
    mgr = _get_business_mgr()
    proj = mgr.get_project(name)
    if not proj:
        click.echo(click.style(f"Project '{name}' not found.", fg="red"))
        available = mgr.list_projects()
        if available:
            click.echo(f"Available: {', '.join(available)}")
        return

    click.echo(f"\n=== {proj['project']['name']} ===\n")
    click.echo(yaml.dump(proj, default_flow_style=False, sort_keys=False))

    filepath = mgr.get_project_filepath(name)
    if filepath:
        click.echo(f"File: {filepath}")


@business.command(name="edit")
@click.argument("name")
def business_edit(name):
    """Open a business YAML file for editing (shows file path)."""
    mgr = _get_business_mgr()
    filepath = mgr.get_project_filepath(name)
    if not filepath:
        click.echo(click.style(f"Project '{name}' not found.", fg="red"))
        return

    click.echo(f"Edit this file: {filepath}")
    click.echo("Changes are auto-detected when the bot is running (hot-reload).")


# ─── SCAN ────────────────────────────────────────────────────────────


@cli.command()
@click.argument("platform", type=click.Choice(["reddit", "twitter", "telegram", "all"]))
@click.option("--project", "-p", default=None, help="Specific project name (or 'all')")
@click.pass_context
def scan(ctx, platform, project):
    """Scan a platform for opportunities."""
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator

    settings = ctx.obj["settings"]
    projects = ctx.obj["projects"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    content_gen = ContentGenerator(llm)

    # Filter projects
    if project and project.lower() != "all":
        matched = find_project(projects, project)
        if not matched:
            click.echo(click.style(f"Project '{project}' not found.", fg="red"))
            click.echo(f"Available: {', '.join(pr['project']['name'] for pr in projects)}")
            db.close()
            return
        projects = [matched]

    if platform in ("reddit", "all"):
        _scan_reddit(db, content_gen, projects)

    if platform in ("twitter", "all"):
        _scan_twitter(db, content_gen, projects)

    if platform in ("telegram", "all"):
        _scan_telegram(db, content_gen, projects)

    db.close()


def _get_reddit_bot(db, content_gen):
    """Create the right Reddit bot based on auth_mode config."""
    reddit_cfg = load_yaml("config/reddit_accounts.yaml")
    auth_mode = reddit_cfg.get("auth_mode", "web")
    accounts = reddit_cfg.get("accounts", [])

    if not accounts:
        click.echo(click.style("No Reddit accounts configured.", fg="red"))
        return None

    account = next((a for a in accounts if a.get("enabled", True)), None)
    if not account:
        click.echo(click.style("No enabled Reddit accounts.", fg="red"))
        return None

    if auth_mode == "api" and account.get("client_id"):
        from platforms.reddit_bot import RedditBot
        click.echo("  (using PRAW/API mode)")
        return RedditBot(db, content_gen, account)
    else:
        from platforms.reddit_web import RedditWebBot
        click.echo("  (using web/cookie mode — no API app needed)")
        return RedditWebBot(db, content_gen, account)


def _scan_reddit(db, content_gen, projects):
    """Run Reddit scan for given projects."""
    bot = _get_reddit_bot(db, content_gen)
    if not bot:
        return

    for proj in projects:
        proj_name = proj.get("project", {}).get("name", "unknown")
        click.echo(f"\nScanning Reddit for {proj_name}...")
        opportunities = bot.scan(proj)
        click.echo(f"  Found {len(opportunities)} opportunities:")
        for opp in opportunities[:10]:
            score_color = "green" if opp["relevance_score"] >= 7 else (
                "yellow" if opp["relevance_score"] >= 5 else "red"
            )
            score_str = f"{opp['relevance_score']:.1f}"
            click.echo(
                f"  [{click.style(score_str, fg=score_color)}] "
                f"r/{opp['subreddit']}: {opp['title'][:60]}"
            )


def _scan_twitter(db, content_gen, projects):
    """Run Twitter scan for given projects."""
    from platforms.twitter_bot import TwitterBot

    accounts = load_yaml("config/twitter_accounts.yaml").get("accounts", [])
    if not accounts:
        click.echo(click.style("No Twitter accounts configured.", fg="red"))
        return

    account = next((a for a in accounts if a.get("enabled", True)), None)
    if not account:
        click.echo(click.style("No enabled Twitter accounts.", fg="red"))
        return

    bot = TwitterBot(db, content_gen, account)

    for proj in projects:
        proj_name = proj.get("project", {}).get("name", "unknown")
        click.echo(f"\nScanning Twitter for {proj_name}...")
        opportunities = bot.scan(proj)
        click.echo(f"  Found {len(opportunities)} opportunities:")
        for opp in opportunities[:10]:
            click.echo(
                f"  @{opp.get('user', '?')}: {opp['text'][:80]}"
            )


def _scan_telegram(db, content_gen, projects):
    """Run Telegram group scan for given projects."""
    from platforms.telegram_group_bot import TelegramGroupBot

    accounts = load_yaml("config/telegram_user_accounts.yaml").get("accounts", [])
    if not accounts:
        click.echo(click.style("No Telegram user accounts configured.", fg="red"))
        return

    account = next(
        (a for a in accounts
         if a.get("enabled", True)
         and a.get("api_id")
         and not a.get("phone", "").startswith("+00XXX")),
        None,
    )
    if not account:
        click.echo(click.style("No enabled Telegram accounts with credentials.", fg="red"))
        return

    bot = TelegramGroupBot(db, content_gen, account)

    for proj in projects:
        tg_cfg = proj.get("telegram", {})
        if not tg_cfg.get("enabled", False):
            continue

        proj_name = proj.get("project", {}).get("name", "unknown")
        click.echo(f"\nScanning Telegram groups for {proj_name}...")
        opportunities = bot.scan(proj)
        click.echo(f"  Found {len(opportunities)} opportunities:")
        for opp in opportunities[:10]:
            click.echo(
                f"  [{opp.get('group_name', '?')}] @{opp.get('author_name', '?')}: "
                f"{opp.get('text', '')[:80]}"
            )


# ─── POST ────────────────────────────────────────────────────────────


@cli.command()
@click.argument("platform", type=click.Choice(["reddit", "twitter"]))
@click.option("--project", "-p", required=True, help="Project name")
@click.option("--dry-run", is_flag=True, help="Generate content but don't post")
@click.option("--target", "-t", default=None, help="Specific target ID to act on")
@click.pass_context
def post(ctx, platform, project, dry_run, target):
    """Post content to a platform for a project."""
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator

    settings = ctx.obj["settings"]
    projects = ctx.obj["projects"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    content_gen = ContentGenerator(llm)

    proj = find_project(projects, project)
    if not proj:
        click.echo(click.style(f"Project '{project}' not found.", fg="red"))
        db.close()
        return

    if platform == "reddit":
        _post_reddit(db, content_gen, proj, dry_run, target)
    elif platform == "twitter":
        _post_twitter(db, content_gen, proj, dry_run, target)

    db.close()


def _post_reddit(db, content_gen, project, dry_run, target_id):
    """Post a Reddit comment."""
    bot = _get_reddit_bot(db, content_gen)
    if not bot:
        return
    proj_name = project.get("project", {}).get("name", "unknown")

    # Get target opportunity
    if target_id:
        opp = {"target_id": target_id, "title": "(manual target)", "body": "", "subreddit": "unknown"}
    else:
        # Pick the best pending opportunity
        pending = db.get_pending_opportunities(platform="reddit", project=proj_name)
        if not pending:
            click.echo("No pending Reddit opportunities. Run 'scan reddit' first.")
            return
        opp = pending[0]
        # Map DB field to what the bot expects
        if "subreddit_or_query" in opp and "subreddit" not in opp:
            opp["subreddit"] = opp["subreddit_or_query"]
        if "body" not in opp:
            opp["body"] = ""
        click.echo(
            f"Best opportunity [{opp['score']:.1f}]: "
            f"r/{opp.get('subreddit', '?')}: {opp['title'][:60]}"
        )

    if dry_run:
        click.echo("\n--- DRY RUN (not posting) ---")
        comment = bot.act_dry_run(opp, project)
        click.echo(f"\nGenerated comment:\n{comment}")
    else:
        click.echo("\nPosting comment...")
        success = bot.act(opp, project)
        if success:
            click.echo(click.style("Comment posted successfully!", fg="green"))
        else:
            click.echo(click.style("Failed to post comment.", fg="red"))


def _post_twitter(db, content_gen, project, dry_run, target_id):
    """Post a tweet or reply."""
    from platforms.twitter_bot import TwitterBot

    accounts = load_yaml("config/twitter_accounts.yaml").get("accounts", [])
    account = next((a for a in accounts if a.get("enabled", True)), None)
    if not account:
        click.echo(click.style("No enabled Twitter accounts.", fg="red"))
        return

    bot = TwitterBot(db, content_gen, account)
    proj_name = project.get("project", {}).get("name", "unknown")

    if target_id:
        opp = {"target_id": target_id, "text": "(manual target)", "user": "unknown"}
    else:
        pending = db.get_pending_opportunities(platform="twitter", project=proj_name)
        if not pending:
            click.echo("No pending Twitter opportunities. Run 'scan twitter' first.")
            return
        opp = pending[0]
        click.echo(f"Best opportunity: {opp['title'][:60]}")

    if dry_run:
        click.echo("\n--- DRY RUN (not posting) ---")
        reply = content_gen.generate_twitter_reply(
            tweet_text=opp.get("text", opp.get("title", "")),
            tweet_author=opp.get("user", "unknown"),
            project=project,
        )
        click.echo(f"\nGenerated reply:\n{reply}")
    else:
        click.echo("\nPosting to Twitter...")
        success = bot.act(opp, project)
        if success:
            click.echo(click.style("Posted successfully!", fg="green"))
        else:
            click.echo(click.style("Failed to post.", fg="red"))


# ─── ENGAGE (Warm-up & organic engagement) ────────────────────────────


@cli.command()
@click.argument("platform", type=click.Choice(["reddit", "twitter", "all"]))
@click.option("--project", "-p", default=None, help="Specific project name (or all)")
@click.pass_context
def engage(ctx, platform, project):
    """Run organic engagement: upvote, subscribe, like, follow.

    Makes accounts look natural. Run before posting for the first time.

    \b
    Examples:
      python miloagent.py engage reddit             # Engage on Reddit (all projects)
      python miloagent.py engage twitter -p my_project   # Engage on Twitter for a project
      python miloagent.py engage all                 # Engage on all platforms
    """
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator

    settings = ctx.obj["settings"]
    projects = ctx.obj["projects"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    content_gen = ContentGenerator(llm)

    # Filter projects
    if project and project.lower() != "all":
        matched = find_project(projects, project)
        if not matched:
            click.echo(click.style(f"Project '{project}' not found.", fg="red"))
            db.close()
            return
        projects = [matched]

    if platform in ("reddit", "all"):
        _engage_reddit(db, content_gen, projects)

    if platform in ("twitter", "all"):
        _engage_twitter(db, content_gen, projects)

    db.close()


def _engage_reddit(db, content_gen, projects):
    """Run Reddit engagement (subscribe, upvote, save)."""
    bot = _get_reddit_bot(db, content_gen)
    if not bot:
        return

    if not hasattr(bot, "warm_up"):
        click.echo(click.style("Reddit bot doesn't support warm-up (API mode).", fg="yellow"))
        return

    for proj in projects:
        proj_name = proj.get("project", {}).get("name", "unknown")
        click.echo(f"\nEngaging on Reddit for {proj_name}...")

        # Show account info first
        info = bot.get_user_info() if hasattr(bot, "get_user_info") else None
        if info:
            click.echo(
                f"  Account: u/{info['username']} | "
                f"Comment karma: {info['comment_karma']} | "
                f"Link karma: {info['link_karma']} | "
                f"Verified: {info['verified']}"
            )

        stats = bot.warm_up(proj)
        click.echo(click.style(
            f"  Subscribed: {stats['subscribed']} | "
            f"Upvoted: {stats['upvoted']} | "
            f"Saved: {stats['saved']}",
            fg="green",
        ))


def _engage_twitter(db, content_gen, projects):
    """Run Twitter engagement (like, follow, retweet)."""
    from platforms.twitter_bot import TwitterBot

    accounts = load_yaml("config/twitter_accounts.yaml").get("accounts", [])
    account = next((a for a in accounts if a.get("enabled", True)), None)
    if not account:
        click.echo(click.style("No enabled Twitter accounts.", fg="red"))
        return

    bot = TwitterBot(db, content_gen, account)

    for proj in projects:
        proj_name = proj.get("project", {}).get("name", "unknown")
        click.echo(f"\nEngaging on Twitter for {proj_name}...")

        stats = bot.warm_up(proj)
        click.echo(click.style(
            f"  Liked: {stats['liked']} | "
            f"Followed: {stats['followed']} | "
            f"Bookmarked: {stats['bookmarked']} | "
            f"Retweeted: {stats['retweeted']}",
            fg="green",
        ))


@cli.command(name="account-info")
@click.argument("platform", type=click.Choice(["reddit", "twitter"]))
def account_info(platform):
    """Show detailed info for an account (karma, followers, etc.)."""
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator

    db = Database("data/miloagent.db")
    llm = LLMProvider("config/llm.yaml")
    content_gen = ContentGenerator(llm)

    if platform == "reddit":
        bot = _get_reddit_bot(db, content_gen)
        if not bot:
            db.close()
            return
        if not hasattr(bot, "get_user_info"):
            click.echo("Account info not available in API mode.")
            db.close()
            return

        info = bot.get_user_info()
        if info:
            click.echo(f"\n=== Reddit Account: u/{info['username']} ===\n")
            click.echo(f"  Comment Karma:  {info['comment_karma']}")
            click.echo(f"  Link Karma:     {info['link_karma']}")
            click.echo(f"  Email Verified: {info['verified']}")
            click.echo(f"  Reddit Gold:    {info['is_gold']}")
            click.echo(f"  Inbox:          {info['inbox_count']} messages")

            # Account age
            if info['created_utc']:
                from datetime import datetime, timezone
                created = datetime.fromtimestamp(info['created_utc'], tz=timezone.utc)
                age_days = (datetime.now(tz=timezone.utc) - created).days
                click.echo(f"  Account Age:    {age_days} days ({created.strftime('%Y-%m-%d')})")
        else:
            click.echo(click.style("Failed to get account info. Check authentication.", fg="red"))

    elif platform == "twitter":
        click.echo("Twitter account info: use 'python miloagent.py test twitter'")

    db.close()


# ─── STATUS ──────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def status(ctx):
    """Show bot status and recent activity."""
    from core.database import Database

    settings = ctx.obj["settings"]
    projects = ctx.obj["projects"]
    db = Database(settings["database"]["path"])

    click.echo("\n=== Milo Status ===\n")

    # Mode info
    mode = settings.get("bot", {}).get("mode", "unknown")
    cost = settings.get("bot", {}).get("cost_mode", "unknown")
    click.echo(f"Mode: {mode} | Cost: {cost}")
    click.echo(f"Projects: {', '.join(p['project']['name'] for p in projects)}\n")

    # Stats
    stats = db.get_stats_summary(hours=24)

    click.echo("--- Last 24h Actions ---")
    actions = stats.get("actions", {})
    if not actions:
        click.echo("  No actions yet.")
    else:
        for platform, types in actions.items():
            for action_type, count in types.items():
                click.echo(f"  {platform}/{action_type}: {count}")

    click.echo(f"\n--- Opportunities ---")
    opps = stats.get("opportunities", {})
    if not opps:
        click.echo("  No opportunities scanned yet.")
    else:
        for status_name, count in opps.items():
            click.echo(f"  {status_name}: {count}")
    click.echo(f"  Avg score: {stats.get('avg_opportunity_score', 0)}")

    # Recent actions
    click.echo("\n--- Recent Actions ---")
    recent = db.get_recent_actions(hours=24, limit=5)
    if not recent:
        click.echo("  None")
    else:
        for action in recent:
            ts = action["timestamp"][:16]
            click.echo(
                f"  [{ts}] {action['platform']}/{action['action_type']} "
                f"by {action['account']} ({action['project']})"
            )

    db.close()


# ─── STATS ───────────────────────────────────────────────────────────


@cli.command()
@click.option("--hours", "-h", default=24, help="Time window in hours")
@click.pass_context
def stats(ctx, hours):
    """Show detailed statistics."""
    from core.database import Database

    settings = ctx.obj["settings"]
    db = Database(settings["database"]["path"])
    summary = db.get_stats_summary(hours=hours)

    click.echo(f"\n=== Milo Stats (last {hours}h) ===\n")

    actions = summary.get("actions", {})
    total = sum(sum(t.values()) for t in actions.values())
    click.echo(f"Total actions: {total}")

    for platform, types in actions.items():
        click.echo(f"\n  {platform.upper()}:")
        for action_type, count in types.items():
            click.echo(f"    {action_type}: {count}")

    opps = summary.get("opportunities", {})
    click.echo(f"\nOpportunities: {sum(opps.values())}")
    for s, c in opps.items():
        click.echo(f"  {s}: {c}")

    click.echo(f"Avg opportunity score: {summary.get('avg_opportunity_score', 0)}")
    db.close()


# ─── TEST ────────────────────────────────────────────────────────────


@cli.command()
@click.argument("service", type=click.Choice(["llm", "reddit", "twitter", "telegram", "telegram-groups", "all"]))
def test(service):
    """Test connectivity to services."""
    results = {}

    if service in ("llm", "all"):
        click.echo("\nTesting LLM providers...")
        from core.llm_provider import LLMProvider
        try:
            llm = LLMProvider("config/llm.yaml")
            llm_results = llm.test_connection()
            for name, ok in llm_results.items():
                label = f"llm/{name}"
                results[label] = ok
        except Exception as e:
            results["llm"] = False
            click.echo(f"  Error: {e}")

    if service in ("reddit", "all"):
        click.echo("\nTesting Reddit...")
        try:
            from core.database import Database
            from core.content_gen import ContentGenerator
            from core.llm_provider import LLMProvider

            db = Database("data/miloagent.db")
            llm = LLMProvider("config/llm.yaml")
            cg = ContentGenerator(llm)
            bot = _get_reddit_bot(db, cg)
            if bot:
                results["reddit"] = bot.test_connection()
            else:
                results["reddit"] = False
            db.close()
        except Exception as e:
            results["reddit"] = False
            click.echo(f"  Error: {e}")

    if service in ("twitter", "all"):
        click.echo("\nTesting Twitter...")
        try:
            accounts = load_yaml("config/twitter_accounts.yaml").get("accounts", [])
            account = next((a for a in accounts if a.get("enabled", True)), None)
            if account and not account.get("username", "").startswith("YOUR_"):
                from core.database import Database
                from core.content_gen import ContentGenerator
                from core.llm_provider import LLMProvider
                from platforms.twitter_bot import TwitterBot

                db = Database("data/miloagent.db")
                llm = LLMProvider("config/llm.yaml")
                cg = ContentGenerator(llm)
                bot = TwitterBot(db, cg, account)
                results["twitter"] = bot.test_connection()
                db.close()
            else:
                results["twitter"] = False
                click.echo("  Twitter account not configured (placeholder values)")
        except Exception as e:
            results["twitter"] = False
            click.echo(f"  Error: {e}")

    if service in ("telegram", "all"):
        click.echo("\nTesting Telegram...")
        try:
            tg_config = load_yaml("config/telegram.yaml")
            token = tg_config.get("bot_token", "")
            if token and not token.startswith("YOUR_"):
                import requests
                resp = requests.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=10,
                )
                results["telegram"] = resp.status_code == 200
            else:
                results["telegram"] = False
                click.echo("  Telegram bot not configured (placeholder values)")
        except Exception as e:
            results["telegram"] = False
            click.echo(f"  Error: {e}")

    if service in ("telegram-groups", "telegram", "all"):
        click.echo("\nTesting Telegram Groups (Telethon)...")
        try:
            tg_user_cfg = load_yaml("config/telegram_user_accounts.yaml")
            accounts = tg_user_cfg.get("accounts", [])
            account = next(
                (a for a in accounts
                 if a.get("enabled", True)
                 and a.get("api_id")
                 and not a.get("phone", "").startswith("+00XXX")),
                None,
            )
            if account:
                from core.database import Database
                from core.content_gen import ContentGenerator
                from core.llm_provider import LLMProvider
                from platforms.telegram_group_bot import TelegramGroupBot

                db = Database("data/miloagent.db")
                llm = LLMProvider("config/llm.yaml")
                cg = ContentGenerator(llm)
                bot = TelegramGroupBot(db, cg, account)
                results["telegram-groups"] = bot.test_connection()
                db.close()
            else:
                results["telegram-groups"] = False
                click.echo("  Telegram user account not configured")
        except Exception as e:
            results["telegram-groups"] = False
            click.echo(f"  Error: {e}")

    # Print results
    click.echo("\n=== Test Results ===")
    for name, ok in results.items():
        icon = click.style("PASS", fg="green") if ok else click.style("FAIL", fg="red")
        click.echo(f"  {name}: {icon}")

    if not results:
        click.echo("  No services tested.")


# ─── SETUP ───────────────────────────────────────────────────────────


@cli.command()
def setup():
    """Check configuration and guide setup."""
    click.echo("\n=== Milo Setup Check ===\n")

    all_warnings = []

    configs = {
        "settings": "config/settings.yaml",
        "llm": "config/llm.yaml",
        "reddit": "config/reddit_accounts.yaml",
        "twitter": "config/twitter_accounts.yaml",
        "telegram": "config/telegram.yaml",
    }

    for name, path in configs.items():
        if os.path.exists(path):
            data = load_yaml(path)
            warnings = check_config_placeholders(data, name)
            if warnings:
                click.echo(click.style(f"  {name}: needs configuration", fg="yellow"))
                all_warnings.extend(warnings)
            else:
                click.echo(click.style(f"  {name}: OK", fg="green"))
        else:
            click.echo(click.style(f"  {name}: MISSING ({path})", fg="red"))

    # Check projects
    projects = load_projects()
    click.echo(f"\n  Projects found: {len(projects)}")
    for p in projects:
        click.echo(f"    - {p['project']['name']}")

    if all_warnings:
        click.echo(click.style("\nPlaceholder values found:", fg="yellow"))
        for w in all_warnings:
            click.echo(w)
        click.echo(
            "\nEdit the config files to replace YOUR_* values with real credentials."
        )
        click.echo("\nQuick links:")
        click.echo("  Groq API Key:    https://console.groq.com")
        click.echo("  Gemini API Key:  https://aistudio.google.com")
        click.echo("  Reddit App:      https://www.reddit.com/prefs/apps")
        click.echo("  Telegram Bot:    Message @BotFather on Telegram")
    else:
        click.echo(click.style("\nAll configs look good!", fg="green"))
        click.echo("Run 'python miloagent.py test all' to verify connections.")


# ─── ACCOUNTS ────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def accounts(ctx):
    """Show account status."""
    from core.database import Database

    settings = ctx.obj["settings"]
    db = Database(settings["database"]["path"])

    click.echo("\n=== Account Status ===\n")

    # Reddit accounts
    reddit_cfg = load_yaml("config/reddit_accounts.yaml")
    click.echo("Reddit:")
    for acc in reddit_cfg.get("accounts", []):
        enabled = "enabled" if acc.get("enabled", True) else "disabled"
        username = acc.get("username", "?")
        if username.startswith("YOUR_"):
            click.echo(f"  {username}: not configured")
        else:
            action_count = db.get_action_count(hours=24, account=username, platform="reddit")
            click.echo(f"  u/{username}: {enabled}, {action_count} actions (24h)")

    # Twitter accounts
    twitter_cfg = load_yaml("config/twitter_accounts.yaml")
    click.echo("\nTwitter:")
    for acc in twitter_cfg.get("accounts", []):
        enabled = "enabled" if acc.get("enabled", True) else "disabled"
        username = acc.get("username", "?")
        if username.startswith("YOUR_"):
            click.echo(f"  {username}: not configured")
        else:
            action_count = db.get_action_count(hours=24, account=username, platform="twitter")
            click.echo(f"  @{username}: {enabled}, {action_count} actions (24h)")

    db.close()


# ─── RUN (Background Mode) ──────────────────────────────────────────


@cli.command()
@click.pass_context
def dashboard(ctx):
    """Launch the Rich TUI dashboard (full-screen terminal).

    Starts the bot and displays a live-updating dashboard with:
    - System resources & bot status
    - LLM provider stats (dual routing)
    - Actions, opportunities, recent activity
    - Relationship pipeline
    - Scheduler jobs

    \b
    Keyboard shortcuts:
      s = trigger scan    a = trigger action    l = trigger learn
      p = pause/resume    r = refresh           q = quit
    """
    # Check environment — warn if headless server
    from core.environment import detect_environment, get_env_summary
    env = detect_environment()
    click.echo(f"Environment: {get_env_summary()}")

    if env["is_headless"] and not env["has_tty"]:
        click.echo(click.style(
            "Headless server detected — TUI requires a terminal.",
            fg="yellow",
        ))
        click.echo("  Use 'python3 miloagent.py run --daemon' + Telegram dashboard instead.")
        return

    click.echo("Starting Milo with TUI dashboard...")

    # Check we're running inside a venv
    if "VIRTUAL_ENV" not in os.environ and not hasattr(sys, "real_prefix"):
        venv_path = os.path.join(PROJECT_ROOT.parent, "venv", "bin", "python3")
        if os.path.exists(venv_path):
            click.echo(click.style("Not running inside the venv. Use:", fg="yellow"))
            click.echo(f"  {venv_path} miloagent.py dashboard")
            return

    try:
        from core.orchestrator import Orchestrator
        from dashboard.tui import RichDashboard
    except ImportError as e:
        click.echo(click.style(f"Missing dependency: {e}", fg="red"))
        click.echo("Run: .venv/bin/pip install -r requirements.txt")
        return

    # Kill existing daemon if running
    pid_file = Path("data/miloagent.pid")
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)
            click.echo(f"Stopping existing daemon (PID {old_pid})...")
            import signal as _sig
            os.kill(old_pid, _sig.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(old_pid, 0)
                except OSError:
                    break
            pid_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    try:
        import signal as _sig

        # Silence console logs BEFORE orchestrator starts — the TUI will
        # capture them via its own log handler and display them in the UI.
        # Without this, dozens of INFO lines flood the terminal before the
        # TUI alternate screen kicks in, making it look "broken".
        root_logger = logging.getLogger()
        _saved_handlers = []
        for h in root_logger.handlers[:]:
            # Only silence console StreamHandlers — keep file handlers alive
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                _saved_handlers.append(h)
                root_logger.removeHandler(h)

        click.echo("Loading Milo...")
        orch = Orchestrator()
        click.echo(f"Mode: {orch._mode} | Starting orchestrator...")
        try:
            orch.start(nonblocking=True)
        except RuntimeError as e:
            click.echo(click.style(f"\nError: {e}", fg="red"))
            # Restore handlers before returning
            for h in _saved_handlers:
                root_logger.addHandler(h)
            return
        except Exception as e:
            click.echo(click.style(f"\nStartup failed: {e}", fg="red"))
            for h in _saved_handlers:
                root_logger.addHandler(h)
            return

        # Override signal handlers: TUI manages its own shutdown loop
        # (the orchestrator's default handler calls sys.exit which breaks the TUI)
        def _tui_signal(signum, frame):
            tui._running = False

        tui = RichDashboard(orch)

        if tui._is_vscode:
            click.echo(click.style(
                "VSCode terminal detected — using scrolling mode (no alternate screen).",
                fg="yellow",
            ))
            click.echo("For best experience, use Terminal.app or iTerm2.\n")

        click.echo("Launching dashboard...\n")

        _sig.signal(_sig.SIGINT, _tui_signal)
        _sig.signal(_sig.SIGTERM, _tui_signal)

        try:
            tui.run()
        finally:
            # Restore original log handlers
            for h in _saved_handlers:
                root_logger.addHandler(h)
            try:
                orch.stop()
            except Exception:
                pass
    except (KeyboardInterrupt, SystemExit):
        click.echo("\nShutting down Milo...")
    except Exception as exc:
        click.echo(click.style(f"\nTUI error: {exc}", fg="red"))
        import traceback
        traceback.print_exc()


@cli.command()
@click.option("--daemon", "-d", is_flag=True, help="Run as background daemon (detach from terminal)")
@click.option("--web", "-w", is_flag=True, help="Start with web dashboard (FastAPI)")
@click.option("--web-port", default=8420, type=int, help="Web dashboard port (default: 8420)")
@click.pass_context
def run(ctx, daemon, web, web_port):
    """Start bot in continuous mode.

    Use --daemon / -d to run in background (survives terminal close).
    Use --web / -w to start with web dashboard on port 8420.
    Logs go to logs/miloagent.log when running as daemon.
    Use 'python3 miloagent.py stop' to stop the daemon.
    """
    if daemon:
        _run_daemon()
        return

    from core.environment import get_env_summary
    click.echo(f"Environment: {get_env_summary()}")

    if web:
        click.echo(f"Starting Milo with web dashboard on port {web_port}...")
        click.echo(f"PID: {os.getpid()}")
        click.echo(f"Dashboard: http://0.0.0.0:{web_port}")
        click.echo("Press Ctrl+C to stop.\n")
        try:
            from core.orchestrator import Orchestrator
            import uvicorn
            from dashboard.web import WebDashboard

            orch = Orchestrator()
            orch.start(nonblocking=True)
            dashboard = WebDashboard(orch)
            uvicorn.run(dashboard.app, host="0.0.0.0", port=web_port, log_level="warning")
        except KeyboardInterrupt:
            click.echo("\nShutting down Milo...")
        finally:
            try:
                orch.stop()
            except Exception:
                pass
        return

    click.echo("Starting Milo in foreground mode...")
    click.echo(f"PID: {os.getpid()}")
    click.echo("Press Ctrl+C to stop.\n")
    click.echo("Tip: use 'python3 miloagent.py run --daemon' to run in background.\n")

    try:
        from core.orchestrator import Orchestrator
        orch = Orchestrator()
        orch.start()
    except KeyboardInterrupt:
        click.echo("\nShutting down Milo...")


def _run_daemon():
    """Fork the process to run as a proper background daemon (double-fork)."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "miloagent.log"

    # Kill old process if running (auto-restart)
    pid_file = Path("data/miloagent.pid")
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)  # Check if process exists
            click.echo(f"Stopping old Milo (PID {old_pid})...")
            import signal as _sig
            import time as _time
            os.kill(old_pid, _sig.SIGTERM)
            # Wait up to 5s for graceful shutdown
            for _ in range(10):
                _time.sleep(0.5)
                try:
                    os.kill(old_pid, 0)
                except OSError:
                    break  # Process died
            else:
                # Still alive — force kill
                try:
                    os.kill(old_pid, _sig.SIGKILL)
                    _time.sleep(0.5)
                except OSError:
                    pass
            click.echo(click.style(f"Old process (PID {old_pid}) stopped.", fg="yellow"))
            pid_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)  # Stale PID file, clean up

    # Double-fork to fully detach from terminal
    # First fork: parent exits, child continues
    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly for child to start, then report
        import time
        time.sleep(1)
        # Read PID from file (written by grandchild)
        daemon_pid = pid  # fallback
        for _ in range(10):
            if pid_file.exists():
                try:
                    daemon_pid = int(pid_file.read_text().strip())
                    break
                except (ValueError, OSError):
                    pass
            time.sleep(0.5)

        click.echo(click.style("Milo daemon started!", fg="green"))
        click.echo(f"  PID: {daemon_pid}")
        click.echo(f"  Logs: {log_file.resolve()}")
        click.echo(f"\nCommands:")
        click.echo(f"  Stop:    python3 miloagent.py stop")
        click.echo(f"  Logs:    tail -f {log_file}")
        click.echo(f"  Status:  python3 miloagent.py status")
        return

    # First child: become session leader
    os.setsid()

    # Second fork: session leader exits, grandchild has no controlling terminal
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Grandchild: this is the actual daemon process
    # Redirect stdin/stdout/stderr to log file
    sys.stdin.close()
    log_fd = open(log_file, "a")
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())

    # Set up logging for daemon
    setup_logging("INFO")

    # Run the orchestrator with web dashboard
    try:
        from core.orchestrator import Orchestrator
        import uvicorn
        from dashboard.web import WebDashboard

        orch = Orchestrator()
        orch.start(nonblocking=True)
        dashboard = WebDashboard(orch)
        uvicorn.run(dashboard.app, host="0.0.0.0", port=8420, log_level="warning")
    except Exception as e:
        logging.getLogger(__name__).critical(f"Daemon crashed: {e}", exc_info=True)
        sys.exit(1)


@cli.command()
def stop():
    """Stop the running Milo daemon."""
    pid_file = Path("data/miloagent.pid")
    if not pid_file.exists():
        click.echo("Milo is not running (no PID file found).")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # Check if alive
    except (OSError, ValueError):
        click.echo("Milo is not running (stale PID file). Cleaning up.")
        pid_file.unlink(missing_ok=True)
        return

    click.echo(f"Stopping Milo (PID {pid})...")
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGTERM)
        # Wait up to 10s for graceful shutdown
        import time as _time
        for _ in range(20):
            _time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                click.echo(click.style("Milo stopped.", fg="green"))
                return
        # Force kill if still alive
        click.echo("Forcing shutdown...")
        os.kill(pid, _signal.SIGKILL)
        click.echo(click.style("Milo force-stopped.", fg="yellow"))
    except OSError:
        click.echo(click.style("Milo stopped.", fg="green"))
    finally:
        pid_file.unlink(missing_ok=True)


# ─── LOGIN (Browser-based authentication) ────────────────────────────


@cli.command()
@click.argument("platform", type=click.Choice(["reddit", "twitter", "telegram"]))
@click.option("--account", "-a", default=None, help="Username to login (skip interactive menu)")
@click.option("--all-accounts", is_flag=True, help="Login all enabled accounts one by one")
def login(platform, account, all_accounts):
    """Login to a platform via browser (Reddit/Twitter) or SMS code (Telegram).

    \b
    Reddit/Twitter: Opens Chrome, log in manually, cookies captured.
    Telegram: Sends SMS code to your phone, enter it in terminal.

    \b
    Examples:
      python miloagent.py login reddit                  # Interactive account picker
      python miloagent.py login reddit -a MyAccount      # Login specific account
      python miloagent.py login telegram                # Telegram SMS auth
    """
    # ── Telegram: Telethon phone-based auth ──
    if platform == "telegram":
        _login_telegram()
        return

    from core.cookie_manager import CookieManager

    # Check if Playwright is installed
    if not CookieManager.is_available():
        click.echo(click.style("Playwright is not installed.", fg="yellow"))
        click.echo("\nInstalling now...")
        try:
            CookieManager.install()
            click.echo(click.style("Playwright installed!", fg="green"))
        except Exception as e:
            click.echo(click.style(f"Installation failed: {e}", fg="red"))
            click.echo("\nManual install:")
            click.echo("  pip install playwright && playwright install chromium")
            click.echo("\nAlternative: use 'paste-cookies' for manual cookie import.")
            return

    # Get account config
    if platform == "reddit":
        cfg = load_yaml("config/reddit_accounts.yaml")
    else:
        cfg = load_yaml("config/twitter_accounts.yaml")

    accounts_list = cfg.get("accounts", [])
    enabled = [a for a in accounts_list if a.get("enabled", True)]
    if not enabled:
        click.echo(click.style(f"No enabled {platform} accounts configured.", fg="red"))
        return

    # Determine which accounts to login
    if all_accounts:
        targets = enabled
    elif account:
        matched = next(
            (a for a in enabled if a["username"].lower() == account.lower()),
            None,
        )
        if not matched:
            click.echo(click.style(f"Account '{account}' not found or disabled.", fg="red"))
            click.echo(f"Available: {', '.join(a['username'] for a in enabled)}")
            return
        targets = [matched]
    else:
        # Interactive selection
        click.echo(f"\n=== {platform.title()} Accounts ===\n")
        for i, acc in enumerate(enabled, 1):
            username = acc["username"]
            cookies_file = acc.get("cookies_file", "")
            has_cookies = os.path.exists(cookies_file) if cookies_file else False
            status = click.style("has cookies", fg="green") if has_cookies else click.style("no cookies", fg="red")
            click.echo(f"  {i}. {username} [{status}]")

        click.echo(f"  0. Login ALL accounts one by one")
        click.echo("")

        choice = click.prompt("Which account?", type=int, default=1)
        if choice == 0:
            targets = enabled
        elif 1 <= choice <= len(enabled):
            targets = [enabled[choice - 1]]
        else:
            click.echo(click.style("Invalid choice.", fg="red"))
            return

    # Login each target account
    mgr = CookieManager()
    success_count = 0

    for i, target_acc in enumerate(targets):
        username = target_acc["username"]
        cookies_file = target_acc.get("cookies_file", f"data/cookies/{platform}_account{i+1}.json")

        click.echo(f"\n{'='*50}")
        click.echo(click.style(f"Login as: {username}", fg="cyan", bold=True))
        click.echo(f"Cookies: {cookies_file}")
        click.echo(f"{'='*50}")
        click.echo("A fresh Chrome window will open (clean profile).")
        click.echo(click.style(f"Log in as @{username}, then come back here and press ENTER.", fg="yellow", bold=True))
        click.echo("")

        if len(targets) > 1 and i > 0:
            if not click.confirm("Ready for next account?", default=True):
                click.echo("Skipping remaining accounts.")
                break

        cookies = mgr.login(platform, cookies_file, timeout=120)

        if cookies:
            click.echo(click.style(f"Login successful for {username}! {len(cookies)} cookies saved.", fg="green"))
            success_count += 1
        else:
            click.echo(click.style(f"Login failed for {username}.", fg="red"))

    # Summary
    if len(targets) > 1:
        click.echo(f"\n--- {success_count}/{len(targets)} accounts logged in ---")

    if success_count > 0:
        click.echo(f"\nRun 'python miloagent.py test {platform}' to verify.")


def _login_telegram():
    """Telethon phone-based auth: SMS code + optional 2FA."""
    import asyncio

    cfg = load_yaml("config/telegram_user_accounts.yaml")
    accounts_list = cfg.get("accounts", [])
    enabled = [a for a in accounts_list if a.get("enabled", True)]

    if not enabled:
        click.echo(click.style("No enabled Telegram accounts configured.", fg="red"))
        click.echo("Edit config/telegram_user_accounts.yaml first.")
        return

    for acc in enabled:
        phone = acc.get("phone", "")
        api_id = acc.get("api_id", "")
        api_hash = acc.get("api_hash", "")
        session_file = acc.get("session_file", "data/sessions/telegram_user1.session")

        if not api_id or not api_hash:
            click.echo(click.style("api_id and api_hash not set.", fg="red"))
            click.echo("  1. Go to https://my.telegram.org")
            click.echo("  2. Log in and create an app")
            click.echo("  3. Copy api_id and api_hash to config/telegram_user_accounts.yaml")
            return

        if not phone or phone.startswith("+00XXX"):
            click.echo(click.style("Phone number not set.", fg="red"))
            click.echo("Edit config/telegram_user_accounts.yaml and set your phone number.")
            return

        click.echo(f"\n{'='*50}")
        click.echo(click.style(f"Telegram Login: {phone}", fg="cyan", bold=True))
        click.echo(f"Session: {session_file}")
        click.echo(f"{'='*50}")

        os.makedirs(os.path.dirname(session_file), exist_ok=True)

        try:
            from telethon import TelegramClient
        except ImportError:
            click.echo(click.style("Telethon not installed.", fg="red"))
            click.echo("  pip install telethon")
            return

        async def _do_login():
            client = TelegramClient(session_file, int(api_id), api_hash)
            await client.connect()

            if await client.is_user_authorized():
                me = await client.get_me()
                name = me.username or me.first_name or phone
                click.echo(click.style(
                    f"Already logged in as @{name}!", fg="green"
                ))
                await client.disconnect()
                return True

            click.echo("Sending SMS code to your phone...")
            await client.send_code_request(phone)

            code = click.prompt("Enter the code you received")
            try:
                await client.sign_in(phone, code)
            except Exception as e:
                if "Two-step" in str(e) or "password" in str(e).lower():
                    password = click.prompt("Enter your 2FA password", hide_input=True)
                    await client.sign_in(password=password)
                else:
                    raise

            me = await client.get_me()
            name = me.username or me.first_name or phone
            click.echo(click.style(
                f"Login successful! Logged in as @{name}", fg="green"
            ))
            click.echo(f"Session saved to: {session_file}")
            await client.disconnect()
            return True

        try:
            asyncio.run(_do_login())
            click.echo(f"\nRun 'python miloagent.py test telegram' to verify.")
        except Exception as e:
            click.echo(click.style(f"Telegram login failed: {e}", fg="red"))


# ─── COOKIES (Import browser cookies) ───────────────────────────────


@cli.command()
@click.argument("platform", type=click.Choice(["reddit", "twitter"]))
def cookies(platform):
    """Import cookies from your browser for authentication.

    Steps:
    1. Login to the platform in Chrome
    2. Run this command
    3. It will try to extract cookies automatically

    If auto-extract fails, use 'paste-cookies' instead.

    Preferred: use 'login' command instead (opens a browser for you).
    """
    if platform == "reddit":
        _import_reddit_cookies()
    elif platform == "twitter":
        click.echo("Twitter cookie auto-import is not supported.")
        click.echo("Use the browser method instead:\n")
        click.echo("  python miloagent.py login twitter")
        click.echo("\nOr manual method:")
        click.echo("  1. Open Chrome and go to x.com (logged in)")
        click.echo("  2. Press F12 -> Console tab")
        click.echo("  3. Type: document.cookie  (press Enter)")
        click.echo("  4. Copy the output")
        click.echo("  5. Run: python miloagent.py paste-cookies twitter")
        click.echo("  6. Paste and press Enter")


def _import_reddit_cookies():
    """Try to import Reddit cookies from Chrome."""
    import sqlite3 as cookie_sqlite3
    import shutil
    import tempfile

    reddit_cfg = load_yaml("config/reddit_accounts.yaml")
    accounts = reddit_cfg.get("accounts", [])
    if not accounts:
        click.echo(click.style("No Reddit accounts configured.", fg="red"))
        return

    account = accounts[0]
    cookies_file = account.get("cookies_file", "data/cookies/reddit_account1.json")
    os.makedirs(os.path.dirname(cookies_file), exist_ok=True)

    # Try Chrome cookie database on macOS
    chrome_cookie_path = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Default/Cookies"
    )

    if not os.path.exists(chrome_cookie_path):
        # Try other Chrome profiles
        chrome_dir = os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/"
        )
        if os.path.isdir(chrome_dir):
            for item in os.listdir(chrome_dir):
                candidate = os.path.join(chrome_dir, item, "Cookies")
                if os.path.exists(candidate):
                    chrome_cookie_path = candidate
                    break

    if not os.path.exists(chrome_cookie_path):
        click.echo(click.style(
            "Chrome cookies not found automatically.\n", fg="yellow"
        ))
        click.echo("Manual method:")
        click.echo("  1. Open Chrome and go to reddit.com (logged in)")
        click.echo("  2. Install 'Cookie-Editor' extension from Chrome Web Store")
        click.echo("  3. Click the extension icon on reddit.com")
        click.echo("  4. Click 'Export' -> 'Export as JSON'")
        click.echo(f"  5. Save the content to: {cookies_file}")
        click.echo("\n  Or use this JS in Chrome DevTools (F12 -> Console):")
        click.echo("  document.cookie.split(';').map(c => c.trim())")
        return

    click.echo(f"Found Chrome cookies at: {chrome_cookie_path}")
    click.echo("Extracting reddit.com cookies...")

    try:
        # Copy cookie DB to temp file (Chrome locks it)
        tmp = tempfile.mktemp(suffix=".db")
        shutil.copy2(chrome_cookie_path, tmp)

        conn = cookie_sqlite3.connect(tmp)
        cursor = conn.execute(
            "SELECT name, value, host_key, path, is_secure, expires_utc "
            "FROM cookies WHERE host_key LIKE '%reddit.com%'"
        )
        rows = cursor.fetchall()
        conn.close()
        os.remove(tmp)

        if not rows:
            click.echo(click.style(
                "No reddit.com cookies found in Chrome. "
                "Make sure you're logged into Reddit in Chrome.",
                fg="yellow",
            ))
            return

        # Convert to dict format
        cookie_dict = {}
        for name, value, host, path, secure, expires in rows:
            if value:  # Chrome encrypts cookies on macOS, value may be empty
                cookie_dict[name] = value

        if not cookie_dict:
            click.echo(click.style(
                "Cookies found but values are encrypted (macOS Keychain).\n",
                fg="yellow",
            ))
            click.echo("Use the manual method instead:")
            click.echo("  1. Open Chrome -> reddit.com (logged in)")
            click.echo("  2. Press F12 -> Console tab")
            click.echo("  3. Paste this and press Enter:")
            click.echo('     copy(document.cookie)')
            click.echo("  4. Run this command:")
            click.echo(f'     python miloagent.py paste-cookies reddit')
            return

        import json
        with open(cookies_file, "w") as f:
            json.dump(cookie_dict, f, indent=2)

        click.echo(click.style(
            f"Exported {len(cookie_dict)} cookies to {cookies_file}",
            fg="green",
        ))
        click.echo("Run 'python miloagent.py test reddit' to verify.")

    except Exception as e:
        click.echo(click.style(f"Error extracting cookies: {e}", fg="red"))
        click.echo("\nUse the manual method instead (see above).")


@cli.command(name="paste-cookies")
@click.argument("platform", type=click.Choice(["reddit", "twitter"]))
def paste_cookies(platform):
    """Paste cookies from browser console for authentication.

    Usage:
    1. Go to reddit.com or x.com in Chrome (logged in)
    2. Press F12 -> Console
    3. Type: document.cookie    (press Enter)
    4. Copy the entire output
    5. Run: python miloagent.py paste-cookies reddit|twitter
    6. Paste the cookies and press Enter
    """
    if platform == "reddit":
        _paste_reddit_cookies()
    elif platform == "twitter":
        _paste_twitter_cookies()


def _paste_reddit_cookies():
    """Parse pasted cookies string into JSON file."""
    reddit_cfg = load_yaml("config/reddit_accounts.yaml")
    accounts = reddit_cfg.get("accounts", [])
    if not accounts:
        click.echo(click.style("No Reddit accounts configured.", fg="red"))
        return

    account = accounts[0]
    cookies_file = account.get("cookies_file", "data/cookies/reddit_account1.json")
    os.makedirs(os.path.dirname(cookies_file), exist_ok=True)

    click.echo("Paste your Reddit cookies below (from browser console):")
    click.echo("(Type 'document.cookie' in Chrome DevTools console, copy the result)")
    click.echo("")

    raw = input("> ").strip()

    # Remove surrounding quotes if present
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
        raw = raw[1:-1]

    if not raw:
        click.echo(click.style("No cookies pasted.", fg="red"))
        return

    # Parse "name=value; name2=value2" format
    cookie_dict = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookie_dict[name.strip()] = value.strip()

    if not cookie_dict:
        click.echo(click.style("Could not parse any cookies.", fg="red"))
        return

    import json
    with open(cookies_file, "w") as f:
        json.dump(cookie_dict, f, indent=2)

    click.echo(click.style(
        f"\nSaved {len(cookie_dict)} cookies to {cookies_file}",
        fg="green",
    ))

    # Check for important cookies
    important = ["reddit_session", "token_v2", "session_tracker"]
    found = [c for c in important if c in cookie_dict]
    if found:
        click.echo(f"Key cookies found: {', '.join(found)}")
        click.echo("Run 'python miloagent.py test reddit' to verify.")
    else:
        click.echo(click.style(
            "Warning: No session cookies found. "
            "Make sure you're logged into Reddit when copying cookies.",
            fg="yellow",
        ))


def _paste_twitter_cookies():
    """Parse pasted cookies from x.com into Twikit cookie file."""
    twitter_cfg = load_yaml("config/twitter_accounts.yaml")
    accounts = twitter_cfg.get("accounts", [])
    if not accounts:
        click.echo(click.style("No Twitter accounts configured.", fg="red"))
        return

    account = accounts[0]
    cookies_file = account.get("cookies_file", "data/cookies/twitter_account1.json")
    os.makedirs(os.path.dirname(cookies_file), exist_ok=True)

    click.echo("Paste your Twitter/X cookies below (from browser console):")
    click.echo("(Open x.com while logged in -> F12 -> Console -> type: document.cookie)")
    click.echo("")

    raw = input("> ").strip()

    # Remove surrounding quotes
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
        raw = raw[1:-1]

    if not raw:
        click.echo(click.style("No cookies pasted.", fg="red"))
        return

    # Parse "name=value; name2=value2" format
    cookie_dict = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookie_dict[name.strip()] = value.strip()

    if not cookie_dict:
        click.echo(click.style("Could not parse any cookies.", fg="red"))
        return

    # Twikit expects cookies in a specific JSON format
    # Save as a list of cookie dicts (Twikit's format)
    import json
    twikit_cookies = []
    for name, value in cookie_dict.items():
        twikit_cookies.append({
            "name": name,
            "value": value,
            "domain": ".x.com",
            "path": "/",
        })

    with open(cookies_file, "w") as f:
        json.dump(twikit_cookies, f, indent=2)

    click.echo(click.style(
        f"\nSaved {len(cookie_dict)} cookies to {cookies_file}",
        fg="green",
    ))

    # Check for important Twitter session cookies
    important = ["auth_token", "ct0", "twid", "kdt"]
    found = [c for c in important if c in cookie_dict]
    if found:
        click.echo(f"Key cookies found: {', '.join(found)}")
        click.echo("Run 'python miloagent.py test twitter' to verify.")
    else:
        click.echo(click.style(
            "Warning: No key session cookies (auth_token, ct0) found. "
            "Make sure you're logged into x.com when copying cookies.",
            fg="yellow",
        ))


# ─── SEND TEST TELEGRAM ──────────────────────────────────────────


@cli.command(name="send-test")
@click.argument("service", type=click.Choice(["telegram"]))
def send_test(service):
    """Send a test message via Telegram to verify config."""
    if service == "telegram":
        _send_test_telegram()


def _send_test_telegram():
    """Send a test message to Telegram admin."""
    import requests as tg_requests

    tg_config = load_yaml("config/telegram.yaml")
    token = tg_config.get("bot_token", "")
    admin_ids = tg_config.get("admin_chat_ids", [])

    if not token or token.startswith("YOUR_"):
        click.echo(click.style("Telegram bot token not configured.", fg="red"))
        return
    if not admin_ids:
        click.echo(click.style("No admin chat IDs configured.", fg="red"))
        return

    msg = (
        "🤖 *Milo — Test*\n\n"
        "✅ Telegram connection is working!\n"
        "Bot is ready to send alerts and reports."
    )

    for chat_id in admin_ids:
        try:
            resp = tg_requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                click.echo(click.style(
                    f"Test message sent to chat {chat_id}!", fg="green"
                ))
            else:
                error = resp.json().get("description", resp.text)
                click.echo(click.style(
                    f"Failed to send to {chat_id}: {error}", fg="red"
                ))
        except Exception as e:
            click.echo(click.style(f"Error sending to {chat_id}: {e}", fg="red"))


# ─── LEARNING & INSIGHTS ─────────────────────────────────────────


@cli.command(name="learn")
@click.option("-p", "--project", default="", help="Project name (all if empty)")
@click.pass_context
def learn_cmd(ctx, project):
    """Run the learning cycle — analyze performance and adapt.

    Analyzes past actions to learn which subreddits, keywords,
    tones, and promo ratios work best. Also discovers new targets via LLM.
    """
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.learning_engine import LearningEngine

    settings = ctx.obj["settings"]
    db = Database(settings["database"]["path"])
    try:
        llm = LLMProvider("config/llm.yaml")
        engine = LearningEngine(db, llm)

        click.echo("Running learning cycle...")
        engine.learn(project=project)

        insights = engine.get_insights(project=project)

        click.echo(click.style("\n=== Learning Insights ===", fg="cyan", bold=True))

        top_subs = insights.get("top_subreddits", [])
        if top_subs:
            click.echo("\nTop subreddits (by engagement):")
            for s in top_subs:
                click.echo(
                    f"  r/{s['name']}: score={s['score']:.2f} "
                    f"avg_eng={s['avg_eng']:.2f} (n={s['samples']})"
                )
        else:
            click.echo("\nNo subreddit data yet (need more actions)")

        top_kw = insights.get("top_keywords", [])
        if top_kw:
            click.echo("\nTop keywords:")
            for k in top_kw:
                click.echo(f"  '{k['name']}': score={k['score']:.2f} (n={k['samples']})")

        click.echo(f"\nBest tone: {insights.get('best_tone', 'N/A')}")
        click.echo(f"Optimal promo ratio: {insights.get('optimal_promo_ratio', 0.2):.0%}")
        click.echo(f"Pending discoveries: {insights.get('pending_discoveries', 0)}")

        # Post-type weights learned
        pt_stats = db.get_post_type_stats(project=project, days=30)
        if pt_stats:
            click.echo(click.style("\n--- Post-Type Weights ---", fg="magenta"))
            for pt in pt_stats:
                removal = (pt["removed_count"] or 0) / max(pt["count"], 1)
                bar = "█" * int((pt["avg_engagement"] or 0) * 3)
                click.echo(
                    f"  {pt['post_type']:15s} {bar} "
                    f"eng={pt['avg_engagement']:.2f} "
                    f"(n={pt['count']}, {removal:.0%} removed)"
                )

        # Sentiment from replies
        sentiment = db.get_sentiment_by_tone(project=project, days=30)
        if sentiment:
            click.echo(click.style("\n--- Reply Sentiment ---", fg="magenta"))
            for s in sentiment:
                score = s["avg_sentiment"]
                indicator = "+" if score > 0 else "-" if score < 0 else "~"
                style = "green" if score > 0.1 else ("red" if score < -0.1 else "yellow")
                click.echo(click.style(
                    f"  {s['tone_style']:20s} {indicator}{abs(score):.2f} "
                    f"({s['total_replies']} replies)",
                    fg=style,
                ))

        # A/B experiments
        from core.ab_testing import ABTestingEngine
        ab = ABTestingEngine(db)
        experiments = ab.get_active_experiments(project)
        if experiments:
            click.echo(click.style("\n--- A/B Experiments ---", fg="magenta"))
            for exp in experiments:
                results = db.get_experiment_results(exp["id"])
                a_data = results.get("a", {})
                b_data = results.get("b", {})
                click.echo(
                    f"  {exp['experiment_name']} [{exp['variable']}]: "
                    f"A={exp['variant_a']}({a_data.get('avg_eng', 0):.2f}) "
                    f"vs B={exp['variant_b']}({b_data.get('avg_eng', 0):.2f})"
                )

        # Evolved prompts
        evolutions = db.conn.execute(
            """SELECT template_name, status, performance_after,
                      timestamp FROM prompt_evolution_log
               WHERE project = ? OR ? = ''
               ORDER BY timestamp DESC LIMIT 3""",
            (project, project),
        ).fetchall()
        if evolutions:
            click.echo(click.style("\n--- Prompt Evolutions ---", fg="magenta"))
            for evo in evolutions:
                st_color = "green" if evo["status"] == "active" else "red"
                perf = evo["performance_after"] or 0
                click.echo(click.style(
                    f"  {evo['template_name']} [{evo['status']}] perf={perf:.2f}",
                    fg=st_color,
                ))

        click.echo(click.style("\nLearning complete!", fg="green"))
    finally:
        db.close()


@cli.command(name="insights")
@click.option("-p", "--project", default="", help="Project name (all if empty)")
@click.pass_context
def insights_cmd(ctx, project):
    """Show what the bot has learned from its actions."""
    from core.database import Database
    from core.learning_engine import LearningEngine

    settings = ctx.obj["settings"]
    db = Database(settings["database"]["path"])
    try:
        engine = LearningEngine(db)
        insights = engine.get_insights(project=project)

        click.echo(click.style("=== Bot Intelligence ===", fg="cyan", bold=True))

        top_subs = insights.get("top_subreddits", [])
        if top_subs:
            click.echo("\nBest-performing subreddits:")
            for s in top_subs:
                bar = "█" * int(s["score"] * 5)
                click.echo(f"  r/{s['name']:20s} {bar} {s['score']:.2f}")
        else:
            click.echo("\nNo performance data yet. Run the bot to start learning!")

        top_kw = insights.get("top_keywords", [])
        if top_kw:
            click.echo("\nBest keywords:")
            for k in top_kw:
                bar = "█" * int(k["score"] * 5)
                click.echo(f"  {k['name']:20s} {bar} {k['score']:.2f}")

        click.echo(f"\nRecommended tone: {insights.get('best_tone', 'helpful_casual')}")
        ratio = insights.get("optimal_promo_ratio", 0.2)
        click.echo(f"Optimal promo/organic: {ratio:.0%} / {1-ratio:.0%}")

        pending = insights.get("pending_discoveries", 0)
        if pending:
            click.echo(click.style(
                f"\n{pending} new subreddits/keywords discovered! "
                f"Run 'python miloagent.py learn' to analyze.",
                fg="yellow",
            ))

        # Post-type performance
        pt_stats = db.get_post_type_stats(project=project, days=30)
        if pt_stats:
            click.echo(click.style("\n=== Post-Type Performance ===", fg="cyan"))
            for pt in pt_stats:
                removal = (pt["removed_count"] or 0) / max(pt["count"], 1)
                bar = "█" * int((pt["avg_engagement"] or 0) * 3)
                click.echo(
                    f"  {pt['post_type']:15s} {bar} "
                    f"eng={pt['avg_engagement']:.2f} "
                    f"(n={pt['count']}, {removal:.0%} removed)"
                )

        # Sentiment analysis
        sentiment = db.get_sentiment_by_tone(project=project, days=30)
        if sentiment:
            click.echo(click.style("\n=== Tone Sentiment (from replies) ===", fg="cyan"))
            for s in sentiment:
                score = s["avg_sentiment"]
                indicator = "+" if score > 0 else "-" if score < 0 else "~"
                style = "green" if score > 0.1 else ("red" if score < -0.1 else "yellow")
                click.echo(click.style(
                    f"  {s['tone_style']:20s} {indicator}{abs(score):.2f} "
                    f"({s['total_replies']} replies analyzed)",
                    fg=style,
                ))

        # A/B Experiments
        from core.ab_testing import ABTestingEngine
        ab = ABTestingEngine(db)
        experiments = ab.get_active_experiments(project)
        if experiments:
            click.echo(click.style("\n=== A/B Experiments ===", fg="cyan"))
            for exp in experiments:
                results = db.get_experiment_results(exp["id"])
                a_data = results.get("a", {})
                b_data = results.get("b", {})
                click.echo(
                    f"  {exp['experiment_name']} [{exp['variable']}]"
                )
                click.echo(
                    f"    A: {exp['variant_a']} "
                    f"(eng={a_data.get('avg_eng', 0):.2f}, "
                    f"n={a_data.get('count', 0)})"
                )
                click.echo(
                    f"    B: {exp['variant_b']} "
                    f"(eng={b_data.get('avg_eng', 0):.2f}, "
                    f"n={b_data.get('count', 0)})"
                )

        # Evolved prompts
        evolutions = db.conn.execute(
            """SELECT template_name, status, performance_after,
                      timestamp FROM prompt_evolution_log
               ORDER BY timestamp DESC LIMIT 5"""
        ).fetchall()
        if evolutions:
            click.echo(click.style("\n=== Prompt Evolutions ===", fg="cyan"))
            for evo in evolutions:
                status_color = "green" if evo["status"] == "active" else "red"
                click.echo(click.style(
                    f"  {evo['template_name']} [{evo['status']}] "
                    f"perf={evo['performance_after']:.2f} "
                    f"({evo['timestamp'][:10]})",
                    fg=status_color,
                ))

        # Show discoveries
        discoveries = db.get_discoveries(project=project, status="candidate")
        if discoveries:
            click.echo(click.style("\n=== Discovered Targets ===", fg="cyan"))
            for d in discoveries[:10]:
                click.echo(
                    f"  [{d['discovery_type']}] {d['value']} "
                    f"(from {d['source']}, score={d['score']:.1f})"
                )
    finally:
        db.close()


# ─── SYSTEM MONITORING & MAINTENANCE ─────────────────────────────


@cli.command(name="system")
@click.argument("action", type=click.Choice(["info", "cleanup", "health", "env"]))
@click.pass_context
def system_cmd(ctx, action):
    """System resource monitoring and maintenance.

    \b
    Actions:
      info    — Show system resource usage (CPU, RAM, disk)
      cleanup — Clean DB, WAL files, temp data, cookies cache
      health  — Full health check (resources + DB + accounts)
      env     — Show detected environment (OS, server, headless, etc.)
    """
    if action == "env":
        from core.environment import detect_environment, get_env_summary
        click.echo(click.style("=== Environment ===", fg="cyan", bold=True))
        click.echo(get_env_summary())
        click.echo("")
        env = detect_environment()
        for k, v in sorted(env.items()):
            click.echo(f"  {k}: {v}")
        return

    from core.resource_monitor import ResourceMonitor
    from core.database import Database

    monitor = ResourceMonitor()
    state = monitor.get_state()

    if action == "info":
        click.echo(click.style("=== System Resources ===", fg="cyan", bold=True))
        click.echo(monitor.get_summary())

    elif action == "cleanup":
        click.echo(click.style("=== Running Cleanup ===", fg="cyan", bold=True))

        # DB cleanup
        settings = ctx.obj["settings"]
        db = Database(settings["database"]["path"])
        try:
            size_before = db.get_db_size_mb()
            db.force_maintenance()
            size_after = db.get_db_size_mb()
            click.echo(
                f"Database: {size_before:.1f}MB -> {size_after:.1f}MB "
                f"(freed {max(0, size_before - size_after):.1f}MB)"
            )
        finally:
            db.close()

        # Clean temp files
        import glob
        temp_patterns = [
            "data/*.tmp",
            "data/cookies/*.bak",
            "__pycache__/**/*.pyc",
        ]
        cleaned = 0
        for pattern in temp_patterns:
            for f in glob.glob(pattern, recursive=True):
                try:
                    os.remove(f)
                    cleaned += 1
                except OSError:
                    pass
        click.echo(f"Temp files cleaned: {cleaned}")

        click.echo(click.style("Cleanup complete!", fg="green"))

    elif action == "health":
        click.echo(click.style("=== System Health ===", fg="cyan", bold=True))
        click.echo(monitor.get_summary())
        click.echo("")

        # DB health
        settings = ctx.obj["settings"]
        db = Database(settings["database"]["path"])
        try:
            db_size = db.get_db_size_mb()
            stats = db.get_stats_summary(hours=24)
            click.echo(click.style("=== Database ===", fg="cyan"))
            click.echo(f"Size: {db_size:.1f}MB")
            click.echo(f"Actions (24h): {stats.get('actions', {})}")
            click.echo(f"Opportunities (24h): {stats.get('opportunities', {})}")
            click.echo(f"Avg score: {stats.get('avg_opportunity_score', 0)}")
        finally:
            db.close()

        click.echo("")

        # Threshold warnings
        warnings = []
        if state.ram_used_percent > 80:
            warnings.append(f"RAM usage high: {state.ram_used_percent:.0f}%")
        if state.disk_used_percent > 90:
            warnings.append(f"Disk usage high: {state.disk_used_percent:.0f}%")
        if db_size > 100:
            warnings.append(f"Database large: {db_size:.0f}MB")

        if warnings:
            click.echo(click.style("=== Warnings ===", fg="yellow"))
            for w in warnings:
                click.echo(click.style(f"  ⚠ {w}", fg="yellow"))
        else:
            click.echo(click.style("All checks passed!", fg="green"))


# ─── HUB MANAGEMENT ──────────────────────────────────────────────


@cli.group()
def hub():
    """Manage owned subreddit hubs (create, list, post)."""
    pass


@hub.command(name="list")
@click.pass_context
def hub_list(ctx):
    """List all registered subreddit hubs."""
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator
    from core.subreddit_hub import SubredditHubManager

    settings = ctx.obj["settings"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    cg = ContentGenerator(llm)
    mgr = SubredditHubManager(db, llm, cg)

    hubs = mgr.get_hubs()
    if not hubs:
        click.echo("No subreddit hubs registered yet.")
        click.echo("Use 'hub register' to add an existing subreddit as a hub.")
        db.close()
        return

    click.echo(f"\n=== {len(hubs)} Hub(s) ===\n")
    for h in hubs:
        click.echo(f"  r/{h['subreddit']} [{h['status']}]")
        click.echo(f"    Project: {h['project']}")
        click.echo(f"    Posts: {h['total_posts']} (organic: {h['organic_posts']}, promo: {h['promo_posts']})")
        click.echo(f"    Created by: u/{h['created_by']}")
        if h.get("last_post_at"):
            click.echo(f"    Last post: {h['last_post_at']}")
        click.echo("")
    db.close()


@hub.command(name="register")
@click.argument("subreddit")
@click.option("--project", "-p", required=True, help="Project name")
@click.option("--account", "-a", required=True, help="Reddit account that mods it")
@click.option("--niche", default="", help="Niche description")
@click.pass_context
def hub_register(ctx, subreddit, project, account, niche):
    """Register an existing subreddit as an owned hub.

    \b
    Example:
      python miloagent.py hub register ProductivityHacks -p my_project -a MyUser
    """
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator
    from core.subreddit_hub import SubredditHubManager

    settings = ctx.obj["settings"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    cg = ContentGenerator(llm)
    mgr = SubredditHubManager(db, llm, cg)

    if mgr.register_hub(subreddit, project.lower(), account, niche=niche):
        click.echo(click.style(f"Registered r/{subreddit} as a hub for {project}", fg="green"))
        click.echo("Milo will now post content to this subreddit on schedule.")
    else:
        click.echo(click.style("Failed to register hub.", fg="red"))
    db.close()


@hub.command(name="suggest")
@click.option("--project", "-p", default=None, help="Project name")
@click.pass_context
def hub_suggest(ctx, project):
    """Suggest subreddit names to create as hubs."""
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator
    from core.subreddit_hub import SubredditHubManager

    settings = ctx.obj["settings"]
    projects = ctx.obj["projects"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    cg = ContentGenerator(llm)
    mgr = SubredditHubManager(db, llm, cg)

    # Pick project
    if project:
        proj = find_project(projects, project)
        if not proj:
            click.echo(click.style(f"Project '{project}' not found.", fg="red"))
            db.close()
            return
    else:
        proj = projects[0] if projects else None

    if not proj:
        click.echo("No projects configured.")
        db.close()
        return

    click.echo(f"Generating hub suggestions for {proj['project']['name']}...")
    suggestions = mgr.suggest_hub_names(proj)

    if not suggestions:
        click.echo("No suggestions generated.")
        db.close()
        return

    click.echo(f"\n=== Suggested Hubs ===\n")
    for i, s in enumerate(suggestions, 1):
        click.echo(f"  {i}. r/{s.get('name', '?')}")
        click.echo(f"     Title: {s.get('title', '-')}")
        click.echo(f"     Niche: {s.get('niche', '-')}")
        click.echo(f"     {s.get('desc', '')}")
        click.echo("")

    click.echo("To create a hub:")
    click.echo("  1. Create the subreddit manually on Reddit")
    click.echo("  2. Run: python miloagent.py hub register <name> -p <project> -a <account>")
    db.close()


@hub.command(name="create")
@click.argument("name")
@click.option("--project", "-p", required=True, help="Project name")
@click.option("--title", "-t", required=True, help="Subreddit display title")
@click.option("--description", "-d", required=True, help="Subreddit description")
@click.pass_context
def hub_create(ctx, name, project, title, description):
    """Create a new subreddit and register it as a hub.

    \b
    Example:
      python miloagent.py hub create MyNicheCommunity -p my_project \\
        -t "My Niche Community" -d "Community for productivity tips"
    """
    from core.database import Database
    from core.llm_provider import LLMProvider
    from core.content_gen import ContentGenerator
    from core.subreddit_hub import SubredditHubManager

    settings = ctx.obj["settings"]
    projects = ctx.obj["projects"]
    db = Database(settings["database"]["path"])
    llm = LLMProvider("config/llm.yaml")
    cg = ContentGenerator(llm)
    mgr = SubredditHubManager(db, llm, cg)

    proj = find_project(projects, project)
    if not proj:
        click.echo(click.style(f"Project '{project}' not found.", fg="red"))
        db.close()
        return

    # Get first Reddit account
    bot = _get_reddit_bot(db, cg)
    if not bot:
        db.close()
        return

    click.echo(f"Creating r/{name}...")
    if mgr.create_subreddit(bot, name, title, description, project.lower()):
        click.echo(click.style(f"Created and registered r/{name} as a hub!", fg="green"))
    else:
        click.echo(click.style("Failed to create subreddit.", fg="red"))
        click.echo("Note: Your account needs 30+ days age and some karma to create subreddits.")
    db.close()


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    cli()
