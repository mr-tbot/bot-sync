#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
botsyncd - cloud folder sync appliance for OpenWrt / GL-iNet routers.

Single-file daemon, stdlib only. Targets Python 3.7+ (matches OpenWrt 21.02
python3-light). All persistent state lives under BOTSYNC_ROOT (default
/mnt/sync). The daemon refuses to start if BOTSYNC_ROOT is on the rootfs
unless BOTSYNC_MOCK=1.

Environment:
    BOTSYNC_ROOT      Root path on USB (default /mnt/sync).
    BOTSYNC_MOCK      "1" -> mock mode for dev (no real rclone, no FS checks).
    BOTSYNC_BIND      Bind address (default 0.0.0.0).
    BOTSYNC_PORT      Port (default 8088).
    BOTSYNC_USER      Initial admin user if state has none.
    BOTSYNC_PASS      Initial admin password if state has none.
    BOTSYNC_RCLONE    Path to rclone binary (default <ROOT>/bin/rclone, then PATH).
"""

from __future__ import annotations

import base64
import collections
import datetime
import glob
import hashlib
import hmac
import json
import logging
import logging.handlers
import math
import os
import platform
import random
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

VERSION = "0.7.15"
IS_MOCK = os.environ.get("BOTSYNC_MOCK") == "1"
IS_DEBUG = os.environ.get("BOTSYNC_DEBUG") == "1"
IS_WINDOWS = platform.system() == "Windows"

ROOT = os.path.abspath(os.environ.get(
    "BOTSYNC_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mock_root") if IS_MOCK else "/mnt/sync"
))
ETC_DIR = os.path.join(ROOT, "etc")
VAR_DIR = os.path.join(ROOT, "var")
LOG_DIR = os.path.join(VAR_DIR, "log")
RUN_DIR = os.path.join(VAR_DIR, "run")
CACHE_DIR = os.path.join(VAR_DIR, "cache")
TMP_DIR = os.path.join(VAR_DIR, "tmp")
BIN_DIR = os.path.join(ROOT, "bin")
DOWNLOADS_DIR = os.path.join(ROOT, "downloads")
UPLOADS_DIR = os.path.join(ROOT, "uploads")

STATE_FILE = os.path.join(ETC_DIR, "botsync.json")
RCLONE_CONF = os.path.join(ETC_DIR, "rclone.conf")
LOG_FILE = os.path.join(LOG_DIR, "botsync.log")
MARKER_FILE = os.path.join(ROOT, ".botsync_marker")
# Reliability / watchdog state files
RUNNING_MARKER = os.path.join(RUN_DIR, "botsyncd.running")     # touched on start, removed on clean stop
HEARTBEAT_FILE = os.path.join(RUN_DIR, "botsyncd.heartbeat")    # bumped every 30s with json {ts,pid,version}
WATCHDOG_KICK_MARKER = "/tmp/botsync-watchdog.kicked"           # touched by external cron watchdog before restart
CRASH_LOG_DIR = os.path.join(LOG_DIR, "crash")

UI_DIR = os.environ.get("BOTSYNC_UI_DIR") or os.path.join(ROOT, "ui")

DEFAULT_STATE: Dict[str, Any] = {
    "version": 1,
    "auth": {"user": "admin", "pass_hash": None, "pass_salt": None},
    "drives": {},      # uuid -> {label, fs, mountpoint, primary, adopted_at, size_bytes, free_bytes, present}
    "remotes": {},     # name -> {provider, health, last_check, expires_at, error}
    "downloads": {},   # id -> {label, url, provider, remote, remote_path, drive_uuid, local_subpath, project_id, state, schedule, last_sync, remote_size, local_size}
    "uploads": {},     # id -> {label, drive_uuid, local_subpath, remote, remote_path, project_id, mode, state, schedule, last_sync, local_size, remote_size}
    "projects": {},    # id -> {name, slug, created_at} — optional grouping; entries with project_id nest under <slug>/ on the local drive.
    "sharing": {"smb": True, "bonjour": True, "nfs": False, "guest_ro": True, "share_user": "guest", "share_pass": ""},
    "settings": {
        "enabled": True,
        "uploads_enabled": True,
        "downloads_enabled": True,
        "setup_complete": False,
        "session_ttl_hours": 12,
        "stuck_job_hours": 6,
        "providers_enabled": {"drive": True, "dropbox": True, "box": False, "onedrive": False, "ftp": True, "sftp": True},
        # Hardware performance profile. Empty = auto-detect on first start
        # (see auto_detect_preset). One of: "router", "pi", "desktop",
        # "custom". When set to "custom", performance_custom is consulted
        # for individual flag overrides on top of the router floor.
        "performance_preset": "",
        "performance_custom": {},
    },
    "limits": {
        "max_concurrent_jobs": 1,            # legacy / overall hint
        "download_concurrency": 1,           # per-type cap
        "upload_concurrency": 1,             # per-type cap
        "bw_limit_kbps": 0,
        "schedule_window": "",
    },
    "oauth_sessions": {},  # session_id -> {provider, name, started_at, auth_url, status}
    # Per-type ring buffer of completed sync jobs. Capped at SYNC_LOG_MAX
    # entries each so the daemon stays well under the router's 256 MB RAM
    # budget even after thousands of syncs.
    "sync_log": {"download": [], "upload": []},
    "notifications": {
        "channels": {},   # id -> {kind, label, config, events, min_severity, enabled, last_send, last_error}
        "events": [],     # ring buffer of recent events (capped)
        # Cross-platform health-monitor thresholds. When any sample exceeds
        # the configured limit for at least sustain_secs, a single
        # system.health_warning event is emitted (cooldown_secs between
        # repeats so we don't spam webhooks). All percentages are 0–100.
        "health_thresholds": {
            "enabled": True,
            "cpu_load_pct": 90,
            "mem_used_pct": 90,
            "swap_used_pct": 80,
            "cpu_temp_c": 80,
            "sustain_secs": 60,
            "cooldown_secs": 600,
        },
    },
    "rclone_status": {
        # Populated by App._update_check_loop. Surfaced via /api/state and the UI banner.
        "installed_version": None,   # e.g. "v1.66.0"
        "latest_version": None,      # e.g. "v1.67.0"
        "update_available": False,
        "checked_at": None,          # ts of last successful check
        "check_error": None,         # last error from version check (if any)
        "release_url": None,         # GitHub release page
        "release_notes": None,       # truncated release body
        "announced_version": None,   # latest version we've already emitted notify for
        "updating": False,           # selfupdate in progress
        "last_update_attempt": None,
        "last_update_error": None,
        "last_update_from": None,
        "last_update_to": None,
    },
}

# Event types the system can emit. UI shows these as toggles per channel.
EVENT_TYPES = [
    "job.started",
    "job.completed",
    "job.failed",
    "job.cancelled",
    "job.interrupted",
    "job.stuck",
    "drive.adopted",
    "drive.online",
    "drive.offline",
    "drive.low_space",
    "drive.primary_missing",
    "drive.primary_returned",
    "remote.health_ok",
    "remote.health_failed",
    "remote.reauth_required",
    "system.error",
    "system.startup",
    "system.shutdown",
    "system.crash_recovered",
    "system.watchdog_restart",
    "system.health_warning",
    "rclone.update_available",
    "rclone.update_installed",
    "rclone.update_failed",
]
SEVERITIES = ["info", "warn", "error"]
EVENT_SEVERITY = {
    "job.started": "info", "job.completed": "info", "job.failed": "error",
    "job.cancelled": "warn", "job.interrupted": "warn", "job.stuck": "error",
    "drive.adopted": "info", "drive.online": "info",
    "drive.offline": "warn", "drive.low_space": "warn",
    "drive.primary_missing": "error", "drive.primary_returned": "info",
    "remote.health_ok": "info", "remote.health_failed": "error",
    "remote.reauth_required": "error",
    "system.error": "error", "system.startup": "info",
    "system.shutdown": "info",
    "system.crash_recovered": "warn",
    "system.watchdog_restart": "warn",
    "system.health_warning": "warn",
    "rclone.update_available": "info",
    "rclone.update_installed": "info",
    "rclone.update_failed": "error",
}

# Substrings (case-insensitive) in rclone error output that indicate a token /
# OAuth re-authentication is required, as opposed to a transient network or
# config error.
REAUTH_ERROR_MARKERS = (
    "invalid_grant",
    "invalid grant",
    "token expired",
    "token has been expired",
    "token has expired",
    "refresh token",
    "oauth2: cannot fetch token",
    "unauthorized_client",
    "401 unauthorized",
    "unauthenticated",
    "authorization failed",
    "couldn't fetch token",
    "failed to retrieve oauth token",
    "please re-run",
    "reauthorize",
    "re-authorize",
    "please reconnect",
)


def _looks_like_reauth(err: Optional[str]) -> bool:
    if not err:
        return False
    s = err.lower()
    return any(m in s for m in REAUTH_ERROR_MARKERS)


# Pattern matches anything that looks token-y or path-y. We prefer false-
# positive redaction over leaking secrets in HTTP 500 bodies.
_SANITISE_RE = re.compile(r"(?:[A-Za-z0-9_\-]{24,}|/[A-Za-z0-9_./\-]{4,})")


def _sanitise_error(msg: Optional[str], max_len: int = 240) -> str:
    """Strip likely-secret substrings from an exception message before it's
    returned over HTTP. The full traceback is still in the daemon log."""
    if not msg:
        return "internal error"
    out = _SANITISE_RE.sub("\u2026", msg)
    if len(out) > max_len:
        out = out[:max_len] + "\u2026"
    return out


def _version_tuple(v: Optional[str]) -> Tuple[int, ...]:
    """Parse ``v1.66.0`` / ``1.66`` / ``1.66.1-beta`` into a comparable tuple."""
    if not v:
        return (0,)
    m = re.search(r"(\d+(?:\.\d+){1,3})", v)
    if not m:
        return (0,)
    parts = m.group(1).split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


def _version_lt(a: Optional[str], b: Optional[str]) -> bool:
    """Return True when version ``a`` is strictly older than ``b``."""
    return _version_tuple(a) < _version_tuple(b)
NOTIFY_CHANNEL_KINDS = {
    "discord":   {"label": "Discord webhook",   "fields": ["url"]},
    "slack":     {"label": "Slack webhook",     "fields": ["url"]},
    "webhook":   {"label": "Generic webhook",   "fields": ["url", "auth_header"]},
    "ntfy":      {"label": "ntfy.sh",           "fields": ["url", "topic", "auth_header"]},
    "email":     {"label": "Email (SMTP)",      "fields": ["host", "port", "username", "password", "from", "to", "tls_mode", "use_tls", "subject_prefix"]},
}

PROVIDERS = {
    "drive":    {"label": "Google Drive", "rclone_type": "drive",   "auth": "oauth"},
    "dropbox":  {"label": "Dropbox",      "rclone_type": "dropbox", "auth": "oauth"},
    "box":      {"label": "Box",          "rclone_type": "box",     "auth": "oauth"},
    "onedrive": {"label": "OneDrive",     "rclone_type": "onedrive","auth": "oauth"},
    "http":     {"label": "HTTP folder",  "rclone_type": "http",    "auth": "none"},
    # FTP / SFTP: no OAuth — plain credentials written into rclone.conf.
    # rclone has both backends built-in; no extra opkg packages are required.
    # Works on every architecture supported by upstream rclone (armv7,
    # aarch64, mipsle, x86_64, etc.) so we don't need to gate by arch.
    "ftp":      {"label": "FTP / FTPS",    "rclone_type": "ftp",     "auth": "basic",
                 "basic_fields": [
                     {"key": "host",     "label": "Host",     "required": True,  "placeholder": "ftp.example.com"},
                     {"key": "port",     "label": "Port",     "required": False, "placeholder": "21 (or 990 for implicit FTPS)", "type": "number"},
                     {"key": "user",     "label": "Username", "required": True,  "placeholder": "anonymous if public"},
                     {"key": "pass",     "label": "Password", "required": False, "placeholder": "leave blank for anonymous", "type": "password"},
                     {"key": "tls",      "label": "TLS mode", "required": False, "type": "select",
                      "options": [
                          {"value": "",         "label": "Plain FTP (no TLS — credentials in clear)"},
                          {"value": "explicit", "label": "Explicit FTPS (FTP+TLS, AUTH TLS)"},
                          {"value": "implicit", "label": "Implicit FTPS (TLS from connect, port 990)"},
                      ]},
                     {"key": "no_epsv",  "label": "Disable EPSV (try if PASV stalls behind NAT)", "required": False, "type": "bool"},
                 ]},
    "sftp":     {"label": "SFTP (SSH)",   "rclone_type": "sftp",    "auth": "basic",
                 "basic_fields": [
                     {"key": "host",     "label": "Host",     "required": True,  "placeholder": "sftp.example.com"},
                     {"key": "port",     "label": "Port",     "required": False, "placeholder": "22", "type": "number"},
                     {"key": "user",     "label": "Username", "required": True,  "placeholder": "e.g. share"},
                     {"key": "pass",     "label": "Password", "required": False, "placeholder": "leave blank if using key", "type": "password"},
                     {"key": "key_pem",  "label": "Private key (PEM, optional)", "required": False, "type": "textarea",
                      "placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"},
                     {"key": "key_pass", "label": "Key passphrase (if any)", "required": False, "type": "password"},
                 ]},
}

# Per-provider help shown inline in the Add Folder / Edit modals so users
# don't have to leave the page to figure out what to type. Keep these short
# enough to render in a sidebar but specific enough to be actionable.
PROVIDER_HELP = {
    "drive": {
        "download": (
            "Paste the Google Drive folder URL (https://drive.google.com/drive/folders/…). "
            "bot-sync extracts the folder ID automatically and ignores Remote path. "
            "The signed-in account must have at least Viewer access to the folder."
        ),
        "upload": (
            "Remote path is the folder *inside your Drive* you want to push to, "
            "e.g. 'ShowUploads/Recordings'. Use '/' to nest. The folder is created "
            "on first sync if missing."
        ),
    },
    "dropbox": {
        "download": (
            "Public shared folders (dropbox.com/scl/fo/… or /sh/…) cannot be fetched directly by rclone. "
            "Open the link in your browser, click 'Add to my Dropbox', then set Remote path "
            "to the folder name that now appears in your Dropbox root — e.g. 'My Shared Folder'. "
            "Leaving Remote path blank would copy your ENTIRE Dropbox and will OOM the router."
        ),
        "upload": (
            "Remote path is the destination folder in your Dropbox. Use '/' to nest, "
            "e.g. 'Backups/Router'. The folder is created on first sync if missing."
        ),
    },
    "box": {
        "download": (
            "Paste the Box folder URL or set Remote path to the folder name as it "
            "appears at the root of your Box account."
        ),
        "upload": (
            "Remote path is the destination folder in your Box account. Use '/' to nest."
        ),
    },
    "onedrive": {
        "download": (
            "Resolve the OneDrive share inside OneDrive first (Add to My OneDrive), "
            "then set Remote path to the folder name as it appears at the root of your OneDrive."
        ),
        "upload": (
            "Remote path is the destination folder in your OneDrive. Use '/' to nest."
        ),
    },
    "http": {
        "download": (
            "Paste the directory listing URL (must end in '/'). rclone will follow links "
            "to fetch files. Authenticated HTTP is not supported via this path."
        ),
        "upload": "HTTP backend is read-only — not usable for uploads.",
    },
    "ftp": {
        "download": (
            "Pick the FTP account and set Remote path to the folder you want to mirror, "
            "e.g. 'pub/show2026' or '/exports/clientA'. Leave blank to copy the entire "
            "FTP root (only safe on small servers). Plain FTP sends credentials in the clear — "
            "use FTPS where possible."
        ),
        "upload": (
            "Remote path is the destination folder on the FTP server. The folder is "
            "created on first sync if your account has write permission."
        ),
    },
    "sftp": {
        "download": (
            "Pick the SFTP account and set Remote path to the absolute or home-relative "
            "folder you want to mirror, e.g. '/srv/exports' or 'projects/show2026'."
        ),
        "upload": (
            "Remote path is the destination folder on the SFTP server. Use an absolute "
            "path (starts with '/') or a path relative to your home directory."
        ),
    },
}

# Cap on per-type sync log entries kept in state. Each entry is ~250 bytes
# so 100 × 2 types ≈ 50 KB — negligible against the 256 MB router budget.
SYNC_LOG_MAX = 100

# ---------------------------------------------------------------------------
# Hardware performance presets
# ---------------------------------------------------------------------------
#
# Why this exists: rclone is *very* willing to saturate a small device.
# On the GL-A1300 (256 MB RAM, 717 MHz dual-core ipq40xx) two concurrent
# rclones with default flags (--transfers 4, --buffer-size 16M, --checkers 8,
# --multi-thread-streams 4) will OOM the kernel within minutes — the user's
# original bug report ("router locks up over time").
#
# The fix is multi-layered:
#   * Cap the *total* number of rclone children regardless of whether they're
#     downloads or uploads (was previously two independent caps that summed).
#   * Pick conservative rclone flags per profile so each child stays under a
#     known RAM ceiling.
#   * Cap each child's virtual address space (RLIMIT_AS) to that ceiling.
#   * Optional global bandwidth cap so a fat pipe doesn't pin the router CPU.
#
# Presets are auto-picked at first start by total RAM and overridable from
# the Settings tab.
HARDWARE_PRESETS: Dict[str, Dict[str, Any]] = {
    "router": {
        "label": "Router (≤512 MB RAM, e.g. GL-A1300)",
        "description": (
            "Survives all-day Dropbox / Drive / FTP downloads on a 256-512 MB "
            "OpenWrt router. Caps the kernel before it OOMs."
        ),
        "max_global_jobs": 1,
        "transfers": 2,
        "checkers": 2,
        "buffer_size_mb": 0,
        "multi_thread_streams": 0,
        "max_backlog": 1000,
        "low_level_retries": 3,
        "rclone_mem_mb": 128,
        "bwlimit_kbps": 0,
        "nice": 5,
    },
    "pi": {
        "label": "Single-board (1-2 GB RAM, Raspberry Pi class)",
        "description": (
            "Modest concurrency for a Pi 3/4/Zero 2 W class device. Two "
            "concurrent transfers with reasonable per-rclone buffers."
        ),
        "max_global_jobs": 2,
        "transfers": 4,
        "checkers": 4,
        "buffer_size_mb": 4,
        "multi_thread_streams": 2,
        "max_backlog": 5000,
        "low_level_retries": 5,
        "rclone_mem_mb": 384,
        "bwlimit_kbps": 0,
        "nice": 0,
    },
    "desktop": {
        "label": "Desktop / NAS (4+ GB RAM)",
        "description": (
            "rclone defaults — full concurrency, multi-thread per-file streams, "
            "16 MB per-transfer buffer. Suitable for x86_64 / aarch64 boxes."
        ),
        "max_global_jobs": 4,
        "transfers": 4,
        "checkers": 8,
        "buffer_size_mb": 16,
        "multi_thread_streams": 4,
        "max_backlog": 10000,
        "low_level_retries": 5,
        "rclone_mem_mb": 1024,
        "bwlimit_kbps": 0,
        "nice": 0,
    },
}


def _detect_total_ram_mb() -> int:
    """Return total physical RAM in MB. Best-effort; returns 0 on failure
    so callers can fall back to a conservative default."""
    if IS_WINDOWS:
        try:
            import ctypes  # noqa: WPS433
            class _MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MEMSTAT(); ms.dwLength = ctypes.sizeof(_MEMSTAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return int(ms.ullTotalPhys / (1024 * 1024))
        except Exception:
            return 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def auto_detect_preset() -> str:
    """Pick a sensible default preset by total RAM. Errs on the safe side:
    anything under 768 MB gets the router profile."""
    mb = _detect_total_ram_mb()
    if mb <= 0:
        return "router"
    if mb < 768:
        return "router"
    if mb < 3072:
        return "pi"
    return "desktop"


# Set by App at startup so runner_rclone (module-level function) can resolve
# the user's chosen preset values without a back-reference to the App. The
# callable returns the effective values dict (already merged with custom
# overrides). None means "no app yet" → router fallback inside runner_rclone.
_ACTIVE_PRESET_GETTER: Optional[Callable[[], Dict[str, Any]]] = None


def get_active_preset(state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Return (name, values) for the preset currently in effect.

    ``state["settings"]["performance_preset"]`` is the user's choice
    (router / pi / desktop / custom). For "custom", values are read from
    ``state["settings"]["performance_custom"]`` with the router preset as a
    floor for any missing key.
    """
    settings = (state.get("settings") or {})
    name = settings.get("performance_preset") or "router"
    if name == "custom":
        base = dict(HARDWARE_PRESETS["router"])
        base.update(settings.get("performance_custom") or {})
        base["label"] = "Custom"
        base["description"] = "User-tuned values. See Settings tab."
        return ("custom", base)
    if name not in HARDWARE_PRESETS:
        name = "router"
    return (name, dict(HARDWARE_PRESETS[name]))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("botsync")


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# State store (atomic JSON file with RLock)
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        self._data = json.load(fh)
                except Exception as e:
                    logger.error("state load failed (%s); using defaults", e)
                    # Preserve the corrupt file for forensics before we
                    # overwrite it with defaults on the next save().
                    try:
                        backup = "{}.corrupt-{}".format(self.path, int(time.time()))
                        shutil.copyfile(self.path, backup)
                        logger.error("corrupt state preserved at %s", backup)
                    except Exception:
                        pass
                    self._data = json.loads(json.dumps(DEFAULT_STATE))
            else:
                self._data = json.loads(json.dumps(DEFAULT_STATE))
            self._merge_defaults()

    def _merge_defaults(self) -> None:
        def _deep(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
            for k, v in src.items():
                if k not in dst:
                    dst[k] = json.loads(json.dumps(v))
                elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _deep(dst[k], v)
        _deep(self._data, DEFAULT_STATE)
        # Backfill multi-project tagging fields on legacy entries. v0.7.11
        # introduced `project_ids` (list) alongside the existing single
        # `project_id`. Older state files only have project_id; we mirror
        # it into project_ids so the rest of the daemon can treat the list
        # as authoritative without special-casing every read.
        for bucket in ("downloads", "uploads"):
            for entry in (self._data.get(bucket) or {}).values():
                if not isinstance(entry, dict):
                    continue
                pid = entry.get("project_id")
                pids = entry.get("project_ids")
                if not isinstance(pids, list):
                    entry["project_ids"] = [pid] if pid else []
                if "auto_delete_at" not in entry:
                    entry["auto_delete_at"] = None
        for proj in (self._data.get("projects") or {}).values():
            if isinstance(proj, dict) and "auto_delete_at" not in proj:
                proj["auto_delete_at"] = None
        for proj in (self._data.get("projects") or {}).values():
            if isinstance(proj, dict):
                proj.setdefault("auto_sync_schedule", "")
                proj.setdefault("last_auto_sync_at", 0)

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.path)

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        with self._lock:
            result = fn(self._data)
            self.save()
            return result


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), 120_000)
    return base64.b64encode(digest).decode("ascii"), salt


def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, pw_hash)


def ensure_auth_seeded(store: Store) -> None:
    def _seed(d: Dict[str, Any]) -> None:
        a = d["auth"]
        if not a.get("pass_hash"):
            user = os.environ.get("BOTSYNC_USER", "admin")
            pw = os.environ.get("BOTSYNC_PASS", "admin")
            h, s = hash_password(pw)
            a["user"] = user
            a["pass_hash"] = h
            a["pass_salt"] = s
            logger.warning("seeded admin user %r with %s password", user,
                           "env-supplied" if "BOTSYNC_PASS" in os.environ else "DEFAULT 'admin'")
    store.update(_seed)


# Bounds for session TTL (hours). Min 1h, max 30 days.
SESSION_TTL_MIN_HOURS = 1
SESSION_TTL_MAX_HOURS = 24 * 30
SESSION_TTL_DEFAULT_HOURS = 12


