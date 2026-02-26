"""FastAPI web dashboard V2 for MiloAgent — full TUI parity + CRUD + auth."""

import asyncio
import gc
import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_security = HTTPBearer(auto_error=False)


# ── Log handler for WebSocket broadcast ──────────────────────────

class _WebLogHandler(logging.Handler):
    """Captures log records for WebSocket broadcast (same pattern as TUI)."""

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record):
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = record.getMessage()[:300]

            # Categorize log (same as TUI tui.py:792-809)
            cat = ""
            name_lower = record.name.lower()
            msg_lower = msg.lower()
            if "scan" in name_lower or "scan" in msg_lower:
                cat = "SCAN"
            elif "action" in msg_lower or "acting" in msg_lower or "comment" in msg_lower:
                cat = "ACT"
            elif "learn" in msg_lower:
                cat = "LEARN"
            elif "telegram" in name_lower:
                cat = "TG"
            elif "error" in msg_lower or record.levelno >= logging.ERROR:
                cat = "ERR"
            elif "relationship" in msg_lower or "dm" in msg_lower:
                cat = "REL"
            elif "engage" in msg_lower or "warm" in msg_lower:
                cat = "ENG"
            elif "research" in msg_lower or "intel" in msg_lower:
                cat = "RES"
            elif "presence" in msg_lower:
                cat = "PRES"

            entry = {
                "seq": self._seq,
                "ts": ts,
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
                "cat": cat,
            }
            with self._lock:
                self._seq += 1
                self.records.append(entry)
        except Exception:
            pass

    def get_recent(self, n: int = 50):
        with self._lock:
            return list(self.records)[-n:]

    def get_since(self, seq: int):
        with self._lock:
            return [r for r in self.records if r["seq"] > seq]


# ── Cross-platform RAM helper ────────────────────────────────────

def _get_ram_info() -> dict:
    """Get RAM info on Linux (/proc/meminfo) or macOS (sysctl) or fallback (psutil)."""
    # Linux
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", 0)
        if total_kb:
            return {
                "total_gb": round(total_kb / (1024 * 1024), 1),
                "used_gb": round((total_kb - avail_kb) / (1024 * 1024), 1),
                "available_gb": round(avail_kb / (1024 * 1024), 1),
                "percent": round(((total_kb - avail_kb) / total_kb) * 100, 1),
            }
    except FileNotFoundError:
        pass
    # macOS — sysctl
    try:
        import subprocess
        total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=2).strip())
        # vm_stat for page info
        vm = subprocess.check_output(["vm_stat"], timeout=2).decode()
        page_size = 16384  # default
        for line in vm.splitlines():
            if "page size" in line.lower():
                page_size = int("".join(c for c in line if c.isdigit()) or 16384)
                break
        free = 0
        for line in vm.splitlines():
            if "Pages free" in line:
                free = int("".join(c for c in line.split(":")[1] if c.isdigit()) or 0) * page_size
        used = total - free
        return {
            "total_gb": round(total / (1024**3), 1),
            "used_gb": round(used / (1024**3), 1),
            "available_gb": round(free / (1024**3), 1),
            "percent": round((used / total) * 100, 1) if total else 0,
        }
    except Exception:
        pass
    # Fallback — psutil
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {
            "total_gb": round(mem.total / (1024**3), 1),
            "used_gb": round(mem.used / (1024**3), 1),
            "available_gb": round(mem.available / (1024**3), 1),
            "percent": mem.percent,
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "available_gb": 0, "percent": 0}


# ── Rate limiter ─────────────────────────────────────────────────

class _RateLimiter:
    """Simple in-memory rate limiter per IP."""

    def __init__(self, max_attempts: int = 10, window_seconds: int = 60):
        self._attempts: dict = {}
        self._max = max_attempts
        self._window = window_seconds
        self._lock = threading.Lock()

    def check_and_record(self, ip: str) -> bool:
        """Check if allowed AND record the attempt atomically. Fixes race condition."""
        now = time.time()
        with self._lock:
            attempts = self._attempts.get(ip, [])
            attempts = [t for t in attempts if now - t < self._window]
            allowed = len(attempts) < self._max
            attempts.append(now)
            if attempts:
                self._attempts[ip] = attempts
            # Periodic cleanup: remove IPs with no recent attempts
            if len(self._attempts) > 1000:
                self._attempts = {
                    k: v for k, v in self._attempts.items() if v
                }
            return allowed


# ── Resource history sampler ─────────────────────────────────────

class _ResourceSampler:
    """Samples server resources periodically for time-series charts."""

    def __init__(self, maxlen: int = 360):  # 30min at 5s interval
        self.samples: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def sample(self, cpu_pct: float, ram_pct: float, disk_pct: float):
        with self._lock:
            self.samples.append({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "cpu": round(cpu_pct, 1),
                "ram": round(ram_pct, 1),
                "disk": round(disk_pct, 1),
            })

    def get_all(self):
        with self._lock:
            return list(self.samples)


# ── Models ───────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1, max_length=500)
    project_type: str = "SaaS"
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    tagline: str = ""
    selling_points: list = []
    target_audiences: list = []


class ProjectUpdate(BaseModel):
    enabled: Optional[bool] = None
    weight: Optional[float] = None
    description: Optional[str] = None
    tagline: Optional[str] = None
    url: Optional[str] = None
    reddit_subreddits_primary: Optional[list] = None
    reddit_subreddits_secondary: Optional[list] = None
    reddit_keywords: Optional[list] = None
    twitter_keywords: Optional[list] = None
    twitter_hashtags: Optional[list] = None
    tone_style: Optional[str] = None


class AccountCreate(BaseModel):
    platform: Literal["reddit", "telegram"]
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)
    email: str = ""
    persona: str = "helpful_casual"
    projects: list = []


# ── WebDashboard V2 ─────────────────────────────────────────────

