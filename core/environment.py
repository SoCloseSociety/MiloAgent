"""Environment detection — auto-detect Mac vs Linux server.

Detects the runtime environment once and caches the result.
Used by resource_monitor, orchestrator, cookie_manager, and CLI
to auto-configure optimizations for the current platform.
"""

import os
import platform
import sys
import functools
import logging

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def detect_environment() -> dict:
    """Detect runtime environment. Called once, cached forever.

    Returns dict with keys:
        os, arch, is_macos, is_linux, is_docker, is_headless,
        is_ssh, has_tty, hostname, cpu_count, is_server, recommended_mode
    """
    info = {
        "os": platform.system().lower(),            # "darwin" | "linux"
        "arch": platform.machine(),                  # "arm64" | "x86_64"
        "is_macos": platform.system() == "Darwin",
        "is_linux": platform.system() == "Linux",
        "is_docker": _is_docker(),
        "is_headless": not _has_display(),
        "is_ssh": "SSH_CLIENT" in os.environ or "SSH_TTY" in os.environ,
        "has_tty": sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False,
        "hostname": platform.node(),
        "cpu_count": os.cpu_count() or 1,
    }
    info["is_server"] = info["is_linux"] and (
        info["is_docker"] or info["is_headless"]
    )
    info["recommended_mode"] = _recommend_mode(info)
    return info


def _has_display() -> bool:
    """Check if a graphical display is available."""
    if platform.system() == "Darwin":
        return True  # macOS always has a display (even headless has virtual)
    return bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def _is_docker() -> bool:
    """Check if running inside a Docker/container environment."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read()
            return "docker" in content or "containerd" in content
    except (FileNotFoundError, PermissionError):
        return False


def _recommend_mode(info: dict) -> str:
    """Recommend a bot mode based on detected environment."""
    if info["is_server"] or info["is_docker"]:
        return "server"
    if info["is_macos"]:
        return "full"
    return "background"


def get_chrome_paths() -> list:
    """Return Chrome/Chromium executable paths for the current platform."""
    if detect_environment()["is_macos"]:
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]


def get_env_summary() -> str:
    """Human-readable one-line summary for CLI/logs."""
    e = detect_environment()
    parts = [
        f"OS: {'macOS' if e['is_macos'] else 'Linux'} ({e['arch']})",
    ]
    if e["is_docker"]:
        parts.append("Docker: yes")
    if e["is_headless"]:
        parts.append("Headless: yes")
    if e["is_ssh"]:
        parts.append("SSH: yes")
    parts.append(f"CPUs: {e['cpu_count']}")
    parts.append(f"Recommended mode: {e['recommended_mode']}")
    return " | ".join(parts)