def _clamp_session_ttl_hours(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return SESSION_TTL_DEFAULT_HOURS
    if n < SESSION_TTL_MIN_HOURS:
        return SESSION_TTL_MIN_HOURS
    if n > SESSION_TTL_MAX_HOURS:
        return SESSION_TTL_MAX_HOURS
    return n


# Bounds for stuck-job watchdog (hours). 0 disables.
STUCK_JOB_MIN_HOURS = 0
STUCK_JOB_MAX_HOURS = 168
STUCK_JOB_DEFAULT_HOURS = 6


def _clamp_stuck_job_hours(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return STUCK_JOB_DEFAULT_HOURS
    if n < STUCK_JOB_MIN_HOURS:
        return STUCK_JOB_MIN_HOURS
    if n > STUCK_JOB_MAX_HOURS:
        return STUCK_JOB_MAX_HOURS
    return n


class SessionStore:
    """In-memory session cookies. Cleared on daemon restart (forces re-login).

    The idle TTL is read from settings on every check via `ttl_provider`,
    so changes in the UI take effect for new requests without a restart.
    """

    def __init__(self, ttl_provider: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        # Callable returning current idle-timeout in seconds.
        self._ttl_provider = ttl_provider or (lambda: SESSION_TTL_DEFAULT_HOURS * 3600)

    def idle_seconds(self) -> int:
        try:
            return int(self._ttl_provider())
        except Exception:
            return SESSION_TTL_DEFAULT_HOURS * 3600

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {"user": user, "created": time.time(),
                                     "last": time.time()}
        return token

    def check(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            if time.time() - s["last"] > self.idle_seconds():
                self._sessions.pop(token, None)
                return None
            s["last"] = time.time()
            return s["user"]

    def revoke(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


# ---------------------------------------------------------------------------
# Drive detection
# ---------------------------------------------------------------------------

class DriveProbe:
    """Detect attached USB drives. Real impl on Linux, mock on others."""

    @staticmethod
    def detect() -> List[Dict[str, Any]]:
        if IS_MOCK or IS_WINDOWS:
            return DriveProbe._mock()
        return DriveProbe._linux()

    @staticmethod
    def _mock() -> List[Dict[str, Any]]:
        return [
            {
                "uuid": "MOCK-AAAA-1111",
                "device": "/dev/sda1",
                "label": "SHOWDRIVE",
                "fs": "exfat",
                "size_bytes": 256 * 1024**3,
                "free_bytes": 180 * 1024**3,
                "mountpoint": os.path.join(ROOT) if IS_MOCK else "/mnt/sync/MOCK-AAAA-1111",
                "has_marker": True,
                "present": True,
            },
            {
                "uuid": "MOCK-BBBB-2222",
                "device": "/dev/sdb1",
                "label": "BACKUPSTICK",
                "fs": "ntfs",
                "size_bytes": 64 * 1024**3,
                "free_bytes": 50 * 1024**3,
                "mountpoint": None,
                "has_marker": False,
                "present": True,
            },
        ]

    @staticmethod
    def _linux() -> List[Dict[str, Any]]:
        """Detect USB block devices via blkid + /proc/mounts.

        OpenWrt/busybox on the GL-A1300 has neither util-linux `lsblk` nor a
        json-capable `findmnt`, so we synthesise the same view from `blkid`
        (uuid/label/fstype) and /proc/mounts (mountpoint).
        """
        out: List[Dict[str, Any]] = []
        # 1. blkid -> per-device {UUID, LABEL, TYPE}
        by_dev: Dict[str, Dict[str, str]] = {}
        try:
            res = subprocess.run(["blkid"], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                m = re.match(r"^(/dev/\S+):\s*(.*)$", line.strip())
                if not m:
                    continue
                dev = m.group(1)
                kvs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
                by_dev[dev] = kvs
        except Exception as e:
            logger.warning("blkid failed: %s", e)

        # 2. /proc/mounts -> device -> mountpoint
        mp_by_dev: Dict[str, str] = {}
        try:
            with open("/proc/mounts", "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].startswith("/dev/"):
                        # mounts encodes spaces as \040
                        mp_by_dev[parts[0]] = parts[1].replace("\\040", " ")
        except Exception:
            pass

        # 3. Filter to removable / external block devices.
        # On OpenWrt: USB shows as /dev/sd[a-z]\d+, SD card as /dev/mmcblk\dp\d+.
        SKIP_FS = {"squashfs", "ubi", "ubifs", "linux_raid_member", "swap", "iso9660"}
        for dev, kvs in by_dev.items():
            base = os.path.basename(dev)
            if not (re.match(r"^sd[a-z]\d+$", base) or re.match(r"^mmcblk\d+p\d+$", base)):
                continue
            fstype = kvs.get("TYPE", "")
            if not fstype or fstype.lower() in SKIP_FS:
                continue
            mp = mp_by_dev.get(dev)
            size = free = 0
            if mp:
                try:
                    st = os.statvfs(mp)
                    size = st.f_blocks * st.f_frsize
                    free = st.f_bavail * st.f_frsize
                except Exception:
                    pass
            marker = bool(mp) and os.path.exists(os.path.join(mp, ".botsync_marker"))
            out.append({
                "uuid": kvs.get("UUID") or base,
                "device": dev,
                "label": kvs.get("LABEL") or base,
                "fs": fstype,
                "size_bytes": size,
                "free_bytes": free,
                "mountpoint": mp,
                "has_marker": marker,
                "present": True,
            })
        return out


def _parse_size(s: Optional[str]) -> int:
    if not s:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    s = s.strip()
    m = re.match(r"^([\d.]+)\s*([KMGTP]?)i?B?$", s, re.IGNORECASE)
    if not m:
        try:
            return int(s)
        except ValueError:
            return 0
    val = float(m.group(1))
    unit = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}.get(unit, 1)
    return int(val * mult)


# ---------------------------------------------------------------------------
# rclone wrapper
# ---------------------------------------------------------------------------

class Rclone:
    def __init__(self) -> None:
        self.binary = self._find_binary()
        self.conf = RCLONE_CONF
        # Reason: rclone rewrites rclone.conf whenever it refreshes an OAuth
        # token. If two rclone processes race, or the box is power-cut mid-
        # write, or the FS is briefly full, the file can end up truncated
        # to 0 bytes -- which silently destroys every account in the conf.
        # We keep an authoritative backup at "<conf>.bak" and restore from
        # it before every rclone invocation if the live conf looks broken.
        self._conf_lock = threading.Lock()
        try:
            self._conf_pre_invoke()  # restore on startup if needed
            self.snapshot_conf()      # seed backup if main is healthy
        except Exception:
            logger.exception("rclone.conf guard init")

    @staticmethod
    def _find_binary() -> Optional[str]:
        env = os.environ.get("BOTSYNC_RCLONE")
        if env and os.path.exists(env):
            return env
        local = os.path.join(BIN_DIR, "rclone.exe" if IS_WINDOWS else "rclone")
        if os.path.exists(local):
            return local
        return shutil.which("rclone")

    def available(self) -> bool:
        return bool(self.binary) and not IS_MOCK or IS_MOCK

    # ------- rclone.conf integrity guard -------

    @property
    def _conf_bak(self) -> str:
        return self.conf + ".bak"

    @staticmethod
    def _conf_is_healthy(path: str) -> bool:
        """A healthy conf has at least one ``[remote]`` section with a ``type=``
        line. We deliberately accept *any* such section so a partially-written
        conf containing only the in-flight remote is still preferable to an
        empty file -- but pure 0-byte / whitespace / no-section files are
        rejected so we restore from backup."""
        try:
            if not os.path.exists(path):
                return False
            if os.path.getsize(path) < 16:
                return False
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(16384)
        except Exception:
            return False
        if "[" not in data or "]" not in data:
            return False
        if "type" not in data:
            return False
        return True

    def _conf_pre_invoke(self) -> None:
        """Restore ``rclone.conf`` from ``rclone.conf.bak`` when the live conf
        is missing/empty/malformed. Called automatically before every rclone
        subprocess via ``_base_args``."""
        with self._conf_lock:
            try:
                if self._conf_is_healthy(self.conf):
                    return
                if not self._conf_is_healthy(self._conf_bak):
                    return
                tmp = self.conf + ".restore.tmp"
                shutil.copyfile(self._conf_bak, tmp)
                try:
                    os.chmod(tmp, 0o600)
                except Exception:
                    pass
                os.replace(tmp, self.conf)
                logger.warning(
                    "rclone.conf was missing/corrupt; restored from %s",
                    self._conf_bak)
            except Exception as e:
                logger.warning("rclone.conf restore failed: %s", e)

    def snapshot_conf(self) -> bool:
        """If the live conf is healthy and differs from the backup, atomically
        refresh the backup. Safe to call from any thread; no-op when nothing
        has changed."""
        with self._conf_lock:
            try:
                if not self._conf_is_healthy(self.conf):
                    return False
                with open(self.conf, "rb") as f:
                    data = f.read()
                try:
                    with open(self._conf_bak, "rb") as f:
                        if f.read() == data:
                            return False
                except FileNotFoundError:
                    pass
                tmp = self._conf_bak + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                try:
                    os.chmod(tmp, 0o600)
                except Exception:
                    pass
                os.replace(tmp, self._conf_bak)
                return True
            except Exception as e:
                logger.warning("rclone.conf snapshot failed: %s", e)
                return False

    def _base_args(self) -> List[str]:
        # Restore from backup if rclone.conf has been truncated or lost --
        # see comment in __init__.
        try:
            self._conf_pre_invoke()
        except Exception:
            pass
        return [
            self.binary or "rclone",
            "--config", self.conf,
            "--cache-dir", CACHE_DIR,
            "--temp-dir", TMP_DIR,
        ]

    def get_remote_section(self, name: str) -> Dict[str, str]:
        """Read a single ``[name]`` section out of rclone.conf as a dict.

        Used by the in-browser OAuth Device-Flow path so we can re-use the
        client_id / client_secret the user already configured (rclone stores
        them per-remote when supplied at ``rclone authorize`` time). We do
        the parse ourselves rather than shelling out to ``rclone config dump``
        because we want to keep this cheap and not trip the conf-guard
        snapshot/restore on every keystroke. Returns ``{}`` on any error.
        """
        try:
            with open(self.conf, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except Exception:
            return {}
        out: Dict[str, str] = {}
        in_section = False
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                in_section = (line[1:-1].strip() == name)
                continue
            if not in_section:
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
        return out

    def list_remotes(self) -> List[str]:
        if IS_MOCK:
            return ["gdrive", "dropbox"]
        if not self.binary:
            return []
        try:
            res = subprocess.run(self._base_args() + ["listremotes"],
                                 capture_output=True, text=True, timeout=5)
            return [ln.rstrip(":") for ln in res.stdout.splitlines() if ln.strip()]
        except Exception as e:
            logger.warning("rclone listremotes failed: %s", e)
            return []

    def remote_about(self, remote: str) -> Dict[str, Any]:
        if IS_MOCK:
            return {"ok": True, "free": 10 * 1024**3, "used": 5 * 1024**3, "total": 15 * 1024**3}
        if not self.binary:
            return {"ok": False, "error": "rclone not installed"}
        try:
            # `about` on Google Drive can take 30+s on a slow link, so give
            # it 60s. `remote_ping` is the cheap auth/connectivity check used
            # by the health loop.
            res = subprocess.run(self._base_args() + ["about", "--json", remote + ":"],
                                 capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                return {"ok": False, "error": res.stderr.strip()[:500]}
            return {"ok": True, **json.loads(res.stdout)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remote_ping(self, remote: str) -> Dict[str, Any]:
        """Fast auth + connectivity check.

        ``rclone lsd --max-depth 1`` exercises the backend's auth & listing
        path without enumerating contents. Much faster than ``about`` on
        Google Drive (where ``about`` can take 30+s) while still detecting
        invalid_grant / 401 / token-expired errors so the reauth banner can
        fire.
        """
        if IS_MOCK:
            return {"ok": True}
        if not self.binary:
            return {"ok": False, "error": "rclone not installed"}
        try:
            res = subprocess.run(
                self._base_args() + ["lsd", remote + ":", "--max-depth", "1"],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                return {"ok": False, "error": res.stderr.strip()[:500]}
            return {"ok": True}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "rclone ping timed out after 30s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def size(self, remote: str, path: str = "") -> Dict[str, Any]:
        if IS_MOCK:
            return {"ok": True, "bytes": 1234567890, "count": 42}
        if not self.binary:
            return {"ok": False, "error": "rclone not installed"}
        target = "{}:{}".format(remote, path)
        try:
            res = subprocess.run(self._base_args() + ["size", "--json", target],
                                 capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                return {"ok": False, "error": res.stderr.strip()[:500]}
            return {"ok": True, **json.loads(res.stdout)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_remote(self, name: str) -> bool:
        if IS_MOCK:
            return True
        if not self.binary:
            return False
        try:
            subprocess.run(self._base_args() + ["config", "delete", name],
                           capture_output=True, text=True, timeout=10)
            return True
        except Exception:
            return False

    # ------- non-OAuth (basic-credentials) remotes: FTP / SFTP -------

    def obscure(self, password: str) -> Dict[str, Any]:
        """Run ``rclone obscure <password>`` and return the obfuscated form.
        rclone refuses plain passwords in its config file; the documented way
        to write FTP/SFTP credentials non-interactively is to obscure them
        first. We never log the password.
        """
        if IS_MOCK or not self.binary:
            return {"ok": True, "obscured": "MOCK_" + ("x" * len(password or ""))}
        if not password:
            return {"ok": True, "obscured": ""}
        try:
            res = subprocess.run([self.binary, "obscure", password],
                                 capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                return {"ok": False, "error": res.stderr.strip()[:300] or "rclone obscure failed"}
            return {"ok": True, "obscured": res.stdout.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_basic_remote(self, name: str, rclone_type: str,
                         params: Dict[str, str]) -> Dict[str, Any]:
        """Create a non-OAuth rclone remote (FTP/SFTP) via ``rclone config create``.
        ``params`` is a flat dict of rclone backend keys; empty values are
        skipped so rclone falls back to its own defaults.
        """
        if IS_MOCK:
            return {"ok": True}
        if not self.binary:
            return {"ok": False, "error": "rclone not installed"}
        argv = self._base_args() + ["config", "create", name, rclone_type]
        for k, v in params.items():
            if v is None or v == "":
                continue
            argv.append(k)
            argv.append(str(v))
        try:
            env = os.environ.copy()
            env["RCLONE_NON_INTERACTIVE"] = "1"
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=20, env=env)
            if res.returncode != 0:
                return {"ok": False,
                        "error": (res.stderr.strip() or res.stdout.strip())[:500]}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------- version / self-update -------

    @staticmethod
    def _normalize_version(s: Optional[str]) -> Optional[str]:
        if not s:
            return None
        s = s.strip()
        m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", s)
        return ("v" + m.group(1)) if m else None

    def version(self) -> Dict[str, Any]:
        """Return ``{ok, version, raw}`` for the locally installed rclone."""
        if IS_MOCK:
            return {"ok": True, "version": "v1.66.0", "raw": "rclone v1.66.0 (mock)"}
        if not self.binary:
            return {"ok": False, "error": "rclone not installed"}
        try:
            res = subprocess.run([self.binary, "version"],
                                 capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                return {"ok": False, "error": res.stderr.strip()[:300] or "non-zero exit"}
            first = (res.stdout.splitlines() or [""])[0]
            v = self._normalize_version(first)
            return {"ok": True, "version": v, "raw": first.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def latest_version(self, timeout: float = 8.0) -> Dict[str, Any]:
        """Query GitHub for the latest rclone release. stdlib-only."""
        if IS_MOCK:
            return {"ok": True, "version": "v1.67.0",
                    "url": "https://github.com/rclone/rclone/releases/latest",
                    "notes": "Mock release notes.", "published_at": None}
        url = "https://api.github.com/repos/rclone/rclone/releases/latest"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"botsyncd/{VERSION}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        tag = data.get("tag_name") or data.get("name")
        v = self._normalize_version(tag)
        if not v:
            return {"ok": False, "error": "could not parse tag"}
        notes = (data.get("body") or "").strip()
        if len(notes) > 4000:
            notes = notes[:4000] + "\n\n... (truncated)"
        return {"ok": True, "version": v,
                "url": data.get("html_url") or "https://github.com/rclone/rclone/releases/latest",
                "notes": notes,
                "published_at": data.get("published_at")}

    def selfupdate(self, beta: bool = False, timeout: float = 600.0) -> Dict[str, Any]:
        """Run ``rclone selfupdate`` (built into rclone >= 1.55).

        Returns ``{ok, stdout, stderr, version_before, version_after, error?}``.
        Performs an in-place binary swap; rclone handles atomic replacement.
        On low-memory devices (e.g. routers) the kernel may OOM-kill the
        selfupdate process; we detect that and surface actionable guidance.
        """
        if IS_MOCK:
            return {"ok": True, "stdout": "mock: pretending to update",
                    "stderr": "", "version_before": "v1.66.0",
                    "version_after": "v1.67.0"}
        if not self.binary:
            return {"ok": False, "error": "rclone not installed"}
        before = self.version().get("version")

        # Best-effort: reap any stuck selfupdate processes from prior failed
        # attempts and drop the page cache to free RAM. Both are advisory and
        # safe to ignore failures.
        try:
            ps = subprocess.run(["ps", "w"], capture_output=True, text=True, timeout=5)
            for line in (ps.stdout or "").splitlines():
                if "rclone selfupdate" in line:
                    parts = line.strip().split()
                    if parts and parts[0].isdigit():
                        subprocess.run(["kill", "-9", parts[0]],
                                       capture_output=True, timeout=3)
        except Exception:
            pass
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
        except Exception:
            pass

        args = [self.binary, "selfupdate"]
        if beta:
            args.append("--beta")
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "selfupdate timed out",
                    "version_before": before}
        except Exception as e:
            return {"ok": False, "error": str(e), "version_before": before}
        ok = res.returncode == 0
        after = self.version().get("version") if ok else before
        out = (res.stdout or "").strip()[:2000]
        err = (res.stderr or "").strip()[:2000]
        # Reason: surface a human-readable cause when rclone exits via signal
        # (returncode<0 on POSIX). The most common case on routers is OOM kill.
        err_msg: Optional[str] = None
        if not ok:
            rc = res.returncode
            if rc < 0:
                sig = -rc
                # SIGKILL=9 is the OOM-killer's signal of choice on Linux.
                if sig == 9:
                    err_msg = ("rclone selfupdate was killed (out of memory). "
                               "This device does not have enough free RAM to "
                               "self-update; reboot to free memory or update "
                               "rclone manually (e.g. opkg / wget).")
                else:
                    err_msg = f"rclone selfupdate killed by signal {sig}"
            elif rc == 137:
                err_msg = ("rclone selfupdate was killed (likely out of "
                           "memory). Reboot to free memory or update rclone "
                           "manually.")
            else:
                err_msg = err or out or f"selfupdate failed (exit {rc})"
        return {
            "ok": ok,
            "stdout": out,
            "stderr": err,
            "returncode": res.returncode,
            "version_before": before,
            "version_after": after,
            "error": None if ok else err_msg,
        }


# ---------------------------------------------------------------------------
# OAuth helper (rclone authorize)
# ---------------------------------------------------------------------------

def _ensure_default_for_provider(d: Dict[str, Any], name: str) -> None:
    """Mark ``name`` as the default account for its provider if no other
    account of the same provider is currently flagged default. Called from
    every place we create a new entry in ``d['remotes']`` so the first
    Dropbox / Drive / FTP / SFTP account added becomes the implicit default
    without forcing the user to click anything. The user can still override
    later via ``POST /api/remotes/<name>/default``."""
    rec = (d.get("remotes") or {}).get(name)
    if not rec:
        return
    provider = rec.get("provider")
    if not provider:
        return
    has_default = any(r.get("provider") == provider and r.get("default")
                      for n, r in d["remotes"].items() if n != name)
    if not has_default:
        rec["default"] = True


def _promote_replacement_default(d: Dict[str, Any], removed: Dict[str, Any]) -> None:
    """If the removed account was the default for its provider, promote
    another account of the same provider so the next download/upload add
    still finds a default. We pick the alphabetically-first remaining
    account so the choice is deterministic."""
    if not removed or not removed.get("default"):
        return
    provider = removed.get("provider")
    if not provider:
        return
    survivors = sorted(n for n, r in (d.get("remotes") or {}).items()
                       if r.get("provider") == provider)
    if survivors:
        d["remotes"][survivors[0]]["default"] = True


class OAuthHelper:
    """
    Headless rclone OAuth flow.

    Running ``rclone authorize`` on the router itself is wrong: it spins up an
    OAuth callback listener on ``127.0.0.1:53682`` of *that* host, but the
    user's browser is on a different machine, and the OAuth providers reject
    public IPs as redirect targets. The official rclone story for boxes
    without a browser is "headless setup" — the user runs
    ``rclone authorize <type>`` on a machine that *does* have a browser, the
    binary spawns a localhost callback there, prints a JSON blob once auth
    completes, and the JSON is pasted back into the headless box, which then
    calls ``rclone config create ... token=<blob>``.

    So we no longer spawn ``rclone authorize`` here. Instead we hand the UI a
    copy-pasteable command, an https link to the rclone download page, and
    register a session so :meth:`finish` can call ``config create`` once the
    user pastes the token.

    In mock mode we still hand out fake URLs so the desktop dev flow works.
    """
    def __init__(self, store: Store, rclone: Rclone) -> None:
        self.store = store
        self.rclone = rclone
        self._lock = threading.Lock()

    def start(self, provider: str, name: str) -> Dict[str, Any]:
        if provider not in PROVIDERS:
            return {"ok": False, "error": "unknown provider"}
        sid = secrets.token_hex(8)
        rclone_type = PROVIDERS[provider]["rclone_type"]

        if IS_MOCK or not self.rclone.binary:
            auth_url = f"https://example.com/mock-oauth/{provider}/{sid}"
            self.store.update(lambda d: d["oauth_sessions"].update({
                sid: {"provider": provider, "name": name, "type": rclone_type,
                      "auth_url": auth_url, "status": "awaiting_token",
                      "started_at": time.time()}
            }))
            return {
                "ok": True, "session_id": sid, "auth_url": auth_url,
                "instructions": "MOCK MODE — paste any text as the token to finish.",
                "command": f'rclone authorize "{rclone_type}"',
                "headless": True,
            }

        # Real mode: do NOT spawn rclone here. Just register a session.
        self.store.update(lambda d: d["oauth_sessions"].update({
            sid: {"provider": provider, "name": name, "type": rclone_type,
                  "auth_url": None, "status": "awaiting_token",
                  "started_at": time.time()}
        }))
        return {
            "ok": True, "session_id": sid,
            "auth_url": "https://rclone.org/downloads/",
            "command": f'rclone authorize "{rclone_type}"',
            "headless": True,
            "instructions": (
                "On your laptop or desktop (anywhere with a browser and rclone "
                "installed):\n"
                f"  1. Run:  rclone authorize \"{rclone_type}\"\n"
                f"     - Windows (PowerShell, if rclone.exe is in the current folder):\n"
                f"           .\\rclone authorize \"{rclone_type}\"\n"
                f"     - Windows (CMD): rclone authorize \"{rclone_type}\"\n"
                f"     - macOS / Linux: rclone authorize \"{rclone_type}\"\n"
                "  2. A browser tab opens. Sign in to your account and grant access.\n"
                "  3. rclone prints a JSON blob that begins with {\"token\":...} or "
                "{\"access_token\":...}.\n"
                "  4. Copy the entire JSON (including the surrounding braces) and "
                "paste it below.\n"
                "If you don't have rclone yet, get a static binary from "
                "https://rclone.org/downloads/. On Windows, place rclone.exe in "
                "a folder you can `cd` into, and run it as `.\\rclone` from "
                "PowerShell (PowerShell does not run executables in the current "
                "directory unless you prefix them with `.\\`)."
            ),
        }

    def finish(self, session_id: str, token_blob: str) -> Dict[str, Any]:
        sess = self.store.get()["oauth_sessions"].get(session_id)
        if not sess:
            return {"ok": False, "error": "unknown session"}

        if IS_MOCK or not self.rclone.binary:
            # Persist a fake remote.
            def _add(d: Dict[str, Any]) -> None:
                d["remotes"][sess["name"]] = {
                    "provider": sess["provider"], "type": sess["type"],
                    "health": "ok", "last_check": time.time(),
                    "expires_at": None, "error": None,
                }
                _ensure_default_for_provider(d, sess["name"])
                d["oauth_sessions"].pop(session_id, None)
            self.store.update(_add)
            return {"ok": True}

        # Real: call rclone config create <name> <type> token=<blob>.
        # `config_is_local=false` is a documented backend parameter that tells
        # rclone the supplied token came from another machine, so it must NOT
        # try to refresh it (refresh starts the OAuth webserver on
        # 127.0.0.1:53682, which doesn't work on a router and collides if
        # anything else is bound there). We also set RCLONE_NON_INTERACTIVE=1
        # via env so older rclone builds — which don't accept
        # `--non-interactive` as a global flag before `config` — still pick it
        # up. The env-var form has been supported since rclone 1.55.
        try:
            env = os.environ.copy()
            env["RCLONE_NON_INTERACTIVE"] = "1"
            res = subprocess.run(
                [self.rclone.binary, "--config", self.rclone.conf,
                 "config", "create",
                 sess["name"], sess["type"],
                 "config_is_local", "false",
                 "token", token_blob],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if res.returncode != 0:
                return {"ok": False, "error": res.stderr.strip()[:500] or res.stdout.strip()[:500]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        def _add(d: Dict[str, Any]) -> None:
            d["remotes"][sess["name"]] = {
                "provider": sess["provider"], "type": sess["type"],
                "health": "unknown", "last_check": None,
                "expires_at": None, "error": None,
            }
            _ensure_default_for_provider(d, sess["name"])
            d["oauth_sessions"].pop(session_id, None)
        self.store.update(_add)
        return {"ok": True}

    # ----- in-browser Google Drive OAuth (Device Authorization Grant) -----
    #
    # Google's "OAuth 2.0 for Limited Input Devices" lets a headless box like
    # this router obtain tokens without requiring the user to install rclone
    # on a laptop. Flow:
    #   1. POST client_id+scope to https://oauth2.googleapis.com/device/code,
    #      get back a short user_code + verification_url + device_code.
    #   2. Show user_code/url to the user; they sign in on any device that has
    #      a browser (their phone is fine).
    #   3. Poll https://oauth2.googleapis.com/token with the device_code until
    #      Google returns access_token + refresh_token.
    #   4. Compose an rclone-compatible token JSON and write it via
    #      ``rclone config update <name> token=<json>`` -- preserving the
    #      existing client_id / client_secret on the remote.
    #
    # Requires an OAuth 2.0 Client of type "Desktop" *or* "TVs and Limited
    # Input Devices" in the user's Google Cloud project. See README for
    # setup. Reauth re-uses whatever client_id/secret were saved on the
    # remote at first-time setup; new remotes need them in the start body.

    GOOGLE_DEVICE_URL = "https://oauth2.googleapis.com/device/code"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

    @staticmethod
    def _http_post_form(url: str, fields: Dict[str, str], timeout: int = 20
                        ) -> Tuple[int, Dict[str, Any]]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"raw": raw}
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"error": "http_error", "raw": raw}
        except Exception as e:
            return 0, {"error": "network_error", "detail": str(e)}

    def start_device(self, name: str, client_id: str = "",
                     client_secret: str = "") -> Dict[str, Any]:
        """Begin a Google Drive device-flow OAuth.

        For an existing remote we look up its client_id/secret from
        rclone.conf so the user doesn't have to re-enter them. For a brand-
        new remote both must be supplied in the body.
        """
        existing = self.rclone.get_remote_section(name) if name else {}
        cid = (client_id or existing.get("client_id") or "").strip()
        csec = (client_secret or existing.get("client_secret") or "").strip()
        if not cid or not csec:
            return {"ok": False, "error": (
                "Google OAuth client_id and client_secret are required. "
                "Create an OAuth client in Google Cloud Console (see README, "
                "'Google Cloud OAuth setup') and supply both fields."),
                "needs_credentials": True}

        if IS_MOCK:
            sid = secrets.token_hex(8)
            self.store.update(lambda d: d["oauth_sessions"].update({
                sid: {"provider": "drive", "name": name, "type": "drive",
                      "client_id": cid, "client_secret": csec,
                      "device_code": "MOCK-DEVICE", "interval": 2,
                      "expires_at": time.time() + 300,
                      "status": "awaiting_user", "started_at": time.time(),
                      "flow": "device"}
            }))
            return {"ok": True, "session_id": sid,
                    "verification_url": "https://www.google.com/device",
                    "verification_url_complete":
                        "https://www.google.com/device?user_code=MOCK-CODE",
                    "user_code": "MOCK-CODE", "interval": 2, "expires_in": 300}

        status, payload = self._http_post_form(
            self.GOOGLE_DEVICE_URL,
            {"client_id": cid, "scope": self.GOOGLE_DRIVE_SCOPE},
        )
        if status != 200:
            return {"ok": False, "error": (
                "Google rejected the device-code request: "
                + str(payload.get("error_description")
                      or payload.get("error")
                      or payload.get("raw")
                      or status)),
                "hint": (
                    "Most common cause: your OAuth client type does not "
                    "permit the device flow. In Google Cloud Console "
                    "create credentials of type 'Desktop' or 'TVs and "
                    "Limited Input Devices' and use that client_id/secret.")}

        sid = secrets.token_hex(8)
        device_code = payload.get("device_code", "")
        user_code = payload.get("user_code", "")
        verification_url = (payload.get("verification_url")
                            or payload.get("verification_uri")
                            or "https://www.google.com/device")
        verification_url_complete = (
            payload.get("verification_url_complete")
            or payload.get("verification_uri_complete")
            or (verification_url + "?user_code=" + urllib.parse.quote(user_code)))
        interval = int(payload.get("interval", 5)) or 5
        expires_in = int(payload.get("expires_in", 1800)) or 1800

        self.store.update(lambda d: d["oauth_sessions"].update({
            sid: {"provider": "drive", "name": name, "type": "drive",
                  "client_id": cid, "client_secret": csec,
                  "device_code": device_code, "interval": interval,
                  "expires_at": time.time() + expires_in,
                  "next_poll_at": 0.0,
                  "status": "awaiting_user", "started_at": time.time(),
                  "flow": "device"}
        }))
        return {"ok": True, "session_id": sid,
                "verification_url": verification_url,
                "verification_url_complete": verification_url_complete,
                "user_code": user_code, "interval": interval,
                "expires_in": expires_in}

    def poll_device(self, session_id: str) -> Dict[str, Any]:
        """Called by the UI on a timer until ``status`` is ``done`` or
        ``error``. Returns ``{ok: true, status: 'pending'|'done'|'error', ...}``.
        Idempotent: rate-limited so the UI may call faster than the Google
        polling interval without us spamming Google."""
        sess = self.store.get()["oauth_sessions"].get(session_id)
        if not sess or sess.get("flow") != "device":
            return {"ok": False, "error": "unknown session"}
        if sess.get("status") == "done":
            return {"ok": True, "status": "done"}
        if time.time() > sess.get("expires_at", 0):
            self.store.update(lambda d: d["oauth_sessions"].pop(session_id, None))
            return {"ok": False, "status": "error",
                    "error": "device code expired — start over"}

        # Honour Google's polling interval to avoid slow_down.
        if time.time() < sess.get("next_poll_at", 0):
            return {"ok": True, "status": "pending"}

        if IS_MOCK:
            # In mock mode complete after ~3s.
            if time.time() - sess.get("started_at", 0) < 3:
                self.store.update(lambda d: d["oauth_sessions"][session_id].update(
                    {"next_poll_at": time.time() + sess["interval"]}))
                return {"ok": True, "status": "pending"}
            def _addmock(d: Dict[str, Any]) -> None:
                d["remotes"][sess["name"]] = {
                    "provider": "drive", "type": "drive",
                    "health": "ok", "last_check": time.time(),
                    "expires_at": None, "error": None, "needs_reauth": False,
                }
                _ensure_default_for_provider(d, sess["name"])
                d["oauth_sessions"][session_id]["status"] = "done"
            self.store.update(_addmock)
            return {"ok": True, "status": "done"}

        status, payload = self._http_post_form(
            self.GOOGLE_TOKEN_URL,
            {"client_id": sess["client_id"],
             "client_secret": sess["client_secret"],
             "device_code": sess["device_code"],
             "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        )
        err = (payload or {}).get("error", "")

        # Pending / slow_down: back off and keep polling.
        if err in ("authorization_pending", "slow_down"):
            backoff = sess["interval"] + (5 if err == "slow_down" else 0)
            self.store.update(lambda d: d["oauth_sessions"][session_id].update(
                {"next_poll_at": time.time() + backoff,
                 "interval": backoff if err == "slow_down" else sess["interval"]}))
            return {"ok": True, "status": "pending"}

        if status != 200 or "access_token" not in (payload or {}):
            self.store.update(lambda d: d["oauth_sessions"].pop(session_id, None))
            return {"ok": False, "status": "error",
                    "error": (payload.get("error_description")
                              or payload.get("error")
                              or "token exchange failed")}

        # Build rclone-compatible token JSON and persist via config update.
        expiry_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000000000Z",
            time.gmtime(time.time() + int(payload.get("expires_in", 3600))))
        token_json = json.dumps({
            "access_token": payload["access_token"],
            "token_type": payload.get("token_type", "Bearer"),
            "refresh_token": payload.get("refresh_token", ""),
            "expiry": expiry_iso,
        })

        try:
            env = os.environ.copy()
            env["RCLONE_NON_INTERACTIVE"] = "1"
            # `config update` if remote exists, else `config create`.
            existing = self.rclone.get_remote_section(sess["name"])
            verb = "update" if existing else "create"
            args = [self.rclone.binary, "--config", self.rclone.conf,
                    "config", verb, sess["name"]]
            if verb == "create":
                args.append(sess["type"])
            args += ["client_id", sess["client_id"],
                     "client_secret", sess["client_secret"],
                     "config_is_local", "false",
                     "token", token_json]
            res = subprocess.run(args, capture_output=True, text=True,
                                 timeout=30, env=env)
            if res.returncode != 0:
                self.store.update(lambda d: d["oauth_sessions"].pop(session_id, None))
                return {"ok": False, "status": "error",
                        "error": (res.stderr.strip()[:500]
                                  or res.stdout.strip()[:500]
                                  or "rclone config update failed")}
        except Exception as e:
            self.store.update(lambda d: d["oauth_sessions"].pop(session_id, None))
            return {"ok": False, "status": "error", "error": str(e)}

        # Snapshot the freshly-written conf and clear reauth.
        try:
            self.rclone.snapshot_conf()
        except Exception:
            pass

        def _done(d: Dict[str, Any]) -> None:
            d["remotes"].setdefault(sess["name"], {
                "provider": "drive", "type": "drive",
                "health": "unknown", "last_check": None,
                "expires_at": None, "error": None,
            })
            d["remotes"][sess["name"]]["needs_reauth"] = False
            d["remotes"][sess["name"]]["error"] = None
            _ensure_default_for_provider(d, sess["name"])
            d["oauth_sessions"][session_id]["status"] = "done"
        self.store.update(_done)
        return {"ok": True, "status": "done"}



# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class Notifier:
    """Multi-channel notifier. Channels post via stdlib only.

    Each channel has:
        kind      one of NOTIFY_CHANNEL_KINDS
        label     human label
        config    dict (per-kind)
        events    list of event types this channel cares about (or ["*"])
        min_severity  "info"|"warn"|"error" — drop anything lower
        enabled   bool

    Events are also recorded in a ring buffer (state.notifications.events).
    Rate limit: per-channel min interval (default 2s) and a global burst limiter
    so a flood of jobs doesn't spam Discord.
    """

    EVENTS_BUFFER_MAX = 200

    def __init__(self, store: "Store") -> None:
        self.store = store
        self._lock = threading.Lock()
        self._send_queue: collections.deque = collections.deque()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        t = threading.Thread(target=self._worker, daemon=True, name="notifier")
        t.start()

    # ---------- public ----------

    def emit(self, event_type: str, message: str, **fields: Any) -> None:
        sev = EVENT_SEVERITY.get(event_type, "info")
        evt = {
            "ts": time.time(),
            "type": event_type,
            "severity": sev,
            "message": message,
            "fields": {k: v for k, v in fields.items() if v is not None},
        }
        # record to ring buffer
        def _push(d: Dict[str, Any]) -> None:
            buf = d["notifications"].setdefault("events", [])
            buf.append(evt)
            if len(buf) > self.EVENTS_BUFFER_MAX:
                del buf[: len(buf) - self.EVENTS_BUFFER_MAX]
        self.store.update(_push)
        logger.info("event %s [%s] %s", event_type, sev, message)
        # enqueue dispatch
        with self._cv:
            self._send_queue.append(evt)
            self._cv.notify()

    def channels(self) -> Dict[str, Any]:
        return self.store.get()["notifications"].get("channels", {})

    def upsert_channel(self, cid: Optional[str], data: Dict[str, Any]) -> str:
        cid = cid or secrets.token_hex(6)
        kind = data.get("kind")
        if kind not in NOTIFY_CHANNEL_KINDS:
            raise ValueError("unknown channel kind: " + str(kind))
        record = {
            "kind": kind,
            "label": data.get("label") or NOTIFY_CHANNEL_KINDS[kind]["label"],
            "config": data.get("config") or {},
            "events": data.get("events") or ["*"],
            "min_severity": data.get("min_severity") or "info",
            "enabled": bool(data.get("enabled", True)),
            "last_send": None, "last_error": None,
        }
        def _u(d: Dict[str, Any]) -> None:
            existing = d["notifications"]["channels"].get(cid, {})
            existing.update(record)
            d["notifications"]["channels"][cid] = existing
        self.store.update(_u)
        return cid

    def delete_channel(self, cid: str) -> None:
        self.store.update(lambda d: d["notifications"]["channels"].pop(cid, None))

    def test_channel(self, cid: str) -> Dict[str, Any]:
        ch = self.channels().get(cid)
        if not ch:
            return {"ok": False, "error": "channel not found"}
        evt = {"ts": time.time(), "type": "test", "severity": "info",
               "message": "BOT-SYNC test notification", "fields": {"channel": ch.get("label")}}
        return self._dispatch_one(cid, ch, evt)

    def events(self, n: int = 100) -> List[Dict[str, Any]]:
        return list(self.store.get()["notifications"].get("events", []))[-n:]

    # ---------- worker ----------

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._send_queue and not self._stop.is_set():
                    self._cv.wait(timeout=2)
                if self._stop.is_set():
                    return
                evt = self._send_queue.popleft() if self._send_queue else None
            if not evt:
                continue
            channels = self.channels()
            for cid, ch in channels.items():
                if not ch.get("enabled"):
                    continue
                if not self._matches(ch, evt):
                    continue
                try:
                    self._dispatch_one(cid, ch, evt)
                except Exception as e:
                    logger.exception("notify channel %s failed", cid)
                    self._record_error(cid, str(e))

    @staticmethod
    def _matches(ch: Dict[str, Any], evt: Dict[str, Any]) -> bool:
        sev_order = {"info": 0, "warn": 1, "error": 2}
        if sev_order.get(evt["severity"], 0) < sev_order.get(ch.get("min_severity", "info"), 0):
            return False
        events = ch.get("events") or ["*"]
        if "*" in events or evt["type"] in events:
            return True
        return False

    def _record_error(self, cid: str, err: Optional[str]) -> None:
        def _u(d: Dict[str, Any]) -> None:
            ch = d["notifications"]["channels"].get(cid)
            if not ch:
                return
            ch["last_send"] = time.time()
            ch["last_error"] = err
        self.store.update(_u)

    # ---------- per-channel dispatch ----------

    def _dispatch_one(self, cid: str, ch: Dict[str, Any], evt: Dict[str, Any]) -> Dict[str, Any]:
        kind = ch["kind"]
        try:
            if kind == "discord":
                self._send_discord(ch, evt)
            elif kind == "slack":
                self._send_slack(ch, evt)
            elif kind == "webhook":
                self._send_webhook(ch, evt)
            elif kind == "ntfy":
                self._send_ntfy(ch, evt)
            elif kind == "email":
                self._send_email(ch, evt)
            else:
                raise ValueError("unsupported channel kind: " + kind)
            self._record_error(cid, None)
            return {"ok": True}
        except Exception as e:
            self._record_error(cid, str(e))
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _http_post_json(url: str, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None,
                        timeout: int = 10) -> None:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"botsyncd/{VERSION}")
        if extra_headers:
            for k, v in extra_headers.items():
                req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"http {resp.status}: {resp.read()[:200]!r}")

    @staticmethod
    def _http_post(url: str, body: bytes, headers: Dict[str, str], timeout: int = 10) -> None:
        import urllib.request
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"http {resp.status}: {resp.read()[:200]!r}")

    def _send_discord(self, ch: Dict[str, Any], evt: Dict[str, Any]) -> None:
        url = (ch.get("config") or {}).get("url")
        if not url:
            raise ValueError("discord: webhook url missing")
        color = {"info": 0x4a8cff, "warn": 0xf3b54a, "error": 0xef5d5d}.get(evt["severity"], 0x808080)
        fields_list = [{"name": k, "value": str(v)[:1000], "inline": True}
                       for k, v in (evt.get("fields") or {}).items()][:10]
        embed = {
            "title": f"[{evt['severity'].upper()}] {evt['type']}",
            "description": evt["message"][:2000],
            "color": color,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(evt["ts"])),
            "fields": fields_list,
        }
        self._http_post_json(url, {"username": "botsync", "embeds": [embed]})

    def _send_slack(self, ch: Dict[str, Any], evt: Dict[str, Any]) -> None:
        url = (ch.get("config") or {}).get("url")
        if not url:
            raise ValueError("slack: webhook url missing")
        emoji = {"info": ":information_source:", "warn": ":warning:", "error": ":rotating_light:"}.get(evt["severity"], "")
        text = f"{emoji} *{evt['type']}* — {evt['message']}"
        fields_str = "\n".join(f"• *{k}*: {v}" for k, v in (evt.get("fields") or {}).items())
        if fields_str:
            text += "\n" + fields_str
        self._http_post_json(url, {"text": text})

    def _send_webhook(self, ch: Dict[str, Any], evt: Dict[str, Any]) -> None:
        cfg = ch.get("config") or {}
        url = cfg.get("url")
        if not url:
            raise ValueError("webhook: url missing")
        headers = {}
        if cfg.get("auth_header"):
            headers["Authorization"] = cfg["auth_header"]
        self._http_post_json(url, evt, extra_headers=headers)

    def _send_ntfy(self, ch: Dict[str, Any], evt: Dict[str, Any]) -> None:
        cfg = ch.get("config") or {}
        base = (cfg.get("url") or "https://ntfy.sh").rstrip("/")
        topic = cfg.get("topic")
        if not topic:
            raise ValueError("ntfy: topic missing")
        prio = {"info": "3", "warn": "4", "error": "5"}.get(evt["severity"], "3")
        body = (evt["message"] + ("\n" + "\n".join(f"{k}: {v}" for k, v in (evt.get("fields") or {}).items())
                                   if evt.get("fields") else "")).encode("utf-8")
        headers = {
            "Title": f"BOT-SYNC · {evt['type']}",
            "Priority": prio,
            "Tags": evt["severity"],
            "User-Agent": f"botsyncd/{VERSION}",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if cfg.get("auth_header"):
            headers["Authorization"] = cfg["auth_header"]
        self._http_post(f"{base}/{topic}", body, headers)

    def _send_email(self, ch: Dict[str, Any], evt: Dict[str, Any]) -> None:
        import smtplib, ssl
        from email.message import EmailMessage
        cfg = ch.get("config") or {}
        host = cfg.get("host"); port = int(cfg.get("port") or 587)
        if not host:
            raise ValueError("email: smtp host missing")
        # tls_mode: "ssl" (implicit, port 465), "starttls" (default), "none"
        # Falls back to legacy use_tls boolean for backward compat.
        mode = (cfg.get("tls_mode") or "").lower().strip()
        if not mode:
            if port == 465:
                mode = "ssl"
            elif cfg.get("use_tls", True):
                mode = "starttls"
            else:
                mode = "none"
        msg = EmailMessage()
        prefix = cfg.get("subject_prefix") or "BOT-SYNC"
        msg["Subject"] = f"[{prefix} {evt['severity']}] {evt['type']}"
        msg["From"] = cfg.get("from") or (cfg.get("username") or "botsync@localhost")
        msg["To"] = cfg.get("to") or ""
        if not msg["To"]:
            raise ValueError("email: 'to' address missing")
        body_lines = [evt["message"], ""]
        for k, v in (evt.get("fields") or {}).items():
            body_lines.append(f"{k}: {v}")
        body_lines += ["", f"time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(evt['ts']))}"]
        msg.set_content("\n".join(body_lines))
        ctx = ssl.create_default_context()
        if mode == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
                if cfg.get("username"):
                    s.login(cfg["username"], cfg.get("password") or "")
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                if mode == "starttls":
                    s.starttls(context=ctx)
                    s.ehlo()
                if cfg.get("username"):
                    s.login(cfg["username"], cfg.get("password") or "")
                s.send_message(msg)


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, jid: str, jtype: str, target_id: str, label: str) -> None:
        self.id = jid
        self.type = jtype          # "download" | "upload" | "size" | "resync"
        self.target_id = target_id
        self.label = label
        self.state = "queued"      # queued | running | done | error | cancelled
        self.progress = 0.0
        self.bytes_done = 0
        self.bytes_total = 0
        self.eta_seconds = 0
        self.transfer_rate = 0     # bytes/sec
        # Number of files actually transferred during this run, surfaced
        # from rclone's JSON stats.transfers field. Used by the notifier
        # to decide whether a "completed" event is worth pinging Discord
        # about — once a folder has synced once, we suppress no-op runs.
        self.files_transferred = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.log: collections.deque = collections.deque(maxlen=200)
        self._proc: Optional[subprocess.Popen] = None
        self._cancel = threading.Event()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "target_id": self.target_id,
            "label": self.label, "state": self.state, "progress": round(self.progress, 1),
            "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
            "files_transferred": self.files_transferred,
            "eta_seconds": self.eta_seconds, "transfer_rate": self.transfer_rate,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "error": self.error, "log_tail": list(self.log)[-20:],
        }


class _Limiter:
    """Resizable counting semaphore.

    Used to cap how many jobs of a given type (download/upload) may run
    concurrently. Unlike :class:`threading.BoundedSemaphore` the cap can
    be changed at runtime — increasing it wakes waiting threads, decreasing
    it just means future ``acquire`` calls block until in-flight slots are
    released.
    """

    def __init__(self, cap: int) -> None:
        self._cv = threading.Condition()
        self._cap = max(1, int(cap))
        self._used = 0

    def acquire(self) -> None:
        with self._cv:
            while self._used >= self._cap:
                self._cv.wait()
            self._used += 1

    def release(self) -> None:
        with self._cv:
            self._used = max(0, self._used - 1)
            self._cv.notify_all()

    def set_cap(self, cap: int) -> int:
        with self._cv:
            self._cap = max(1, int(cap))
            self._cv.notify_all()
            return self._cap

    @property
    def cap(self) -> int:
        with self._cv:
            return self._cap


class JobManager:
    def __init__(self, store: Store, rclone: Rclone, notifier: Optional["Notifier"] = None) -> None:
        self.store = store
        self.rclone = rclone
        self.notifier = notifier
        self.on_complete: Optional[Callable[["Job"], None]] = None
        self._jobs: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: collections.deque = collections.deque()
        self._workers_started = False
        self._worker_count = 0
        self._stop = threading.Event()
        # Per-type concurrency caps (resizable at runtime).
        self._limiters: Dict[str, _Limiter] = {
            "download": _Limiter(1),
            "upload":   _Limiter(1),
        }
        # Global cap across ALL job types. The per-type caps above only
        # bound each lane; on a 256 MB router two simultaneous rclones
        # (one download + one upload, each at default per-type cap=1) is
        # already enough to OOM. This top-level limiter is the real
        # safety belt — see HARDWARE_PRESETS["router"]["max_global_jobs"].
        self._global: _Limiter = _Limiter(1)

    def start_workers(self, dl_max: int = 1, ul_max: int = 1) -> None:
        """Spawn enough worker threads to handle dl_max + ul_max concurrent
        jobs (plus a small buffer for short-lived non-transfer jobs)."""
        if self._workers_started:
            self.set_concurrency("download", dl_max)
            self.set_concurrency("upload", ul_max)
            return
        self._workers_started = True
        self.set_concurrency("download", dl_max)
        self.set_concurrency("upload", ul_max)
        self._ensure_workers(max(2, int(dl_max) + int(ul_max)))

    def _ensure_workers(self, target: int) -> None:
        """Spawn additional worker threads up to *target*. Idempotent."""
        with self._lock:
            need = max(0, int(target) - self._worker_count)
            if need <= 0:
                return
            start = self._worker_count
            self._worker_count += need
        for i in range(need):
            t = threading.Thread(target=self._worker,
                                 name=f"job-worker-{start + i}", daemon=True)
            t.start()

    def set_concurrency(self, jtype: str, n: int) -> int:
        """Update the cap for a job type, spawning more workers if needed."""
        lim = self._limiters.get(jtype)
        if lim is None:
            return 0
        cap = lim.set_cap(n)
        # Make sure we have enough threads to fill the new cap of this type
        # plus the cap of every other type.
        total = sum(l.cap for l in self._limiters.values())
        self._ensure_workers(max(2, total))
        return cap

    def get_concurrency(self) -> Dict[str, int]:
        return {k: l.cap for k, l in self._limiters.items()}

    def set_global_cap(self, n: int) -> int:
        """Cap on the *total* number of concurrently-running jobs across
        download + upload lanes. Returns the applied cap."""
        n = max(1, int(n))
        cap = self._global.set_cap(n)
        # Worker count must cover the SUM of per-type caps so the global
        # limiter is the binding constraint, not thread starvation.
        total = sum(l.cap for l in self._limiters.values())
        self._ensure_workers(max(2, total))
        return cap

    def apply_preset(self, preset: Dict[str, Any]) -> Dict[str, int]:
        """Apply a HARDWARE_PRESETS-shaped dict's concurrency settings.
        rclone *flags* are read live by runner_rclone — only the worker
        caps need to be plumbed here."""
        gmax = max(1, int(preset.get("max_global_jobs", 1)))
        # Per-type caps default to the global cap so a single lane can
        # use the whole budget when the other is idle.
        dl = max(1, int(preset.get("download_concurrency", gmax)))
        ul = max(1, int(preset.get("upload_concurrency", gmax)))
        self.set_global_cap(gmax)
        self.set_concurrency("download", dl)
        self.set_concurrency("upload", ul)
        return {"global": gmax, "download": dl, "upload": ul}

    def _target_first_sync(self, job: "Job") -> bool:
        """Return True iff this download/upload entry has never completed
        a successful sync before. Used to gate "started"/"completed" pings
        so a polling schedule on an unchanged folder doesn't spam Discord.

        Non download/upload jobs (size scans, ad-hoc admin tasks) always
        count as "first" — we don't want to silence those.
        """
        if job.type not in ("download", "upload"):
            return True
        try:
            d = self.store.get()
        except Exception:
            return True
        bucket = "downloads" if job.type == "download" else "uploads"
        item = (d.get(bucket) or {}).get(job.target_id)
        if not item:
            return True
        # last_sync is stamped on every successful run by _push_log; if
        # it's set, the entry has at least one prior good sync.
        return not bool(item.get("last_sync"))

    def submit(self, jtype: str, target_id: str, label: str, runner: Callable[[Job], None]) -> Job:
        jid = secrets.token_hex(6)
        job = Job(jid, jtype, target_id, label)
        with self._cv:
            self._jobs[jid] = job
            # cap retained job history: drop ALL terminal jobs in insertion
            # order until under cap (don't break on the first running job —
            # that left stale queued entries before it un-pruned).
            if len(self._jobs) > 50:
                terminal_keys = [k for k, j in self._jobs.items()
                                 if j.state in ("done", "error", "cancelled")]
                for k in terminal_keys:
                    if len(self._jobs) <= 50:
                        break
                    self._jobs.pop(k, None)
            self._queue.append((job, runner))
            self._cv.notify()
        return job

    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self._jobs.get(jid)
            if not j:
                return False
            j._cancel.set()
            proc = j._proc
        # Outside the lock: terminate gracefully, then SIGKILL on timeout so
        # zombies (rclone wedged in a syscall) don't pile up.
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        return True

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    def get(self, jid: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(jid)

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._queue and not self._stop.is_set():
                    self._cv.wait(timeout=1)
                if self._stop.is_set():
                    return
                if not self._queue:
                    continue
                job, runner = self._queue.popleft()
            # Gate execution by global cap first, then per-type concurrency.
            # The job stays in state="queued" while it waits for slots.
            self._global.acquire()
            limiter = self._limiters.get(job.type)
            if limiter is not None:
                limiter.acquire()
            try:
                job.state = "running"
                job.started_at = time.time()
                logger.info("job %s start: %s %s", job.id, job.type, job.label)
                # Decide whether this is the entry's *first* sync. If it
                # is, every event for the run gets pushed to Discord/etc.
                # If it has synced before, we suppress the per-run start
                # and "completed - 0 bytes" pings so repeat polls of an
                # unchanged folder don't spam the channel. Failures and
                # cancellations always notify regardless.
                first_sync = self._target_first_sync(job)
                if self.notifier and first_sync:
                    self.notifier.emit("job.started", f"{job.type}: {job.label}",
                                       job_id=job.id, type=job.type)
                try:
                    runner(job)
                    if job.state == "running":
                        job.state = "done"
                        job.progress = 100.0
                except Exception as e:
                    job.state = "error"
                    job.error = str(e)
                    job.log.append(f"ERROR: {e}")
                    logger.exception("job %s failed", job.id)
                job.finished_at = time.time()
                # Capture the final state ONCE so all downstream branches
                # (notifications, sync_log, last_sync stamp) see a consistent
                # value even if another thread mutates job.state mid-flight.
                final_state = job.state
                logger.info("job %s end: %s (%.1fs)", job.id, final_state,
                            (job.finished_at or 0) - (job.started_at or 0))
                if self.notifier:
                    dur = (job.finished_at or 0) - (job.started_at or 0)
                    if final_state == "done":
                        # Suppress no-op "completed" pings on repeat runs:
                        # if the entry has already synced once and this run
                        # transferred zero files / zero bytes, the channel
                        # would otherwise get spammed every schedule tick.
                        changed = (int(job.files_transferred or 0) > 0
                                   or int(job.bytes_done or 0) > 0)
                        if first_sync or changed:
                            self.notifier.emit(
                                "job.completed", f"{job.type} done: {job.label}",
                                job_id=job.id, bytes=job.bytes_done,
                                files_transferred=int(job.files_transferred or 0),
                                duration_s=int(dur))
                    elif final_state == "cancelled":
                        self.notifier.emit("job.cancelled", f"{job.type} cancelled: {job.label}",
                                           job_id=job.id)
                    else:
                        self.notifier.emit("job.failed", f"{job.type} failed: {job.label}",
                                           job_id=job.id, error=job.error)
                # Append to per-type sync history ring buffer (capped).
                if job.type in ("download", "upload"):
                    entry = {
                        "job_id": job.id,
                        "type": job.type,
                        "target_id": job.target_id,
                        "label": job.label,
                        "state": final_state,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                        "duration_s": int((job.finished_at or 0) - (job.started_at or 0)),
                        "bytes": int(job.bytes_done or 0),
                        "files_transferred": int(job.files_transferred or 0),
                        "error": job.error,
                    }
                    def _push_log(d: Dict[str, Any], _e=entry, _t=job.type, _j=job, _fs=final_state) -> None:
                        sl = d.setdefault("sync_log", {"download": [], "upload": []})
                        buf = sl.setdefault(_t, [])
                        buf.append(_e)
                        if len(buf) > SYNC_LOG_MAX:
                            del buf[: len(buf) - SYNC_LOG_MAX]
                        # Stamp last_sync once a sync actually succeeds so the
                        # UI can show "Last sync: ..." accurately.
                        if _fs == "done" and _j.target_id:
                            bucket = "downloads" if _t == "download" else "uploads"
                            item = (d.get(bucket) or {}).get(_j.target_id)
                            if item is not None:
                                item["last_sync"] = _j.finished_at
                    try:
                        self.store.update(_push_log)
                    except Exception:
                        logger.exception("sync_log append failed for job %s", job.id)
                # Fire post-completion hook (mirroring to additional project
                # tags, etc.). Hook is best-effort: failures are logged but
                # never propagate, so a broken mirror can't take the worker
                # down.
                if self.on_complete is not None:
                    try:
                        self.on_complete(job)
                    except Exception:
                        logger.exception("on_complete hook failed for job %s", job.id)
            finally:
                if limiter is not None:
                    limiter.release()
                self._global.release()


# ---------------------------------------------------------------------------
# Sync runners (real + mock)
# ---------------------------------------------------------------------------

def runner_mock_sync(direction: str) -> Callable[[Job], None]:
    def run(job: Job) -> None:
        total = secrets.randbelow(900_000_000) + 100_000_000
        job.bytes_total = total
        chunk = total // 50
        for i in range(50):
            if job._cancel.is_set():
                job.state = "cancelled"
                return
            time.sleep(0.2)
            job.bytes_done += chunk
            job.progress = (job.bytes_done / total) * 100
            job.transfer_rate = chunk * 5
            job.eta_seconds = int((total - job.bytes_done) / max(1, job.transfer_rate))
            job.log.append(f"{direction}: {_human(job.bytes_done)}/{_human(total)} ({job.progress:.1f}%)")
        job.bytes_done = total
        job.progress = 100.0
    return run


def runner_rclone(rclone: Rclone, args: List[str], src_label: str) -> Callable[[Job], None]:
    def run(job: Job) -> None:
        # Resolve performance flags from the active hardware preset.
        # Looked up *each run* so a preset change from the UI takes effect
        # on the next job without a daemon restart. Falls back to the
        # router preset on any error so a corrupt state never leaves a
        # rclone child unconstrained.
        try:
            preset = _ACTIVE_PRESET_GETTER() if _ACTIVE_PRESET_GETTER else dict(HARDWARE_PRESETS["router"])
        except Exception:
            preset = dict(HARDWARE_PRESETS["router"])
        buf_mb   = max(0, int(preset.get("buffer_size_mb", 0)))
        transfers = max(1, int(preset.get("transfers", 2)))
        checkers  = max(1, int(preset.get("checkers", 2)))
        mts       = max(0, int(preset.get("multi_thread_streams", 0)))
        backlog   = max(100, int(preset.get("max_backlog", 1000)))
        retries   = max(1, int(preset.get("low_level_retries", 3)))
        bw        = max(0, int(preset.get("bwlimit_kbps", 0)))
        # Buffer size: 0 == "off" (forces explicit "0" to bypass rclone's
        # 16 MB default which is fatal on the router). Non-zero passed as MB.
        buf_arg = "0" if buf_mb == 0 else f"{buf_mb}M"
        mem_flags = [
            "--buffer-size", buf_arg,
            "--use-mmap",                    # release memory back to OS faster
            "--transfers", str(transfers),
            "--checkers", str(checkers),
            "--multi-thread-streams", str(mts),
            "--low-level-retries", str(retries),
            "--max-backlog", str(backlog),
        ]
        if bw > 0:
            # rclone takes "<rate>K" / "<rate>M" suffix; we always pass kbps.
            mem_flags += ["--bwlimit", f"{bw}k"]
        cmd = rclone._base_args() + args + mem_flags + ["--use-json-log", "--stats=2s", "--stats-log-level=NOTICE", "-v"]
        job.log.append("$ " + " ".join(shlex.quote(a) for a in cmd))
        # preexec_fn caps RLIMIT_AS and bumps oom_score_adj +800 in the
        # child — see _rclone_preexec / _harden_self_for_oom for why. On
        # non-POSIX (Windows dev) preexec_fn is unsupported, so skip there.
        popen_kw: Dict[str, Any] = dict(stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
        if not IS_WINDOWS and not IS_MOCK:
            popen_kw["preexec_fn"] = _rclone_preexec
        proc = subprocess.Popen(cmd, **popen_kw)
        job._proc = proc
        try:
            for raw in proc.stdout or []:
                if job._cancel.is_set():
                    try: proc.terminate()
                    except Exception: pass
                    job.state = "cancelled"
                    break
                line = raw.rstrip()
                # Cap individual log lines to avoid runaway memory if rclone
                # spits out a giant single line (256 MB router).
                if len(line) > 2000:
                    line = line[:2000] + "... (truncated)"
                job.log.append(line)
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    if IS_DEBUG:
                        logger.debug("unparseable rclone JSON: %s", line[:200])
                    continue
                except Exception:
                    continue
                stats = evt.get("stats")
                if stats:
                    job.bytes_done = stats.get("bytes", job.bytes_done)
                    job.bytes_total = stats.get("totalBytes", job.bytes_total) or job.bytes_total
                    if job.bytes_total:
                        job.progress = (job.bytes_done / job.bytes_total) * 100
                    job.transfer_rate = int(stats.get("speed", 0))
                    job.eta_seconds = int(stats.get("eta") or 0)
                    # rclone's stats.transfers counts completed file
                    # transfers since process start. We expose this so
                    # the job runner can tell "nothing actually changed"
                    # from "X files moved" when deciding to notify.
                    try:
                        ft = int(stats.get("transfers") or 0)
                        if ft > job.files_transferred:
                            job.files_transferred = ft
                    except (TypeError, ValueError):
                        pass
        finally:
            # Always reap the child, even on exceptions inside the loop, so
            # we never leak a zombie rclone process.
            try:
                rc = proc.wait(timeout=10)
            except Exception:
                try: proc.kill()
                except Exception: pass
                try: rc = proc.wait(timeout=5)
                except Exception: rc = -1
            if rc != 0 and job.state == "running":
                job.state = "error"
                job.error = f"rclone exited with code {rc}"
            # rclone may have rewritten rclone.conf to persist a refreshed
            # OAuth token; mirror that into the backup so any later truncation
            # can be repaired.
            try:
                rclone.snapshot_conf()
            except Exception:
                pass
    return run


# ---------------------------------------------------------------------------
# URL parsing for download links
# ---------------------------------------------------------------------------

def parse_link(url: str) -> Dict[str, Any]:
    """Identify provider + remote path/id from a folder URL."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    path = u.path or ""

    if "drive.google.com" in host:
        m = re.search(r"/folders/([A-Za-z0-9_\-]+)", path)
        if m:
            return {"provider": "drive", "remote_path": "", "folder_id": m.group(1),
                    "label": "Google Drive folder " + m.group(1)[:8]}
    if "dropbox.com" in host:
        # Public shared folder - rclone needs the user to "Add to Dropbox" first.
        m = re.search(r"/(scl/fo|sh)/([A-Za-z0-9_\-]+)", path)
        if m:
            return {"provider": "dropbox", "remote_path": "", "share_id": m.group(2),
                    "label": "Dropbox shared folder " + m.group(2)[:8],
                    "warning": (
                        "Dropbox + rclone limitation: rclone cannot fetch a "
                        "folder by shared-link URL. Open this link in a "
                        "browser, click 'Add to my Dropbox', then set "
                        "'Remote path' below to the folder name that "
                        "appears in your Dropbox root. Leaving Remote "
                        "path blank will try to copy your ENTIRE Dropbox "
                        "and will likely OOM the router."
                    )}
    if "box.com" in host or "app.box.com" in host:
        m = re.search(r"/(folder|s)/([A-Za-z0-9_\-]+)", path)
        if m:
            return {"provider": "box", "remote_path": "", "share_id": m.group(2),
                    "label": "Box folder " + m.group(2)[:8]}
    if "1drv.ms" in host or "onedrive.live.com" in host or "sharepoint.com" in host:
        return {"provider": "onedrive", "remote_path": "",
                "label": "OneDrive shared folder",
                "warning": "Resolve the share URL inside OneDrive first; set remote path manually."}
    if u.scheme in ("http", "https"):
        return {"provider": "http", "remote_path": "", "url": url, "label": host + path}
    if u.scheme in ("ftp", "ftps"):
        # We can't auto-create an FTP rclone remote from the URL alone
        # (we'd need credentials), so we just hint that the user must pick
        # an existing FTP account and split host/path for the label.
        return {"provider": "ftp", "remote_path": (path or "").lstrip("/"),
                "label": (host or "FTP") + (path or ""),
                "warning": "Pick or create an FTP account on the Accounts tab first — the URL alone has no credentials."}
    if u.scheme == "sftp":
        return {"provider": "sftp", "remote_path": (path or "").lstrip("/"),
                "label": (host or "SFTP") + (path or ""),
                "warning": "Pick or create an SFTP account on the Accounts tab first — the URL alone has no credentials."}
    return {"provider": None, "label": url, "warning": "unrecognised URL"}


# ---------------------------------------------------------------------------
# Sharing / system
# ---------------------------------------------------------------------------

def _ensure_swap_if_needed() -> None:
    """Best-effort: invoke /usr/sbin/botsync-swap so low-RAM routers get a
    swapfile on the USB drive. The helper is itself a no-op on devices with
    enough RAM or when swap is already active. Never raises."""
    if IS_MOCK or IS_WINDOWS:
        return
    helper = "/usr/sbin/botsync-swap"
    if not os.path.isfile(helper) or not os.access(helper, os.X_OK):
        return
    try:
        env = os.environ.copy()
        env["BOTSYNC_ROOT"] = ROOT
        out = subprocess.run([helper, "ensure", ROOT], env=env, timeout=30,
                             capture_output=True, text=True)
        msg = (out.stdout or out.stderr or "").strip().splitlines()[-1:] or [""]
        logger.info("botsync-swap ensure: rc=%s %s", out.returncode, msg[0])
    except Exception as e:
        logger.warning("botsync-swap invocation failed: %s", e)


# ---------------------------------------------------------------------------
# OOM hardening
# ---------------------------------------------------------------------------
#
# On the 248MB GL-iNet A1300 the leading "router locks up under sync" failure
# mode is the kernel OOM killer reaping *botsyncd* (and dropbear, and the UI)
# while rclone's resident-set sits at 80-120MB. The lockup symptom is exactly
# this: TCP still SYN-ACKs (kernel is alive), but every userspace daemon the
# user cares about has been killed.
#
# Strategy:
#   - botsyncd writes its own oom_score_adj to a strongly negative value so
#     the kernel always picks any other process first. (procd's `oom_adj`
#     param does the same on OpenWrt; this is belt-and-braces for the case
#     where the daemon is started outside procd, e.g. the install-time
#     bootstrap or `python3 botsyncd.py` during dev.)
#   - Every rclone child gets a strongly *positive* oom_score_adj so the
#     kernel reaps it long before it touches the daemon. We also cap rclone's
#     virtual address space via RLIMIT_AS so a runaway listing on a folder
#     with millions of entries can't push the box past the OOM line.
#
# All of this is best-effort: the calls are wrapped in try/except so a
# kernel that doesn't expose oom_score_adj (or a non-Linux dev host) just
# falls through.

# Rclone child memory cap, in bytes. Default 128MB is enough for a realistic
# sync with --buffer-size 0 and --transfers 2 on the router. Override via env
# BOTSYNC_RCLONE_MEM_MB. The active value is held in a mutable list so
# _rclone_preexec (which runs in the forked child and takes no args) can
# read whatever the current preset says without us having to thread state
# through Popen. Updated by App._apply_active_preset().
try:
    _rclone_mem_mb = int(os.environ.get("BOTSYNC_RCLONE_MEM_MB", "128"))
except Exception:
    _rclone_mem_mb = 128
RCLONE_MEM_BYTES = max(48, _rclone_mem_mb) * 1024 * 1024
# [bytes_cap, nice_value] — single-element-update is atomic in CPython, so
# no lock needed. Read by _rclone_preexec.
_RCLONE_CHILD_LIMITS: List[int] = [RCLONE_MEM_BYTES, 5]


def _set_oom_score_adj(adj: int) -> None:
    """Write ``adj`` into ``/proc/self/oom_score_adj``. No-op on systems
    without that file (Windows, macOS dev hosts, mock mode)."""
    if IS_MOCK or IS_WINDOWS:
        return
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write(str(int(adj)))
    except Exception as e:
        logger.debug("could not set own oom_score_adj=%s: %s", adj, e)


def _harden_self_for_oom() -> None:
    """Bias the kernel OOM killer away from this process. See module-level
    OOM-hardening notes above. Called once from main()."""
    _set_oom_score_adj(-500)


def _rclone_preexec() -> None:
    """preexec_fn for rclone subprocesses.

    Runs in the child between fork() and exec(). Three jobs:
      1. Bias the OOM killer toward this child (+800).
      2. Cap the child's virtual-memory size (RLIMIT_AS) so a runaway
         listing can't OOM the whole box — rclone will get malloc failures
         and exit non-zero, which we recover from gracefully.
      3. Reduce CPU priority so the daemon's HTTP loop stays responsive.

    Anything that throws here aborts the child *before* exec, so each step
    is wrapped individually — we still want to start rclone if e.g. the
    kernel doesn't expose RLIMIT_AS the way we expect.
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("800")
    except Exception:
        pass
    try:
        import resource  # noqa: WPS433 (deliberately late; not on Windows)
        # Cap address space. Soft limit only — hard limit unchanged so the
        # parent could still raise it if needed.
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        new_soft = int(_RCLONE_CHILD_LIMITS[0])
        new_hard = hard if hard != resource.RLIM_INFINITY and hard < new_soft else new_soft
        resource.setrlimit(resource.RLIMIT_AS, (new_soft, new_hard))
    except Exception:
        pass
    try:
        os.nice(int(_RCLONE_CHILD_LIMITS[1]))
    except Exception:
        pass


def _read_cpu_temp_c() -> Tuple[Optional[float], Optional[str]]:
    """Best-effort cross-platform CPU temperature (Celsius) and source label.

    Order of preference (each step swallows errors and falls through):
      1. Linux/OpenWrt: /sys/class/thermal/thermal_zone*/temp — picks the
         hottest CPU/SoC zone (filtered by type if available).
      2. Linux: /sys/class/hwmon/hwmon*/temp*_input named coretemp / k10temp /
         zenpower / cpu_thermal.
      3. Linux generic: any hwmon temp*_input (e.g. ath10k_hwmon on
         GL-iNet routers — the wifi chip is on the same die / is the
         best thermal proxy available).
      4. macOS: powermetrics is root-only; we just skip and let user know.
      5. Windows: ROOT\\WMI MSAcpi_ThermalZoneTemperature (works on most
         laptops/desktops; some boards expose nothing without vendor SDKs).
      6. Mock mode: returns a synthetic value that drifts a bit so the UI
         can be exercised offline.
    Returns (temp_c, source_label) or (None, None) when no probe succeeded.
    """
    if IS_MOCK:
        # Drift between 38–62 °C so the threshold UI is testable.
        base = 45.0 + 8.0 * math.sin(time.time() / 30.0)
        return (round(base + random.uniform(-2.0, 2.0), 1), "mock")
    # ---- Linux thermal_zone ----
    try:
        zones = sorted(glob.glob("/sys/class/thermal/thermal_zone*"))
        best: Optional[float] = None
        best_type: Optional[str] = None
        for z in zones:
            try:
                with open(os.path.join(z, "type")) as f:
                    ztype = f.read().strip().lower()
            except Exception:
                ztype = ""
            try:
                with open(os.path.join(z, "temp")) as f:
                    raw = int(f.read().strip())
            except Exception:
                continue
            # Kernel exposes millidegrees; some embedded SoCs use degrees
            # already — anything < 200 is degrees, anything else is mC.
            t = raw / 1000.0 if abs(raw) >= 200 else float(raw)
            if t <= 0 or t > 150:
                continue
            if best is None or t > best:
                best = t
                best_type = ztype or "thermal_zone"
        if best is not None:
            return (round(best, 1), "thermal_zone:" + (best_type or ""))
    except Exception:
        pass
    # ---- Linux hwmon (named CPU sensors first, then any) ----
    try:
        hw_entries: List[Tuple[str, str]] = []  # (name, path)
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                with open(os.path.join(hw, "name")) as f:
                    name = f.read().strip().lower()
            except Exception:
                name = ""
            hw_entries.append((name, hw))
        # Pass 1: known CPU sensor names.
        for name, hw in hw_entries:
            if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal"):
                continue
            for tf in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
                try:
                    with open(tf) as f:
                        raw = int(f.read().strip())
                    t = raw / 1000.0 if abs(raw) >= 200 else float(raw)
                    if 0 < t <= 150:
                        return (round(t, 1), "hwmon:" + name)
                except Exception:
                    continue
        # Pass 2: any readable hwmon (best-effort thermal proxy).
        best_t: Optional[float] = None
        best_name: Optional[str] = None
        for name, hw in hw_entries:
            for tf in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
                try:
                    with open(tf) as f:
                        raw = int(f.read().strip())
                    t = raw / 1000.0 if abs(raw) >= 200 else float(raw)
                    if 0 < t <= 150 and (best_t is None or t > best_t):
                        best_t = t
                        best_name = name or "hwmon"
                except Exception:
                    continue
        if best_t is not None:
            return (round(best_t, 1), "hwmon:" + (best_name or ""))
    except Exception:
        pass
    # ---- Windows ----
    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop).CurrentTemperature"],
                stderr=subprocess.DEVNULL, timeout=4,
            ).decode().strip().splitlines()
            vals = []
            for line in out:
                line = line.strip()
                if line.isdigit():
                    # Tenths of Kelvin
                    vals.append(int(line) / 10.0 - 273.15)
            if vals:
                return (round(max(vals), 1), "wmi")
        except Exception:
            pass
    return (None, None)


def _cpu_count() -> int:
    try:
        n = os.cpu_count() or 1
        return max(1, int(n))
    except Exception:
        return 1


def system_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {"hostname": socket.gethostname(),
                            "platform": platform.platform(), "version": VERSION,
                            "mock": IS_MOCK, "rclone": None,
                            "root": ROOT, "root_ok": True}
    info["rclone"] = bool(Rclone._find_binary()) or IS_MOCK
    try:
        if not IS_WINDOWS:
            with open("/proc/loadavg") as f:
                info["loadavg"] = f.read().split()[:3]
            with open("/proc/meminfo") as f:
                mem: Dict[str, int] = {}
                for line in f:
                    k, _, rest = line.partition(":")
                    parts = rest.strip().split()
                    if parts:
                        mem[k.strip()] = int(parts[0]) * 1024
                info["mem_total"] = mem.get("MemTotal", 0)
                info["mem_free"] = mem.get("MemAvailable", mem.get("MemFree", 0))
                swap_total = mem.get("SwapTotal", 0)
                swap_free = mem.get("SwapFree", 0)
                info["swap_total"] = swap_total
                info["swap_free"] = swap_free
                info["swap_used"] = max(0, swap_total - swap_free)
                info["swap_active"] = swap_total > 0
            try:
                with open("/proc/swaps") as f:
                    devs = []
                    for i, line in enumerate(f):
                        if i == 0:
                            continue
                        parts = line.split()
                        if len(parts) >= 3:
                            devs.append({"device": parts[0], "type": parts[1],
                                         "size": int(parts[2]) * 1024,
                                         "used": int(parts[3]) * 1024 if len(parts) > 3 else 0})
                    info["swap_devices"] = devs
            except Exception:
                info["swap_devices"] = []
            with open("/proc/uptime") as f:
                info["uptime"] = float(f.read().split()[0])
    except Exception:
        pass
    if IS_WINDOWS or IS_MOCK:
        info.setdefault("loadavg", ["0.10", "0.15", "0.12"])
        info.setdefault("mem_total", 256 * 1024**2)
        info.setdefault("mem_free", 120 * 1024**2)
        info.setdefault("swap_total", 256 * 1024**2)
        info.setdefault("swap_free", 256 * 1024**2)
        info.setdefault("swap_used", 0)
        info.setdefault("swap_active", True)
        info.setdefault("swap_devices", [{"device": "/dev/loop0", "type": "file",
                                          "size": 256 * 1024**2, "used": 0}])
        info.setdefault("uptime", 12345.0)
    try:
        st = shutil.disk_usage(ROOT) if os.path.exists(ROOT) else None
        if st:
            info["root_total"] = st.total
            info["root_free"] = st.free
    except Exception:
        pass
    # Derived metrics used by the UI status pages and the health-monitor.
    try:
        info["cpu_count"] = _cpu_count()
        la = info.get("loadavg") or ["0", "0", "0"]
        try:
            la1 = float(la[0])
        except Exception:
            la1 = 0.0
        info["cpu_load_pct"] = round(min(999.0, (la1 / max(1, info["cpu_count"])) * 100.0), 1)
    except Exception:
        info["cpu_count"] = 1
        info["cpu_load_pct"] = 0.0
    try:
        mt = float(info.get("mem_total") or 0)
        mf = float(info.get("mem_free") or 0)
        info["mem_used_pct"] = round(((mt - mf) / mt) * 100.0, 1) if mt > 0 else 0.0
    except Exception:
        info["mem_used_pct"] = 0.0
    try:
        st_total = float(info.get("swap_total") or 0)
        st_used = float(info.get("swap_used") or 0)
        info["swap_used_pct"] = round((st_used / st_total) * 100.0, 1) if st_total > 0 else 0.0
    except Exception:
        info["swap_used_pct"] = 0.0
    try:
        info["cpu_temp_c"], info["cpu_temp_source"] = _read_cpu_temp_c()
    except Exception:
        info["cpu_temp_c"] = None
        info["cpu_temp_source"] = None
    return info


def apply_sharing(state: Dict[str, Any]) -> Dict[str, Any]:
    """Render shares to /etc/config/samba4 etc. on the router; no-op in mock."""
    if IS_MOCK or IS_WINDOWS:
        return {"ok": True, "applied": False, "reason": "mock"}
    sh = state.get("sharing", {})
    actions: List[str] = []
    try:
        # We only manage one share called 'botsync' under /mnt/sync.
        # Use UCI directly. This is idempotent.
        def uci(*args: str) -> None:
            subprocess.run(["uci", *args], check=False, capture_output=True)
        uci("delete", "samba4.botsync"); uci("delete", "samba4.@sambashare[-1]")  # best-effort
        uci("set", "samba4.botsync=sambashare")
        uci("set", "samba4.botsync.name=botsync")
        uci("set", "samba4.botsync.path=" + ROOT)
        uci("set", "samba4.botsync.read_only=" + ("yes" if sh.get("guest_ro") else "no"))
        uci("set", "samba4.botsync.guest_ok=yes")
        uci("set", "samba4.botsync.create_mask=0666")
        uci("set", "samba4.botsync.dir_mask=0777")
        uci("commit", "samba4")
        subprocess.run(["/etc/init.d/samba4", "enabled" if sh.get("smb") else "disable"], check=False)
        subprocess.run(["/etc/init.d/samba4", "restart" if sh.get("smb") else "stop"], check=False)
        actions.append("samba4 reloaded")

        # Avahi (Bonjour)
        if sh.get("bonjour"):
            os.makedirs("/etc/avahi/services", exist_ok=True)
            with open("/etc/avahi/services/botsync-smb.service", "w") as f:
                f.write(_AVAHI_SMB_SERVICE)
            subprocess.run(["/etc/init.d/avahi-daemon", "enable"], check=False)
            subprocess.run(["/etc/init.d/avahi-daemon", "restart"], check=False)
            actions.append("avahi up")
        else:
            try: os.remove("/etc/avahi/services/botsync-smb.service")
            except OSError: pass
            subprocess.run(["/etc/init.d/avahi-daemon", "restart"], check=False)
    except Exception as e:
        return {"ok": False, "error": str(e), "applied_actions": actions}
    return {"ok": True, "applied": True, "actions": actions}


_AVAHI_SMB_SERVICE = """<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h botsync</name>
  <service>
    <type>_smb._tcp</type>
    <port>445</port>
  </service>
  <service>
    <type>_device-info._tcp</type>
    <port>0</port>
    <txt-record>model=TimeCapsule6,106</txt-record>
  </service>
</service-group>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def _human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n //= 1
        n_f = float(n)
        if n_f < 1024:
            return f"{n_f:.1f} {u}"
        n = int(n_f / 1024)
    return f"{n} PB"


class App:
    def __init__(self) -> None:
        self.store = Store(STATE_FILE)
        self.rclone = Rclone()
        self.oauth = OAuthHelper(self.store, self.rclone)
        self.notifier = Notifier(self.store)
        self.jobs = JobManager(self.store, self.rclone, notifier=self.notifier)
        self.jobs.on_complete = self._on_job_complete
        self.jobs.start_workers(
            dl_max=self.store.get()["limits"].get("download_concurrency",
                       self.store.get()["limits"].get("max_concurrent_jobs", 1)),
            ul_max=self.store.get()["limits"].get("upload_concurrency",
                       self.store.get()["limits"].get("max_concurrent_jobs", 1)),
        )
        # Auto-detect a hardware preset on first run, then apply it. After
        # this returns the global concurrency cap, per-type caps, rclone
        # flags, and rclone child RLIMIT_AS are all in sync with the
        # active profile.
        global _ACTIVE_PRESET_GETTER
        _ACTIVE_PRESET_GETTER = self._active_preset_values
        self._auto_detect_preset_if_unset()
        self._apply_active_preset()
        # Tracks whether the primary drive was present on the previous
        # _drives_merged() pass so we only emit a notification on the
        # transition rather than on every poll. None = unknown (first poll).
        self._primary_present_last: Optional[bool] = None
        self._primary_missing_since: Optional[float] = None
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()
        self.sessions = SessionStore(ttl_provider=self._session_ttl_seconds)
        # Reliability state: set on startup, polled by /api/system + UI.
        self._started_at = time.time()
        self._last_watchdog_ping: Optional[float] = None
        self._stop_event = threading.Event()
        # Detect crash / watchdog kick BEFORE seeding events so first event
        # in the buffer reflects the recovery rather than a normal startup.
        self._handle_crash_recovery()
        self._reset_interrupted_jobs()
        self._migrate_clear_stale_warnings()
        # Heartbeat + stuck-job watchdog.
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat")
        self._heartbeat_thread.start()
        self._jobwatch_thread = threading.Thread(
            target=self._stuck_job_loop, daemon=True, name="job-watchdog")
        self._jobwatch_thread.start()
        self._update_thread = threading.Thread(
            target=self._update_check_loop, daemon=True, name="rclone-update")
        self._update_thread.start()
        # Auto-sync loop: kicks off pending downloads/uploads and retries any
        # that haven't successfully synced yet, with exponential backoff. This
        # is what makes the daemon survive power loss / network outages — the
        # loop wakes up after boot and just keeps retrying.
        self._autosync_state: Dict[str, Dict[str, float]] = {}
        self._autosync_lock = threading.Lock()
        # `live_targets` is a per-tick membership snapshot; we also use
        # `_inflight_submits` to bridge the gap between a successful submit
        # and the next call to JobManager.list() so that two threads racing
        # to autosync the same target can't both win.
        self._inflight_submits: set = set()
        self._autosync_thread = threading.Thread(
            target=self._autosync_loop, daemon=True, name="autosync")
        self._autosync_thread.start()
        # Mark daemon as running. Removed in shutdown(); if it's still here on
        # next start, we know the previous run crashed.
        try:
            os.makedirs(RUN_DIR, exist_ok=True)
            with open(RUNNING_MARKER, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "started": self._started_at,
                                     "version": VERSION}))
        except Exception:
            logger.exception("could not write running marker %s", RUNNING_MARKER)
        self._maybe_seed_discord()
        self.notifier.emit("system.startup", f"botsyncd {VERSION} started",
                           root=ROOT, mock=IS_MOCK, pid=os.getpid())

    # ------- shutdown -------

    def shutdown(self, reason: str = "stop") -> None:
        """Best-effort clean shutdown. Removes running marker so next boot is
        treated as clean. Safe to call multiple times."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        try:
            self.notifier.emit("system.shutdown", f"botsyncd stopping ({reason})",
                               reason=reason, pid=os.getpid())
        except Exception:
            pass
        for path in (RUNNING_MARKER,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    # ------- reliability helpers -------

    def _session_ttl_seconds(self) -> int:
        st = (self.store.get().get("settings") or {})
        return _clamp_session_ttl_hours(st.get("session_ttl_hours", SESSION_TTL_DEFAULT_HOURS)) * 3600

    # ------- hardware preset wiring -------

    def _active_preset_values(self) -> Dict[str, Any]:
        """Resolve the currently-active preset values dict. Used by
        runner_rclone (via _ACTIVE_PRESET_GETTER) and the /api/performance
        endpoint."""
        _, vals = get_active_preset(self.store.get())
        return vals

    def _auto_detect_preset_if_unset(self) -> None:
        """Persist an auto-detected preset on first start so the user has
        a reasonable default that survives reboots."""
        st = (self.store.get().get("settings") or {})
        if st.get("performance_preset"):
            return
        choice = auto_detect_preset()
        ram_mb = _detect_total_ram_mb()
        def _u(d: Dict[str, Any]) -> None:
            d.setdefault("settings", {})["performance_preset"] = choice
        try:
            self.store.update(_u)
            logger.info("auto-detected hardware preset: %s (ram=%dMB)", choice, ram_mb)
        except Exception:
            logger.exception("auto-detect preset persist failed")

    def _apply_active_preset(self) -> None:
        """Push the active preset's concurrency caps into JobManager and the
        rclone child RLIMIT_AS holder. Safe to call repeatedly."""
        try:
            name, vals = get_active_preset(self.store.get())
        except Exception:
            logger.exception("get_active_preset failed; staying on previous values")
            return
        # Per-rclone-child caps. Updated atomically so an in-flight
        # _rclone_preexec call sees a consistent pair.
        mem_bytes = max(48, int(vals.get("rclone_mem_mb", 128))) * 1024 * 1024
        nice_val  = int(vals.get("nice", 5))
        _RCLONE_CHILD_LIMITS[0] = mem_bytes
        _RCLONE_CHILD_LIMITS[1] = nice_val
        # Job manager caps. apply_preset only affects future job dispatches;
        # already-running rclone children keep their old flags until they
        # finish (acceptable — we are NOT going to SIGKILL active syncs to
        # apply a preset change).
        applied = self.jobs.apply_preset(vals)
        logger.info("applied performance preset %s: caps=%s rclone_mem=%dMB nice=%d",
                    name, applied, mem_bytes // (1024*1024), nice_val)

    def _handle_crash_recovery(self) -> None:
        """If a running marker survived from a previous run, the daemon
        crashed or was killed. Emit a recovery event so the user is told."""
        kicked = False
        try:
            if os.path.exists(WATCHDOG_KICK_MARKER):
                kicked = True
        except Exception:
            kicked = False
        finally:
            # Always remove the marker so a daemon crash *between* exists()
            # and remove() (previous code) can't leave us stuck reporting a
            # phantom watchdog kick on every subsequent restart.
            try:
                if os.path.exists(WATCHDOG_KICK_MARKER):
                    os.remove(WATCHDOG_KICK_MARKER)
            except Exception:
                pass
        prior_pid = None
        prior_started = None
        had_marker = os.path.exists(RUNNING_MARKER)
        if had_marker:
            try:
                with open(RUNNING_MARKER, "r", encoding="utf-8") as fh:
                    info = json.load(fh)
                    prior_pid = info.get("pid")
                    prior_started = info.get("started")
            except Exception:
                pass
        if kicked:
            # Notifier might emit before channels are configured; still useful in event ring.
            self.notifier.emit(
                "system.watchdog_restart",
                "external watchdog restarted botsyncd after consecutive ping failures",
                prior_pid=prior_pid, prior_started=prior_started)
        elif had_marker:
            self.notifier.emit(
                "system.crash_recovered",
                "botsyncd restarted after unclean shutdown",
                prior_pid=prior_pid, prior_started=prior_started)

    def _migrate_clear_stale_warnings(self) -> None:
        """One-shot: drop the persisted setup-hint `warning` field from every
        download/upload. Setup hints (e.g. the Dropbox shared-folder hint)
        are now returned once at create-time only, so any stored value here
        is a leftover from older daemon versions and should not keep shouting
        from the UI."""
        def _m(d: Dict[str, Any]) -> None:
            for item in (d.get("downloads") or {}).values():
                if "warning" in item:
                    item.pop("warning", None)
            for item in (d.get("uploads") or {}).values():
                if "warning" in item:
                    item.pop("warning", None)
        try:
            self.store.update(_m)
        except Exception:
            logger.exception("warning migration failed")

    def _reset_interrupted_jobs(self) -> None:
        """Any job persisted in 'running' state at startup means the daemon
        died mid-job. Mark them failed and notify."""
        interrupted: List[Dict[str, Any]] = []
        def _u(d: Dict[str, Any]) -> None:
            for jid, j in list((d.get("jobs") or {}).items()):
                if j.get("state") == "running":
                    j["state"] = "error"
                    j["error"] = "interrupted by daemon restart"
                    j["finished_at"] = time.time()
                    interrupted.append({"id": jid, "type": j.get("type"),
                                        "label": j.get("label")})
        # Jobs aren't persisted in this build (in-memory only), but call the
        # update so future schemas work; harmless no-op today.
        try:
            self.store.update(_u)
        except Exception:
            pass
        for job in interrupted:
            self.notifier.emit("job.interrupted",
                               f"job {job.get('label')} was interrupted",
                               **job)

    def record_watchdog_ping(self) -> None:
        self._last_watchdog_ping = time.time()

    def reliability_info(self) -> Dict[str, Any]:
        try:
            with open(HEARTBEAT_FILE, "r", encoding="utf-8") as fh:
                hb = json.load(fh)
            hb_ts = float(hb.get("ts") or 0)
        except Exception:
            hb_ts = 0.0
        return {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "uptime_s": int(time.time() - self._started_at),
            "last_heartbeat": hb_ts or None,
            "last_watchdog_ping": self._last_watchdog_ping,
            "watchdog_active": bool(self._last_watchdog_ping
                                    and (time.time() - self._last_watchdog_ping) < 180),
        }

    # ------- background loops -------

    def _heartbeat_loop(self) -> None:
        seq = 0
        while not self._stop_event.is_set():
            seq += 1
            try:
                os.makedirs(RUN_DIR, exist_ok=True)
                tmp = HEARTBEAT_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"ts": time.time(), "pid": os.getpid(),
                               "version": VERSION, "seq": seq}, fh)
                os.replace(tmp, HEARTBEAT_FILE)
            except Exception:
                # Loud at warning so the watchdog operator notices stuck
                # heartbeats; previously this was logged at exception level
                # but never escalated, so the watchdog could fire blind.
                logger.warning("heartbeat write failed (seq=%d); next try in 30s",
                               seq, exc_info=True)
            self._stop_event.wait(30)

    def _stuck_job_loop(self) -> None:
        """Every 5 min, check for jobs running longer than `stuck_job_hours`
        with no recent progress. Cancel them and emit job.stuck."""
        while not self._stop_event.is_set():
            try:
                st = (self.store.get().get("settings") or {})
                hours = _clamp_stuck_job_hours(st.get("stuck_job_hours",
                                                      STUCK_JOB_DEFAULT_HOURS))
                if hours > 0:
                    cutoff = time.time() - hours * 3600
                    for j in self.jobs.list():
                        if j.get("state") != "running":
                            continue
                        started = float(j.get("started_at") or 0)
                        if started and started < cutoff:
                            jid = j.get("id")
                            label = j.get("label") or jid
                            logger.warning("job %s appears stuck (%.1fh); cancelling",
                                           jid, (time.time() - started) / 3600)
                            try:
                                self.jobs.cancel(jid)
                            except Exception:
                                logger.exception("cancel stuck job %s", jid)
                            self.notifier.emit(
                                "job.stuck",
                                f"job {label} stuck > {hours}h; cancelled",
                                job_id=jid, type=j.get("type"),
                                duration_s=int(time.time() - started))
            except Exception:
                logger.exception("stuck-job watchdog")
            self._stop_event.wait(300)

    def _maybe_seed_discord(self) -> None:
        url = os.environ.get("BOTSYNC_DISCORD_WEBHOOK", "").strip()
        if not url:
            return
        existing = self.store.get()["notifications"].get("channels", {})
        for ch in existing.values():
            if ch.get("kind") == "discord" and (ch.get("config") or {}).get("url") == url:
                return
        try:
            self.notifier.upsert_channel(None, {
                "kind": "discord", "label": "Discord (default)",
                "config": {"url": url}, "events": ["*"],
                "min_severity": "info", "enabled": True,
            })
            logger.info("seeded default Discord webhook channel")
        except Exception:
            logger.exception("failed to seed discord channel")

    # ------- helpers -------

    def _live_mountpoint(self, uuid: str) -> Optional[str]:
        """Return the current mountpoint for an adopted drive.

        The stored drive record can be missing ``mountpoint`` if the drive was
        auto-adopted (via ``.botsync_marker``) on a fresh boot, or if the
        OS remounted it at a different path after a reboot. Always trust
        ``DriveProbe.detect()`` for the live value; fall back to the stored
        record only if detection turns up nothing (mock / Windows dev).
        """
        if not uuid:
            return None
        try:
            for live in DriveProbe.detect():
                if live.get("uuid") == uuid:
                    mp = live.get("mountpoint")
                    if mp:
                        # Persist so future calls don't have to probe again
                        # and so the UI's stored record stays accurate.
                        cur = (self.store.get().get("drives") or {}).get(uuid, {})
                        if cur.get("mountpoint") != mp:
                            def _save(d: Dict[str, Any], _uid=uuid, _mp=mp) -> None:
                                if _uid in d.get("drives", {}):
                                    d["drives"][_uid]["mountpoint"] = _mp
                            self.store.update(_save)
                        return mp
                    break
        except Exception:
            logger.exception("live mountpoint probe failed for %s", uuid)
        return (self.store.get().get("drives") or {}).get(uuid, {}).get("mountpoint")

    def _drives_merged(self) -> List[Dict[str, Any]]:
        detected = {d["uuid"]: d for d in DriveProbe.detect()}
        # Auto-adopt drives that carry a valid .botsync_marker so the user
        # doesn't have to re-adopt after every reboot or hub reshuffle.
        adopted_now = self.store.get()["drives"]
        to_adopt: List[Dict[str, Any]] = []
        for uid, live in detected.items():
            if uid in adopted_now or not live.get("has_marker") or not live.get("mountpoint"):
                continue
            try:
                with open(os.path.join(live["mountpoint"], ".botsync_marker"), "r") as fh:
                    marker = json.load(fh)
            except Exception:
                continue
            to_adopt.append({
                "uuid": marker.get("uuid") or uid,
                "label": live.get("label"),
                "fs": live.get("fs"),
                "mountpoint": live.get("mountpoint"),
                "primary": bool(marker.get("primary", True)),
                "adopted_at": marker.get("adopted_at") or time.time(),
            })
        if to_adopt:
            def _adopt(d: Dict[str, Any]) -> None:
                has_primary = any(x.get("primary") for x in d["drives"].values())
                for entry in to_adopt:
                    uid = entry["uuid"]
                    if uid in d["drives"]:
                        continue
                    if entry["primary"] and has_primary:
                        entry["primary"] = False
                    elif entry["primary"]:
                        has_primary = True
                    d["drives"][uid] = entry
            self.store.update(_adopt)
            logger.info("auto-adopted %d drive(s) from marker: %s",
                        len(to_adopt), [a["uuid"] for a in to_adopt])

        adopted = self.store.get()["drives"]
        out: List[Dict[str, Any]] = []
        for uid, info in adopted.items():
            d = dict(info)
            d["uuid"] = uid
            d["adopted"] = True
            d["paused"] = bool(info.get("paused"))
            live = detected.get(uid)
            if live:
                d.update({k: live[k] for k in ("device", "fs", "size_bytes", "free_bytes", "mountpoint", "label")})
                d["present"] = True
            else:
                d["present"] = False
                d["mountpoint"] = None
            out.append(d)
        for uid, live in detected.items():
            if uid not in adopted:
                d = dict(live)
                d["adopted"] = False
                out.append(d)
        # Primary-drive presence tracking. If the primary drive disappears
        # while the daemon is running we surface that as a notification +
        # banner so the user knows syncs are stalled until the drive
        # comes back. Mock / Windows dev never has a real primary.
        if not IS_MOCK and not IS_WINDOWS:
            primary = next((d for d in out if d.get("adopted") and d.get("primary")), None)
            if primary is not None:
                present = bool(primary.get("present"))
                last = self._primary_present_last
                if last is None:
                    # First observation after startup — record without
                    # firing a transition event. If the drive is missing
                    # at boot, the system.startup event already covers it.
                    self._primary_present_last = present
                    if not present:
                        self._primary_missing_since = time.time()
                elif present and not last:
                    self._primary_present_last = True
                    self._primary_missing_since = None
                    try:
                        self.notifier.emit(
                            "drive.primary_returned",
                            "primary drive {} reconnected".format(primary.get("label") or primary.get("uuid") or "?"),
                            severity="info",
                            uuid=primary.get("uuid"))
                    except Exception:
                        logger.exception("notify primary_returned")
                elif (not present) and last:
                    self._primary_present_last = False
                    self._primary_missing_since = time.time()
                    try:
                        self.notifier.emit(
                            "drive.primary_missing",
                            "primary drive {} pulled \u2014 syncs paused until reconnected".format(primary.get("label") or primary.get("uuid") or "?"),
                            severity="error",
                            uuid=primary.get("uuid"))
                    except Exception:
                        logger.exception("notify primary_missing")
                    # Cancel any in-flight jobs against the primary so the
                    # rclone children don't keep hammering a vanished
                    # filesystem. New syncs already early-exit on
                    # mountpoint missing, so we only need to clean up
                    # what's already running.
                    try:
                        self._cancel_drive_jobs(primary.get("uuid") or "", wait=0.0)
                    except Exception:
                        logger.exception("cancel jobs on primary_missing")
        return out

    # ------- health loop -------

    def _health_loop(self) -> None:
        while True:
            # Refresh the rclone.conf backup on every health pass so token
            # refreshes performed by rclone itself are captured. snapshot_conf
            # is a no-op when nothing has changed.
            try:
                self.rclone.snapshot_conf()
            except Exception:
                logger.exception("rclone.conf snapshot")
            try:
                names = list(self.store.get()["remotes"].keys())
                for name in names:
                    prev_remote = self.store.get()["remotes"].get(name, {}) or {}
                    prev_health = prev_remote.get("health")
                    prev_reauth = bool(prev_remote.get("needs_reauth"))
                    res = self.rclone.remote_ping(name)
                    new_health = "ok" if res.get("ok") else "error"
                    err = res.get("error")
                    needs_reauth = (new_health == "error") and _looks_like_reauth(err)
                    def _upd(d: Dict[str, Any], n=name, r=res, nh=new_health, nr=needs_reauth) -> None:
                        if n in d["remotes"]:
                            d["remotes"][n]["last_check"] = time.time()
                            d["remotes"][n]["health"] = nh
                            d["remotes"][n]["error"] = r.get("error")
                            d["remotes"][n]["needs_reauth"] = nr
                    self.store.update(_upd)
                    if prev_health and prev_health != new_health:
                        if new_health == "error":
                            self.notifier.emit("remote.health_failed",
                                               f"account {name} unreachable",
                                               remote=name, error=err)
                        else:
                            self.notifier.emit("remote.health_ok",
                                               f"account {name} recovered", remote=name)
                    if needs_reauth and not prev_reauth:
                        self.notifier.emit(
                            "remote.reauth_required",
                            f"account {name} needs to be reconnected — token expired or revoked",
                            remote=name, error=err)
            except Exception:
                logger.exception("health loop")
            time.sleep(600)

    # ------- auto-sync loop -------

    # Backoff schedule (seconds) when a sync fails. Capped at the last value.
    _AUTOSYNC_BACKOFF = (30, 60, 120, 300, 900, 1800, 3600)
    # How often the loop wakes up to scan for pending work.
    _AUTOSYNC_TICK = 15

    @staticmethod
    def _schedule_seconds(raw: Any) -> int:
        """Parse a per-entry ``schedule`` value into an interval in seconds.

        Accepts plain integers / numeric strings (treated as seconds) and a
        few human suffixes (``30s``, ``5m``, ``2h``, ``1d``) so the API stays
        forgiving even if a caller skips the UI's preset dropdown. Returns
        ``0`` for empty / unparseable / non-positive values, which means
        "manual only" — the autosync loop will run the entry once and then
        leave it alone."""
        if raw is None:
            return 0
        if isinstance(raw, bool):
            return 0
        if isinstance(raw, (int, float)):
            n = int(raw)
            return n if n > 0 else 0
        s = str(raw).strip().lower()
        if not s or s in ("manual", "off", "none", "0"):
            return 0
        m = re.match(r"^(\d+)\s*([smhd]?)$", s)
        if not m:
            return 0
        n = int(m.group(1))
        mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return n * mult if n > 0 else 0

    def _autosync_due(self, target_id: str) -> bool:
        with self._autosync_lock:
            st = self._autosync_state.get(target_id)
            if not st:
                return True
            # Guard against malformed/None next_at values from older state.
            return time.time() >= (st.get("next_at") or 0)

    def _autosync_record(self, target_id: str, ok: bool) -> None:
        with self._autosync_lock:
            st = self._autosync_state.setdefault(target_id, {"attempts": 0.0, "next_at": 0.0})
            if ok:
                st["attempts"] = 0.0
                # Done items don't need to be re-attempted by the loop; the
                # presence of a successful sync_log entry will keep them from
                # being re-queued. Reset the timer anyway.
                st["next_at"] = 0.0
            else:
                n = int(st.get("attempts", 0)) + 1
                idx = min(n - 1, len(self._AUTOSYNC_BACKOFF) - 1)
                st["attempts"] = float(n)
                st["next_at"] = time.time() + self._AUTOSYNC_BACKOFF[idx]

    def _autosync_reset(self, target_id: str) -> None:
        """Drop all backoff/attempt state for a target so the next tick treats
        it as fresh. Called when the user updates schedule, or re-activates a
        previously failed sync."""
        with self._autosync_lock:
            self._autosync_state.pop(target_id, None)
            self._inflight_submits.discard(target_id)

    def _autosync_loop(self) -> None:
        # Small initial delay so the daemon's HTTP server is up and drives have
        # had a chance to mount before we start firing syncs.
        if self._stop_event.wait(10):
            return
        while not self._stop_event.is_set():
            try:
                self._autosync_tick()
            except Exception:
                logger.exception("autosync tick")
            try:
                self._purge_due()
            except Exception:
                logger.exception("purge tick")
            try:
                self._health_check_tick()
            except Exception:
                logger.exception("health check tick")
            if self._stop_event.wait(self._AUTOSYNC_TICK):
                return

    def _autosync_tick(self) -> None:
        s = self.store.get()
        settings = s.get("settings") or {}
        if not settings.get("enabled", True):
            return
        # Build set of target_ids that already have a queued/running job so we
        # don't double-submit. Include in-flight submits being processed by an
        # HTTP handler that hasn't surfaced as a Job yet (race fix).
        with self._autosync_lock:
            inflight = set(self._inflight_submits)
        live_targets = {j.get("target_id") for j in self.jobs.list()
                        if j.get("state") in ("queued", "running")} | inflight
        # Also need to know which targets have *ever* successfully synced so
        # we don't keep retrying ones the user hasn't touched. For downloads
        # the sync is a "copy" (idempotent) — a successful run covers the
        # crash-resume case the user asked for, and the user can press Sync
        # again to pull new files. Future runs come from the schedule field.
        sl = s.get("sync_log") or {}
        ever_done_dl = {e.get("target_id") for e in (sl.get("download") or [])
                        if e.get("state") == "done"}
        ever_done_ul = {e.get("target_id") for e in (sl.get("upload") or [])
                        if e.get("state") == "done"}

        if settings.get("downloads_enabled", True):
            for did, item in (s.get("downloads") or {}).items():
                if item.get("state") != "active":
                    continue
                if did in live_targets:
                    continue
                interval = self._schedule_seconds(item.get("schedule"))
                last = item.get("last_sync") or 0
                if did in ever_done_dl or last:
                    # Already ran at least once. Only re-fire if the user set
                    # a positive interval and enough time has passed.
                    if interval <= 0 or (time.time() - last) < interval:
                        continue
                drv = (s.get("drives") or {}).get(item.get("drive_uuid")) or {}
                if drv.get("paused"):
                    continue
                if not self._autosync_due(did):
                    continue
                self._autosync_attempt("download", did, item.get("label", did))

        if settings.get("uploads_enabled", True):
            for uid_, item in (s.get("uploads") or {}).items():
                if item.get("state") != "active":
                    continue
                if uid_ in live_targets:
                    continue
                interval = self._schedule_seconds(item.get("schedule"))
                last = item.get("last_sync") or 0
                if uid_ in ever_done_ul or last:
                    if interval <= 0 or (time.time() - last) < interval:
                        continue
                drv = (s.get("drives") or {}).get(item.get("drive_uuid")) or {}
                if drv.get("paused"):
                    continue
                if not self._autosync_due(uid_):
                    continue
                self._autosync_attempt("upload", uid_, item.get("label", uid_))

        # Project-level auto-sync: any project with a positive
        # ``auto_sync_schedule`` whose interval has elapsed since
        # ``last_auto_sync_at`` will queue all of its active member
        # entries (downloads + uploads with project_id or project_ids
        # containing this pid).
        for pid, proj in (s.get("projects") or {}).items():
            interval = self._schedule_seconds(proj.get("auto_sync_schedule"))
            if interval <= 0:
                continue
            last = float(proj.get("last_auto_sync_at") or 0)
            if (time.time() - last) < interval:
                continue
            try:
                self.api_project_sync_now(pid)
            except Exception:
                logger.exception("project autosync %s failed", pid)

    def _autosync_attempt(self, kind: str, target_id: str, label: str) -> None:
        # Guard against the target disappearing between tick and attempt.
        bucket = "downloads" if kind == "download" else "uploads"
        if not (self.store.get().get(bucket) or {}).get(target_id):
            with self._autosync_lock:
                self._autosync_state.pop(target_id, None)
            return
        with self._autosync_lock:
            self._inflight_submits.add(target_id)
        try:
            try:
                res = (self.api_download_sync(target_id) if kind == "download"
                       else self.api_upload_sync(target_id))
            except Exception as e:
                logger.exception("autosync %s %s failed", kind, target_id)
                res = {"ok": False, "error": str(e)}
        finally:
            with self._autosync_lock:
                self._inflight_submits.discard(target_id)
        if res.get("ok"):
            logger.info("autosync queued %s %s (%s)", kind, target_id, label)
            # Reset attempt counter on submit; failure of the actual rclone
            # run will be picked up via the queued/running -> error flow on
            # the next tick.
            self._autosync_record(target_id, ok=True)
        else:
            err = res.get("error") or "unknown"
            # Drive not mounted / paused / disabled aren't real failures, just
            # transient: re-check shortly without inflating the backoff.
            transient = any(s in err for s in (
                "drive not mounted", "drive is paused",
                "BOT-SYNC master switch", "Downloads are disabled",
                "Uploads are disabled"))
            if transient:
                with self._autosync_lock:
                    self._autosync_state.setdefault(
                        target_id, {"attempts": 0.0, "next_at": 0.0})["next_at"] = time.time() + self._AUTOSYNC_TICK * 2
            else:
                logger.info("autosync deferred %s %s: %s", kind, target_id, err)
                self._autosync_record(target_id, ok=False)

    # ------- rclone update loop -------

    def _update_check_loop(self) -> None:
        """Compare installed rclone version to the latest GitHub release.

        Runs every ``UPDATE_CHECK_INTERVAL`` seconds (with an initial 60s
        delay so startup isn't blocked on network). Persists results into
        ``state.rclone_status`` and emits ``rclone.update_available`` exactly
        once per newly-discovered upstream version.
        """
        # Initial delay so we don't hammer the network at boot.
        if self._stop_event.wait(60):
            return
        while not self._stop_event.is_set():
            try:
                self.refresh_rclone_status(notify=True)
            except Exception:
                logger.exception("rclone update check")
            # 24h between checks.
            if self._stop_event.wait(24 * 3600):
                return

    def refresh_rclone_status(self, notify: bool = True) -> Dict[str, Any]:
        """Run version + latest-release check; update store; optionally notify."""
        v = self.rclone.version()
        installed = v.get("version") if v.get("ok") else None
        latest_res = self.rclone.latest_version()
        if not latest_res.get("ok"):
            def _err(d: Dict[str, Any], e=latest_res.get("error")) -> None:
                rs = d.setdefault("rclone_status", {})
                rs["installed_version"] = installed
                rs["check_error"] = e
                rs["checked_at"] = time.time()
            self.store.update(_err)
            return self.store.get().get("rclone_status", {})
        latest = latest_res.get("version")
        avail = bool(installed and latest and _version_lt(installed, latest))
        prev_announced = (self.store.get().get("rclone_status") or {}).get("announced_version")

        def _apply(d: Dict[str, Any]) -> None:
            rs = d.setdefault("rclone_status", {})
            rs["installed_version"] = installed
            rs["latest_version"] = latest
            rs["update_available"] = avail
            rs["checked_at"] = time.time()
            rs["check_error"] = None
            rs["release_url"] = latest_res.get("url")
            # Cap stored release notes so a chatty rclone changelog can't
            # bloat state.json on a 256MB router.
            notes = latest_res.get("notes")
            if isinstance(notes, str) and len(notes) > 4096:
                notes = notes[:4096] + "\n\u2026 (truncated)"
            rs["release_notes"] = notes
            if avail:
                rs["announced_version"] = latest
        self.store.update(_apply)

        if notify and avail and prev_announced != latest:
            self.notifier.emit(
                "rclone.update_available",
                f"rclone {latest} is available (installed {installed or 'unknown'})",
                installed=installed, latest=latest, url=latest_res.get("url"))
        return self.store.get().get("rclone_status", {})

    def perform_rclone_update(self, beta: bool = False) -> Dict[str, Any]:
        """Trigger ``rclone selfupdate``. Updates store and notifies on result."""
        rs_now = self.store.get().get("rclone_status", {}) or {}
        if rs_now.get("updating"):
            return {"ok": False, "error": "update already in progress"}
        self.store.update(lambda d: d.setdefault("rclone_status", {}).update({
            "updating": True,
            "last_update_attempt": time.time(),
            "last_update_error": None,
        }))
        try:
            res = self.rclone.selfupdate(beta=beta)
        except Exception as e:
            res = {"ok": False, "error": str(e)}

        def _apply(d: Dict[str, Any]) -> None:
            rs = d.setdefault("rclone_status", {})
            rs["updating"] = False
            rs["last_update_from"] = res.get("version_before")
            rs["last_update_to"] = res.get("version_after")
            rs["last_update_error"] = None if res.get("ok") else res.get("error")
            if res.get("ok") and res.get("version_after"):
                rs["installed_version"] = res.get("version_after")
                latest = rs.get("latest_version")
                rs["update_available"] = bool(latest and _version_lt(res.get("version_after"), latest))
        self.store.update(_apply)

        if res.get("ok"):
            self.notifier.emit(
                "rclone.update_installed",
                f"rclone updated {res.get('version_before') or '?'} → {res.get('version_after') or '?'}",
                version_before=res.get("version_before"),
                version_after=res.get("version_after"))
        else:
            self.notifier.emit(
                "rclone.update_failed",
                f"rclone update failed: {res.get('error') or 'unknown error'}",
                error=res.get("error"))
        return res

    # ------- API handlers -------

    def api_state(self) -> Dict[str, Any]:
        s = self.store.get()
        s.pop("auth", None)
        s["drives_live"] = self._drives_merged()
        s["jobs"] = self.jobs.list()
        s["system"] = system_info()
        # Compact summary of the primary drive's presence so the UI can
        # render a top-of-page banner without re-walking drives_live. The
        # `since` epoch is non-null only while the drive is unexpectedly
        # missing (i.e. last seen present, now gone) so the UI can show
        # a "missing for Nm" duration.
        primary = next((d for d in s["drives_live"] if d.get("adopted") and d.get("primary")), None)
        if primary is not None:
            s["primary_drive"] = {
                "uuid": primary.get("uuid"),
                "label": primary.get("label"),
                "fs": primary.get("fs"),
                "present": bool(primary.get("present")),
                "mountpoint": primary.get("mountpoint"),
                "missing_since": self._primary_missing_since,
            }
        else:
            s["primary_drive"] = None
        # filter providers by per-service enable toggle. New providers
        # added in later versions (ftp/sftp shipped in 0.7.4) default to
        # enabled so they show up without the user having to flip a
        # settings switch first.
        enabled = (s.get("settings", {}) or {}).get("providers_enabled", {}) or {}
        _DEFAULT_ENABLED = {"drive", "dropbox", "ftp", "sftp"}
        s["providers"] = {k: v for k, v in PROVIDERS.items()
                          if enabled.get(k, k in _DEFAULT_ENABLED)}
        s["providers_all"] = PROVIDERS
        s["provider_help"] = PROVIDER_HELP
        s["mock"] = IS_MOCK
        s["notify_kinds"] = NOTIFY_CHANNEL_KINDS
        s["event_types"] = EVENT_TYPES
        s["severities"] = SEVERITIES
        # Redact channel secrets in state snapshot
        for ch in (s.get("notifications", {}).get("channels", {}) or {}).values():
            cfg = ch.get("config") or {}
            for secret_field in ("password", "auth_header"):
                if cfg.get(secret_field):
                    cfg[secret_field] = "•••"
        return s

    def api_drives(self) -> Dict[str, Any]:
        return {"drives": self._drives_merged()}

    def api_drive_adopt(self, body: Dict[str, Any]) -> Dict[str, Any]:
        uid = body.get("uuid")
        label = body.get("label") or "botsync"
        primary = body.get("primary", False)
        if not uid:
            return {"ok": False, "error": "uuid required"}
        live = {d["uuid"]: d for d in DriveProbe.detect()}.get(uid)
        if not live and not IS_MOCK:
            return {"ok": False, "error": "drive not detected"}
        mp = (live or {}).get("mountpoint") or os.path.join("/mnt/sync", uid)
        # Lay down skeleton & marker on the drive.
        try:
            for sub in ("etc", "var/log", "var/run", "var/cache", "var/tmp", "bin", "downloads",
                        "uploads/drive", "uploads/dropbox", "uploads/box", "uploads/onedrive"):
                os.makedirs(os.path.join(mp, sub), exist_ok=True)
            with open(os.path.join(mp, ".botsync_marker"), "w") as f:
                f.write(json.dumps({"version": 1, "uuid": uid, "adopted_at": time.time()}))
        except Exception as e:
            return {"ok": False, "error": f"could not initialise drive: {e}"}

        def _add(d: Dict[str, Any]) -> None:
            if primary or not any(x.get("primary") for x in d["drives"].values()):
                for x in d["drives"].values():
                    x["primary"] = False
                primary_v = True
            else:
                primary_v = False
            d["drives"][uid] = {
                "label": label, "fs": (live or {}).get("fs", "exfat"),
                "mountpoint": mp, "primary": primary_v or primary,
                "adopted_at": time.time(),
                "size_bytes": (live or {}).get("size_bytes", 0),
                "free_bytes": (live or {}).get("free_bytes", 0),
            }
        self.store.update(_add)
        self.notifier.emit("drive.adopted", f"drive {label} adopted",
                           uuid=uid, mountpoint=mp)
        return {"ok": True, "uuid": uid, "mountpoint": mp}

    def api_drive_action(self, uid: str, action: str) -> Dict[str, Any]:
        if action == "primary":
            def _p(d: Dict[str, Any]) -> None:
                if uid not in d["drives"]:
                    return
                for x in d["drives"].values():
                    x["primary"] = False
                d["drives"][uid]["primary"] = True
            self.store.update(_p)
            return {"ok": True}
        if action == "forget":
            self.store.update(lambda d: d["drives"].pop(uid, None))
            return {"ok": True}
        if action in ("pause", "resume"):
            paused = (action == "pause")
            def _set(d: Dict[str, Any]) -> None:
                if uid in d["drives"]:
                    d["drives"][uid]["paused"] = paused
            self.store.update(_set)
            cancelled = self._cancel_drive_jobs(uid) if paused else 0
            return {"ok": True, "paused": paused, "cancelled": cancelled}
        if action in ("mount", "eject"):
            if action == "eject":
                # Pause this drive and cancel any in-flight syncs against it
                # so we don't yank the device while rclone is still writing.
                def _pause(d: Dict[str, Any]) -> None:
                    if uid in d["drives"]:
                        d["drives"][uid]["paused"] = True
                self.store.update(_pause)
                self._cancel_drive_jobs(uid, wait=5.0)
            if IS_MOCK or IS_WINDOWS:
                return {"ok": True, "mock": True}
            mp = self.store.get()["drives"].get(uid, {}).get("mountpoint") or os.path.join("/mnt/sync", uid)
            if action == "mount":
                live = {d["uuid"]: d for d in DriveProbe.detect()}.get(uid)
                if not live:
                    return {"ok": False, "error": "drive not present"}
                os.makedirs(mp, exist_ok=True)
                # If the device is already mounted at mp (e.g. eject failed
                # because the FS was busy), treat as success and clear the
                # auto-pause flag so the user isn't stuck.
                already = False
                try:
                    with open("/proc/mounts") as f:
                        for ln in f:
                            parts = ln.split()
                            if len(parts) >= 2 and parts[0] == live["device"] and parts[1] == mp:
                                already = True; break
                except Exception:
                    pass
                if already:
                    r_rc = 0; r_err = ""
                else:
                    r = subprocess.run(["mount", live["device"], mp], capture_output=True, text=True)
                    r_rc = r.returncode; r_err = r.stderr.strip()
                if r_rc == 0:
                    # Re-mount implies the user is done pulling the drive,
                    # so clear the auto-pause set on eject.
                    def _resume(d: Dict[str, Any]) -> None:
                        if uid in d["drives"]:
                            d["drives"][uid]["paused"] = False
                    self.store.update(_resume)
                return {"ok": r_rc == 0, "stderr": r_err}
            else:
                subprocess.run(["sync"], check=False)
                r = subprocess.run(["umount", mp], capture_output=True, text=True)
                if r.returncode != 0:
                    # Unmount failed (typically EBUSY). Roll back the auto-pause
                    # so the user isn't stranded with a paused-but-mounted drive.
                    def _unpause(d: Dict[str, Any]) -> None:
                        if uid in d["drives"]:
                            d["drives"][uid]["paused"] = False
                    self.store.update(_unpause)
                    return {"ok": False, "stderr": r.stderr.strip(), "paused": False}
                return {"ok": True, "stderr": r.stderr.strip(), "paused": True}
        return {"ok": False, "error": "unknown action"}

    def _cancel_drive_jobs(self, drive_uuid: str, wait: float = 0.0) -> int:
        """Cancel every running/queued job whose target download or upload
        lives on the given drive. Returns the number of jobs cancelled. If
        *wait* > 0, blocks up to that many seconds for them to actually exit."""
        # Snapshot under the store lock so a concurrent add can't race us
        # into missing a freshly-queued job for this drive.
        with self.store._lock:
            s = self.store._data
            target_ids: set = set()
            for did, item in (s.get("downloads") or {}).items():
                if item.get("drive_uuid") == drive_uuid:
                    target_ids.add(did)
            for uid_, item in (s.get("uploads") or {}).items():
                if item.get("drive_uuid") == drive_uuid:
                    target_ids.add(uid_)
        cancelled = 0
        for j in self.jobs.list():
            if j.get("target_id") in target_ids and j.get("state") in ("running", "queued"):
                if self.jobs.cancel(j["id"]):
                    cancelled += 1
        if wait > 0 and cancelled:
            deadline = time.time() + wait
            while time.time() < deadline:
                live = [j for j in self.jobs.list()
                        if j.get("target_id") in target_ids
                        and j.get("state") in ("running", "queued")]
                if not live:
                    break
                time.sleep(0.2)
        return cancelled

    # ---- remotes ----

    def api_remotes(self) -> Dict[str, Any]:
        remotes = self.store.get()["remotes"]
        # Surface a flat provider->name map so the UI doesn't have to scan
        # the remotes dict to know which one is default for each provider.
        defaults: Dict[str, str] = {}
        for n, r in remotes.items():
            if r.get("default"):
                p = r.get("provider")
                if p and p not in defaults:
                    defaults[p] = n
        return {"remotes": remotes,
                "defaults": defaults,
                "providers": PROVIDERS,
                "provider_help": PROVIDER_HELP}

    def api_remote_oauth_start(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.oauth.start(body.get("provider", ""), body.get("name", "").strip() or "remote")

    def api_remote_oauth_finish(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.oauth.finish(body.get("session_id", ""), body.get("token", ""))

    def api_remote_oauth_device_start(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.oauth.start_device(
            (body.get("name") or "").strip(),
            (body.get("client_id") or "").strip(),
            (body.get("client_secret") or "").strip(),
        )

    def api_remote_oauth_device_poll(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.oauth.poll_device((body.get("session_id") or "").strip())

    def api_remote_basic_create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Create a non-OAuth rclone remote (FTP / SFTP) from a credentials form.

        Validates required fields per ``PROVIDERS[provider]['basic_fields']``,
        obscures the password, persists an SFTP private key to a file inside
        ``etc/`` (chmod 600), and finally calls ``rclone config create``.

        Returns ``{ok, error?, field?, fix?}`` where ``field`` and ``fix`` are
        included on validation failures so the UI can highlight the offending
        input and tell the user how to fix it.
        """
        provider = (body.get("provider") or "").strip()
        meta = PROVIDERS.get(provider)
        if not meta or meta.get("auth") != "basic":
            return {"ok": False, "error": "provider must be ftp or sftp",
                    "field": "provider",
                    "fix": "Pick FTP / FTPS or SFTP — Google Drive / Dropbox / Box / OneDrive use the OAuth flow above."}

        name = (body.get("name") or "").strip()
        if not re.match(r"^[A-Za-z0-9_\-]{1,40}$", name):
            return {"ok": False, "error": "invalid account name",
                    "field": "name",
                    "fix": "Use 1-40 letters, digits, underscore or dash. No spaces or punctuation."}
        if name in self.store.get()["remotes"]:
            return {"ok": False, "error": "account name already exists",
                    "field": "name",
                    "fix": f"'{name}' is already configured. Pick a different name or delete the existing entry first."}

        host = (body.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "host required",
                    "field": "host",
                    "fix": "Enter the server hostname or IP, e.g. ftp.example.com or 192.0.2.10 (no scheme, no path)."}

        user = (body.get("user") or "").strip()
        if not user:
            return {"ok": False, "error": "username required",
                    "field": "user",
                    "fix": "Enter the login username. For public FTP servers use 'anonymous'."}

        port_raw = (body.get("port") or "").strip()
        port: Optional[str] = None
        if port_raw:
            try:
                p = int(port_raw)
                if not (1 <= p <= 65535):
                    raise ValueError
                port = str(p)
            except ValueError:
                return {"ok": False, "error": "port must be 1-65535",
                        "field": "port",
                        "fix": "Leave blank for default (21 for FTP, 22 for SFTP) or enter a TCP port between 1 and 65535."}

        password = body.get("pass") or ""
        params: Dict[str, str] = {"host": host, "user": user}
        if port:
            params["port"] = port

        if password:
            obs = self.rclone.obscure(password)
            if not obs.get("ok"):
                return {"ok": False, "error": obs.get("error") or "could not obscure password"}
            params["pass"] = obs["obscured"]

        if provider == "ftp":
            tls = (body.get("tls") or "").strip()
            if tls == "explicit":
                params["explicit_tls"] = "true"
            elif tls == "implicit":
                params["tls"] = "true"
            if body.get("no_epsv"):
                params["disable_epsv"] = "true"
        elif provider == "sftp":
            key_pem = (body.get("key_pem") or "").strip()
            key_pass = body.get("key_pass") or ""
            if key_pem:
                # Persist the key to <conf-dir>/keys/<name>.pem with 0600.
                conf_dir = os.path.dirname(self.rclone.conf) or ROOT
                keys_dir = os.path.join(conf_dir, "keys")
                try:
                    os.makedirs(keys_dir, exist_ok=True)
                    os.chmod(keys_dir, 0o700)
                except Exception:
                    pass
                key_path = os.path.join(keys_dir, f"{name}.pem")
                try:
                    with open(key_path, "w") as fh:
                        fh.write(key_pem)
                        if not key_pem.endswith("\n"):
                            fh.write("\n")
                    os.chmod(key_path, 0o600)
                except Exception as e:
                    return {"ok": False, "error": f"could not write key file: {e}",
                            "field": "key_pem",
                            "fix": "Check that the storage path is mounted and writable."}
                params["key_file"] = key_path
                if key_pass:
                    obsk = self.rclone.obscure(key_pass)
                    if not obsk.get("ok"):
                        return {"ok": False, "error": obsk.get("error") or "could not obscure key passphrase"}
                    params["key_file_pass"] = obsk["obscured"]
            elif not password:
                return {"ok": False, "error": "password or private key required",
                        "field": "pass",
                        "fix": "Either type the SSH password or paste a PEM-format private key. SFTP servers don't accept passwordless logins."}

        res = self.rclone.add_basic_remote(name, meta["rclone_type"], params)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "rclone config create failed"}

        def _add(d: Dict[str, Any]) -> None:
            d["remotes"][name] = {
                "provider": provider, "type": meta["rclone_type"],
                "health": "unknown", "last_check": None,
                "expires_at": None, "error": None, "needs_reauth": False,
                "auth": "basic",
            }
            _ensure_default_for_provider(d, name)
        self.store.update(_add)
        return {"ok": True, "name": name}

    def api_remote_delete(self, name: str) -> Dict[str, Any]:
        self.rclone.remove_remote(name)
        # Best-effort: remove a stored SFTP private key so we don't leave
        # secrets behind on disk after the user removes an account.
        try:
            conf_dir = os.path.dirname(self.rclone.conf) or ROOT
            key_path = os.path.join(conf_dir, "keys", f"{name}.pem")
            if os.path.isfile(key_path):
                os.remove(key_path)
        except Exception:
            pass
        def _drop(d: Dict[str, Any]) -> None:
            removed = d["remotes"].pop(name, None)
            # If the removed account was the per-provider default, promote
            # another remote of the same provider so future downloads /
            # uploads still resolve to a sensible default automatically.
            _promote_replacement_default(d, removed or {})
        self.store.update(_drop)
        return {"ok": True}

    def api_remote_set_default(self, name: str) -> Dict[str, Any]:
        """Mark ``name`` as the default account for its provider, clearing
        the flag on any sibling accounts of the same provider so the
        invariant 'at most one default per provider' holds."""
        cur = (self.store.get().get("remotes") or {}).get(name)
        if not cur:
            return {"ok": False, "error": "unknown account"}
        provider = cur.get("provider")
        if not provider:
            return {"ok": False, "error": "account has no provider"}
        def _set(d: Dict[str, Any]) -> None:
            for n, r in d["remotes"].items():
                if r.get("provider") == provider:
                    r["default"] = (n == name)
        self.store.update(_set)
        return {"ok": True, "name": name, "provider": provider}

    def api_remote_check(self, name: str) -> Dict[str, Any]:
        res = self.rclone.remote_ping(name)
        err = res.get("error")
        needs_reauth = (not res.get("ok")) and _looks_like_reauth(err)
        prev_reauth = bool((self.store.get()["remotes"].get(name, {}) or {}).get("needs_reauth"))
        def _upd(d: Dict[str, Any]) -> None:
            if name in d["remotes"]:
                d["remotes"][name]["last_check"] = time.time()
                d["remotes"][name]["health"] = "ok" if res.get("ok") else "error"
                d["remotes"][name]["error"] = err
                d["remotes"][name]["needs_reauth"] = needs_reauth
        self.store.update(_upd)
        if needs_reauth and not prev_reauth:
            self.notifier.emit(
                "remote.reauth_required",
                f"account {name} needs to be reconnected — token expired or revoked",
                remote=name, error=err)
        out = dict(res)
        out["needs_reauth"] = needs_reauth
        return out

    # ---- rclone version / update ----

    def api_rclone_status(self) -> Dict[str, Any]:
        return {"ok": True, "rclone": self.store.get().get("rclone_status", {}) or {}}

    def api_rclone_check(self) -> Dict[str, Any]:
        rs = self.refresh_rclone_status(notify=True)
        return {"ok": True, "rclone": rs}

    def api_rclone_update(self, body: Dict[str, Any]) -> Dict[str, Any]:
        beta = bool((body or {}).get("beta"))
        # selfupdate can take 30-60s on slow links; the route will block.
        return self.perform_rclone_update(beta=beta)

    # ---- projects ----
    # A project is a lightweight grouping that nests its downloads and
    # uploads under a per-project folder on the local drive. Spaces in the
    # project name become "-" for the on-disk slug. Projects are persistent
    # across daemon restarts (stored in botsync.json under "projects") but
    # are otherwise inert: they don't change rclone behaviour, they just
    # rewrite local_subpath at create time. Existing entries (project_id
    # missing/empty) keep their flat layout.

    @staticmethod
    def _project_slug(name: str) -> str:
        # Spec: "spaces are replaced with dash marks". Then strip anything
        # not safe for a path component to keep the on-disk name predictable
        # across SMB / vfat / ext4. Keep case so the folder is recognisable.
        s = re.sub(r"\s+", "-", (name or "").strip())
        s = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
        s = s.strip("._-") or ""
        return s[:80]

    @staticmethod
    def _parse_auto_delete(value: Any) -> Optional[float]:
        """Normalise an auto_delete_at field. Accepts:
          - None / "" / 0 -> None (no auto-delete)
          - epoch seconds (int / float / numeric string)
          - ISO-8601 string (best-effort, naive treated as local time)
        """
        if value in (None, "", 0, "0"):
            return None
        try:
            if isinstance(value, (int, float)):
                return float(value) or None
            s = str(value).strip()
            if not s:
                return None
            # Numeric-string fast path.
            try:
                return float(s) or None
            except ValueError:
                pass
            # ISO-8601 fallback: "YYYY-MM-DDTHH:MM" from <input type=datetime-local>.
            try:
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                # datetime-local: assume the user's local clock.
                return dt.timestamp()
            return dt.timestamp()
        except Exception:
            return None

    def api_project_list(self) -> Dict[str, Any]:
        return {"projects": self.store.get().get("projects", {}) or {}}

    def api_project_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        slug = self._project_slug(name)
        if not slug:
            return {"ok": False, "error": "name has no usable characters"}
        pid = secrets.token_hex(6)
        # Reject duplicate slug — two projects sharing a slug would collide
        # on disk and make it ambiguous which one a download belongs to.
        existing = (self.store.get().get("projects") or {})
        if any((p.get("slug") or "") == slug for p in existing.values()):
            return {"ok": False, "error": f"a project with slug '{slug}' already exists"}

        def _add(d: Dict[str, Any]) -> None:
            d.setdefault("projects", {})[pid] = {
                "name": name, "slug": slug, "created_at": time.time(),
                "auto_delete_at": self._parse_auto_delete(body.get("auto_delete_at")),
            }
        self.store.update(_add)
        return {"ok": True, "id": pid, "slug": slug}

    def api_project_patch(self, pid: str, body: Dict[str, Any]) -> Dict[str, Any]:
        # Only the display name, auto-delete timestamp, and auto-sync
        # schedule are editable. The slug is frozen because changing it
        # would orphan existing on-disk folders. Users who want a new
        # slug should create a new project and reassign entries.
        name = (body.get("name") or "").strip() if "name" in body else None
        if "name" in body and not name:
            return {"ok": False, "error": "name required"}
        def _u(d: Dict[str, Any]) -> None:
            p = (d.get("projects") or {}).get(pid)
            if p:
                if name is not None:
                    p["name"] = name
                if "auto_delete_at" in body:
                    p["auto_delete_at"] = self._parse_auto_delete(body.get("auto_delete_at"))
                if "auto_sync_schedule" in body:
                    raw = body.get("auto_sync_schedule")
                    p["auto_sync_schedule"] = ("" if raw in (None, "", "manual", "off", "none") else str(raw))
                    # Reset the timer so the new schedule kicks in promptly.
                    p["last_auto_sync_at"] = 0
        self.store.update(_u)
        return {"ok": True}

    def api_project_sync_now(self, pid: str) -> Dict[str, Any]:
        """Queue every download/upload tagged with this project (primary or
        mirror). Called by the UI's "Sync now" button and by the autosync
        loop when a project's auto_sync_schedule is due."""
        s = self.store.get()
        if not (s.get("projects") or {}).get(pid):
            return {"ok": False, "error": "unknown project"}
        queued = 0
        for did, item in (s.get("downloads") or {}).items():
            if item.get("project_id") == pid or pid in (item.get("project_ids") or []):
                if item.get("state") == "active":
                    self._autosync_attempt("download", did, item.get("label", did))
                    queued += 1
        for uid_, item in (s.get("uploads") or {}).items():
            if item.get("project_id") == pid or pid in (item.get("project_ids") or []):
                if item.get("state") == "active":
                    self._autosync_attempt("upload", uid_, item.get("label", uid_))
                    queued += 1
        def _stamp(d: Dict[str, Any]) -> None:
            p = (d.get("projects") or {}).get(pid)
            if p:
                p["last_auto_sync_at"] = time.time()
        self.store.update(_stamp)
        return {"ok": True, "queued": queued}

    def api_project_delete(self, pid: str) -> Dict[str, Any]:
        s = self.store.get()
        # Refuse to delete a project that still has assigned entries —
        # otherwise their local_subpath would point at a now-meaningless
        # slug and the user has no way to reassign them via the UI.
        used_dl = [did for did, d in (s.get("downloads") or {}).items()
                   if d.get("project_id") == pid or pid in (d.get("project_ids") or [])]
        used_up = [uid for uid, u in (s.get("uploads") or {}).items()
                   if u.get("project_id") == pid or pid in (u.get("project_ids") or [])]
        if used_dl or used_up:
            return {"ok": False, "error": f"project still has {len(used_dl)} download(s) and {len(used_up)} upload(s) — reassign or delete them first"}
        def _u(d: Dict[str, Any]) -> None:
            (d.get("projects") or {}).pop(pid, None)
        self.store.update(_u)
        return {"ok": True}

    def _project_slug_for(self, pid: Optional[str]) -> Optional[str]:
        if not pid:
            return None
        p = (self.store.get().get("projects") or {}).get(pid)
        return (p or {}).get("slug") or None

    def _relocate_local(self, item_id: str, kind: str, old_sub: str, new_sub: str) -> None:
        """Best-effort move of a download/upload's local data folder when the
        user re-assigns it to a different project. ``kind`` is "downloads" or
        "uploads"; both are nested under the same drive mountpoint, so we
        resolve the live mountpoint via the entry's drive_uuid and then
        ``os.rename`` from old_sub to new_sub.

        Failures are logged but never raised — the next sync will recreate
        the destination directory and re-fetch missing files. The caller has
        already updated state to point at new_sub, so leaving stale data at
        old_sub is the worst case (recoverable by re-sync)."""
        try:
            entry = (self.store.get().get(kind) or {}).get(item_id)
            if not entry:
                return
            mp = self._live_mountpoint(entry.get("drive_uuid"))
            if not mp:
                return
            old_abs = os.path.realpath(os.path.join(mp, old_sub))
            new_abs = os.path.realpath(os.path.join(mp, new_sub))
            mp_real = os.path.realpath(mp) + os.sep
            # Refuse to move outside the drive mountpoint — paranoia in case
            # of a crafted project slug or a corrupt state file.
            if not old_abs.startswith(mp_real) or not new_abs.startswith(mp_real):
                logger.warning("_relocate_local refusing out-of-mount move: %s -> %s", old_abs, new_abs)
                return
            if not os.path.isdir(old_abs):
                # Nothing to move — next sync will create new_abs.
                return
            os.makedirs(os.path.dirname(new_abs), exist_ok=True)
            if os.path.exists(new_abs):
                logger.warning("_relocate_local target exists, skipping mv: %s", new_abs)
                return
            os.rename(old_abs, new_abs)
            logger.info("%s %s relocated %s -> %s", kind, item_id, old_sub, new_sub)
        except Exception:
            logger.exception("_relocate_local failed for %s/%s", kind, item_id)

    # ---- multi-project mirroring (v0.7.11) ----
    # A download/upload may carry a list of project_ids. The first entry is
    # the "primary" — that's the project whose slug appears in
    # local_subpath, so it's the actual sync target. Any additional
    # project_ids cause the daemon to mirror the primary's local directory
    # into each of those project folders after a successful sync, so an
    # artist's media drop that's relevant to two simultaneous shows ends up
    # under both per-show folders on the drive without re-downloading from
    # the cloud twice.

    @staticmethod
    def _replace_first_segment(local_sub: str, depth: int, new_slug: Optional[str]) -> str:
        """Rewrite the project-slug component inside local_subpath.

        Download paths are ``downloads/<slug>/<leaf>`` (depth=1) or
        ``downloads/<leaf>`` (no slug). Upload paths are
        ``uploads/<provider>/<slug>/<leaf>`` (depth=2) or
        ``uploads/<provider>/<leaf>`` (no slug). depth = number of fixed
        prefix segments before the slug.
        """
        parts = (local_sub or "").split("/")
        # Strip empties from leading/trailing slashes.
        parts = [p for p in parts if p != ""]
        if len(parts) <= depth:
            return local_sub
        prefix = parts[:depth]
        rest = parts[depth:]
        # Detect whether a slug is currently present: rest has >= 2 parts
        # if a slug + leaf, or 1 part if no slug. We don't try to identify
        # which segment is the slug from disk — instead callers always
        # construct the *additional* mirror path from the primary by
        # taking the leaf (last segment) and inserting the new slug.
        leaf = rest[-1]
        if new_slug:
            return "/".join(prefix + [new_slug, leaf])
        return "/".join(prefix + [leaf])

    def _additional_mirror_subpaths(self, item: Dict[str, Any], kind: str) -> List[Tuple[str, str]]:
        """Return list of (project_id, local_subpath) for *additional*
        project tags (i.e. all project_ids after the primary)."""
        ids = item.get("project_ids") or []
        primary = item.get("project_id")
        # Build the set of "extra" pids (preserve order, drop primary +
        # duplicates + unknowns).
        seen = set()
        if primary:
            seen.add(primary)
        extras: List[str] = []
        for pid in ids:
            if pid and pid != primary and pid not in seen:
                seen.add(pid)
                extras.append(pid)
        if not extras:
            return []
        primary_sub = item.get("local_subpath") or ""
        depth = 1 if kind == "downloads" else 2  # uploads have a provider segment
        out: List[Tuple[str, str]] = []
        for pid in extras:
            slug = self._project_slug_for(pid)
            if not slug:
                continue
            mirror = self._replace_first_segment(primary_sub, depth, slug)
            if mirror and mirror != primary_sub:
                out.append((pid, mirror))
        return out

    def _post_sync_mirror(self, item_id: str, kind: str) -> None:
        """After a successful sync, copy the primary local directory into
        each additional project folder. Uses ``shutil.copytree`` with
        ``dirs_exist_ok=True`` so subsequent syncs incrementally update the
        mirrors. Failures are logged and swallowed — a broken mirror must
        never take the worker thread down."""
        if IS_MOCK:
            return
        item = (self.store.get().get(kind) or {}).get(item_id)
        if not item:
            return
        extras = self._additional_mirror_subpaths(item, kind)
        if not extras:
            return
        mp = self._live_mountpoint(item.get("drive_uuid"))
        if not mp:
            return
        primary_sub = (item.get("local_subpath") or "").lstrip("/")
        src = os.path.join(mp, primary_sub)
        if not os.path.isdir(src):
            return
        for pid, mirror_sub in extras:
            dst = os.path.join(mp, mirror_sub.lstrip("/"))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # copytree(dirs_exist_ok=True) requires Python 3.8+. We
                # require 3.9 anyway, so this is fine on every supported
                # platform.
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False)
                logger.info("mirror %s/%s -> project %s (%s)",
                            kind, item_id, pid, mirror_sub)
            except Exception:
                logger.exception("mirror failed: %s/%s -> %s", kind, item_id, mirror_sub)

    def _on_job_complete(self, job: "Job") -> None:
        """JobManager hook. Runs after every job finishes (success or
        failure). Currently only mirrors successful download/upload jobs to
        any additional project tags — purges are handled separately by the
        autosync loop on a fixed cadence."""
        if job.state != "done" or not job.target_id:
            return
        kind = "downloads" if job.type == "download" else (
               "uploads" if job.type == "upload" else None)
        if not kind:
            return
        try:
            self._post_sync_mirror(job.target_id, kind)
        except Exception:
            logger.exception("_post_sync_mirror failed for %s/%s", kind, job.target_id)

    # ---- auto-purge (v0.7.11) ----
    # Each download / upload / project may carry an ``auto_delete_at``
    # epoch-seconds timestamp. When that time has passed, the autosync
    # loop deletes the entry and (for downloads/uploads) wipes the local
    # data directory. Deleting a project cascades to every download and
    # upload that has the project anywhere in its project_ids list.

    def _purge_due(self) -> None:
        now = time.time()
        s = self.store.get()
        # Downloads
        for did, item in list((s.get("downloads") or {}).items()):
            t = item.get("auto_delete_at")
            if isinstance(t, (int, float)) and t and t <= now:
                logger.info("auto-purge download %s (%s) — expired %.0fs ago",
                            did, item.get("label", ""), now - t)
                try:
                    self.api_download_delete(did, delete_files=True)
                except Exception:
                    logger.exception("auto-purge download %s failed", did)
                # Also wipe any additional-project mirrors for this entry
                # so we don't leave orphan copies behind.
                try:
                    self._purge_mirrors(item, "downloads")
                except Exception:
                    logger.exception("auto-purge mirrors failed for download %s", did)
        # Uploads
        s = self.store.get()
        for uid_, item in list((s.get("uploads") or {}).items()):
            t = item.get("auto_delete_at")
            if isinstance(t, (int, float)) and t and t <= now:
                logger.info("auto-purge upload %s (%s) — expired %.0fs ago",
                            uid_, item.get("label", ""), now - t)
                try:
                    # Uploads currently don't expose a "delete_files" flag in
                    # api_upload_delete — wipe the staging dir manually.
                    self._purge_mirrors(item, "uploads")
                    mp = self._live_mountpoint(item.get("drive_uuid"))
                    sub = (item.get("local_subpath") or "").lstrip("/")
                    if mp and sub and not IS_MOCK:
                        local = os.path.realpath(os.path.join(mp, sub))
                        mp_real = os.path.realpath(mp) + os.sep
                        if local.startswith(mp_real):
                            shutil.rmtree(local, ignore_errors=True)
                    self.api_upload_delete(uid_)
                except Exception:
                    logger.exception("auto-purge upload %s failed", uid_)
        # Projects: cascade-delete every download/upload tagged with the
        # project, then drop the project itself.
        s = self.store.get()
        for pid, proj in list((s.get("projects") or {}).items()):
            if not isinstance(proj, dict):
                continue
            t = proj.get("auto_delete_at")
            if isinstance(t, (int, float)) and t and t <= now:
                logger.info("auto-purge project %s (%s) — expired %.0fs ago",
                            pid, proj.get("name", ""), now - t)
                try:
                    self._purge_project_cascade(pid)
                except Exception:
                    logger.exception("auto-purge project %s failed", pid)

    def _purge_mirrors(self, item: Dict[str, Any], kind: str) -> None:
        """Wipe the additional-project mirror directories for a
        download/upload that's about to be deleted."""
        if IS_MOCK:
            return
        mp = self._live_mountpoint(item.get("drive_uuid"))
        if not mp:
            return
        mp_real = os.path.realpath(mp) + os.sep
        for _pid, mirror_sub in self._additional_mirror_subpaths(item, kind):
            try:
                dst = os.path.realpath(os.path.join(mp, mirror_sub.lstrip("/")))
                if dst.startswith(mp_real):
                    shutil.rmtree(dst, ignore_errors=True)
            except Exception:
                logger.exception("purge mirror failed: %s -> %s", kind, mirror_sub)

    def _purge_project_cascade(self, pid: str) -> None:
        s = self.store.get()
        # Wipe any download/upload that tags this project (primary OR
        # additional). Delete files for downloads, staging dirs for uploads.
        for did, item in list((s.get("downloads") or {}).items()):
            tags = set([item.get("project_id")] + (item.get("project_ids") or []))
            if pid in tags:
                try:
                    self._purge_mirrors(item, "downloads")
                    self.api_download_delete(did, delete_files=True)
                except Exception:
                    logger.exception("cascade delete download %s failed", did)
        s = self.store.get()
        for uid_, item in list((s.get("uploads") or {}).items()):
            tags = set([item.get("project_id")] + (item.get("project_ids") or []))
            if pid in tags:
                try:
                    self._purge_mirrors(item, "uploads")
                    mp = self._live_mountpoint(item.get("drive_uuid"))
                    sub = (item.get("local_subpath") or "").lstrip("/")
                    if mp and sub and not IS_MOCK:
                        local = os.path.realpath(os.path.join(mp, sub))
                        mp_real = os.path.realpath(mp) + os.sep
                        if local.startswith(mp_real):
                            shutil.rmtree(local, ignore_errors=True)
                    self.api_upload_delete(uid_)
                except Exception:
                    logger.exception("cascade delete upload %s failed", uid_)
        # Finally drop the project record itself.
        def _u(d: Dict[str, Any]) -> None:
            (d.get("projects") or {}).pop(pid, None)
        try:
            self.store.update(_u)
        except Exception:
            logger.exception("project record drop failed: %s", pid)

    # ---- health monitor ----

    def _health_check_tick(self) -> None:
        """Sample CPU load %, mem %, swap %, CPU temp; fire health_warning.

        State machine per metric:
          - When a sample first exceeds its threshold, record `since` ts.
          - When it stays over threshold for `sustain_secs`, emit one
            event and stamp `last_alert`. Subsequent over-threshold
            samples are silenced until `cooldown_secs` elapses.
          - When a sample drops back under threshold, clear `since`.
        Per-metric state is kept in-memory only (resets at restart).
        """
        if not hasattr(self, "_health_state"):
            self._health_state: Dict[str, Dict[str, float]] = {}
        s = self.store.get()
        cfg = ((s.get("notifications") or {}).get("health_thresholds") or {})
        if not cfg.get("enabled", True):
            return
        sustain = max(0, int(cfg.get("sustain_secs", 60) or 0))
        cooldown = max(60, int(cfg.get("cooldown_secs", 600) or 600))
        info = system_info()
        now = time.time()
        metrics = [
            ("cpu_load_pct", info.get("cpu_load_pct"), float(cfg.get("cpu_load_pct", 90)), "CPU load",
             lambda v: f"{v:.0f}%"),
            ("mem_used_pct", info.get("mem_used_pct"), float(cfg.get("mem_used_pct", 90)), "Memory used",
             lambda v: f"{v:.0f}%"),
            ("swap_used_pct", info.get("swap_used_pct"), float(cfg.get("swap_used_pct", 80)), "Swap used",
             lambda v: f"{v:.0f}%"),
            ("cpu_temp_c", info.get("cpu_temp_c"), float(cfg.get("cpu_temp_c", 80)), "CPU temperature",
             lambda v: f"{v:.1f} \u00b0C"),
        ]
        triggered: List[Tuple[str, str, float, float]] = []  # (key, label, value, threshold)
        for key, value, threshold, label, _fmt in metrics:
            st = self._health_state.setdefault(key, {"since": 0.0, "last_alert": 0.0})
            if value is None:
                st["since"] = 0.0
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v >= threshold:
                if st["since"] == 0.0:
                    st["since"] = now
                # Sustained long enough?
                if (now - st["since"]) >= sustain and (now - st["last_alert"]) >= cooldown:
                    triggered.append((key, label, v, threshold))
                    st["last_alert"] = now
            else:
                st["since"] = 0.0
        if not triggered:
            return
        # Single combined event so a webhook hit covers everything currently
        # over threshold instead of N back-to-back posts.
        parts = []
        fields: Dict[str, Any] = {}
        for key, label, v, thr in triggered:
            unit = "\u00b0C" if key == "cpu_temp_c" else "%"
            parts.append(f"{label} {v:.1f}{unit} (\u2265 {thr:.0f}{unit})")
            fields[key] = v
            fields[key + "_threshold"] = thr
        msg = "Health threshold exceeded: " + "; ".join(parts)
        try:
            self.notify.emit("system.health_warning", msg, **fields)
        except Exception:
            logger.exception("emit health_warning failed")

    def api_health_get(self) -> Dict[str, Any]:
        s = self.store.get()
        cfg = ((s.get("notifications") or {}).get("health_thresholds") or {})
        info = system_info()
        return {
            "ok": True,
            "thresholds": cfg,
            "current": {
                "cpu_load_pct": info.get("cpu_load_pct"),
                "mem_used_pct": info.get("mem_used_pct"),
                "swap_used_pct": info.get("swap_used_pct"),
                "cpu_temp_c": info.get("cpu_temp_c"),
                "cpu_count": info.get("cpu_count"),
            },
        }

    def api_health_patch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        ALLOWED = {"enabled", "cpu_load_pct", "mem_used_pct", "swap_used_pct",
                   "cpu_temp_c", "sustain_secs", "cooldown_secs"}
        clean: Dict[str, Any] = {}
        for k, v in (body or {}).items():
            if k not in ALLOWED:
                continue
            if k == "enabled":
                clean[k] = bool(v)
            else:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{k}: must be a number"}
                if fv < 0:
                    return {"ok": False, "error": f"{k}: must be >= 0"}
                if k in ("sustain_secs", "cooldown_secs"):
                    clean[k] = int(fv)
                else:
                    clean[k] = round(fv, 1)
        def _u(d: Dict[str, Any]) -> None:
            d.setdefault("notifications", {}).setdefault("health_thresholds", {}).update(clean)
        self.store.update(_u)
        # Reset the per-metric state so a fresh threshold takes effect immediately.
        self._health_state = {}
        return self.api_health_get()

    # ---- downloads ----

    def api_download_list(self) -> Dict[str, Any]:
        return {"downloads": self.store.get()["downloads"]}

    def api_download_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = (body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url required", "field": "url",
                    "fix": "Paste the share URL (Drive folder, Dropbox shared link, etc.) or for FTP/SFTP enter ftp://host/path or sftp://host/path."}
        parsed = parse_link(url)
        provider = parsed.get("provider")
        remote = (body.get("remote") or "").strip()
        if not remote and provider:
            # Auto-pick the default account for the detected provider —
            # e.g. paste a Dropbox share link and we'll fill in the user's
            # default Dropbox account so they don't have to touch the
            # dropdown when there's only one obvious choice.
            for n, r in (self.store.get().get("remotes") or {}).items():
                if r.get("provider") == provider and r.get("default"):
                    remote = n
                    break
        remote_path = (body.get("remote_path") or parsed.get("remote_path") or "").strip()
        # Provider-specific guard rails. The most common footgun is leaving
        # remote_path blank on Dropbox/Box/OneDrive/FTP/SFTP, which causes
        # rclone to copy the entire account/server and OOM the router.
        if provider in ("dropbox", "box", "onedrive") and not remote_path:
            return {"ok": False, "error": "remote path required for " + provider,
                    "field": "remote_path",
                    "fix": ("Set Remote path to the folder name as it appears at the root of your "
                            + PROVIDERS[provider]["label"] + " account. Leaving this blank would copy your entire account.")}
        if provider in ("ftp", "sftp") and not remote:
            return {"ok": False, "error": "pick an FTP/SFTP account",
                    "field": "remote",
                    "fix": "Create an FTP/SFTP account on the Accounts tab first, then pick it here."}
        if provider in ("dropbox", "box", "onedrive", "ftp", "sftp") and not remote:
            return {"ok": False, "error": "account required",
                    "field": "remote",
                    "fix": "Pick the connected account that owns this folder. Connect one on the Accounts tab if the dropdown is empty."}
        did = secrets.token_hex(6)
        primary = next((u for u, d in self.store.get()["drives"].items() if d.get("primary")), None)
        drive_uuid = body.get("drive_uuid") or primary
        if not drive_uuid:
            return {"ok": False, "error": "no adopted drive — adopt a drive first"}
        label = body.get("label") or parsed.get("label") or url
        local_subpath = re.sub(r"[^A-Za-z0-9 _.\-]", "_", label)[:80] or did
        # Optional project nesting: if the user picked a project at create
        # time, all of this download's local files live under
        # downloads/<project-slug>/<label>. The project_id is also stored
        # on the entry so the UI can show the grouping and so future
        # operations (delete, edit) know which slug to honour.
        project_id = (body.get("project_id") or "").strip() or None
        proj_slug = self._project_slug_for(project_id)
        if project_id and not proj_slug:
            return {"ok": False, "error": "unknown project_id"}
        # Multi-project tagging (v0.7.11). project_ids is a list whose first
        # entry is the *primary* (== project_id, the one whose slug nests
        # local_subpath); any extras get mirrored after each successful
        # sync. We accept either field for forward/back compat.
        raw_ids = body.get("project_ids")
        if isinstance(raw_ids, list):
            extra_ids: List[str] = [str(x).strip() for x in raw_ids if x]
        else:
            extra_ids = []
        # Validate each extra against known projects.
        for xid in extra_ids:
            if not self._project_slug_for(xid):
                return {"ok": False, "error": f"unknown project_id in project_ids: {xid}",
                        "field": "project_ids"}
        # If the user supplied project_ids but no project_id, promote the
        # first to primary so local_subpath gets a slug.
        if extra_ids and not project_id:
            project_id = extra_ids[0]
            proj_slug = self._project_slug_for(project_id)
        # Canonical ordered list with primary first, no duplicates.
        merged_ids: List[str] = []
        if project_id:
            merged_ids.append(project_id)
        for xid in extra_ids:
            if xid not in merged_ids:
                merged_ids.append(xid)
        sub = ("downloads/" + proj_slug + "/" + local_subpath) if proj_slug else ("downloads/" + local_subpath)
        # Auto-delete: epoch seconds, optional. UI sends a number; we accept
        # numeric strings too so curl users can `-d auto_delete_at=1814400000`.
        auto_delete_at = self._parse_auto_delete(body.get("auto_delete_at"))

        def _add(d: Dict[str, Any]) -> None:
            d["downloads"][did] = {
                "label": label, "url": url,
                "provider": parsed.get("provider"),
                "remote": remote,
                "remote_path": remote_path,
                "folder_id": parsed.get("folder_id"),
                "drive_uuid": drive_uuid,
                "local_subpath": sub,
                "project_id": project_id,
                "project_ids": merged_ids,
                "auto_delete_at": auto_delete_at,
                "state": "active",
                "schedule": body.get("schedule", ""),
                "last_sync": None, "remote_size": 0, "local_size": 0,
            }
        self.store.update(_add)
        # Setup hint (e.g. "Add this Dropbox shared folder first") is returned
        # once with the create response so the UI can show it inline at the
        # form, rather than persisting on the item where it would stick around
        # after the user has actually completed setup.
        out = {"ok": True, "id": did}
        if parsed.get("warning"):
            out["warning"] = parsed["warning"]
        # Kick off the sync immediately so the user doesn't have to click
        # Sync after adding. If it can't start right now (drive not mounted,
        # syncs disabled, etc.) the autosync loop will retry on the usual
        # backoff, including across daemon restarts.
        try:
            sub = self.api_download_sync(did)
            if sub.get("ok"):
                out["job_id"] = sub.get("job_id")
            else:
                out["queued"] = True
                out["queued_reason"] = sub.get("error")
        except Exception:
            logger.exception("auto-submit on add failed for %s", did)
            out["queued"] = True
        return out

    def api_download_patch(self, did: str, body: Dict[str, Any]) -> Dict[str, Any]:
        # Schedule changes or re-activation should reset autosync backoff and
        # clear the sticky last_sync skip so the loop picks the item up again.
        reset = ("schedule" in body) or (body.get("state") == "active")
        # Project change is the only field that may rewrite local_subpath at
        # patch time. We resolve the slug up front so an invalid id rejects
        # before we mutate state, and we capture the old subpath so the
        # post-update mv() can find it.
        new_project_id = None
        new_proj_slug: Optional[str] = None
        old_subpath = None
        new_subpath = None
        if "project_id" in body:
            new_project_id = (body.get("project_id") or "").strip() or None
            new_proj_slug = self._project_slug_for(new_project_id) if new_project_id else ""
            if new_project_id and not new_proj_slug:
                return {"ok": False, "error": "unknown project_id",
                        "field": "project_id",
                        "fix": "Pick an existing project, choose '— none —', or create a new project first."}
            cur = (self.store.get().get("downloads") or {}).get(did)
            if cur:
                old_subpath = cur.get("local_subpath")
                # Reuse the leaf folder name (i.e. the original label-derived
                # slug) so we don't accidentally rename data on disk just
                # because the user is moving it between projects.
                leaf = os.path.basename((old_subpath or "").rstrip("/")) or did
                new_subpath = ("downloads/" + new_proj_slug + "/" + leaf) if new_proj_slug else ("downloads/" + leaf)
        # Multi-project tag list update. Validate each id; primary
        # (project_id, set above) always heads the list.
        new_project_ids: Optional[List[str]] = None
        promote_primary: Optional[str] = None
        if "project_ids" in body:
            raw_ids = body.get("project_ids") or []
            if not isinstance(raw_ids, list):
                return {"ok": False, "error": "project_ids must be a list",
                        "field": "project_ids"}
            cleaned: List[str] = []
            for x in raw_ids:
                xid = str(x).strip()
                if not xid:
                    continue
                if not self._project_slug_for(xid):
                    return {"ok": False, "error": f"unknown project_id in project_ids: {xid}",
                            "field": "project_ids"}
                if xid not in cleaned:
                    cleaned.append(xid)
            # Decide the primary head ahead of the closure so we don't try
            # to reassign new_project_ids inside _u (which would shadow it).
            cur_dl = (self.store.get().get("downloads") or {}).get(did) or {}
            cur_primary = new_project_id if "project_id" in body else cur_dl.get("project_id")
            if cur_primary and cur_primary in cleaned:
                cleaned = [cur_primary] + [p for p in cleaned if p != cur_primary]
            elif cleaned and not cur_primary:
                # No primary set anywhere — promote the first tag so
                # local_subpath gets a slug nesting on next add/relocate.
                promote_primary = cleaned[0]
            new_project_ids = cleaned
        def _u(d: Dict[str, Any]) -> None:
            if did in d["downloads"]:
                for k, v in body.items():
                    if k in ("label", "state", "schedule", "remote", "remote_path"):
                        d["downloads"][did][k] = v
                if "project_id" in body:
                    d["downloads"][did]["project_id"] = new_project_id
                    if new_subpath is not None:
                        d["downloads"][did]["local_subpath"] = new_subpath
                elif promote_primary:
                    d["downloads"][did]["project_id"] = promote_primary
                if new_project_ids is not None:
                    d["downloads"][did]["project_ids"] = list(new_project_ids)
                if "auto_delete_at" in body:
                    d["downloads"][did]["auto_delete_at"] = self._parse_auto_delete(
                        body.get("auto_delete_at"))
                if reset:
                    d["downloads"][did]["last_sync"] = None
        self.store.update(_u)
        # Best-effort relocate of the existing data folder so the user
        # doesn't have to re-download everything when shuffling projects.
        # Failures (cross-mount move, missing dir, etc.) are logged but
        # never raised — the next sync will recreate the target either way.
        if new_subpath and old_subpath and new_subpath != old_subpath:
            self._relocate_local(did, "downloads", old_subpath, new_subpath)
        if reset:
            self._autosync_reset(did)
        return {"ok": True}

    def api_download_delete(self, did: str, delete_files: bool) -> Dict[str, Any]:
        s = self.store.get()
        item = s["downloads"].get(did)
        if item and delete_files:
            # Re-probe the live mountpoint at delete time so we never wipe
            # the wrong filesystem if the drive was unmounted/remounted at a
            # different path between the UI patch and this call.
            mp = self._live_mountpoint(item["drive_uuid"])
            sub = (item.get("local_subpath") or "").lstrip("/")
            if mp and sub and os.path.ismount(mp) and not IS_MOCK:
                local = os.path.realpath(os.path.join(mp, sub))
                if local.startswith(os.path.realpath(mp) + os.sep):
                    shutil.rmtree(local, ignore_errors=True)
            elif IS_MOCK and mp and sub:
                shutil.rmtree(os.path.join(mp, sub), ignore_errors=True)
        self.store.update(lambda d: d["downloads"].pop(did, None))
        self._autosync_reset(did)
        return {"ok": True}

    def api_download_sync(self, did: str, fresh: bool = False) -> Dict[str, Any]:
        st = (self.store.get().get("settings") or {})
        if not st.get("enabled", True):
            return {"ok": False, "error": "BOT-SYNC master switch is off"}
        if not st.get("downloads_enabled", True):
            return {"ok": False, "error": "Downloads are disabled in Settings"}
        item = self.store.get()["downloads"].get(did)
        if not item:
            return {"ok": False, "error": "not found"}
        drv = self.store.get()["drives"].get(item["drive_uuid"], {})
        if drv.get("paused"):
            return {"ok": False, "error": "drive is paused (resume the drive on the Drives tab)"}
        mp = self._live_mountpoint(item["drive_uuid"])
        if not mp and not IS_MOCK:
            return {"ok": False, "error": "drive not mounted"}
        local = os.path.join(mp or ROOT, item["local_subpath"])
        if fresh and os.path.exists(local):
            shutil.rmtree(local, ignore_errors=True)
        os.makedirs(local, exist_ok=True)

        if IS_MOCK or not item.get("remote"):
            job = self.jobs.submit("download", did, item["label"], runner_mock_sync("download"))
        else:
            extra: List[str] = []
            # --drive-root-folder-id is a Google Drive backend flag; pass it
            # only when the source remote is actually a drive remote, so we
            # don't poison Dropbox/Box/OneDrive copies that happen to have a
            # folder_id field left over from an earlier link parse.
            use_folder_id = bool(item.get("folder_id")) and (item.get("provider") == "drive")
            if use_folder_id:
                extra += ["--drive-root-folder-id", item["folder_id"]]
                # When --drive-root-folder-id is set, that folder *is* the
                # remote root. Appending remote_path here would tell rclone
                # to descend into a subfolder of that name, which fails with
                # "directory not found" if the user typed the linked folder's
                # own name (very common). Anchor at the root.
                src = "{}:".format(item["remote"])
            else:
                src = "{}:{}".format(item["remote"], item.get("remote_path", ""))
            args = ["copy", src, local] + extra
            job = self.jobs.submit("download", did, item["label"], runner_rclone(self.rclone, args, item["label"]))
        return {"ok": True, "job_id": job.id}

    # ---- uploads ----

    def api_upload_list(self) -> Dict[str, Any]:
        return {"uploads": self.store.get()["uploads"]}

    def api_upload_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        primary = next((u for u, d in self.store.get()["drives"].items() if d.get("primary")), None)
        drive_uuid = body.get("drive_uuid") or primary
        if not drive_uuid:
            return {"ok": False, "error": "adopt a drive first"}
        provider = body.get("provider", "drive")
        if provider not in PROVIDERS:
            return {"ok": False, "error": "unknown provider", "field": "provider",
                    "fix": "Pick a provider from the dropdown."}
        if provider == "http":
            return {"ok": False, "error": "HTTP backend is read-only",
                    "field": "provider",
                    "fix": "HTTP can only be used for downloads. Pick Drive / Dropbox / Box / OneDrive / FTP / SFTP for uploads."}
        remote = (body.get("remote") or "").strip()
        if not remote:
            # Auto-pick the default account for this provider so the user
            # doesn't have to fill in the dropdown when there's only one
            # connected account (or one they've explicitly defaulted).
            for n, r in (self.store.get().get("remotes") or {}).items():
                if r.get("provider") == provider and r.get("default"):
                    remote = n
                    break
        if not remote:
            return {"ok": False, "error": "account required",
                    "field": "remote",
                    "fix": "Pick the connected account that should receive these files. Connect one on the Accounts tab if the dropdown is empty."}
        remote_path = (body.get("remote_path") or "").strip()
        if not remote_path:
            return {"ok": False, "error": "remote path required",
                    "field": "remote_path",
                    "fix": ("Set Remote path to the destination folder, e.g. 'ShowUploads/2026'. "
                            "Leaving this blank would write into the root of the account, which is rarely what you want.")}
        label = body.get("label") or "Upload to " + PROVIDERS[provider]["label"]
        sub = re.sub(r"[^A-Za-z0-9 _.\-]", "_", body.get("local_name") or label)[:80]
        uid = secrets.token_hex(6)
        # Optional project nesting: when set, the local staging folder
        # becomes uploads/<provider>/<project-slug>/<sub>. Remote path is
        # left as-is — the project is purely a local-organisation concept.
        project_id = (body.get("project_id") or "").strip() or None
        proj_slug = self._project_slug_for(project_id)
        if project_id and not proj_slug:
            return {"ok": False, "error": "unknown project_id"}
        # Multi-project tagging (v0.7.11). See api_download_add for details.
        raw_ids = body.get("project_ids")
        if isinstance(raw_ids, list):
            extra_ids: List[str] = [str(x).strip() for x in raw_ids if x]
        else:
            extra_ids = []
        for xid in extra_ids:
            if not self._project_slug_for(xid):
                return {"ok": False, "error": f"unknown project_id in project_ids: {xid}",
                        "field": "project_ids"}
        if extra_ids and not project_id:
            project_id = extra_ids[0]
            proj_slug = self._project_slug_for(project_id)
        merged_ids: List[str] = []
        if project_id:
            merged_ids.append(project_id)
        for xid in extra_ids:
            if xid not in merged_ids:
                merged_ids.append(xid)
        local_subpath = ("uploads/{}/{}/{}".format(provider, proj_slug, sub)
                         if proj_slug else "uploads/{}/{}".format(provider, sub))
        auto_delete_at = self._parse_auto_delete(body.get("auto_delete_at"))

        def _add(d: Dict[str, Any]) -> None:
            d["uploads"][uid] = {
                "label": label, "drive_uuid": drive_uuid,
                "local_subpath": local_subpath,
                "provider": provider,
                "remote": remote,
                "remote_path": remote_path,
                "mode": body.get("mode", "push"),
                "project_id": project_id,
                "project_ids": merged_ids,
                "auto_delete_at": auto_delete_at,
                "state": "active", "schedule": body.get("schedule", ""),
                "last_sync": None, "local_size": 0, "remote_size": 0,
            }
        self.store.update(_add)
        # Try to mkdir locally
        s = self.store.get()
        mp = self._live_mountpoint(drive_uuid)
        if mp:
            try: os.makedirs(os.path.join(mp, s["uploads"][uid]["local_subpath"]), exist_ok=True)
            except Exception: pass
        out = {"ok": True, "id": uid}
        # Auto-submit so the first push happens without a manual click.
        # Same retry-on-restart guarantee as downloads.
        try:
            sub = self.api_upload_sync(uid)
            if sub.get("ok"):
                out["job_id"] = sub.get("job_id")
            else:
                out["queued"] = True
                out["queued_reason"] = sub.get("error")
        except Exception:
            logger.exception("auto-submit on add failed for upload %s", uid)
            out["queued"] = True
        return out

    def api_upload_patch(self, uid: str, body: Dict[str, Any]) -> Dict[str, Any]:
        reset = ("schedule" in body) or (body.get("state") == "active")
        new_project_id = None
        new_proj_slug: Optional[str] = None
        old_subpath = None
        new_subpath = None
        if "project_id" in body:
            new_project_id = (body.get("project_id") or "").strip() or None
            new_proj_slug = self._project_slug_for(new_project_id) if new_project_id else ""
            if new_project_id and not new_proj_slug:
                return {"ok": False, "error": "unknown project_id",
                        "field": "project_id",
                        "fix": "Pick an existing project, choose '— none —', or create a new project first."}
            cur = (self.store.get().get("uploads") or {}).get(uid)
            if cur:
                old_subpath = cur.get("local_subpath")
                # Upload subpaths are uploads/<provider>/<slug?>/<leaf>; we
                # preserve the provider segment so the move is purely the
                # project slug being inserted/removed.
                provider = cur.get("provider") or ""
                leaf = os.path.basename((old_subpath or "").rstrip("/")) or uid
                new_subpath = ("uploads/{}/{}/{}".format(provider, new_proj_slug, leaf)
                               if new_proj_slug else "uploads/{}/{}".format(provider, leaf))
        # Multi-project tag list update for uploads (mirrors download path).
        new_project_ids: Optional[List[str]] = None
        promote_primary: Optional[str] = None
        if "project_ids" in body:
            raw_ids = body.get("project_ids") or []
            if not isinstance(raw_ids, list):
                return {"ok": False, "error": "project_ids must be a list",
                        "field": "project_ids"}
            cleaned: List[str] = []
            for x in raw_ids:
                xid = str(x).strip()
                if not xid:
                    continue
                if not self._project_slug_for(xid):
                    return {"ok": False, "error": f"unknown project_id in project_ids: {xid}",
                            "field": "project_ids"}
                if xid not in cleaned:
                    cleaned.append(xid)
            cur_ul = (self.store.get().get("uploads") or {}).get(uid) or {}
            cur_primary = new_project_id if "project_id" in body else cur_ul.get("project_id")
            if cur_primary and cur_primary in cleaned:
                cleaned = [cur_primary] + [p for p in cleaned if p != cur_primary]
            elif cleaned and not cur_primary:
                promote_primary = cleaned[0]
            new_project_ids = cleaned
        def _u(d: Dict[str, Any]) -> None:
            if uid in d["uploads"]:
                for k, v in body.items():
                    if k in ("label", "state", "schedule", "remote", "remote_path", "mode"):
                        d["uploads"][uid][k] = v
                if "project_id" in body:
                    d["uploads"][uid]["project_id"] = new_project_id
                    if new_subpath is not None:
                        d["uploads"][uid]["local_subpath"] = new_subpath
                elif promote_primary:
                    d["uploads"][uid]["project_id"] = promote_primary
                if new_project_ids is not None:
                    d["uploads"][uid]["project_ids"] = list(new_project_ids)
                if "auto_delete_at" in body:
                    d["uploads"][uid]["auto_delete_at"] = self._parse_auto_delete(
                        body.get("auto_delete_at"))
                if reset:
                    d["uploads"][uid]["last_sync"] = None
        self.store.update(_u)
        if new_subpath and old_subpath and new_subpath != old_subpath:
            self._relocate_local(uid, "uploads", old_subpath, new_subpath)
        if reset:
            self._autosync_reset(uid)
        return {"ok": True}

    def api_upload_delete(self, uid: str) -> Dict[str, Any]:
        self.store.update(lambda d: d["uploads"].pop(uid, None))
        self._autosync_reset(uid)
        return {"ok": True}

    def api_upload_sync(self, uid: str) -> Dict[str, Any]:
        st = (self.store.get().get("settings") or {})
        if not st.get("enabled", True):
            return {"ok": False, "error": "BOT-SYNC master switch is off"}
        if not st.get("uploads_enabled", True):
            return {"ok": False, "error": "Uploads are disabled in Settings"}
        item = self.store.get()["uploads"].get(uid)
        if not item:
            return {"ok": False, "error": "not found"}
        drv = self.store.get()["drives"].get(item["drive_uuid"], {})
        if drv.get("paused"):
            return {"ok": False, "error": "drive is paused (resume the drive on the Drives tab)"}
        mp = self._live_mountpoint(item["drive_uuid"])
        if not mp and not IS_MOCK:
            return {"ok": False, "error": "drive not mounted"}
        local = os.path.join(mp or ROOT, item["local_subpath"])
        os.makedirs(local, exist_ok=True)
        if IS_MOCK or not item.get("remote"):
            job = self.jobs.submit("upload", uid, item["label"], runner_mock_sync("upload"))
        else:
            dest = "{}:{}".format(item["remote"], item.get("remote_path", ""))
            verb = {"push": "copy", "mirror": "sync", "bisync": "bisync"}.get(item.get("mode", "push"), "copy")
            args = [verb, local, dest]
            if verb == "sync":
                args += ["--max-delete", "100"]
            if verb == "bisync":
                args += ["--max-delete", "10"]
                # rclone bisync requires --resync on the very first run for a
                # given pair, otherwise it errors out with "cannot find prior
                # listing". Detect first-run via last_sync == None across all
                # providers (Dropbox/Box/OneDrive included).
                if not item.get("last_sync"):
                    args += ["--resync"]
            job = self.jobs.submit("upload", uid, item["label"], runner_rclone(self.rclone, args, item["label"]))
        return {"ok": True, "job_id": job.id}

    # ---- sharing & system ----

    def api_sharing_get(self) -> Dict[str, Any]:
        return self.store.get()["sharing"]

    def api_sharing_patch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        def _u(d: Dict[str, Any]) -> None:
            for k in ("smb", "bonjour", "nfs", "guest_ro", "share_user", "share_pass"):
                if k in body:
                    d["sharing"][k] = body[k]
        self.store.update(_u)
        return apply_sharing(self.store.get())

    def api_jobs(self) -> Dict[str, Any]:
        return {"jobs": self.jobs.list()}

    def api_job_cancel(self, jid: str) -> Dict[str, Any]:
        return {"ok": self.jobs.cancel(jid)}

    # ---- sync log ----

    def api_sync_log(self, jtype: Optional[str] = None, limit: int = SYNC_LOG_MAX) -> Dict[str, Any]:
        sl = (self.store.get().get("sync_log") or {})
        try:
            n = max(1, min(int(limit), SYNC_LOG_MAX))
        except (TypeError, ValueError):
            n = SYNC_LOG_MAX
        if jtype in ("download", "upload"):
            return {"type": jtype, "entries": (sl.get(jtype) or [])[-n:]}
        return {
            "max": SYNC_LOG_MAX,
            "download": (sl.get("download") or [])[-n:],
            "upload": (sl.get("upload") or [])[-n:],
        }

    def api_sync_log_clear(self, jtype: Optional[str]) -> Dict[str, Any]:
        def _clear(d: Dict[str, Any]) -> None:
            sl = d.setdefault("sync_log", {"download": [], "upload": []})
            if jtype in ("download", "upload"):
                sl[jtype] = []
            else:
                sl["download"] = []
                sl["upload"] = []
        self.store.update(_clear)
        return {"ok": True}

    # ---- notifications ----

    def api_notify_list(self) -> Dict[str, Any]:
        # Redact secrets in a copy.
        chs = json.loads(json.dumps(self.notifier.channels()))
        for ch in chs.values():
            cfg = ch.get("config") or {}
            for secret_field in ("password", "auth_header"):
                if cfg.get(secret_field):
                    cfg[secret_field] = "•••"
        return {"channels": chs, "kinds": NOTIFY_CHANNEL_KINDS,
                "event_types": EVENT_TYPES, "severities": SEVERITIES}

    def api_notify_upsert(self, cid: Optional[str], body: Dict[str, Any]) -> Dict[str, Any]:
        # Preserve secrets the UI redacted ("•••") by merging with existing.
        if cid:
            existing = self.notifier.channels().get(cid, {})
            new_cfg = body.get("config") or {}
            old_cfg = existing.get("config") or {}
            for secret_field in ("password", "auth_header"):
                if new_cfg.get(secret_field) in (None, "•••"):
                    if old_cfg.get(secret_field):
                        new_cfg[secret_field] = old_cfg[secret_field]
            body["config"] = new_cfg
            for k in ("kind", "label", "events", "min_severity", "enabled"):
                if k not in body and k in existing:
                    body[k] = existing[k]
        try:
            new_id = self.notifier.upsert_channel(cid, body)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "id": new_id}

    def api_notify_delete(self, cid: str) -> Dict[str, Any]:
        self.notifier.delete_channel(cid)
        return {"ok": True}

    def api_notify_test(self, cid: str) -> Dict[str, Any]:
        return self.notifier.test_channel(cid)

    def api_notify_events(self, tail: int = 100) -> Dict[str, Any]:
        return {"events": self.notifier.events(tail)}

    # ---- settings / setup / password ----

    def api_limits_get(self) -> Dict[str, Any]:
        lim = dict(self.store.get().get("limits", {}) or {})
        # Reflect the live caps in case state and runtime ever diverge.
        lim.update(self.jobs.get_concurrency())
        return {"ok": True, "limits": lim}

    def api_limits_patch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        # Clamp to a sane range; on a router 1-4 is realistic.
        def _clamp(v: Any, default: int = 1) -> int:
            try:
                n = int(v)
            except Exception:
                return default
            return max(1, min(8, n))

        applied: Dict[str, Any] = {}
        if "download_concurrency" in body:
            n = _clamp(body["download_concurrency"])
            self.jobs.set_concurrency("download", n)
            applied["download_concurrency"] = n
        if "upload_concurrency" in body:
            n = _clamp(body["upload_concurrency"])
            self.jobs.set_concurrency("upload", n)
            applied["upload_concurrency"] = n
        if "bw_limit_kbps" in body:
            try:
                applied["bw_limit_kbps"] = max(0, int(body["bw_limit_kbps"]))
            except Exception:
                pass
        if "schedule_window" in body:
            applied["schedule_window"] = str(body["schedule_window"] or "")[:64]

        if applied:
            def _u(d: Dict[str, Any]) -> None:
                lim = d.setdefault("limits", {})
                lim.update(applied)
            self.store.update(_u)
        return {"ok": True, "limits": self.store.get().get("limits", {})}

    # ---- hardware performance preset ----

    def api_performance_get(self) -> Dict[str, Any]:
        st = (self.store.get().get("settings") or {})
        active_name, active_vals = get_active_preset(self.store.get())
        return {
            "ok": True,
            "preset": active_name,
            "presets": HARDWARE_PRESETS,
            "active_values": active_vals,
            "custom": st.get("performance_custom") or {},
            "detected": {
                "ram_mb": _detect_total_ram_mb(),
                "auto_choice": auto_detect_preset(),
            },
            "concurrency": self.jobs.get_concurrency(),
        }

    def api_performance_patch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        new_preset = body.get("preset")
        new_custom = body.get("custom")
        if new_preset is not None and new_preset not in HARDWARE_PRESETS and new_preset != "custom":
            return {"ok": False, "error": "unknown preset",
                    "field": "preset",
                    "fix": "Choose one of: " + ", ".join(list(HARDWARE_PRESETS) + ["custom"])}
        # Sanitise custom overrides — only allow keys we know.
        clean_custom: Optional[Dict[str, Any]] = None
        if new_custom is not None:
            allowed = {"max_global_jobs", "transfers", "checkers",
                       "buffer_size_mb", "multi_thread_streams",
                       "max_backlog", "low_level_retries",
                       "rclone_mem_mb", "bwlimit_kbps", "nice"}
            clean_custom = {}
            for k, v in (new_custom or {}).items():
                if k not in allowed:
                    continue
                try:
                    clean_custom[k] = max(0, int(v))
                except Exception:
                    return {"ok": False, "error": f"{k} must be an integer",
                            "field": k, "fix": "Enter a non-negative number."}

        def _u(d: Dict[str, Any]) -> None:
            st = d.setdefault("settings", {})
            if new_preset is not None:
                st["performance_preset"] = new_preset
            if clean_custom is not None:
                st["performance_custom"] = clean_custom
        self.store.update(_u)
        # Push into JobManager + rclone child caps immediately.
        self._apply_active_preset()
        return self.api_performance_get()

    def api_settings_get(self) -> Dict[str, Any]:
        s = self.store.get()
        st = s.get("settings", {}) or {}
        return {
            "settings": st,
            "providers_all": PROVIDERS,
            "auth_user": s.get("auth", {}).get("user"),
        }

    def api_settings_patch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        def _u(d: Dict[str, Any]) -> None:
            st = d.setdefault("settings", {})
            if "enabled" in body:
                st["enabled"] = bool(body["enabled"])
            if "uploads_enabled" in body:
                st["uploads_enabled"] = bool(body["uploads_enabled"])
            if "downloads_enabled" in body:
                st["downloads_enabled"] = bool(body["downloads_enabled"])
            if "setup_complete" in body:
                st["setup_complete"] = bool(body["setup_complete"])
            if "session_ttl_hours" in body:
                st["session_ttl_hours"] = _clamp_session_ttl_hours(body["session_ttl_hours"])
            if "stuck_job_hours" in body:
                st["stuck_job_hours"] = _clamp_stuck_job_hours(body["stuck_job_hours"])
            if "providers_enabled" in body and isinstance(body["providers_enabled"], dict):
                pe = st.setdefault("providers_enabled", {})
                for k, v in body["providers_enabled"].items():
                    if k in PROVIDERS:
                        pe[k] = bool(v)
        self.store.update(_u)
        return {"ok": True, "settings": self.store.get().get("settings")}

    def api_password_change(self, body: Dict[str, Any]) -> Dict[str, Any]:
        old = body.get("old_password") or ""
        new = body.get("new_password") or ""
        new_user = (body.get("user") or "").strip()
        if len(new) < 6:
            return {"ok": False, "error": "new password must be at least 6 characters"}
        a = self.store.get()["auth"]
        if not a.get("pass_hash") or not verify_password(old, a["pass_hash"], a["pass_salt"]):
            return {"ok": False, "error": "current password incorrect"}
        h, salt = hash_password(new)
        def _u(d: Dict[str, Any]) -> None:
            d["auth"]["pass_hash"] = h
            d["auth"]["pass_salt"] = salt
            if new_user:
                d["auth"]["user"] = new_user
        self.store.update(_u)
        return {"ok": True}

    def api_setup_status(self) -> Dict[str, Any]:
        s = self.store.get()
        st = s.get("settings", {}) or {}
        return {
            "setup_complete": bool(st.get("setup_complete")),
            "has_drive": bool(s.get("drives")),
            "has_remote": bool(s.get("remotes")),
            "has_notification": bool((s.get("notifications", {}) or {}).get("channels")),
            "user": s.get("auth", {}).get("user"),
            "enabled": bool(st.get("enabled", True)),
            "uploads_enabled": bool(st.get("uploads_enabled", True)),
            "downloads_enabled": bool(st.get("downloads_enabled", True)),
        }

    def api_logs(self, tail: int = 200) -> Dict[str, Any]:
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return {"lines": lines[-tail:]}
        except FileNotFoundError:
            return {"lines": []}

    # ---- file explorer (read/move/rename/delete on adopted USB drives) ----
    #
    # Every operation is rooted at an adopted, mounted drive's mountpoint.
    # Paths from the client are treated as relative to that root and
    # resolved with `_safe_resolve`, which rejects any result that escapes
    # the root via `..`, symlinks, or absolute paths. Hidden BOT-SYNC
    # bookkeeping (`.botsync_marker`, `var/run`, `var/cache/.tmp`, etc.) is
    # not specially blocked — power users can sometimes need to clean it up
    # — but the marker file itself cannot be removed (would un-adopt the
    # drive).

    _FILES_PROTECTED_NAMES = {".botsync_marker"}

    def _files_root(self, drive_uuid: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (mountpoint, error). Mountpoint is None on failure."""
        drives = self.store.get().get("drives", {}) or {}
        info = drives.get(drive_uuid)
        if not info:
            return None, "drive not adopted"
        live = {d["uuid"]: d for d in DriveProbe.detect()}.get(drive_uuid)
        mp = (live or {}).get("mountpoint") or info.get("mountpoint")
        if not mp:
            return None, "drive not mounted"
        if not IS_MOCK and not os.path.isdir(mp):
            return None, "drive mountpoint missing"
        return mp, None

    @staticmethod
    def _safe_resolve(root: str, rel: str) -> Optional[str]:
        """Resolve `rel` under `root`. Returns absolute path or None if it
        escapes the root or contains traversal/illegal sequences."""
        if rel is None:
            rel = ""
        rel = str(rel).replace("\\", "/").lstrip("/")
        # Reject NUL and other obvious nonsense.
        if "\x00" in rel:
            return None
        target = os.path.normpath(os.path.join(root, rel))
        # realpath catches symlinks pointing outside the root.
        try:
            real_root = os.path.realpath(root)
            real_target = os.path.realpath(target)
        except OSError:
            return None
        if real_target != real_root and not real_target.startswith(real_root + os.sep):
            return None
        return target

    def api_files_list(self, drive_uuid: str, rel_path: str) -> Dict[str, Any]:
        root, error = self._files_root(drive_uuid)
        if error:
            return {"ok": False, "error": error}
        target = self._safe_resolve(root, rel_path)
        if target is None:
            return {"ok": False, "error": "path escapes drive root"}
        if not os.path.isdir(target):
            return {"ok": False, "error": "not a directory"}
        entries: List[Dict[str, Any]] = []
        try:
            with os.scandir(target) as it:
                for ent in it:
                    try:
                        st = ent.stat(follow_symlinks=False)
                        entries.append({
                            "name": ent.name,
                            "is_dir": ent.is_dir(follow_symlinks=False),
                            "is_symlink": ent.is_symlink(),
                            "size": int(st.st_size),
                            "mtime": float(st.st_mtime),
                            "mode": int(st.st_mode),
                        })
                    except OSError:
                        # unreadable entry — surface name only
                        entries.append({"name": ent.name, "is_dir": False,
                                        "size": 0, "mtime": 0,
                                        "error": "unreadable"})
        except PermissionError:
            return {"ok": False, "error": "permission denied"}
        except OSError as e:
            return {"ok": False, "error": f"i/o error: {e}"}
        entries.sort(key=lambda e: (not e.get("is_dir"), e["name"].lower()))
        # Free space on the underlying filesystem (handy for the UI).
        try:
            usage = shutil.disk_usage(root)
            free = int(usage.free)
            total = int(usage.total)
        except OSError:
            free = total = 0
        rel_norm = os.path.relpath(target, root).replace("\\", "/")
        if rel_norm == ".":
            rel_norm = ""
        return {"ok": True, "drive_uuid": drive_uuid, "path": rel_norm,
                "root": root, "entries": entries,
                "free_bytes": free, "total_bytes": total}

    def api_files_mkdir(self, drive_uuid: str, rel_path: str) -> Dict[str, Any]:
        root, error = self._files_root(drive_uuid)
        if error:
            return {"ok": False, "error": error}
        target = self._safe_resolve(root, rel_path)
        if target is None or os.path.normpath(target) == os.path.normpath(root):
            return {"ok": False, "error": "invalid path"}
        try:
            os.makedirs(target, exist_ok=False)
        except FileExistsError:
            return {"ok": False, "error": "already exists"}
        except OSError as e:
            return {"ok": False, "error": f"i/o error: {e}"}
        return {"ok": True}

    def api_files_delete(self, drive_uuid: str, rel_path: str,
                          recursive: bool = False) -> Dict[str, Any]:
        root, error = self._files_root(drive_uuid)
        if error:
            return {"ok": False, "error": error}
        target = self._safe_resolve(root, rel_path)
        if target is None or os.path.normpath(target) == os.path.normpath(root):
            return {"ok": False, "error": "refusing to delete drive root"}
        name = os.path.basename(target)
        if name in self._FILES_PROTECTED_NAMES:
            return {"ok": False, "error": "this file is protected"}
        try:
            if os.path.islink(target) or not os.path.isdir(target):
                os.unlink(target)
            elif recursive:
                shutil.rmtree(target)
            else:
                os.rmdir(target)
        except FileNotFoundError:
            return {"ok": False, "error": "not found"}
        except OSError as e:
            return {"ok": False, "error": f"i/o error: {e}"}
        return {"ok": True}

    def api_files_rename(self, drive_uuid: str, rel_path: str,
                          new_rel_path: str) -> Dict[str, Any]:
        """Rename / move within the same drive. Cross-drive moves go via the
        UI as download-then-upload (out of scope for v0.6.7)."""
        root, error = self._files_root(drive_uuid)
        if error:
            return {"ok": False, "error": error}
        src = self._safe_resolve(root, rel_path)
        dst = self._safe_resolve(root, new_rel_path)
        if src is None or dst is None:
            return {"ok": False, "error": "invalid path"}
        if os.path.normpath(src) == os.path.normpath(root):
            return {"ok": False, "error": "cannot move drive root"}
        if os.path.basename(src) in self._FILES_PROTECTED_NAMES:
            return {"ok": False, "error": "this file is protected"}
        if os.path.exists(dst):
            return {"ok": False, "error": "destination already exists"}
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            return {"ok": False, "error": f"i/o error: {e}"}
        return {"ok": True}


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"botsyncd/{VERSION}"
    app: App  # set by ThreadingHTTPServer subclass

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    # ---- auth ----
    SESSION_COOKIE = "botsync_session"

    def _get_cookie(self, name: str) -> Optional[str]:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _check_auth(self) -> bool:
        token = self._get_cookie(self.SESSION_COOKIE)
        if self.app.sessions.check(token):
            return True
        # legacy basic auth fallback (so curl/scripts still work)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            a = self.app.store.get()["auth"]
            if user == a.get("user") and a.get("pass_hash") and a.get("pass_salt") \
                    and verify_password(pw, a["pass_hash"], a["pass_salt"]):
                return True
        return False

    def _require_auth(self) -> bool:
        if self._check_auth():
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"error":"authentication required"}')
        return False

    def _do_login(self, body: Dict[str, Any]) -> None:
        user = (body.get("user") or "").strip()
        pw = body.get("password") or ""
        a = self.app.store.get()["auth"]
        ok = (user == a.get("user") and a.get("pass_hash")
              and verify_password(pw, a["pass_hash"], a["pass_salt"]))
        if not ok:
            time.sleep(0.5)
            return self._send_json({"ok": False, "error": "invalid credentials"}, 401)
        token = self.app.sessions.create(user)
        body_out = json.dumps({"ok": True, "user": user}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        # Idle TTL is configurable via settings.session_ttl_hours (default 12h).
        # HttpOnly so JS can't read it; SameSite=Lax to allow form post.
        max_age = self.app.sessions.idle_seconds()
        self.send_header("Set-Cookie",
                         f"{self.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_out)

    def _do_logout(self) -> None:
        token = self._get_cookie(self.SESSION_COOKIE)
        self.app.sessions.revoke(token)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie",
                         f"{self.SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    # ---- response helpers ----
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: str) -> None:
        full = os.path.normpath(os.path.join(UI_DIR, path.lstrip("/")))
        if not full.startswith(UI_DIR) or not os.path.isfile(full):
            self.send_error(404, "not found")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png",
                 ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        if ext == ".html":
            data = data.replace(b"__BOTSYNC_VERSION__", VERSION.encode("utf-8"))
        # Build a weak ETag from mtime + size + version so the browser
        # always re-validates and picks up new builds without manual
        # cache-busting.
        try:
            st = os.stat(full)
            etag = 'W/"' + VERSION + '-' + str(int(st.st_mtime)) + '-' + str(len(data)) + '"'
        except OSError:
            etag = 'W/"' + VERSION + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        # no-cache (not no-store) lets the browser keep a copy but
        # forces a conditional GET on every reload, so a fresh deploy
        # is picked up immediately while the 304 path stays cheap.
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    # JSON request bodies are tiny (config patches, etc); large uploads use
    # /api/files/upload which streams off the socket separately.
    _MAX_JSON_BODY = 1_048_576  # 1 MiB

    def _read_body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        if n > self._MAX_JSON_BODY:
            # Drain a single chunk to keep the socket healthy and bail.
            try: self.rfile.read(min(n, 65536))
            except Exception: pass
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- file explorer binary handlers ----
    #
    # These deliberately bypass _read_body / _send_json because they stream
    # arbitrary binary data. Both still go through _require_auth via the
    # caller in _route -> _dispatch_api.

    _FILE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB hard cap

    @staticmethod
    def _guess_mime(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        return {
            ".txt": "text/plain; charset=utf-8",
            ".log": "text/plain; charset=utf-8",
            ".json": "application/json",
            ".csv": "text/csv",
            ".html": "text/html; charset=utf-8",
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp", ".svg": "image/svg+xml",
            ".mp3": "audio/mpeg", ".wav": "audio/wav",
            ".mp4": "video/mp4", ".m4v": "video/mp4",
            ".mkv": "video/x-matroska", ".webm": "video/webm",
            ".zip": "application/zip", ".tar": "application/x-tar",
            ".gz": "application/gzip",
        }.get(ext, "application/octet-stream")

    def _serve_file_raw(self, drive_uuid: str, rel_path: str) -> None:
        a = self.app
        root, error = a._files_root(drive_uuid)
        if error or root is None:
            return self._send_json({"ok": False, "error": error or "no root"}, 404)
        target = a._safe_resolve(root, rel_path)
        if target is None or not os.path.isfile(target):
            return self._send_json({"ok": False, "error": "not found"}, 404)
        try:
            st = os.stat(target)
        except OSError:
            return self._send_json({"ok": False, "error": "not found"}, 404)
        size = st.st_size
        # Optional Range support — handy for video preview / resume.
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        is_partial = False
        if rng.startswith("bytes="):
            try:
                spec = rng.split("=", 1)[1].split(",", 1)[0].strip()
                a_str, _, b_str = spec.partition("-")
                if a_str:
                    start = int(a_str)
                    end = int(b_str) if b_str else size - 1
                else:
                    n = int(b_str)
                    start = max(0, size - n)
                    end = size - 1
                if start > end or start >= size:
                    raise ValueError
                is_partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = end - start + 1
        ctype = self._guess_mime(target)
        self.send_response(206 if is_partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if is_partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # Quote-safe filename for the Content-Disposition header.
        fname = os.path.basename(target).replace('"', "_")
        dl = self.headers.get("X-Disposition") or \
            ("inline" if ctype.startswith(("image/", "text/", "video/", "audio/")) else "attachment")
        try:
            ascii_name = fname.encode("ascii").decode("ascii")
            self.send_header("Content-Disposition",
                             f'{dl}; filename="{ascii_name}"')
        except UnicodeEncodeError:
            self.send_header(
                "Content-Disposition",
                f"{dl}; filename*=UTF-8''" + urllib.parse.quote(fname))
        self.send_header("Cache-Control", "private, max-age=0, must-revalidate")
        self.end_headers()
        try:
            with open(target, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_file_upload(self) -> None:
        """Streaming upload endpoint.

        Headers:
          X-Drive : adopted drive UUID (required)
          X-Path  : relative target path *including* the new filename (required)
          X-Overwrite : "1" to allow overwriting an existing file
          Content-Length : size in bytes (required)

        Body: raw file bytes. (Multipart is intentionally not supported —
        the UI builds the headers manually so we don't need a parser.)
        """
        a = self.app
        drive_uuid = self.headers.get("X-Drive", "")
        rel_path = self.headers.get("X-Path", "")
        overwrite = self.headers.get("X-Overwrite", "0") in ("1", "true", "yes")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json({"ok": False, "error": "Content-Length required"}, 400)
        if length > self._FILE_UPLOAD_MAX_BYTES:
            return self._send_json({"ok": False, "error": "file too large"}, 413)
        if not drive_uuid or not rel_path:
            return self._send_json({"ok": False,
                                    "error": "X-Drive and X-Path headers required"}, 400)
        root, error = a._files_root(drive_uuid)
        if error or root is None:
            return self._send_json({"ok": False, "error": error or "no root"}, 400)
        target = a._safe_resolve(root, rel_path)
        if target is None:
            return self._send_json({"ok": False, "error": "invalid path"}, 400)
        if os.path.isdir(target):
            return self._send_json({"ok": False,
                                    "error": "destination is a directory"}, 400)
        if os.path.exists(target) and not overwrite:
            return self._send_json({"ok": False, "error": "already exists"}, 409)
        # Ensure free space.
        try:
            free = shutil.disk_usage(root).free
            if length > free - (16 * 1024 * 1024):
                return self._send_json({"ok": False,
                                        "error": "not enough free space"}, 507)
        except OSError:
            pass
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".upload-" + secrets.token_hex(4)
        remaining = length
        try:
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining != 0:
                raise IOError(f"short upload: {remaining} bytes missing")
            os.replace(tmp, target)
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return self._send_json({"ok": False, "error": f"upload failed: {e}"}, 500)
        return self._send_json({"ok": True,
                                "path": os.path.relpath(target, root).replace("\\", "/"),
                                "size": length})

    # ---- routing ----
    PUBLIC_API = {
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/whoami"),
        ("GET", "/api/setup/status"),
        ("POST", "/api/watchdog/ping"),
    }

    def _route(self, method: str) -> None:
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)

        # static
        if method == "GET":
            if path in ("/", "/index.html"):
                if not self._check_auth():
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers(); return
                self._send_static("index.html"); return
            if path == "/login" or path == "/login.html":
                self._send_static("login.html"); return
            if path.startswith("/ui/"):
                # static assets needed for both login and main app
                self._send_static(path[4:]); return
            if path == "/healthz":
                self._send_json({"ok": True, "version": VERSION}); return

        if not path.startswith("/api/"):
            self.send_error(404); return

        # auth gate (skip for public endpoints)
        if (method, path) not in self.PUBLIC_API and not self._require_auth():
            return

        try:
            self._dispatch_api(method, path, qs)
        except Exception as e:
            logger.exception("api error: %s %s", method, path)
            # Sanitise the error string before exposing it to the network —
            # rclone errors can include filesystem paths, OAuth tokens, etc.
            # The full traceback is in the daemon log; only mock/debug builds
            # surface it in the HTTP response.
            err = _sanitise_error(str(e))
            payload: Dict[str, Any] = {"ok": False, "error": err}
            if IS_MOCK or IS_DEBUG:
                payload["trace"] = traceback.format_exc()
            self._send_json(payload, 500)

    def _dispatch_api(self, method: str, path: str, qs: Dict[str, List[str]]) -> None:
        a = self.app
        # File upload streams its body straight off the socket; do NOT pre-read it.
        if path == "/api/files/upload" and method == "POST":
            return self._handle_file_upload()
        body: Dict[str, Any] = self._read_body() if method in ("POST", "PATCH", "PUT", "DELETE") else {}

        # ---- auth ----
        if path == "/api/auth/login" and method == "POST":
            return self._do_login(body)
        if path == "/api/auth/logout" and method == "POST":
            return self._do_logout()
        if path == "/api/auth/whoami" and method == "GET":
            token = self._get_cookie(self.SESSION_COOKIE)
            user = self.app.sessions.check(token)
            return self._send_json({"ok": bool(user), "user": user})
        if path == "/api/auth/password" and method == "POST":
            return self._send_json(a.api_password_change(body))

        # ---- settings / setup ----
        if path == "/api/setup/status" and method == "GET":
            return self._send_json(a.api_setup_status())
        if path == "/api/settings" and method == "GET":
            return self._send_json(a.api_settings_get())
        if path == "/api/settings" and method == "PATCH":
            return self._send_json(a.api_settings_patch(body))
        if path == "/api/limits" and method == "GET":
            return self._send_json(a.api_limits_get())
        if path == "/api/limits" and method == "PATCH":
            return self._send_json(a.api_limits_patch(body))
        if path == "/api/performance" and method == "GET":
            return self._send_json(a.api_performance_get())
        if path == "/api/performance" and method == "PATCH":
            return self._send_json(a.api_performance_patch(body))

        # ---- top-level ----
        if path == "/api/state" and method == "GET":
            return self._send_json(a.api_state())
        if path == "/api/system" and method == "GET":
            info = system_info()
            try:
                info["reliability"] = a.reliability_info()
            except Exception:
                pass
            return self._send_json(info)
        if path == "/api/watchdog/ping" and method == "POST":
            # Public endpoint hit by the local cron watchdog every minute.
            # Bind is typically 0.0.0.0; the only thing this can do is bump a
            # timestamp, so leaving it unauthenticated is acceptable.
            client_ip = self.client_address[0] if self.client_address else ""
            a.record_watchdog_ping()
            return self._send_json({"ok": True, "ts": time.time(),
                                    "version": VERSION, "client": client_ip})
        if path == "/api/jobs" and method == "GET":
            return self._send_json(a.api_jobs())
        if path == "/api/logs" and method == "GET":
            tail = int((qs.get("tail") or ["200"])[0])
            return self._send_json(a.api_logs(tail))

        # ---- drives ----
        if path == "/api/drives" and method == "GET":
            return self._send_json(a.api_drives())
        if path == "/api/drives/adopt" and method == "POST":
            return self._send_json(a.api_drive_adopt(body))
        m = re.match(r"^/api/drives/([^/]+)/(mount|eject|primary|forget|pause|resume)$", path)
        if m and method == "POST":
            return self._send_json(a.api_drive_action(m.group(1), m.group(2)))

        # ---- remotes ----
        if path == "/api/remotes" and method == "GET":
            return self._send_json(a.api_remotes())
        if path == "/api/remotes/oauth/start" and method == "POST":
            return self._send_json(a.api_remote_oauth_start(body))
        if path == "/api/remotes/oauth/finish" and method == "POST":
            return self._send_json(a.api_remote_oauth_finish(body))
        if path == "/api/remotes/oauth/device/start" and method == "POST":
            return self._send_json(a.api_remote_oauth_device_start(body))
        if path == "/api/remotes/oauth/device/poll" and method == "POST":
            return self._send_json(a.api_remote_oauth_device_poll(body))
        if path == "/api/remotes/basic" and method == "POST":
            return self._send_json(a.api_remote_basic_create(body))
        m = re.match(r"^/api/remotes/([^/]+)$", path)
        if m and method == "DELETE":
            return self._send_json(a.api_remote_delete(m.group(1)))
        m = re.match(r"^/api/remotes/([^/]+)/check$", path)
        if m and method == "POST":
            return self._send_json(a.api_remote_check(m.group(1)))
        m = re.match(r"^/api/remotes/([^/]+)/default$", path)
        if m and method == "POST":
            return self._send_json(a.api_remote_set_default(m.group(1)))

        # ---- rclone version / self-update ----
        if path == "/api/system/rclone" and method == "GET":
            return self._send_json(a.api_rclone_status())
        if path == "/api/system/rclone/check" and method == "POST":
            return self._send_json(a.api_rclone_check())
        if path == "/api/system/rclone/update" and method == "POST":
            return self._send_json(a.api_rclone_update(body))

        # ---- projects ----
        if path == "/api/projects" and method == "GET":
            return self._send_json(a.api_project_list())
        if path == "/api/projects" and method == "POST":
            return self._send_json(a.api_project_add(body))
        m = re.match(r"^/api/projects/([^/]+)$", path)
        if m and method == "PATCH":
            return self._send_json(a.api_project_patch(m.group(1), body))
        if m and method == "DELETE":
            return self._send_json(a.api_project_delete(m.group(1)))
        m = re.match(r"^/api/projects/([^/]+)/sync$", path)
        if m and method == "POST":
            return self._send_json(a.api_project_sync_now(m.group(1)))

        # ---- downloads ----
        if path == "/api/downloads" and method == "GET":
            return self._send_json(a.api_download_list())
        if path == "/api/downloads" and method == "POST":
            return self._send_json(a.api_download_add(body))
        m = re.match(r"^/api/downloads/([^/]+)$", path)
        if m and method == "PATCH":
            return self._send_json(a.api_download_patch(m.group(1), body))
        if m and method == "DELETE":
            df = (qs.get("delete_files") or ["0"])[0] in ("1", "true", "yes")
            return self._send_json(a.api_download_delete(m.group(1), df))
        m = re.match(r"^/api/downloads/([^/]+)/(sync|resync)$", path)
        if m and method == "POST":
            return self._send_json(a.api_download_sync(m.group(1), fresh=(m.group(2) == "resync")))

        # ---- uploads ----
        if path == "/api/uploads" and method == "GET":
            return self._send_json(a.api_upload_list())
        if path == "/api/uploads" and method == "POST":
            return self._send_json(a.api_upload_add(body))
        m = re.match(r"^/api/uploads/([^/]+)$", path)
        if m and method == "PATCH":
            return self._send_json(a.api_upload_patch(m.group(1), body))
        if m and method == "DELETE":
            return self._send_json(a.api_upload_delete(m.group(1)))
        m = re.match(r"^/api/uploads/([^/]+)/sync$", path)
        if m and method == "POST":
            return self._send_json(a.api_upload_sync(m.group(1)))

        # ---- sharing ----
        if path == "/api/sharing" and method == "GET":
            return self._send_json(a.api_sharing_get())
        if path == "/api/sharing" and method == "PATCH":
            return self._send_json(a.api_sharing_patch(body))

        # ---- jobs ----
        m = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
        if m and method == "POST":
            return self._send_json(a.api_job_cancel(m.group(1)))

        # ---- sync log ----
        if path == "/api/sync-log" and method == "GET":
            jt = (qs.get("type") or [None])[0]
            limit = int((qs.get("limit") or [str(SYNC_LOG_MAX)])[0])
            return self._send_json(a.api_sync_log(jt, limit))
        if path == "/api/sync-log/clear" and method == "POST":
            return self._send_json(a.api_sync_log_clear((body or {}).get("type")))

        # ---- file explorer ----
        if path == "/api/files" and method == "GET":
            uid = (qs.get("drive") or [""])[0]
            rel = (qs.get("path") or [""])[0]
            return self._send_json(a.api_files_list(uid, rel))
        if path == "/api/files/mkdir" and method == "POST":
            return self._send_json(a.api_files_mkdir(
                (body or {}).get("drive", ""), (body or {}).get("path", "")))
        if path == "/api/files/rename" and method == "POST":
            return self._send_json(a.api_files_rename(
                (body or {}).get("drive", ""), (body or {}).get("path", ""),
                (body or {}).get("new_path", "")))
        if path == "/api/files" and method == "DELETE":
            uid = (body or {}).get("drive") or (qs.get("drive") or [""])[0]
            rel = (body or {}).get("path") or (qs.get("path") or [""])[0]
            recursive = bool((body or {}).get("recursive")) or \
                (qs.get("recursive") or ["0"])[0] in ("1", "true", "yes")
            return self._send_json(a.api_files_delete(uid, rel, recursive))
        if path == "/api/files/raw" and method == "GET":
            uid = (qs.get("drive") or [""])[0]
            rel = (qs.get("path") or [""])[0]
            return self._serve_file_raw(uid, rel)

        # ---- notifications ----
        if path == "/api/notifications/channels" and method == "GET":
            return self._send_json(a.api_notify_list())
        if path == "/api/notifications/channels" and method == "POST":
            return self._send_json(a.api_notify_upsert(None, body))
        m = re.match(r"^/api/notifications/channels/([^/]+)$", path)
        if m and method == "PATCH":
            return self._send_json(a.api_notify_upsert(m.group(1), body))
        if m and method == "DELETE":
            return self._send_json(a.api_notify_delete(m.group(1)))
        m = re.match(r"^/api/notifications/channels/([^/]+)/test$", path)
        if m and method == "POST":
            return self._send_json(a.api_notify_test(m.group(1)))
        if path == "/api/notifications/events" and method == "GET":
            tail = int((qs.get("tail") or ["100"])[0])
            return self._send_json(a.api_notify_events(tail))
        if path == "/api/notifications/health" and method == "GET":
            return self._send_json(a.api_health_get())
        if path == "/api/notifications/health" and method == "PATCH":
            return self._send_json(a.api_health_patch(body))

        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_GET(self) -> None: self._route("GET")
    def do_POST(self) -> None: self._route("POST")
    def do_PATCH(self) -> None: self._route("PATCH")
    def do_PUT(self) -> None: self._route("PUT")
    def do_DELETE(self) -> None: self._route("DELETE")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    for d in (ROOT, ETC_DIR, VAR_DIR, LOG_DIR, CRASH_LOG_DIR, RUN_DIR, CACHE_DIR, TMP_DIR, BIN_DIR,
              DOWNLOADS_DIR, UPLOADS_DIR,
              os.path.join(UPLOADS_DIR, "drive"),
              os.path.join(UPLOADS_DIR, "dropbox"),
              os.path.join(UPLOADS_DIR, "box"),
              os.path.join(UPLOADS_DIR, "onedrive")):
        os.makedirs(d, exist_ok=True)


def safety_check() -> None:
    if IS_MOCK:
        return
    # Windows has no statvfs/mountpoint concept — and cross-platform installs
    # explicitly opt-in via BOTSYNC_ALLOW_ROOTFS — so honour that early.
    if IS_WINDOWS or os.environ.get("BOTSYNC_ALLOW_ROOTFS", "").lower() in ("1", "true", "yes"):
        return
    # Refuse to run if ROOT is inside rootfs.
    try:
        st = os.statvfs(ROOT)
    except Exception as e:
        logger.error("statvfs(%s) failed: %s", ROOT, e); sys.exit(2)
    if not os.path.ismount(ROOT) and ROOT != "/":
        logger.error("BOTSYNC_ROOT=%s is not a mountpoint; refusing to run "
                     "(set BOTSYNC_ALLOW_ROOTFS=1 to override)", ROOT)
        sys.exit(2)


def main() -> None:
    ensure_dirs()
    setup_logging()
    safety_check()
    logger.info("botsyncd %s starting (root=%s, mock=%s)", VERSION, ROOT, IS_MOCK)
    _harden_self_for_oom()
    _ensure_swap_if_needed()

    app = App()
    ensure_auth_seeded(app.store)
    Handler.app = app  # type: ignore

    # Last-ditch crash logger so the procd respawn cycle is debuggable.
    def _excepthook(etype, evalue, etb):
        try:
            os.makedirs(CRASH_LOG_DIR, exist_ok=True)
            ts = int(time.time())
            crash = os.path.join(CRASH_LOG_DIR, f"crash-{ts}.log")
            with open(crash, "w", encoding="utf-8") as fh:
                fh.write(f"botsyncd {VERSION} fatal at {ts}\n\n")
                traceback.print_exception(etype, evalue, etb, file=fh)
            logger.error("fatal exception logged to %s", crash)
        except Exception:
            pass
        try:
            app.notifier.emit("system.error",
                              f"fatal: {etype.__name__}: {evalue}",
                              traceback="".join(traceback.format_exception(etype, evalue, etb))[-2000:])
        except Exception:
            pass
        # Hand off to default printer so procd captures it in syslog too.
        sys.__excepthook__(etype, evalue, etb)
    sys.excepthook = _excepthook

    bind = os.environ.get("BOTSYNC_BIND", "0.0.0.0")
    port = int(os.environ.get("BOTSYNC_PORT", "8585"))
    httpd = ThreadingHTTPServer((bind, port), Handler)
    logger.info("listening on %s:%d", bind, port)

    def _graceful(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        try:
            app.shutdown(reason=f"signal {signum}")
        finally:
            try: httpd.shutdown()
            except Exception: pass
    if not IS_WINDOWS:
        try:
            import signal as _sig
            _sig.signal(_sig.SIGTERM, _graceful)
            _sig.signal(_sig.SIGINT, _graceful)
        except Exception:
            logger.exception("could not install signal handlers")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down (keyboard interrupt)")
    finally:
        app.shutdown(reason="main exit")
        try: httpd.shutdown()
        except Exception: pass


if __name__ == "__main__":
    main()
