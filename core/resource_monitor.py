"""System resource monitor — adapts bot behavior to hardware limits.

Checks CPU, RAM, and disk usage. Automatically throttles the bot
when resources are constrained to prevent system slowdowns.

Cross-platform: works on macOS (sysctl/vm_stat) and Linux (/proc/*).
Auto-detects the platform and uses the right commands.

SAFETY: This monitor is designed to PREVENT freezes.
- is_safe_to_proceed() must be called before any heavy operation
- Hard RSS limit kills operations immediately
- Checks every 30s (not 60s) for faster reaction
"""

import gc
import os
import logging
import resource as _resource
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from core.environment import detect_environment

logger = logging.getLogger(__name__)

# Thresholds (percentage)
_RAM_WARN = 80       # Start throttling
_RAM_CRITICAL = 90   # Pause bot
_DISK_WARN = 90      # Warn + cleanup
_DISK_CRITICAL = 95  # Pause bot
_CPU_WARN = 80       # Throttle scan frequency


@dataclass
class SystemState:
    """Snapshot of system resource usage."""
    ram_total_gb: float = 0.0
    ram_used_percent: float = 0.0
    ram_available_gb: float = 0.0
    cpu_percent: float = 0.0
    cpu_cores: int = 1
    disk_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_used_percent: float = 0.0
    process_rss_mb: float = 0.0
    is_apple_silicon: bool = False
    cpu_name: str = "unknown"


