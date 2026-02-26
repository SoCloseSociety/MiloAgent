"""LLM provider abstraction — parallel dual-LLM architecture.

Groq (cloud, fast, high-quality) + Ollama (local, free, unlimited) running
in parallel.  Groq handles creative tasks (comment/DM/tweet generation),
Ollama handles analytical tasks (research, failure analysis, discovery).

When one provider is rate-limited or down, the other takes over automatically.
The only real bottleneck is the Mac hardware — not API quotas.
"""

import logging
import threading
import time
import concurrent.futures
from collections import deque
from typing import Optional, List, Dict, Tuple

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── Task categories ──────────────────────────────────────────────
TASK_CREATIVE = "creative"       # Comments, DMs, tweets — need quality
TASK_ANALYTICAL = "analytical"   # Research, trends, failure analysis — need throughput

# ── Groq free-tier limits ────────────────────────────────────────
GROQ_RPM_LIMIT = 30         # 30 requests per minute
GROQ_RPD_LIMIT = 14400      # 14,400 requests per day
GROQ_TPM_LIMIT = 6000       # ~6k tokens per minute (free tier varies)


class _RateTracker:
    """Sliding-window rate tracker for Groq free tier."""

    def __init__(self, rpm: int = GROQ_RPM_LIMIT, rpd: int = GROQ_RPD_LIMIT):
        self.rpm_limit = rpm
        self.rpd_limit = rpd
        self._minute_window: deque = deque()   # timestamps within last 60s
        self._day_window: deque = deque()       # timestamps within last 24h
        self._lock = threading.Lock()

    def record(self):
        """Record a successful request."""
        now = time.time()
        with self._lock:
            self._minute_window.append(now)
            self._day_window.append(now)

    def _prune(self):
        """Remove expired timestamps."""
        now = time.time()
        while self._minute_window and self._minute_window[0] < now - 60:
            self._minute_window.popleft()
        while self._day_window and self._day_window[0] < now - 86400:
            self._day_window.popleft()

    def can_request(self) -> bool:
        """Check if we're within rate limits."""
        with self._lock:
            self._prune()
            return (
                len(self._minute_window) < self.rpm_limit
                and len(self._day_window) < self.rpd_limit
            )

    def wait_time(self) -> float:
        """Seconds to wait before next request is safe (0 = go now)."""
        with self._lock:
            self._prune()
            if len(self._minute_window) < self.rpm_limit:
                return 0.0
            oldest = self._minute_window[0]
            return max(0.0, 60.0 - (time.time() - oldest) + 0.5)

    @property
    def minute_usage(self) -> int:
        with self._lock:
            self._prune()
            return len(self._minute_window)

    @property
    def day_usage(self) -> int:
        with self._lock:
            self._prune()
            return len(self._day_window)