class WebDashboard:
    """FastAPI web dashboard V2 — full TUI parity + CRUD + user/password auth."""

    def __init__(self, orchestrator):
        self.orch = orchestrator
        self.app = FastAPI(title="MiloAgent", docs_url=None, redoc_url=None)

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

        # Auth: username/password
        self._user = os.environ.get("MILO_WEB_USER", "") or "admin"
        self._pass = os.environ.get("MILO_WEB_PASS", "") or self.orch.settings.get("web_dashboard", {}).get("password", "")
        if not self._pass:
            # Fallback to legacy token
            self._pass = os.environ.get("MILO_WEB_TOKEN", "") or self.orch.settings.get("web_dashboard", {}).get("token", "")
        if not self._pass:
            logger.warning("MILO_WEB_PASS not set — using 'milo' as default")
            self._pass = "milo"

        # Session tokens: {token_hex: {user, created_at, ip}}
        self._sessions: Dict[str, dict] = {}
        self._session_ttl = 86400  # 24h

        # Log handler
        self._log_handler = _WebLogHandler()
        logging.getLogger().addHandler(self._log_handler)

        # Rate limiter
        self._login_limiter = _RateLimiter(max_attempts=10, window_seconds=60)

        # WebSocket clients
        self._ws_clients: list = []

        # Emergency stop
        self._emergency_stopped = False

        # Resource sampler for time-series
        self._resource_sampler = _ResourceSampler()
        self._sampler_thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler_thread.start()

        self._setup_routes()
        self._setup_static()
        self._start_time = time.time()

    # ── Resource sampling loop ─────────────────────────────────

    def _sample_loop(self):
        while True:
            try:
                cpu_pct = 0.0
                ram_pct = 0.0
                disk_pct = 0.0
                try:
                    load1 = os.getloadavg()[0]
                    cores = os.cpu_count() or 1
                    cpu_pct = (load1 / cores) * 100
                except Exception:
                    pass
                ram_info = _get_ram_info()
                ram_pct = ram_info.get("percent", 0)
                try:
                    disk = shutil.disk_usage("/")
                    disk_pct = (disk.used / disk.total) * 100
                except Exception:
                    pass
                self._resource_sampler.sample(cpu_pct, ram_pct, disk_pct)
            except Exception:
                pass
            time.sleep(5)

    # ── Auth ──────────────────────────────────────────────────

    def _create_session(self, user: str, ip: str) -> str:
        token = secrets.token_hex(32)
        self._sessions[token] = {
            "user": user,
            "created_at": time.time(),
            "ip": ip,
        }
        # Clean expired sessions
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v["created_at"] > self._session_ttl]
        for k in expired:
            del self._sessions[k]
        return token

    def _validate_session(self, token: str) -> bool:
        session = self._sessions.get(token)
        if not session:
            # Fallback: check legacy token (MILO_WEB_TOKEN)
            legacy = os.environ.get("MILO_WEB_TOKEN", "")
            if legacy and token == legacy:
                return True
            return False
        if time.time() - session["created_at"] > self._session_ttl:
            del self._sessions[token]
            return False
        return True

    async def _verify_token(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ):
        if not credentials or not self._validate_session(credentials.credentials):
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        return True

    # ── Static files ─────────────────────────────────────────

    def _setup_static(self):
        static_dir = Path(__file__).parent / "static"
        if static_dir.exists():
            self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── Routes ───────────────────────────────────────────────

    def _setup_routes(self):
        app = self.app

        # ── HTML entry point ───────────────────────────────
        @app.get("/")
        async def index():
            html = Path(__file__).parent / "static" / "index.html"
            if html.exists():
                return FileResponse(str(html))
            return {"error": "index.html not found"}

        # ── GET /health (no auth — for Docker healthcheck) ─
        @app.get("/health")
        async def health():
            return {"status": "ok", "uptime": int(time.time() - self._start_time)}

        # ── POST /api/auth/login ───────────────────────────
        @app.post("/api/auth/login")
        async def login(body: LoginRequest, request: Request):
            ip = request.client.host if request.client else "unknown"
            if not self._login_limiter.check_and_record(ip):
                raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
            if body.username == self._user and body.password == self._pass:
                token = self._create_session(body.username, ip)
                return {"ok": True, "token": token}
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # ── GET /api/status ────────────────────────────────
        @app.get("/api/status")
        async def get_status(_=Depends(self._verify_token)):
            paused = getattr(self.orch, "_paused", False)
            mode = getattr(self.orch, "_mode", "unknown")
            projects = []
            for p in getattr(self.orch, "projects", []):
                proj = p.get("project", {})
                projects.append({"name": proj.get("name", ""), "enabled": proj.get("enabled", True)})
            return {
                "paused": paused,
                "mode": mode,
                "uptime_seconds": int(time.time() - self._start_time),
                "projects": projects,
                "version": self.orch.settings.get("bot", {}).get("version", "?"),
                "emergency_stopped": self._emergency_stopped,
            }

        # ── GET /api/stats ─────────────────────────────────
        @app.get("/api/stats")
        async def get_stats(_=Depends(self._verify_token)):
            try:
                raw = self.orch.db.get_stats_summary(hours=24)
                actions = raw.get("actions", {})
                by_platform = {}
                by_type = {}
                total = 0
                for plat, types in actions.items():
                    plat_total = sum(types.values())
                    by_platform[plat] = plat_total
                    total += plat_total
                    for t, c in types.items():
                        by_type[t] = by_type.get(t, 0) + c
                return {
                    "total_actions": total,
                    "by_platform": by_platform,
                    "by_type": by_type,
                    "opportunities": raw.get("opportunities", {}),
                    "avg_opportunity_score": raw.get("avg_opportunity_score", 0),
                }
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/actions ───────────────────────────────
        @app.get("/api/actions")
        async def get_actions(limit: int = Query(30, le=200), _=Depends(self._verify_token)):
            try:
                rows = self.orch.db.get_recent_actions(hours=24, limit=limit)
                return [dict(r) for r in (rows or [])]
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/accounts ──────────────────────────────
        @app.get("/api/accounts")
        async def get_accounts(_=Depends(self._verify_token)):
            result = []
            try:
                for platform in ("reddit", "telegram"):
                    accounts = self.orch.account_mgr.load_accounts(platform)
                    for acc in accounts:
                        username = acc["username"]
                        key = f"{platform}:{username}"
                        recent = self.orch.db.get_recent_actions(hours=24, account=username, platform=platform, limit=200)
                        types = {}
                        for a in (recent or []):
                            t = a.get("action_type", "unknown")
                            types[t] = types.get(t, 0) + 1
                        comments = types.get("comment", 0) + types.get("reply", 0)
                        likes = types.get("upvote", 0) + types.get("like", 0)
                        posts = types.get("post", 0) + types.get("tweet", 0) + types.get("seed", 0)
                        result.append({
                            "username": username,
                            "platform": platform,
                            "total_24h": sum(types.values()),
                            "types": types,
                            "comments": comments,
                            "likes": likes,
                            "posts": posts,
                            "status": self.orch.account_mgr._statuses.get(key, "healthy"),
                            "has_cookies": os.path.exists(acc.get("cookies_file", "")),
                            "persona": acc.get("persona", ""),
                            "email": acc.get("email", ""),
                            "enabled": acc.get("enabled", True),
                        })
            except Exception as e:
                logger.debug(f"Accounts error: {e}")
            return result

        # ── GET /api/projects ──────────────────────────────
        @app.get("/api/projects")
        async def get_projects(_=Depends(self._verify_token)):
            result = []
            for p in getattr(self.orch, "projects", []):
                proj = p.get("project", {})
                name = proj.get("name", "")
                try:
                    actions = self.orch.db.get_recent_actions(hours=24, limit=500)
                    count = sum(1 for a in (actions or []) if a.get("project") == name)
                except Exception:
                    count = 0
                result.append({
                    "name": name,
                    "url": proj.get("url", ""),
                    "enabled": proj.get("enabled", True),
                    "weight": proj.get("weight", 1.0),
                    "description": proj.get("description", ""),
                    "type": proj.get("type", ""),
                    "tagline": proj.get("tagline", ""),
                    "actions_24h": count,
                })
            return result

        # ── GET /api/projects/{name} ───────────────────────
        @app.get("/api/projects/{name}")
        async def get_project_detail(name: str, _=Depends(self._verify_token)):
            proj = self.orch.business_mgr.get_project(name)
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            return proj

        # ── POST /api/projects ─────────────────────────────
        @app.post("/api/projects")
        async def create_project(body: ProjectCreate, _=Depends(self._verify_token)):
            try:
                filepath = self.orch.business_mgr.add_project(
                    name=body.name,
                    url=body.url,
                    description=body.description,
                    project_type=body.project_type,
                    weight=body.weight,
                    tagline=body.tagline,
                    selling_points=body.selling_points,
                    target_audiences=body.target_audiences,
                )
                return {"ok": True, "filepath": filepath}
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # ── PUT /api/projects/{name} ───────────────────────
        @app.put("/api/projects/{name}")
        async def update_project(name: str, body: ProjectUpdate, _=Depends(self._verify_token)):
            import yaml
            # Find project file
            projects_dir = self.orch.business_mgr.projects_dir
            for f in projects_dir.glob("*.yaml"):
                try:
                    with open(f) as fh:
                        data = yaml.safe_load(fh) or {}
                    if data.get("project", {}).get("name", "").lower() != name.lower():
                        continue
                    # Apply updates
                    proj = data["project"]
                    if body.enabled is not None:
                        proj["enabled"] = body.enabled
                    if body.weight is not None:
                        proj["weight"] = body.weight
                    if body.description is not None:
                        proj["description"] = body.description
                    if body.tagline is not None:
                        proj["tagline"] = body.tagline
                    if body.url is not None:
                        proj["url"] = body.url
                    if body.tone_style is not None:
                        data.setdefault("tone", {})["style"] = body.tone_style
                    # Reddit config
                    reddit = data.setdefault("reddit", {})
                    subs = reddit.setdefault("target_subreddits", {})
                    if body.reddit_subreddits_primary is not None:
                        subs["primary"] = body.reddit_subreddits_primary
                    if body.reddit_subreddits_secondary is not None:
                        subs["secondary"] = body.reddit_subreddits_secondary
                    if body.reddit_keywords is not None:
                        reddit["keywords"] = body.reddit_keywords
                    # Twitter config
                    twitter = data.setdefault("twitter", {})
                    if body.twitter_keywords is not None:
                        twitter["keywords"] = body.twitter_keywords
                    if body.twitter_hashtags is not None:
                        twitter["hashtags"] = body.twitter_hashtags
                    with open(f, "w") as fh:
                        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)
                    self.orch.business_mgr.reload()
                    return {"ok": True}
                except Exception as e:
                    raise HTTPException(status_code=500, detail=str(e))
            raise HTTPException(status_code=404, detail="Project not found")

        # ── DELETE /api/projects/{name} ────────────────────
        @app.delete("/api/projects/{name}")
        async def delete_project(name: str, _=Depends(self._verify_token)):
            ok = self.orch.business_mgr.delete_project(name)
            if not ok:
                raise HTTPException(status_code=404, detail="Project not found")
            return {"ok": True}

        # ── POST /api/accounts ─────────────────────────────
        @app.post("/api/accounts")
        async def create_account(body: AccountCreate, _=Depends(self._verify_token)):
            msg = self.orch.account_mgr.add_account(
                platform=body.platform,
                username=body.username,
                password=body.password,
                email=body.email,
                persona=body.persona,
                projects=body.projects or None,
            )
            ok = "added" in msg.lower() or "success" in msg.lower()
            return {"ok": ok, "message": msg}

        # ── DELETE /api/accounts/{platform}/{username} ─────
        @app.delete("/api/accounts/{platform}/{username}")
        async def remove_account(platform: str, username: str, _=Depends(self._verify_token)):
            msg = self.orch.account_mgr.remove_account(platform, username)
            ok = "disabled" in msg.lower() or "removed" in msg.lower()
            return {"ok": ok, "message": msg}

        # ── GET /api/insights ──────────────────────────────
        @app.get("/api/insights")
        async def get_insights(_=Depends(self._verify_token)):
            try:
                from core.ab_testing import ABTestingEngine
                from core.learning_engine import LearningEngine
                engine = LearningEngine(self.orch.db)
                insights = engine.get_insights()
                # Aggregate across all projects
                all_pt = []
                all_sent = []
                for proj in getattr(self.orch, "projects", []):
                    pname = proj.get("project", {}).get("name", "")
                    all_pt.extend(self.orch.db.get_post_type_stats(pname, days=30) or [])
                    all_sent.extend(self.orch.db.get_sentiment_by_tone(pname, days=30) or [])
                insights["post_type_stats"] = [dict(r) for r in all_pt]
                insights["sentiment"] = [dict(r) for r in all_sent]
                ab = ABTestingEngine(self.orch.db)
                exps = ab.get_active_experiments()
                exp_list = []
                for exp in (exps or []):
                    results = self.orch.db.get_experiment_results(exp["id"])
                    exp_list.append({
                        "name": exp["experiment_name"],
                        "variable": exp["variable"],
                        "variant_a": exp["variant_a"],
                        "variant_b": exp["variant_b"],
                        "a_eng": results.get("a", {}).get("avg_eng", 0),
                        "b_eng": results.get("b", {}).get("avg_eng", 0),
                        "a_n": results.get("a", {}).get("count", 0),
                        "b_n": results.get("b", {}).get("count", 0),
                    })
                insights["experiments"] = exp_list
                return insights
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/schedule ──────────────────────────────
        @app.get("/api/schedule")
        async def get_schedule(_=Depends(self._verify_token)):
            jobs = []
            try:
                for job in self.orch.scheduler.get_jobs():
                    next_run = job.next_run_time
                    if next_run:
                        now = datetime.now(next_run.tzinfo)
                        secs = max(0, (next_run - now).total_seconds())
                    else:
                        secs = -1
                    interval = ""
                    trigger = getattr(job, "trigger", None)
                    if hasattr(trigger, "interval"):
                        total = int(trigger.interval.total_seconds())
                        interval = f"{total // 3600}h" if total >= 3600 else f"{total // 60}m"
                    elif hasattr(trigger, "fields"):
                        interval = "cron"
                    name = job.name.replace("Orchestrator.", "").replace("_safe", "")
                    jobs.append({
                        "name": name,
                        "next_run": next_run.isoformat() if next_run else None,
                        "seconds_until": int(secs),
                        "interval": interval,
                    })
            except Exception as e:
                logger.debug(f"Schedule error: {e}")
            return sorted(jobs, key=lambda j: j["seconds_until"])

        # ── GET /api/opportunities ─────────────────────────
        @app.get("/api/opportunities")
        async def get_opportunities(limit: int = Query(20, le=100), _=Depends(self._verify_token)):
            try:
                rows = self.orch.db.conn.execute(
                    """SELECT * FROM opportunities
                       WHERE status = 'pending'
                       ORDER BY score DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/opportunities/rejected ──────────────────
        @app.get("/api/opportunities/rejected")
        async def get_rejected_opportunities(
            hours: int = Query(24, le=168),
            limit: int = Query(50, le=200),
            _=Depends(self._verify_token),
        ):
            try:
                return self.orch.db.get_rejected_opportunities(hours=hours, limit=limit)
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/decisions ──────────────────────────────
        @app.get("/api/decisions")
        async def get_recent_decisions(
            hours: int = Query(2, le=24),
            decision_type: str = Query("", description="Filter by type"),
            limit: int = Query(30, le=100),
            _=Depends(self._verify_token),
        ):
            try:
                return self.orch.db.get_recent_decisions(
                    hours=hours, decision_type=decision_type, limit=limit,
                )
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/server ────────────────────────────────
        @app.get("/api/server")
        async def get_server_stats(_=Depends(self._verify_token)):
            result = {}
            # CPU
            try:
                load1, load5, load15 = os.getloadavg()
                cores = os.cpu_count() or 1
                result["cpu"] = {
                    "cores": cores,
                    "load_1m": round(load1, 2),
                    "load_5m": round(load5, 2),
                    "load_15m": round(load15, 2),
                    "usage_pct": round((load1 / cores) * 100, 1),
                }
            except Exception:
                result["cpu"] = {"cores": os.cpu_count() or 1, "usage_pct": 0}
            # RAM (cross-platform)
            result["ram"] = _get_ram_info()
            # Disk
            try:
                disk = shutil.disk_usage("/")
                result["disk"] = {
                    "total_gb": round(disk.total / (1024**3), 1),
                    "used_gb": round(disk.used / (1024**3), 1),
                    "free_gb": round(disk.free / (1024**3), 1),
                    "percent": round((disk.used / disk.total) * 100, 1),
                }
            except Exception:
                result["disk"] = {"total_gb": 0, "percent": 0}
            # Process
            try:
                import resource as _resource
                rusage = _resource.getrusage(_resource.RUSAGE_SELF)
                rss_mb = rusage.ru_maxrss / (1024 * 1024) if os.uname().sysname == "Darwin" else rusage.ru_maxrss / 1024
                result["process"] = {
                    "pid": os.getpid(),
                    "rss_mb": round(rss_mb, 1),
                    "uptime_seconds": int(time.time() - self._start_time),
                    "threads": threading.active_count(),
                }
            except Exception:
                result["process"] = {"pid": os.getpid(), "rss_mb": 0}
            # DB size
            try:
                db_path = Path("data/miloagent.db")
                if db_path.exists():
                    result["database"] = {"size_mb": round(db_path.stat().st_size / (1024 * 1024), 1)}
                    wal_path = Path("data/miloagent.db-wal")
                    if wal_path.exists():
                        result["database"]["wal_mb"] = round(wal_path.stat().st_size / (1024 * 1024), 1)
            except Exception:
                pass
            # Resource history for charts
            result["history"] = self._resource_sampler.get_all()
            return result

        # ── GET /api/brain ─────────────────────────────────
        @app.get("/api/brain")
        async def get_brain(_=Depends(self._verify_token)):
            """Agent intelligence data (mirrors TUI brain panel)."""
            result: Dict[str, Any] = {}
            try:
                # Learning insights
                try:
                    insights = self.orch.learning.get_insights()
                    result["top_subreddits"] = insights.get("top_subreddits", [])
                    result["promo_ratio"] = insights.get("optimal_promo_ratio", 0.25)
                    result["best_tone"] = insights.get("best_tone", "N/A")
                    result["discoveries"] = insights.get("pending_discoveries", 0)
                except Exception:
                    result["top_subreddits"] = []
                    result["promo_ratio"] = 0.25
                    result["best_tone"] = "N/A"
                    result["discoveries"] = 0

                # Post-type performers
                try:
                    pt_parts = []
                    for proj in self.orch.projects:
                        pname = proj.get("project", {}).get("name", "")
                        pt_stats = self.orch.db.get_post_type_stats(pname, days=30)
                        for pt in (pt_stats or [])[:3]:
                            pt_parts.append({"type": pt["post_type"], "avg_eng": round(pt["avg_engagement"], 1)})
                    result["post_type_top"] = pt_parts[:5]
                except Exception:
                    result["post_type_top"] = []

                # Sentiment
                try:
                    all_sent = []
                    for proj in self.orch.projects:
                        pname = proj.get("project", {}).get("name", "")
                        sent = self.orch.db.get_sentiment_by_tone(pname, days=30)
                        all_sent.extend(sent or [])
                    if all_sent:
                        avg = sum(s["avg_sentiment"] for s in all_sent) / len(all_sent)
                        total_r = sum(s["total_replies"] for s in all_sent)
                        result["sentiment"] = {"avg": round(avg, 2), "total_replies": total_r}
                    else:
                        result["sentiment"] = {"avg": 0, "total_replies": 0}
                except Exception:
                    result["sentiment"] = {"avg": 0, "total_replies": 0}

                # A/B tests
                try:
                    ab_parts = []
                    for proj in self.orch.projects:
                        pname = proj.get("project", {}).get("name", "")
                        exps = self.orch.ab_testing.get_active_experiments(pname)
                        for exp in (exps or []):
                            results_data = self.orch.db.get_experiment_results(exp["id"])
                            ab_parts.append({
                                "variable": exp["variable"],
                                "variant_a": exp.get("variant_a", ""),
                                "variant_b": exp.get("variant_b", ""),
                                "a_n": results_data.get("a", {}).get("count", 0),
                                "b_n": results_data.get("b", {}).get("count", 0),
                                "a_eng": round(results_data.get("a", {}).get("avg_eng", 0), 1),
                                "b_eng": round(results_data.get("b", {}).get("avg_eng", 0), 1),
                            })
                    result["ab_tests"] = ab_parts
                except Exception:
                    result["ab_tests"] = []

                # Evolved prompts
                try:
                    evo_count = self.orch.db.conn.execute(
                        "SELECT COUNT(*) as c FROM prompt_evolution_log WHERE status='active'"
                    ).fetchone()["c"]
                    result["evolved_prompts"] = evo_count
                except Exception:
                    result["evolved_prompts"] = 0

                # LLM stats
                try:
                    llm_stats = self.orch.llm.get_stats()
                    groq = llm_stats.get("groq_rate", {})
                    routing = llm_stats.get("routing", {})
                    result["llm_stats"] = {
                        "total_calls": llm_stats.get("total_calls", 0),
                        "total_errors": llm_stats.get("total_errors", 0),
                        "groq_rpd": groq.get("day", 0),
                        "groq_limit": groq.get("day_limit", 14400),
                        "disabled_providers": llm_stats.get("disabled_providers", {}),
                        "creative_chain": " > ".join(routing.get("creative", [])),
                    }
                except Exception:
                    result["llm_stats"] = {}

                # Relationships
                try:
                    total_rels = 0
                    friends = 0
                    for proj in self.orch.projects:
                        pname = proj.get("project", {}).get("name", "")
                        rel_stats = self.orch.db.get_relationship_stats(pname)
                        total_rels += sum(rel_stats.values())
                        friends += rel_stats.get("friend", 0) + rel_stats.get("advocate", 0)
                    result["relationships"] = {"total": total_rels, "friends": friends}
                except Exception:
                    result["relationships"] = {"total": 0, "friends": 0}

                # Resources
                try:
                    state = self.orch.resource_monitor.get_state()
                    result["resources"] = {
                        "ram_pct": round(state.ram_used_percent, 1),
                        "rss_mb": round(state.process_rss_mb, 1),
                        "disk_pct": round(state.disk_used_percent, 1),
                    }
                except Exception:
                    result["resources"] = {}

            except Exception as e:
                result["error"] = str(e)
            return result

        # ── GET /api/performance ───────────────────────────
        @app.get("/api/performance")
        async def get_performance(_=Depends(self._verify_token)):
            """Performance scoring (mirrors TUI performance panel)."""
            try:
                raw = self.orch.db.get_stats_summary(hours=24)
                actions = raw.get("actions", {})
                total_actions = sum(sum(t.values()) for t in actions.values())

                max_expected = self.orch.settings.get("bot", {}).get("max_actions_per_hour", 18) * 24
                activity_score = min(40, (total_actions / max(max_expected, 1)) * 40)

                r_actions = sum(actions.get("reddit", {}).values())
                tg_actions = sum(actions.get("telegram", {}).values())
                total_plat = r_actions + tg_actions
                balance_score = ((1.0 - abs(r_actions - tg_actions) / total_plat) * 20) if total_plat > 0 else 0

                # Account usage
                acc_active = 0
                acc_total = 0
                for platform in ("reddit", "telegram"):
                    accs = self.orch.account_mgr.load_accounts(platform)
                    for acc in accs:
                        acc_total += 1
                        recent = self.orch.db.get_recent_actions(hours=24, account=acc["username"], platform=platform, limit=1)
                        if recent:
                            acc_active += 1
                usage_score = (acc_active / max(acc_total, 1)) * 20

                all_types = set()
                for plat_types in actions.values():
                    all_types.update(plat_types.keys())
                diversity_score = min(20, len(all_types) * 4)

                total_score = activity_score + balance_score + usage_score + diversity_score
                grade = "A+" if total_score >= 90 else "A" if total_score >= 80 else "B" if total_score >= 70 else "C" if total_score >= 60 else "D" if total_score >= 50 else "F"

                improvements = []
                if activity_score < 20:
                    improvements.append("Low activity")
                if balance_score < 10:
                    improvements.append("Platform imbalance")
                if usage_score < 15:
                    improvements.append(f"Only {acc_active}/{acc_total} accounts active")
                if diversity_score < 12:
                    improvements.append("Need more action types")

                return {
                    "score": round(total_score, 1),
                    "grade": grade,
                    "components": {
                        "activity": round(activity_score, 1),
                        "balance": round(balance_score, 1),
                        "accounts": round(usage_score, 1),
                        "diversity": round(diversity_score, 1),
                    },
                    "max_per_component": {"activity": 40, "balance": 20, "accounts": 20, "diversity": 20},
                    "improvements": improvements,
                    "total_actions": total_actions,
                }
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/minimaps ──────────────────────────────
        @app.get("/api/minimaps")
        async def get_minimaps(_=Depends(self._verify_token)):
            """Reddit + Telegram activity minimaps."""
            result: Dict[str, Any] = {"reddit": [], "telegram": {"groups": []}}
            try:
                # Reddit minimap
                recent_reddit = self.orch.db.get_recent_actions(hours=24, platform="reddit", limit=200)
                sub_counts: Dict[str, int] = {}
                for a in (recent_reddit or []):
                    meta = a.get("metadata", "")
                    if isinstance(meta, str) and meta:
                        try:
                            m = json.loads(meta)
                            sub = m.get("subreddit", "")
                            if sub:
                                sub_counts[sub] = sub_counts.get(sub, 0) + 1
                        except Exception:
                            pass
                # Add zero-activity subs from config
                for proj in self.orch.projects[:1]:
                    subs_cfg = proj.get("reddit", {}).get("target_subreddits", {})
                    if isinstance(subs_cfg, dict):
                        all_subs = subs_cfg.get("primary", []) + subs_cfg.get("secondary", [])
                    else:
                        all_subs = subs_cfg if isinstance(subs_cfg, list) else []
                    for s in all_subs:
                        if s not in sub_counts:
                            sub_counts[s] = 0
                # Get presence stages
                for sub_name, count in sorted(sub_counts.items(), key=lambda x: -x[1]):
                    stage = ""
                    try:
                        for proj in self.orch.projects[:1]:
                            pname = proj.get("project", {}).get("name", "")
                            presence = self.orch.db.get_community_presence(pname)
                            for p in (presence or []):
                                if p.get("subreddit") == sub_name:
                                    stage = p.get("stage", "new")
                                    break
                    except Exception:
                        pass
                    result["reddit"].append({
                        "subreddit": sub_name,
                        "count_24h": count,
                        "stage": stage or "new",
                        "activity_level": min(5, count),
                    })

                # Telegram minimap
                recent_telegram = self.orch.db.get_recent_actions(hours=24, platform="telegram", limit=200)
                tg_group_counts: Dict[str, int] = {}
                for a in (recent_telegram or []):
                    meta = a.get("metadata", "")
                    group = "Unknown"
                    if isinstance(meta, str) and meta:
                        try:
                            m = json.loads(meta)
                            group = m.get("group_name", m.get("group", "Unknown"))
                        except Exception:
                            pass
                    tg_group_counts[group] = tg_group_counts.get(group, 0) + 1
                for gname, count in sorted(tg_group_counts.items(), key=lambda x: -x[1]):
                    result["telegram"]["groups"].append({"name": gname, "count": count})
            except Exception as e:
                logger.debug(f"Minimaps error: {e}")
            return result

        # ── GET /api/conversations ─────────────────────────
        @app.get("/api/conversations")
        async def get_conversations(limit: int = Query(30, le=100), _=Depends(self._verify_token)):
            """DM conversations + Telegram alerts."""
            result: Dict[str, Any] = {"dms": [], "alerts": []}
            try:
                convos = self.orch.db.conn.execute(
                    """SELECT c.timestamp, c.direction, r.username, r.platform, c.content
                       FROM conversations c
                       JOIN relationships r ON c.relationship_id = r.id
                       ORDER BY c.timestamp DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                for row in convos:
                    result["dms"].append({
                        "timestamp": row["timestamp"],
                        "direction": row["direction"],
                        "username": row["username"],
                        "platform": row["platform"],
                        "content": (row["content"] or "")[:200],
                    })
            except Exception:
                pass
            try:
                alerts = list(getattr(self.orch, "_alert_log", []))[-30:]
                for ts_iso, msg in reversed(alerts):
                    result["alerts"].append({"timestamp": ts_iso, "message": msg[:200]})
            except Exception:
                pass
            return result

        # ── GET /api/history ───────────────────────────────
        @app.get("/api/history")
        async def get_history(hours: int = Query(168, le=720), _=Depends(self._verify_token)):
            """Action history aggregated by hour for timeline charts."""
            try:
                since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
                rows = self.orch.db.conn.execute(
                    """SELECT strftime('%Y-%m-%dT%H:00', timestamp) as hour,
                              platform, COUNT(*) as count
                       FROM actions
                       WHERE timestamp > ? AND success = 1
                       GROUP BY hour, platform
                       ORDER BY hour""",
                    (since,),
                ).fetchall()
                hourly: Dict[str, Dict[str, int]] = {}
                for row in rows:
                    h = row["hour"]
                    if h not in hourly:
                        hourly[h] = {"reddit": 0, "telegram": 0}
                    hourly[h][row["platform"]] = row["count"]
                result = [{"hour": h, **v} for h, v in sorted(hourly.items())]
                # Daily aggregation
                daily: Dict[str, int] = {}
                for h, v in hourly.items():
                    day = h[:10]
                    daily[day] = daily.get(day, 0) + sum(v.values())
                daily_list = [{"date": d, "total": t} for d, t in sorted(daily.items())]
                return {"hourly": result, "daily": daily_list}
            except Exception as e:
                return {"error": str(e)}

        # ── POST /api/control/* ────────────────────────────
        @app.post("/api/control/scan")
        async def control_scan(_=Depends(self._verify_token)):
            if self._emergency_stopped:
                return {"ok": False, "error": "Emergency stop active"}
            threading.Thread(target=self.orch._scan_all_safe, daemon=True).start()
            return {"ok": True, "message": "Scan started"}

        @app.post("/api/control/learn")
        async def control_learn(_=Depends(self._verify_token)):
            if self._emergency_stopped:
                return {"ok": False, "error": "Emergency stop active"}
            threading.Thread(target=self.orch._learn, daemon=True).start()
            return {"ok": True, "message": "Learning started"}

        @app.post("/api/control/pause")
        async def control_pause(_=Depends(self._verify_token)):
            self.orch._paused = True
            return {"ok": True, "paused": True}

        @app.post("/api/control/resume")
        async def control_resume(_=Depends(self._verify_token)):
            if self._emergency_stopped:
                return {"ok": False, "error": "Emergency stop active — use emergency-reset first"}
            self.orch._paused = False
            return {"ok": True, "paused": False}

        @app.post("/api/control/emergency-stop")
        async def emergency_stop(_=Depends(self._verify_token)):
            self._emergency_stopped = True
            self.orch._paused = True
            logger.critical("EMERGENCY STOP triggered via web dashboard")
            try:
                self.orch.scheduler.pause()
            except Exception:
                pass
            gc.collect()
            return {"ok": True, "message": "Emergency stop — all jobs paused, scheduler frozen"}

        @app.post("/api/control/emergency-reset")
        async def emergency_reset(_=Depends(self._verify_token)):
            self._emergency_stopped = False
            self.orch._paused = False
            logger.warning("Emergency stop RESET via web dashboard")
            try:
                self.orch.scheduler.resume()
            except Exception:
                pass
            return {"ok": True, "message": "Emergency reset — bot resuming"}

        @app.post("/api/control/act")
        async def control_act(_=Depends(self._verify_token)):
            """Act on the best pending opportunity."""
            if self._emergency_stopped:
                return {"ok": False, "error": "Emergency stop active"}
            threading.Thread(target=self.orch._act_on_best_safe, daemon=True).start()
            return {"ok": True, "message": "Act cycle started"}

        @app.post("/api/control/engage")
        async def control_engage(_=Depends(self._verify_token)):
            """Run organic engagement (upvotes, likes, follows)."""
            if self._emergency_stopped:
                return {"ok": False, "error": "Emergency stop active"}
            threading.Thread(target=self.orch._engage_safe, daemon=True).start()
            return {"ok": True, "message": "Engagement started"}

        @app.post("/api/control/auto-improve")
        async def control_auto_improve(_=Depends(self._verify_token)):
            """Run auto-improvement cycle."""
            if self._emergency_stopped:
                return {"ok": False, "error": "Emergency stop active"}
            threading.Thread(target=self.orch._auto_improve_safe, daemon=True).start()
            return {"ok": True, "message": "Auto-improve started"}

        @app.post("/api/control/reload-config")
        async def control_reload_config(_=Depends(self._verify_token)):
            """Reload projects and accounts config from YAML files."""
            try:
                self.orch.business_mgr.reload()
                for platform in ("reddit", "telegram"):
                    self.orch.account_mgr.load_accounts(platform)
                return {"ok": True, "message": "Config reloaded"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        @app.post("/api/control/cleanup")
        async def control_cleanup(_=Depends(self._verify_token)):
            try:
                self.orch.db.force_maintenance()
                gc.collect()
                return {"ok": True, "message": "Cleanup + GC done"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # ── GET /api/cookies — cookie status for all accounts ─
        @app.get("/api/cookies")
        async def get_cookies_status(_=Depends(self._verify_token)):
            """Get cookie status for all accounts."""
            result = []
            for platform in ("reddit",):
                accounts = self.orch.account_mgr.load_accounts(platform)
                for acc in accounts:
                    cookies_file = acc.get("cookies_file", "")
                    has_cookies = os.path.exists(cookies_file) if cookies_file else False
                    cookie_info = {"platform": platform, "username": acc["username"],
                                   "cookies_file": cookies_file, "has_cookies": has_cookies}
                    if has_cookies:
                        try:
                            stat = os.stat(cookies_file)
                            cookie_info["size_kb"] = round(stat.st_size / 1024, 1)
                            cookie_info["modified"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                            with open(cookies_file) as f:
                                data = json.load(f)
                            if isinstance(data, dict):
                                cookie_info["key_cookies"] = [k for k in ("auth_token", "ct0", "twid", "reddit_session") if k in data]
                                cookie_info["count"] = len(data)
                            elif isinstance(data, list):
                                names = {c.get("name", "") for c in data}
                                cookie_info["key_cookies"] = [k for k in ("auth_token", "ct0", "twid", "reddit_session") if k in names]
                                cookie_info["count"] = len(data)
                        except Exception:
                            pass
                    result.append(cookie_info)
            return result

        # ── POST /api/cookies/paste — paste cookies from browser ─
        @app.post("/api/cookies/paste")
        async def paste_cookies(body: dict, _=Depends(self._verify_token)):
            """Paste cookies from browser console (document.cookie output)."""
            platform = body.get("platform", "")
            username = body.get("username", "")
            raw_cookies = body.get("cookies", "")
            if not platform or not username or not raw_cookies:
                raise HTTPException(status_code=400, detail="platform, username and cookies required")
            # Find account
            accounts = self.orch.account_mgr.load_accounts(platform)
            target = None
            for acc in accounts:
                if acc["username"].lower() == username.lower():
                    target = acc
                    break
            if not target:
                raise HTTPException(status_code=404, detail=f"Account @{username} not found for {platform}")
            cookies_file = target.get("cookies_file", f"data/cookies/{platform}_{username}.json")
            # Parse "name=value; name2=value2" format
            raw = raw_cookies.strip()
            if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
                raw = raw[1:-1]
            cookie_dict = {}
            for pair in raw.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    cookie_dict[name.strip()] = value.strip()
            if not cookie_dict:
                raise HTTPException(status_code=400, detail="No cookies parsed from input")
            # Save
            os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
            if platform == "twitter":
                # Save as list format for Twikit compatibility
                twikit_list = [{"name": k, "value": v, "domain": ".x.com", "path": "/"} for k, v in cookie_dict.items()]
                with open(cookies_file, "w") as f:
                    json.dump(twikit_list, f, indent=2)
            else:
                with open(cookies_file, "w") as f:
                    json.dump(cookie_dict, f, indent=2)
            # Check key cookies
            important = {"reddit": ["reddit_session"], "twitter": ["auth_token", "ct0", "twid"]}
            found = [k for k in important.get(platform, []) if k in cookie_dict]
            missing = [k for k in important.get(platform, []) if k not in cookie_dict]
            return {
                "ok": True,
                "message": f"Saved {len(cookie_dict)} cookies to {cookies_file}",
                "key_cookies_found": found,
                "key_cookies_missing": missing,
                "total": len(cookie_dict),
            }

        # ── DELETE /api/cookies — delete cookies for an account ─
        @app.delete("/api/cookies/{platform}/{username}")
        async def delete_cookies(platform: str, username: str, _=Depends(self._verify_token)):
            """Delete cookie file for an account (forces re-login)."""
            accounts = self.orch.account_mgr.load_accounts(platform)
            for acc in accounts:
                if acc["username"].lower() == username.lower():
                    cookies_file = acc.get("cookies_file", "")
                    if cookies_file and os.path.exists(cookies_file):
                        os.remove(cookies_file)
                        return {"ok": True, "message": f"Cookies deleted for @{username}"}
                    return {"ok": False, "error": "No cookie file found"}
            raise HTTPException(status_code=404, detail="Account not found")

        # ── GET /api/actions/search — filtered action search ─
        @app.get("/api/actions/search")
        async def search_actions(
            platform: Optional[str] = Query(None),
            account: Optional[str] = Query(None),
            project: Optional[str] = Query(None),
            action_type: Optional[str] = Query(None),
            hours: int = Query(24, le=168),
            limit: int = Query(50, le=500),
            _=Depends(self._verify_token),
        ):
            """Search actions with filters."""
            try:
                rows = self.orch.db.get_recent_actions(
                    hours=hours, limit=limit,
                    account=account, platform=platform,
                )
                results = [dict(r) for r in (rows or [])]
                if project:
                    results = [r for r in results if r.get("project") == project]
                if action_type:
                    results = [r for r in results if r.get("action_type") == action_type]
                return results
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/export/actions — CSV export ─────────
        @app.get("/api/export/actions")
        async def export_actions_csv(
            hours: int = Query(24, le=720),
            platform: Optional[str] = Query(None),
            _=Depends(self._verify_token),
        ):
            """Export actions as CSV."""
            from fastapi.responses import StreamingResponse
            import csv
            import io

            try:
                rows = self.orch.db.get_recent_actions(hours=hours, limit=10000, platform=platform)
                actions = [dict(r) for r in (rows or [])]

                output = io.StringIO()
                if actions:
                    writer = csv.DictWriter(output, fieldnames=actions[0].keys())
                    writer.writeheader()
                    writer.writerows(actions)

                output.seek(0)
                return StreamingResponse(
                    iter([output.getvalue()]),
                    media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=milo_actions_{hours}h.csv"},
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # ── GET /api/export/opportunities — CSV export ───
        @app.get("/api/export/opportunities")
        async def export_opportunities_csv(
            status: str = Query("pending"),
            _=Depends(self._verify_token),
        ):
            """Export opportunities as CSV."""
            from fastapi.responses import StreamingResponse
            import csv
            import io

            try:
                if status == "pending":
                    rows = self.orch.db.get_pending_opportunities(limit=5000)
                else:
                    rows = self.orch.db.conn.execute(
                        "SELECT * FROM opportunities WHERE status = ? ORDER BY timestamp DESC LIMIT 5000",
                        (status,),
                    ).fetchall()
                    rows = [dict(r) for r in rows]

                output = io.StringIO()
                if rows:
                    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)

                output.seek(0)
                return StreamingResponse(
                    iter([output.getvalue()]),
                    media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=milo_opportunities_{status}.csv"},
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # ── GET /api/settings — view current settings ────
        @app.get("/api/settings")
        async def get_settings(_=Depends(self._verify_token)):
            """Return current settings (safe subset)."""
            try:
                settings = self.orch.settings
                return {
                    "scan_interval_minutes": settings.get("scan_interval_minutes", 8),
                    "action_interval_minutes": settings.get("action_interval_minutes", 2),
                    "active_hours": settings.get("active_hours", {}),
                    "rate_limits": settings.get("rate_limits", {}),
                    "safety": settings.get("safety", {}),
                    "http": {
                        "proxy": bool(settings.get("http", {}).get("proxy")),
                        "reddit_proxy": bool(settings.get("http", {}).get("reddit_proxy")),
                    },
                    "promotion_rate": settings.get("promotion_rate", 0.3),
                    "llm_providers": [
                        p.get("name", "?") for p in settings.get("llm_providers", [])
                    ],
                }
            except Exception as e:
                return {"error": str(e)}

        # ── PUT /api/settings — update settings ──────────
        @app.put("/api/settings")
        async def update_settings(request: Request, _=Depends(self._verify_token)):
            """Update settings (limited keys)."""
            try:
                import yaml
                body = await request.json()
                allowed_keys = {
                    "scan_interval_minutes", "action_interval_minutes",
                    "promotion_rate", "active_hours", "rate_limits",
                }
                settings_path = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    "config", "settings.yaml",
                )
                with open(settings_path) as f:
                    current = yaml.safe_load(f) or {}
                changed = []
                for key, value in body.items():
                    if key in allowed_keys:
                        current[key] = value
                        changed.append(key)
                if changed:
                    with open(settings_path, "w") as f:
                        yaml.dump(current, f, default_flow_style=False, sort_keys=False)
                    # Hot-reload settings
                    self.orch.settings = current
                    return {"ok": True, "changed": changed}
                return {"ok": False, "error": "No valid keys provided"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # ── GET /api/accounts/{platform}/{username}/health — detail ─
        @app.get("/api/accounts/{platform}/{username}/health")
        async def get_account_health(platform: str, username: str, _=Depends(self._verify_token)):
            """Detailed health info for one account."""
            try:
                key = f"{platform}:{username}"
                status = self.orch.account_mgr._statuses.get(key, "healthy")
                recent = self.orch.db.get_recent_actions(
                    hours=24, account=username, platform=platform, limit=100,
                )
                actions = [dict(r) for r in (recent or [])]
                types = {}
                for a in actions:
                    t = a.get("action_type", "unknown")
                    types[t] = types.get(t, 0) + 1

                successes = sum(1 for a in actions if a.get("success", 1))
                failures = len(actions) - successes
                success_rate = successes / max(len(actions), 1)

                # Check cookie freshness
                accounts = self.orch.account_mgr.load_accounts(platform)
                cookie_file = ""
                for acc in accounts:
                    if acc["username"].lower() == username.lower():
                        cookie_file = acc.get("cookies_file", "")
                        break
                cookie_age_hours = None
                if cookie_file and os.path.exists(cookie_file):
                    mtime = os.path.getmtime(cookie_file)
                    cookie_age_hours = round((time.time() - mtime) / 3600, 1)

                return {
                    "username": username,
                    "platform": platform,
                    "status": status,
                    "actions_24h": len(actions),
                    "action_types": types,
                    "success_rate": round(success_rate, 2),
                    "failures_24h": failures,
                    "cookie_age_hours": cookie_age_hours,
                    "write_disabled": False,
                }
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/summary — high-level dashboard summary ─
        @app.get("/api/summary")
        async def get_summary(_=Depends(self._verify_token)):
            """Quick summary for dashboard header."""
            try:
                actions_24h = self.orch.db.get_recent_actions(hours=24, limit=10000)
                actions = [dict(r) for r in (actions_24h or [])]
                pending = self.orch.db.get_pending_opportunities(limit=1)

                by_platform = {}
                by_project = {}
                successes = 0
                for a in actions:
                    p = a.get("platform", "?")
                    proj = a.get("project", "?")
                    by_platform[p] = by_platform.get(p, 0) + 1
                    by_project[proj] = by_project.get(proj, 0) + 1
                    if a.get("success", 1):
                        successes += 1

                return {
                    "total_actions_24h": len(actions),
                    "success_rate": round(successes / max(len(actions), 1), 2),
                    "by_platform": by_platform,
                    "by_project": by_project,
                    "pending_opportunities": len(pending),
                    "paused": getattr(self.orch, "_paused", False),
                    "uptime_seconds": int(time.time() - self._start_time),
                }
            except Exception as e:
                return {"error": str(e)}

        # ── GET /api/accounts/reddit/performance — all Reddit accounts perf ──
        @app.get("/api/accounts/reddit/performance")
        async def get_reddit_accounts_performance(_=Depends(self._verify_token)):
            """Per-account Reddit performance overview."""
            result = []
            try:
                accounts = self.orch.account_mgr.load_accounts("reddit")
                for acc in accounts:
                    username = acc["username"]
                    if not acc.get("enabled", True):
                        continue
                    key = f"reddit:{username}"
                    status = self.orch.account_mgr._statuses.get(key, "healthy")

                    # Actions breakdown (24h)
                    recent = self.orch.db.get_recent_actions(
                        hours=24, account=username, platform="reddit", limit=500,
                    )
                    actions = [dict(r) for r in (recent or [])]
                    types = {}
                    subreddits_acted = set()
                    for a in actions:
                        t = a.get("action_type", "unknown")
                        types[t] = types.get(t, 0) + 1
                        meta = a.get("metadata", "")
                        if isinstance(meta, str) and meta:
                            try:
                                m = json.loads(meta)
                                sub = m.get("subreddit", "")
                                if sub:
                                    subreddits_acted.add(sub)
                            except Exception:
                                pass

                    successes = sum(1 for a in actions if a.get("success", 1))
                    failures = len(actions) - successes

                    # Actions last 4h (rotation window)
                    recent_4h = self.orch.db.get_action_count(
                        hours=4, account=username, platform="reddit"
                    )

                    # Cookie freshness
                    cookie_file = acc.get("cookies_file", "")
                    cookie_age_hours = None
                    has_session = False
                    if cookie_file and os.path.exists(cookie_file):
                        mtime = os.path.getmtime(cookie_file)
                        cookie_age_hours = round((time.time() - mtime) / 3600, 1)
                        try:
                            with open(cookie_file) as f:
                                cdata = json.load(f)
                            has_session = "reddit_session" in (
                                cdata if isinstance(cdata, dict) else
                                {c.get("name", "") for c in cdata} if isinstance(cdata, list) else set()
                            )
                        except Exception:
                            pass

                    # Cooldown info
                    cd = self.orch.account_mgr._cooldowns.get(key)
                    cooldown_remaining = 0
                    if cd and cd > datetime.now():
                        cooldown_remaining = int((cd - datetime.now()).total_seconds())

                    result.append({
                        "username": username,
                        "persona": acc.get("persona", "default"),
                        "assigned_projects": acc.get("assigned_projects", []),
                        "status": status,
                        "total_24h": len(actions),
                        "total_4h": recent_4h,
                        "action_types": types,
                        "comments": types.get("comment", 0) + types.get("reply", 0),
                        "posts": types.get("post", 0) + types.get("seed_post", 0),
                        "upvotes": types.get("upvote", 0),
                        "subscribes": types.get("subscribe", 0),
                        "successes": successes,
                        "failures": failures,
                        "success_rate": round(successes / max(len(actions), 1), 2),
                        "subreddits_active": sorted(subreddits_acted),
                        "subreddits_count": len(subreddits_acted),
                        "has_cookies": bool(cookie_file and os.path.exists(cookie_file)),
                        "has_reddit_session": has_session,
                        "cookie_age_hours": cookie_age_hours,
                        "cooldown_remaining": cooldown_remaining,
                    })
            except Exception as e:
                logger.error(f"Reddit performance error: {e}")
            return result

        # ── Community Management Endpoints ────────────────

        @app.get("/api/communities")
        async def get_communities(_=Depends(self._verify_token)):
            """Get all owned/managed communities with status."""
            try:
                communities = self.orch.community_manager.get_all_managed_communities()
                return {"communities": communities, "count": len(communities)}
            except Exception as e:
                return {"error": str(e), "communities": []}

        @app.get("/api/communities/{subreddit}")
        async def get_community_detail(subreddit: str, _=Depends(self._verify_token)):
            """Get detailed info for a specific community."""
            try:
                hub = self.orch.hub_manager.get_hub(subreddit)
                if not hub:
                    return {"error": f"Community r/{subreddit} not found"}
                setup_status = self.orch.community_manager.get_setup_status(subreddit)
                return {"hub": hub, "setup_status": setup_status}
            except Exception as e:
                return {"error": str(e)}

        @app.get("/api/takeover/targets")
        async def get_takeover_targets(_=Depends(self._verify_token)):
            """Get recent takeover target scans."""
            try:
                # Return cached results from last scan
                rows = self.orch.db.conn.execute(
                    """SELECT * FROM subreddit_requests
                       ORDER BY submitted_at DESC LIMIT 20"""
                ).fetchall()
                return {"targets": [dict(r) for r in rows]}
            except Exception as e:
                return {"error": str(e), "targets": []}

        @app.get("/api/takeover/requests")
        async def get_takeover_requests(_=Depends(self._verify_token)):
            """Get pending r/redditrequest submissions."""
            try:
                requests = self.orch.community_manager.get_pending_requests()
                return {"requests": requests, "count": len(requests)}
            except Exception as e:
                return {"error": str(e), "requests": []}

        # ── WebSocket /ws/logs ─────────────────────────────
        @app.websocket("/ws/logs")
        async def ws_logs(ws: WebSocket, token: str = Query("")):
            if not self._validate_session(token):
                await ws.close(code=4001)
                return
            await ws.accept()
            self._ws_clients.append(ws)
            last_seq = 0
            try:
                for record in self._log_handler.get_recent(50):
                    await ws.send_json(record)
                    last_seq = record["seq"]
                while True:
                    await asyncio.sleep(0.5)
                    new = self._log_handler.get_since(last_seq)
                    for record in new:
                        await ws.send_json(record)
                        last_seq = record["seq"]
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                if ws in self._ws_clients:
                    self._ws_clients.remove(ws)