class ResourceMonitor:
    """Monitors system resources and auto-adapts bot behavior.

    Cross-platform: macOS (sysctl/vm_stat) and Linux (/proc/*).

    SAFETY FEATURES:
    - is_safe_to_proceed() — call before any heavy operation
    - Hard RSS limit (200MB Mac, 500MB server) before forced GC
    - Auto-pause at critical RAM threshold
    - Checks every 30s for faster reaction time
    - Lightweight checks (cached reads, no external deps)
    """

    # Max RSS for this process before forced GC (MB)
    MAX_PROCESS_RSS_MB = 400  # Overridden for servers in _apply_env_config

    def __init__(self, check_interval: int = 30):
        self._interval = check_interval
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._state = SystemState()
        self._callbacks: List[Callable] = []
        self._throttle_factor: float = 1.0  # 1.0 = normal, 2.0 = 2x slower
        self._last_ram_check: float = 0.0
        self._ram_cache_ttl: float = 10.0  # Cache RAM for 10s

        # Detect environment (cached, no cost on repeated calls)
        self._env = detect_environment()

        # Detect CPU type (once)
        self._state.is_apple_silicon = self._detect_apple_silicon()
        self._state.cpu_name = self._detect_cpu_name()
        self._state.cpu_cores = os.cpu_count() or 1

        # Cache total RAM (doesn't change)
        self._total_ram_bytes: int = 0
        self._init_total_ram()

        # Apply environment-specific thresholds
        self._apply_env_config()

    def _init_total_ram(self):
        """Detect total RAM — macOS via sysctl, Linux via /proc/meminfo."""
        try:
            if self._env["is_macos"]:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=3,
                )
                self._total_ram_bytes = int(result.stdout.strip())
            elif self._env["is_linux"]:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            self._total_ram_bytes = int(line.split()[1]) * 1024
                            break
            self._state.ram_total_gb = self._total_ram_bytes / (1024 ** 3)
        except Exception:
            self._total_ram_bytes = 8 * (1024 ** 3)  # Assume 8GB
            self._state.ram_total_gb = 8.0

    def _detect_apple_silicon(self) -> bool:
        """Check if running on Apple Silicon."""
        if not self._env["is_macos"]:
            return False
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=3,
            )
            return "Apple" in result.stdout
        except Exception:
            return False

    def _detect_cpu_name(self) -> str:
        """Get CPU model name — macOS via sysctl, Linux via /proc/cpuinfo."""
        try:
            if self._env["is_macos"]:
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=3,
                )
                return result.stdout.strip() if result.returncode == 0 else "unknown"
            elif self._env["is_linux"]:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if "model name" in line:
                            return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return "unknown"

    def _apply_env_config(self):
        """Apply environment-specific thresholds.

        Servers have more headroom → higher limits.
        Macs need tighter limits to prevent UI freezes.
        """
        global _RAM_WARN, _RAM_CRITICAL, _DISK_WARN, _DISK_CRITICAL
        if self._env["is_server"]:
            self.MAX_PROCESS_RSS_MB = 500
            _RAM_WARN = 85
            _RAM_CRITICAL = 95
            _DISK_WARN = 92
            _DISK_CRITICAL = 97
            logger.info("Resource monitor: server mode — relaxed thresholds "
                        f"(RSS={self.MAX_PROCESS_RSS_MB}MB, RAM warn={_RAM_WARN}%, "
                        f"RAM crit={_RAM_CRITICAL}%)")

    def get_state(self) -> SystemState:
        """Get current system state (refreshes data)."""
        self._update_state()
        return self._state

    @property
    def throttle_factor(self) -> float:
        """Current throttle multiplier (1.0 = normal, higher = slower)."""
        return self._throttle_factor

    def is_safe_to_proceed(self) -> bool:
        """Quick check: is it safe to start a heavy operation?

        Call this before scans, LLM calls, or any resource-intensive work.
        Uses cached values for speed (< 1ms if cache is fresh).
        """
        # Fast path: use cached state if recent
        now = time.monotonic()
        if now - self._last_ram_check > self._ram_cache_ttl:
            self._update_ram_fast()
            self._update_process_memory()
            self._last_ram_check = now

        # Hard limits
        if self._state.ram_used_percent >= _RAM_CRITICAL:
            gc.collect()
            return False
        if self._state.process_rss_mb > self.MAX_PROCESS_RSS_MB:
            gc.collect()
            return False
        return True

    def get_process_rss_mb(self) -> float:
        """Get current process RSS quickly."""
        self._update_process_memory()
        return self._state.process_rss_mb

    def on_threshold(self, callback: Callable):
        """Register callback for threshold events."""
        self._callbacks.append(callback)

    def start(self):
        """Start periodic monitoring in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="resource-monitor",
        )
        self._thread.start()
        platform_label = "macOS" if self._env["is_macos"] else "Linux"
        server_tag = " [SERVER]" if self._env["is_server"] else ""
        logger.info(
            f"Resource monitor started: {platform_label}{server_tag}, "
            f"{self._state.cpu_name}, "
            f"{self._state.cpu_cores} cores, "
            f"{self._state.ram_total_gb:.0f}GB RAM, "
            f"RSS limit={self.MAX_PROCESS_RSS_MB}MB, "
            f"interval={self._interval}s"
        )

    def stop(self):
        """Stop monitoring."""
        self._running = False

    def _monitor_loop(self):
        """Background monitoring loop."""
        while self._running:
            try:
                self._update_state()
                self._check_thresholds()
            except Exception as e:
                logger.debug(f"Resource monitor error: {e}")
            time.sleep(self._interval)

    def _update_state(self):
        """Update system state using platform-native commands."""
        self._update_ram()
        self._update_cpu()
        self._update_disk()
        self._update_process_memory()
        self._last_ram_check = time.monotonic()

    def _update_ram_fast(self):
        """Quick RAM check — macOS via vm_stat, Linux via /proc/meminfo."""
        if self._env["is_macos"]:
            self._update_ram_macos()
        elif self._env["is_linux"]:
            self._update_ram_linux()

    def _update_ram_macos(self):
        """Get RAM usage via vm_stat (macOS native)."""
        try:
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=3,
            )
            if result.returncode != 0:
                return

            lines = result.stdout.strip().split("\n")
            page_size = 4096
            if "page size of" in lines[0]:
                page_size = int(lines[0].split("page size of")[1].strip().split()[0])

            stats = {}
            for line in lines[1:]:
                if ":" in line:
                    key, val = line.split(":", 1)
                    val = val.strip().rstrip(".")
                    try:
                        stats[key.strip()] = int(val)
                    except ValueError:
                        pass

            free = stats.get("Pages free", 0) * page_size
            active = stats.get("Pages active", 0) * page_size
            inactive = stats.get("Pages inactive", 0) * page_size
            speculative = stats.get("Pages speculative", 0) * page_size
            wired = stats.get("Pages wired down", 0) * page_size
            compressed = stats.get("Pages occupied by compressor", 0) * page_size

            total_used = active + wired + compressed
            total_available = free + inactive + speculative

            if self._total_ram_bytes > 0:
                self._state.ram_available_gb = total_available / (1024 ** 3)
                self._state.ram_used_percent = (total_used / self._total_ram_bytes) * 100
        except Exception:
            pass

    def _update_ram_linux(self):
        """Get RAM usage via /proc/meminfo (Linux native)."""
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024  # KB → bytes

            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            if total > 0:
                used = total - available
                self._state.ram_available_gb = available / (1024 ** 3)
                self._state.ram_used_percent = (used / total) * 100
                self._total_ram_bytes = total
                self._state.ram_total_gb = total / (1024 ** 3)
        except Exception:
            pass

    def _update_ram(self):
        """Get RAM usage (cross-platform)."""
        self._update_ram_fast()

    def _update_cpu(self):
        """Get CPU load average (POSIX — works on macOS and Linux)."""
        try:
            load1, load5, load15 = os.getloadavg()
            cores = self._state.cpu_cores or 1
            self._state.cpu_percent = min((load1 / cores) * 100, 100)
        except Exception:
            pass

    def _update_disk(self):
        """Get disk usage for the working directory."""
        try:
            usage = shutil.disk_usage(os.getcwd())
            self._state.disk_total_gb = usage.total / (1024 ** 3)
            self._state.disk_free_gb = usage.free / (1024 ** 3)
            self._state.disk_used_percent = (usage.used / usage.total) * 100
        except Exception:
            pass

    def _update_process_memory(self):
        """Get current process RSS in MB (cross-platform)."""
        try:
            rusage = _resource.getrusage(_resource.RUSAGE_SELF)
            if self._env["is_macos"]:
                # macOS: ru_maxrss is in bytes
                self._state.process_rss_mb = rusage.ru_maxrss / (1024 * 1024)
            else:
                # Linux: ru_maxrss is in kilobytes
                self._state.process_rss_mb = rusage.ru_maxrss / 1024
        except Exception:
            pass

    def _check_thresholds(self):
        """Check resource thresholds and fire callbacks."""
        old_factor = self._throttle_factor
        events = []

        # RAM checks (tighter thresholds)
        if self._state.ram_used_percent >= _RAM_CRITICAL:
            self._throttle_factor = 5.0
            events.append("ram_critical")
            gc.collect()
        elif self._state.ram_used_percent >= _RAM_WARN:
            self._throttle_factor = 2.0
            events.append("ram_warn")
        else:
            self._throttle_factor = 1.0

        # CPU check
        if self._state.cpu_percent >= _CPU_WARN:
            self._throttle_factor = max(self._throttle_factor, 2.0)
            events.append("cpu_warn")

        # Disk check
        if self._state.disk_used_percent >= _DISK_CRITICAL:
            events.append("disk_critical")
        elif self._state.disk_used_percent >= _DISK_WARN:
            events.append("disk_warn")

        # Process memory check (stricter)
        if self._state.process_rss_mb > self.MAX_PROCESS_RSS_MB:
            events.append("process_memory_warn")
            gc.collect()

        # Recovery
        if old_factor > 1.0 and self._throttle_factor == 1.0:
            events.append("recovered")

        # Fire callbacks
        for event in events:
            for cb in self._callbacks:
                try:
                    cb(event, self._state)
                except Exception as e:
                    logger.debug(f"Resource callback error: {e}")

        # Log significant changes
        if events:
            logger.info(
                f"Resource monitor: {', '.join(events)} | "
                f"RAM={self._state.ram_used_percent:.0f}% "
                f"CPU={self._state.cpu_percent:.0f}% "
                f"RSS={self._state.process_rss_mb:.0f}MB "
                f"throttle={self._throttle_factor}x"
            )

    def get_summary(self) -> str:
        """Get a human-readable summary of system state."""
        s = self._state
        env = self._env
        platform_label = "macOS" if env["is_macos"] else "Linux"
        if s.is_apple_silicon:
            cpu_label = "Apple Silicon"
        elif s.cpu_name != "unknown":
            cpu_label = s.cpu_name
        else:
            cpu_label = "Intel"
        server_tag = " [SERVER]" if env["is_server"] else ""
        return (
            f"System: {platform_label} — {cpu_label} "
            f"({s.cpu_cores} cores){server_tag}\n"
            f"RAM: {s.ram_used_percent:.0f}% used "
            f"({s.ram_available_gb:.1f}GB available / {s.ram_total_gb:.1f}GB total)\n"
            f"Disk: {s.disk_used_percent:.0f}% used "
            f"({s.disk_free_gb:.0f}GB free / {s.disk_total_gb:.0f}GB total)\n"
            f"CPU: {s.cpu_percent:.0f}% load\n"
            f"Process RSS: {s.process_rss_mb:.0f}MB "
            f"(limit: {self.MAX_PROCESS_RSS_MB}MB)\n"
            f"Throttle: {self._throttle_factor}x"
        )