class LLMProvider:
    """Parallel dual-LLM provider — Groq + Ollama working simultaneously.

    Task routing:
      creative  → Groq (70B, fast, quality) → fallback Ollama
      analytical → Ollama (local, free, unlimited) → fallback Groq

    Features:
      - Rate-limit aware: auto-routes to Ollama when Groq is throttled
      - Circuit-breaker: auto-disables providers after quota exhaustion (429)
      - Parallel execution: batch_generate() runs multiple prompts concurrently
      - Stats tracking: calls, tokens, latency per provider
      - Zero-cost: both Groq free tier and Ollama local are $0.00/day
    """

    # How long (seconds) to disable a provider after quota exhaustion
    CIRCUIT_BREAKER_COOLDOWN = 3600  # 1 hour

    def __init__(self, config_path: str = "config/llm.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.clients: Dict[str, OpenAI] = {}
        self.provider_configs: Dict[str, Dict] = {}
        self.fallback_chain: List[str] = self.config.get(
            "fallback_chain", ["groq", "gemini", "ollama"]
        )

        # Task routing config
        routing = self.config.get("routing", {})
        self._creative_chain: List[str] = routing.get(
            "creative", ["groq", "gemini", "ollama"]
        )
        self._analytical_chain: List[str] = routing.get(
            "analytical", ["ollama", "groq", "gemini"]
        )

        # Rate tracking for Groq
        self._groq_rate = _RateTracker()

        # Circuit-breaker: provider → timestamp when it becomes available again
        self._disabled_until: Dict[str, float] = {}
        self._cb_lock = threading.Lock()

        # Stats
        self._stats_lock = threading.Lock()
        self._stats: Dict[str, Dict] = {}  # provider → {calls, tokens, errors, total_ms}

        # Thread pool for parallel execution
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="llm"
        )

        self._init_clients()

    def _init_clients(self):
        """Initialize OpenAI client for each enabled provider."""
        for name, cfg in self.config.get("providers", {}).items():
            if not cfg.get("enabled", False):
                continue
            api_key = cfg.get("api_key", "")
            if api_key.startswith("YOUR_"):
                logger.warning(
                    f"LLM provider '{name}' has placeholder API key — skipping"
                )
                continue
            try:
                # Disable built-in retries: we handle fallback ourselves.
                # The openai client retries 429s internally with backoff,
                # flooding logs when a provider's quota is fully exhausted.
                self.clients[name] = OpenAI(
                    base_url=cfg["base_url"],
                    api_key=api_key,
                    max_retries=0,
                    timeout=30.0,
                )
                self.provider_configs[name] = cfg
                self._stats[name] = {
                    "calls": 0, "tokens": 0, "errors": 0, "total_ms": 0,
                }
                logger.info(f"LLM provider ready: {name} ({cfg.get('model', '?')})")
            except Exception as e:
                logger.warning(f"Failed to initialize LLM provider '{name}': {e}")

        if not self.clients:
            logger.warning(
                "No LLM providers configured. "
                "Run 'python miloagent.py setup' to configure API keys."
            )

        # Log routing setup
        available = set(self.clients.keys())
        creative_active = [p for p in self._creative_chain if p in available]
        analytical_active = [p for p in self._analytical_chain if p in available]
        logger.info(
            f"LLM routing: creative={creative_active}, "
            f"analytical={analytical_active}"
        )

    # ── Circuit Breaker ────────────────────────────────────────

    def _trip_circuit(self, provider: str, seconds: Optional[float] = None):
        """Disable a provider for `seconds` (default: CIRCUIT_BREAKER_COOLDOWN)."""
        cooldown = seconds or self.CIRCUIT_BREAKER_COOLDOWN
        until = time.time() + cooldown
        with self._cb_lock:
            self._disabled_until[provider] = until
        logger.warning(
            f"Circuit-breaker tripped for '{provider}' — "
            f"disabled for {cooldown / 60:.0f}min"
        )

    def _is_circuit_open(self, provider: str) -> bool:
        """Check if a provider is currently disabled by the circuit-breaker."""
        with self._cb_lock:
            until = self._disabled_until.get(provider, 0)
            if until and time.time() < until:
                return True
            # Expired — remove entry
            self._disabled_until.pop(provider, None)
            return False

    def _is_quota_exhaustion(self, error) -> bool:
        """Detect 429 / quota-exhausted errors from any provider."""
        err_str = str(error).lower()
        # OpenAI-compatible 429 errors
        if "429" in err_str or "rate_limit" in err_str or "rate limit" in err_str:
            return True
        if "quota" in err_str or "resource_exhausted" in err_str:
            return True
        if "limit: 0" in err_str:
            return True
        return False

    # ── Core Generation ──────────────────────────────────────────

    def _get_chain_for_task(self, task: str) -> List[str]:
        """Get the provider chain for a task category."""
        if task == TASK_CREATIVE:
            return self._creative_chain
        elif task == TASK_ANALYTICAL:
            return self._analytical_chain
        return self.fallback_chain

    def _record_stat(self, provider: str, tokens: int, latency_ms: float, error: bool = False):
        """Record usage stats for a provider."""
        with self._stats_lock:
            s = self._stats.get(provider)
            if not s:
                return
            if error:
                s["errors"] += 1
            else:
                s["calls"] += 1
                s["tokens"] += tokens
                s["total_ms"] += int(latency_ms)

    def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        provider: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        task: str = "",
    ) -> str:
        """Generate text using task-aware routing with fallback.

        Args:
            prompt: The user prompt.
            system_prompt: Optional system prompt.
            provider: Force a specific provider (bypasses routing).
            max_tokens: Max tokens to generate.
            temperature: Sampling temperature.
            task: Task category — "creative" or "analytical".
                  If empty, uses the default fallback chain.

        Returns the generated text string.
        Raises RuntimeError if all providers fail.
        """
        # Build provider chain
        if provider:
            chain = [provider]
        elif task:
            chain = self._get_chain_for_task(task)
        else:
            chain = self.fallback_chain

        last_error = None

        for pname in chain:
            if pname not in self.clients:
                continue

            # Circuit-breaker check (quota exhaustion cooldown)
            if self._is_circuit_open(pname):
                logger.debug(f"Provider '{pname}' circuit-breaker open — skipping")
                continue

            # Rate-limit check for Groq
            if pname == "groq" and not self._groq_rate.can_request():
                wait = self._groq_rate.wait_time()
                if wait > 5:
                    # Too long to wait — skip to next provider
                    logger.debug(
                        f"Groq rate-limited (wait {wait:.0f}s), "
                        f"routing to next provider"
                    )
                    continue
                elif wait > 0:
                    time.sleep(wait)

            try:
                cfg = self.provider_configs[pname]
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})

                t0 = time.time()
                response = self.clients[pname].chat.completions.create(
                    model=cfg["model"],
                    messages=messages,
                    max_tokens=max_tokens or cfg.get("max_tokens", 1024),
                    temperature=temperature or cfg.get("temperature", 0.7),
                )
                latency = (time.time() - t0) * 1000

                text = response.choices[0].message.content.strip()
                tok_count = getattr(response.usage, "total_tokens", len(text) // 4)

                # Record stats
                self._record_stat(pname, tok_count, latency)
                if pname == "groq":
                    self._groq_rate.record()

                task_label = f" [{task}]" if task else ""
                logger.debug(
                    f"LLM{task_label}: {len(text)} chars via {pname} "
                    f"({cfg['model']}) in {latency:.0f}ms"
                )
                return text

            except Exception as e:
                last_error = e
                self._record_stat(pname, 0, 0, error=True)

                # Trip circuit-breaker on quota exhaustion (429 / limit:0)
                if self._is_quota_exhaustion(e):
                    self._trip_circuit(pname)
                else:
                    logger.warning(f"LLM provider '{pname}' failed: {e}")
                continue

        raise RuntimeError(
            f"All LLM providers failed. Last error: {last_error}"
        )

    # ── Parallel Batch Generation ────────────────────────────────

    def batch_generate(
        self,
        prompts: List[Dict],
        task: str = TASK_ANALYTICAL,
    ) -> List[str]:
        """Generate multiple prompts in parallel.

        Each item in `prompts` is a dict with:
          - prompt: str (required)
          - system_prompt: str (optional)
          - max_tokens: int (optional)
          - temperature: float (optional)

        Returns list of generated texts (same order as input).
        Failed prompts return empty string.
        """
        if not prompts:
            return []

        futures = []
        for p in prompts:
            future = self._pool.submit(
                self.generate,
                prompt=p["prompt"],
                system_prompt=p.get("system_prompt", ""),
                max_tokens=p.get("max_tokens"),
                temperature=p.get("temperature"),
                task=task,
            )
            futures.append(future)

        results = []
        for i, future in enumerate(futures):
            try:
                results.append(future.result(timeout=60))
            except Exception as e:
                logger.warning(f"Batch prompt {i} failed: {e}")
                results.append("")

        return results

    # ── Convenience Methods ──────────────────────────────────────

    def generate_creative(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Generate creative content (comments, DMs, tweets) via Groq."""
        return self.generate(
            prompt=prompt, system_prompt=system_prompt,
            max_tokens=max_tokens, temperature=temperature,
            task=TASK_CREATIVE,
        )

    def generate_analytical(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Generate analytical content (research, analysis) via Ollama."""
        return self.generate(
            prompt=prompt, system_prompt=system_prompt,
            max_tokens=max_tokens, temperature=temperature,
            task=TASK_ANALYTICAL,
        )

    # ── Stats & Monitoring ───────────────────────────────────────

    def get_stats(self) -> Dict:
        """Get usage stats for all providers.

        Returns:
            {
                "providers": {
                    "groq": {"calls": 42, "tokens": 18000, "errors": 1,
                             "avg_ms": 350, "rpm": 5, "rpd": 42},
                    "ollama": {"calls": 80, "tokens": 24000, "errors": 0,
                               "avg_ms": 1200, "rpm": 0, "rpd": 80},
                },
                "groq_rate": {"minute": 5, "day": 42,
                              "minute_limit": 30, "day_limit": 14400},
                "routing": {"creative": [...], "analytical": [...]},
                "total_calls": 122,
                "total_errors": 1,
            }
        """
        with self._stats_lock:
            providers = {}
            total_calls = 0
            total_errors = 0

            for name, s in self._stats.items():
                avg_ms = s["total_ms"] / max(s["calls"], 1)
                providers[name] = {
                    "calls": s["calls"],
                    "tokens": s["tokens"],
                    "errors": s["errors"],
                    "avg_ms": round(avg_ms),
                }
                total_calls += s["calls"]
                total_errors += s["errors"]

            # Add Groq-specific rate info
            if "groq" in providers:
                providers["groq"]["rpm"] = self._groq_rate.minute_usage
                providers["groq"]["rpd"] = self._groq_rate.day_usage

            # Circuit-breaker status
            disabled = {}
            with self._cb_lock:
                now = time.time()
                for pname, until in self._disabled_until.items():
                    if until > now:
                        disabled[pname] = int(until - now)

            return {
                "providers": providers,
                "groq_rate": {
                    "minute": self._groq_rate.minute_usage,
                    "day": self._groq_rate.day_usage,
                    "minute_limit": GROQ_RPM_LIMIT,
                    "day_limit": GROQ_RPD_LIMIT,
                },
                "routing": {
                    "creative": self._creative_chain,
                    "analytical": self._analytical_chain,
                },
                "disabled_providers": disabled,
                "total_calls": total_calls,
                "total_errors": total_errors,
            }

    def is_groq_available(self) -> bool:
        """Check if Groq can accept requests right now."""
        return "groq" in self.clients and self._groq_rate.can_request()

    def is_ollama_available(self) -> bool:
        """Check if Ollama is configured (always available if running)."""
        return "ollama" in self.clients

    # ── Testing & Utilities ──────────────────────────────────────

    def test_connection(
        self, provider: Optional[str] = None
    ) -> Dict[str, bool]:
        """Test connectivity to each enabled provider."""
        results = {}
        targets = [provider] if provider else list(self.clients.keys())

        for pname in targets:
            if pname not in self.clients:
                results[pname] = False
                continue
            try:
                self.generate(
                    "Reply with exactly: OK",
                    provider=pname,
                    max_tokens=5,
                )
                results[pname] = True
                logger.info(f"LLM provider '{pname}': connected")
            except Exception as e:
                results[pname] = False
                logger.error(f"LLM provider '{pname}': failed — {e}")

        return results

    def get_available_providers(self) -> List[str]:
        """List names of configured (non-placeholder) providers."""
        return list(self.clients.keys())

    def shutdown(self):
        """Shutdown the thread pool."""
        self._pool.shutdown(wait=False)
