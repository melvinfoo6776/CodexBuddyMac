#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DISCOVERY_REQUEST = b"CODEXBUDDY_DISCOVER_V1"
DISCOVERY_REPLY_PREFIX = "CODEXBUDDY_USAGE_V1 "
CLAUDE_USAGE_FILE = Path(__file__).with_name("claude_usage.json")

# Live Claude usage (Pro/Max OAuth). Same metric Claude Code shows in /usage.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CREDENTIALS_FILE = Path("~/.claude/.credentials.json")

# Claude Code OAuth refresh. Access tokens are short-lived (~1h); when expired
# the usage endpoint returns 401. Claude Code refreshes via this endpoint using
# the stored refresh token; we do the same so the bridge can self-heal instead
# of going dark until the user next opens Claude Code. The refreshed credentials
# are written back to the SAME Keychain item (single source of truth) so Claude
# Code stays in sync; everything other than the three claudeAiOauth token fields
# (notably the mcpOAuth blob) is preserved untouched.
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Refresh a little before actual expiry so a poll never races the deadline.
CLAUDE_TOKEN_REFRESH_SKEW_SECONDS = 120.0
# Don't hammer the token endpoint if refresh keeps failing.
CLAUDE_REFRESH_BACKOFF_SECONDS = 300.0
_claude_refresh_lock = threading.Lock()
_claude_refresh_retry_after: float = 0.0
# A refresh token rejected with `invalid_grant` is permanently dead (revoked,
# rotated away, or expired); waiting cannot revive it, so the user must sign in
# again. Remember the dead token so we stop calling the endpoint with it and can
# tell the UI to prompt for re-authentication until a different credential
# appears. State self-heals once a new credential is stored.
_claude_reauth_required = False
_claude_dead_refresh_token: str | None = None

# The /claude/refresh endpoint changes state (rotates the token, writes the
# Keychain). Gate it behind a loopback token so a stray local process or a
# browser hitting 127.0.0.1 cannot trigger it, and rate-limit successful manual
# refreshes so it cannot be spammed into churning token rotations.
BRIDGE_AUTH_HEADER = "X-CodexBuddy-Token"
MANUAL_REFRESH_MIN_INTERVAL_SECONDS = 30.0
_manual_refresh_lock = threading.Lock()
_last_manual_refresh_at: float = 0.0

# Runtime identity and durable state. The build ID is calculated while the
# module loads, so replacing the script on disk does not make an older running
# process claim that it already loaded the new code.
try:
    BRIDGE_BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
except OSError:
    BRIDGE_BUILD_ID = "unknown"
BRIDGE_INSTANCE_ID = secrets.token_hex(12)
BRIDGE_STARTED_AT = time.time()
CLAUDE_RETRY_STATE_FILE = Path(__file__).with_name("bridge_state.json")
BRIDGE_LOCK_FILE = Path(__file__).with_name(".bridge.lock")
BRIDGE_PID_FILE = Path(__file__).with_name("bridge.pid")
MAX_PERSISTED_RETRY_SECONDS = 24 * 60 * 60
BRIDGE_VERSION = "1.1"


def load_or_create_auth_token(path: Path) -> str:
    """Read the loopback auth token, creating it with 0600 perms if missing."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token

# The OAuth usage endpoint is sensitive to request rate and will return 429 if
# polled too often (the ESP, the statusline script, and Claude Code all hit it).
# Cache the last live result in-process so we call upstream at most once per TTL
# regardless of how often clients poll, and back off after a failure.
CLAUDE_LIVE_TTL_SECONDS = 300.0
CLAUDE_LIVE_BACKOFF_SECONDS = 300.0
_claude_live_lock = threading.Lock()
_claude_live_cache: dict[str, Any] | None = None
_claude_live_fetched_at: float = 0.0
_claude_live_retry_after: float = 0.0


def acquire_bridge_lock(path: Path):
    """Acquire the single-instance bridge lock and keep its file handle open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def write_runtime_pid(path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(os.getpid()), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_claude_retry_deadline(path: Path, now: float | None = None) -> float:
    """Restore a valid wall-clock 429 deadline, ignoring stale/corrupt state."""
    global _claude_live_retry_after
    current = time.time() if now is None else now
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        deadline = float(payload["claude_retry_after_epoch"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        deadline = 0.0
    if deadline <= current or deadline > current + MAX_PERSISTED_RETRY_SECONDS:
        deadline = 0.0
    _claude_live_retry_after = deadline
    return deadline


def persist_claude_retry_deadline(path: Path, deadline: float) -> None:
    """Atomically persist a wall-clock 429 deadline for the next bridge process."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        if deadline <= 0:
            path.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            return
        tmp.write_text(
            json.dumps({"claude_retry_after_epoch": deadline}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


class DiscoveryResponder(threading.Thread):
    def __init__(self, host: str, http_port: int, discovery_port: int, advertise_host: str | None) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.http_port = http_port
        self.discovery_port = discovery_port
        self.advertise_host = advertise_host

    def run(self) -> None:
        bind_host = self.host if self.host not in ("", "0.0.0.0", "::") else "0.0.0.0"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, self.discovery_port))
            while True:
                data, addr = sock.recvfrom(1024)
                if data.strip() != DISCOVERY_REQUEST:
                    continue

                host = self.advertise_host or local_address_for(addr[0])
                url = f"http://{host}:{self.http_port}/usage.json"
                reply = (DISCOVERY_REPLY_PREFIX + url).encode("utf-8")
                for _ in range(3):
                    sock.sendto(reply, addr)
                    time.sleep(0.03)


def local_address_for(remote_host: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((remote_host, 9))
            return sock.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())


def trusted_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def find_value(data: Any, names: tuple[str, ...]) -> str | None:
    if isinstance(data, dict):
        for name in names:
            value = data.get(name)
            if isinstance(value, str) and value:
                return value
        for value in data.values():
            found = find_value(value, names)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_value(value, names)
            if found:
                return found
    return None


def load_codex_auth(auth_file: Path) -> tuple[str, str | None]:
    data = json.loads(auth_file.expanduser().read_text(encoding="utf-8"))
    access_token = find_value(data, ("access_token", "accessToken"))
    account_id = (
        os.environ.get("CHATGPT_ACCOUNT_ID")
        or find_value(data, ("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId"))
    )
    if not access_token:
        raise RuntimeError(f"No Codex OAuth access token found in {auth_file}")
    return access_token, account_id


def iso_from_epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_datetime_label(dt: datetime) -> str:
    local = dt.astimezone()
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{local.day}/{local.month} {hour}:{local.minute:02d}{suffix}"


def reset_in_label(reset_at: Any, now: datetime) -> str:
    reset_dt = parse_datetime(reset_at)
    if not reset_dt:
        return "Resets unknown"

    seconds = int((reset_dt.astimezone(timezone.utc) - now).total_seconds())
    if seconds <= 0:
        return "Resets unknown"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days > 0:
        return f"Resets In {days}d {hours}h"
    if hours > 0:
        return f"Resets In {hours}h {minutes}m"
    return f"Resets In {minutes}m"


def convert_window(window: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(window, dict):
        return {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""}

    used = round(float(window.get("used_percent", 0)))
    used = max(0, min(100, used))
    return {
        "used": used,
        "limit": 100,
        "remaining": 100 - used,
        "reset_at": iso_from_epoch(window.get("reset_at")),
    }


def convert_percent_window(window: dict[str, Any] | None, reset_key: str = "reset_at") -> dict[str, Any]:
    if not isinstance(window, dict):
        return {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""}

    if "used" in window and "remaining" in window:
        used = int(round(float(window.get("used", -1))))
        remaining = int(round(float(window.get("remaining", -1))))
    else:
        used = int(round(float(window.get("used_percent", window.get("used_percentage", -1)))))
        remaining = 100 - used if used >= 0 else -1

    if used >= 0:
        used = max(0, min(100, used))
    if remaining >= 0:
        remaining = max(0, min(100, remaining))

    reset_at = window.get(reset_key) or window.get("reset_at") or window.get("resets_at")
    if isinstance(reset_at, (int, float)):
        reset_at = iso_from_epoch(reset_at)
    elif not isinstance(reset_at, str):
        reset_at = ""

    return {
        "used": used,
        "limit": int(window.get("limit", 100) or 100),
        "remaining": remaining,
        "reset_at": reset_at,
    }


def expire_passed_window(window: dict[str, Any], now_dt: datetime) -> None:
    """If a cached window's reset time has already passed, treat it as reset.

    Cached/fallback data can outlive its window (e.g. the host sleeps through a
    reset). In that case the stored ``used`` percentage is stale: the real window
    has rolled over to ~0. Zero it out rather than report the old value, and clear
    the now-meaningless reset_at.
    """
    if not isinstance(window, dict):
        return
    reset_dt = parse_datetime(window.get("reset_at"))
    if reset_dt and reset_dt.astimezone(timezone.utc) <= now_dt:
        limit = int(window.get("limit", 100) or 100)
        window["used"] = 0
        window["remaining"] = limit
        window["reset_at"] = ""


def load_claude_usage(claude_file: Path) -> dict[str, Any] | None:
    data = json.loads(claude_file.expanduser().read_text(encoding="utf-8"))
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    if isinstance(data.get("claude"), dict):
        claude = dict(data["claude"])
    elif isinstance(data.get("rate_limits"), dict):
        rate_limits = data["rate_limits"]
        claude = {
            "five_hour": convert_percent_window(rate_limits.get("five_hour"), "resets_at"),
            "weekly": convert_percent_window(rate_limits.get("seven_day"), "resets_at"),
        }
    else:
        claude = {
            "five_hour": convert_percent_window(data.get("five_hour")),
            "weekly": convert_percent_window(data.get("weekly") or data.get("seven_day")),
        }

    claude["updated_at"] = str(data.get("updated_at") or now)
    claude["source"] = str(data.get("source") or claude_file)
    for key in ("five_hour", "weekly", "seven_day"):
        expire_passed_window(claude.get(key), now_dt)
    return claude


def _read_keychain_credentials() -> tuple[str, dict[str, Any]] | None:
    """Return (raw_json, parsed) for the Claude Code Keychain item, or None."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    raw = result.stdout.strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return raw, parsed


def _keychain_account() -> str | None:
    """The account (-a) the Keychain item was created with; needed to update it."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('"acct"') and '="' in line:
            return line.split('="', 1)[1].rstrip('"')
    return None


def claude_token_status() -> dict[str, Any]:
    """Report Claude token validity for the Settings UI (no secrets returned)."""
    global _claude_reauth_required, _claude_dead_refresh_token
    creds = _read_keychain_credentials()
    if creds is None:
        return {"present": False, "valid": False, "can_refresh": False,
                "message": "No Claude credential found in Keychain"}
    oauth = creds[1].get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return {"present": False, "valid": False, "can_refresh": False,
                "message": "No Claude access token in credential"}
    # A newly stored credential (different refresh token) clears a prior
    # invalid_grant, so re-auth state self-heals after `claude auth login`.
    if (_claude_dead_refresh_token is not None
            and oauth.get("refreshToken") != _claude_dead_refresh_token):
        _claude_reauth_required = False
        _claude_dead_refresh_token = None
    can_refresh = bool(oauth.get("refreshToken"))
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)):
        seconds = (expires_at - time.time() * 1000) / 1000
        valid = seconds > 0
        if valid:
            _claude_reauth_required = False
            _claude_dead_refresh_token = None
        return {"present": True, "valid": valid, "can_refresh": can_refresh,
                "reauth_required": _claude_reauth_required,
                "expires_in_seconds": int(seconds),
                "refresh_in_seconds": int(seconds - CLAUDE_TOKEN_REFRESH_SKEW_SECONDS),
                "message": "Valid" if valid else "Expired"}
    return {"present": True, "valid": True, "can_refresh": can_refresh,
            "reauth_required": False,
            "message": "Valid (no expiry recorded)"}


def refresh_claude_token(expected_access_token: str | None = None) -> dict[str, Any]:
    """Refresh the Claude access token using the stored refresh token.

    Preserves the entire credential and updates only the three token fields under
    ``claudeAiOauth``. Backs the original up in memory and restores it if the
    post-write read-back doesn't verify, so a bad write can't corrupt the shared
    item (which also holds the mcpOAuth plugin tokens).
    """
    global _claude_refresh_retry_after, _claude_reauth_required, _claude_dead_refresh_token

    with _claude_refresh_lock:
        now = time.monotonic()
        if now < _claude_refresh_retry_after:
            return {"ok": False, "error": "Refresh backing off after a recent failure"}

        creds = _read_keychain_credentials()
        if creds is None:
            return {"ok": False, "error": "No Claude credential in Keychain"}
        raw, data = creds
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
            return {"ok": False, "error": "No refresh token available"}
        # A token already rejected with invalid_grant won't come back to life;
        # skip the network call and ask for re-auth until a new credential is
        # stored (detected by a different refresh token).
        if (_claude_dead_refresh_token is not None
                and oauth["refreshToken"] == _claude_dead_refresh_token):
            return {"ok": False, "reauth_required": True,
                    "error": "Claude session expired. Run `claude auth login` to sign in again."}
        if expected_access_token is not None and oauth.get("accessToken") != expected_access_token:
            return {"ok": True, "message": "Claude token already refreshed by another request",
                    "expires_in_seconds": None, "rotated_refresh_token": False}
        account = _keychain_account()
        if not account:
            return {"ok": False, "error": "Could not determine Keychain account"}

        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": CLAUDE_OAUTH_CLIENT_ID,
        }).encode("utf-8")
        request = Request(
            CLAUDE_OAUTH_TOKEN_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "codex-cli"},
        )
        try:
            with urlopen(request, timeout=15, context=trusted_ssl_context()) as response:
                token_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except OSError:
                pass
            if exc.code == 400 and "invalid_grant" in detail:
                # Refresh token is permanently dead. Don't start a transient
                # backoff (waiting can't fix it); remember the dead token and
                # flag re-auth so the next refresh is skipped until sign-in.
                _claude_reauth_required = True
                _claude_dead_refresh_token = oauth["refreshToken"]
                return {"ok": False, "reauth_required": True,
                        "error": "Claude session expired. Run `claude auth login` to sign in again."}
            _claude_refresh_retry_after = now + CLAUDE_REFRESH_BACKOFF_SECONDS
            return {"ok": False, "error": f"Refresh failed: HTTP {exc.code} {detail}".strip()}
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            _claude_refresh_retry_after = now + CLAUDE_REFRESH_BACKOFF_SECONDS
            return {"ok": False, "error": f"Refresh failed: {exc}"}

        new_access = token_data.get("access_token")
        if not new_access:
            _claude_refresh_retry_after = now + CLAUDE_REFRESH_BACKOFF_SECONDS
            return {"ok": False, "error": "Refresh response missing access_token"}
        new_refresh = token_data.get("refresh_token") or oauth["refreshToken"]
        expires_in = token_data.get("expires_in")

        updated = copy.deepcopy(data)
        updated["claudeAiOauth"]["accessToken"] = new_access
        updated["claudeAiOauth"]["refreshToken"] = new_refresh
        if isinstance(expires_in, (int, float)):
            updated["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000 + expires_in * 1000)
        new_raw = json.dumps(updated)

        write = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", account,
             "-s", CLAUDE_KEYCHAIN_SERVICE, "-w", new_raw],
            capture_output=True, text=True, timeout=10,
        )
        if write.returncode != 0:
            return {"ok": False, "error": f"Keychain write failed: {write.stderr.strip()}"}

        # Verify the write: access token updated and the rest of the structure
        # (especially the mcpOAuth plugin tokens) is intact. Otherwise restore.
        check = _read_keychain_credentials()
        original_mcp = set((data.get("mcpOAuth") or {}).keys())
        verified = (
            check is not None
            and isinstance(check[1].get("claudeAiOauth"), dict)
            and check[1]["claudeAiOauth"].get("accessToken") == new_access
            and set((check[1].get("mcpOAuth") or {}).keys()) == original_mcp
        )
        if not verified:
            subprocess.run(
                ["security", "add-generic-password", "-U", "-a", account,
                 "-s", CLAUDE_KEYCHAIN_SERVICE, "-w", raw],
                capture_output=True, text=True, timeout=10,
            )
            return {"ok": False, "error": "Write verification failed; restored original credential"}

        _claude_refresh_retry_after = 0.0
        _claude_reauth_required = False
        _claude_dead_refresh_token = None

    return {"ok": True, "message": "Claude token refreshed",
            "expires_in_seconds": int(expires_in) if isinstance(expires_in, (int, float)) else None,
            "rotated_refresh_token": new_refresh != oauth["refreshToken"]}


def load_claude_oauth_token() -> str | None:
    """Resolve the Claude Code OAuth token: env override, macOS Keychain, then file.

    When the Keychain access token is expired (or about to expire), refresh it in
    place first so callers never use a dead token. This is the "mandatory check"
    that keeps live usage from silently going to 401.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token

    creds = _read_keychain_credentials()
    if creds is not None:
        oauth = creds[1].get("claudeAiOauth")
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            expires_at = oauth.get("expiresAt")
            expired = (
                isinstance(expires_at, (int, float))
                and expires_at <= (time.time() + CLAUDE_TOKEN_REFRESH_SKEW_SECONDS) * 1000
            )
            if expired and oauth.get("refreshToken"):
                result = refresh_claude_token()
                if result.get("ok"):
                    refreshed = _read_keychain_credentials()
                    if refreshed is not None:
                        new_oauth = refreshed[1].get("claudeAiOauth")
                        if isinstance(new_oauth, dict) and new_oauth.get("accessToken"):
                            return new_oauth["accessToken"]
                # Refresh failed: fall through and try the (possibly stale) token
                # anyway; the caller handles the resulting 401 with backoff.
            return oauth["accessToken"]

    # Legacy flat structures / file fallback.
    creds = creds[1] if creds else None
    if creds:
        token = find_value(creds, ("accessToken", "access_token"))
        if token:
            return token

    try:
        data = json.loads(CLAUDE_CREDENTIALS_FILE.expanduser().read_text(encoding="utf-8"))
        token = find_value(data, ("accessToken", "access_token"))
        if token:
            return token
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    return None


def convert_utilization_window(window: dict[str, Any] | None) -> dict[str, Any]:
    """Map the OAuth usage shape ({utilization, resets_at}) to the bridge window shape."""
    if not isinstance(window, dict):
        return {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""}

    try:
        used = round(float(window.get("utilization", -1)))
    except (TypeError, ValueError):
        used = -1
    if used >= 0:
        used = max(0, min(100, used))

    reset_at = window.get("resets_at") or window.get("reset_at") or ""
    if isinstance(reset_at, (int, float)):
        reset_at = iso_from_epoch(reset_at)
    elif not isinstance(reset_at, str):
        reset_at = ""

    return {
        "used": used,
        "limit": 100,
        "remaining": 100 - used if used >= 0 else -1,
        "reset_at": reset_at,
    }


def fetch_claude_usage(token: str) -> dict[str, Any]:
    """Live Claude Pro/Max usage via the undocumented OAuth usage endpoint."""
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    request = Request(CLAUDE_USAGE_URL, headers=headers)
    with urlopen(request, timeout=10, context=trusted_ssl_context()) as response:
        raw = response.read()

    upstream = json.loads(raw.decode("utf-8"))
    now_dt = datetime.now(timezone.utc)
    claude = {
        "five_hour": convert_utilization_window(upstream.get("five_hour")),
        "weekly": convert_utilization_window(upstream.get("seven_day")),
        "updated_at": now_dt.isoformat(),
        "source": CLAUDE_USAGE_URL,
    }
    for key in ("five_hour", "weekly"):
        reset_dt = parse_datetime(claude[key].get("reset_at"))
        if reset_dt and reset_dt.astimezone(timezone.utc) <= now_dt:
            claude[key]["reset_at"] = ""
    return claude


def _format_backoff_eta(seconds: float) -> str:
    """Human label for a backoff duration, e.g. '~46m' or '~1h 5m'."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        hours, minutes = divmod(seconds // 60, 60)
        return f"~{hours}h {minutes}m"
    if seconds >= 60:
        return f"~{seconds // 60}m"
    return f"~{seconds}s"


def _retry_after_seconds(exc: HTTPError, default: float) -> float:
    """Parse the Retry-After header (delta-seconds) off a 429/503; else return default.

    The OAuth usage endpoint returns Retry-After as an integer number of seconds
    (observed ~2775s). We only handle the numeric form; an HTTP-date falls back to
    the default. Honoring this is what stops the premature retries that otherwise
    keep re-tripping the rate limit.
    """
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw:
        try:
            return max(0.0, float(int(str(raw).strip())))
        except (TypeError, ValueError):
            pass
    return default


def _persist_claude_usage(path: Path, claude: dict[str, Any]) -> None:
    """Atomically write the latest live Claude usage to disk.

    Why: the disk file is the cross-restart fallback. Without this, a server
    restart drops the in-process cache and the next 429 falls back to the
    bundled placeholder of zeros. Persisting on every successful live fetch
    means the fallback is always the last known live values.
    """
    payload = {
        "updated_at": claude.get("updated_at"),
        "source": claude.get("source"),
        "claude": {key: claude[key] for key in claude if key not in ("updated_at", "source", "warning")},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _handle_claude_http_error(
    exc: HTTPError,
    now: float,
    retry_state_file: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a fallback result for a failed Claude usage request.

    The caller holds ``_claude_live_lock``. Authentication failures are not
    rate-limit failures, so a 401 must never create a Retry-After countdown.
    """
    global _claude_live_retry_after

    if exc.code == 401:
        return None, "Claude login expired (HTTP 401); refresh required"

    backoff = _retry_after_seconds(exc, CLAUDE_LIVE_BACKOFF_SECONDS)
    _claude_live_retry_after = now + backoff
    if exc.code == 429 and retry_state_file is not None:
        persist_claude_retry_deadline(retry_state_file, _claude_live_retry_after)
    eta = _format_backoff_eta(backoff)
    reason = "rate-limited" if exc.code == 429 else f"unavailable (HTTP {exc.code})"
    if _claude_live_cache is not None:
        stale = dict(_claude_live_cache)
        now_dt = datetime.now(timezone.utc)
        for key in ("five_hour", "weekly", "seven_day"):
            expire_passed_window(stale.get(key), now_dt)
        stale["warning"] = f"Live Claude usage {reason}; showing last known live values (retry in {eta})"
        return stale, None
    return None, f"Live Claude usage {reason}; retry in {eta}"


def get_live_claude_usage(
    claude_file: Path | None = None,
    retry_state_file: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Live Claude usage with an in-process TTL cache and failure backoff.

    Returns (claude, warning). When a usable value is available (freshly fetched,
    still within TTL, or a last-known-good value served during a backoff) the
    dict is returned and the caller should use it. When nothing live is available
    (no token, or a failure with no prior good value) the dict is None and the
    caller falls back to the cached file. This decouples upstream request rate
    from client poll rate, which is what avoids the 429s.
    """
    global _claude_live_cache, _claude_live_fetched_at, _claude_live_retry_after

    token = load_claude_oauth_token()
    if not token:
        return None, "Claude OAuth token not found; using cached file"

    monotonic_now = time.monotonic()
    epoch_now = time.time()
    with _claude_live_lock:
        # Serve the cached live value while it is still fresh.
        if _claude_live_cache is not None and monotonic_now - _claude_live_fetched_at < CLAUDE_LIVE_TTL_SECONDS:
            return dict(_claude_live_cache), None

        # In a backoff window after a recent failure: do NOT call upstream again.
        # Retrying inside the Retry-After window just renews the 429 and the limit
        # never clears. This must hold even with no in-process cache (e.g. right
        # after a restart) -- otherwise every timer refresh re-trips the limit.
        if epoch_now < _claude_live_retry_after:
            eta = _format_backoff_eta(_claude_live_retry_after - epoch_now)
            if _claude_live_cache is not None:
                stale = dict(_claude_live_cache)
                now_dt = datetime.now(timezone.utc)
                for key in ("five_hour", "weekly", "seven_day"):
                    expire_passed_window(stale.get(key), now_dt)
                stale["warning"] = f"Live Claude usage rate-limited; showing last known live values (retry in {eta})"
                return stale, None
            # No cached live value: fall back to the on-disk file (persisted
            # last-known-good) without touching the rate-limited endpoint.
            return None, f"Live Claude usage rate-limited; retry in {eta}"

        try:
            data = fetch_claude_usage(token)
            _claude_live_cache = data
            _claude_live_fetched_at = monotonic_now
            _claude_live_retry_after = 0.0
            if retry_state_file is not None:
                persist_claude_retry_deadline(retry_state_file, 0.0)
            if claude_file is not None:
                _persist_claude_usage(claude_file, data)
            return dict(data), None
        except HTTPError as exc:
            if exc.code != 401:
                return _handle_claude_http_error(exc, epoch_now, retry_state_file)
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            _claude_live_retry_after = epoch_now + CLAUDE_LIVE_BACKOFF_SECONDS
            if _claude_live_cache is not None:
                stale = dict(_claude_live_cache)
                now_dt = datetime.now(timezone.utc)
                for key in ("five_hour", "weekly", "seven_day"):
                    expire_passed_window(stale.get(key), now_dt)
                stale["warning"] = f"Live Claude usage unavailable ({exc}); showing last known live values"
                return stale, None
            return None, f"Live Claude usage unavailable: {exc}"

    # Refresh outside _claude_live_lock. Manual refresh takes the refresh lock
    # before clearing live state, so refreshing while holding the live lock would
    # invert the lock order and could deadlock.
    refresh = refresh_claude_token(expected_access_token=token)
    if not refresh.get("ok"):
        detail = str(refresh.get("error") or "unknown refresh error")
        return None, f"Claude login expired (HTTP 401); {detail}"

    refreshed_token = load_claude_oauth_token()
    if not refreshed_token:
        return None, "Claude login expired (HTTP 401); refreshed token unavailable"

    retry_monotonic_now = time.monotonic()
    retry_epoch_now = time.time()
    with _claude_live_lock:
        # Another request may have completed the refresh and usage fetch while
        # this request was waiting for the refresh lock.
        if (
            _claude_live_cache is not None
            and retry_monotonic_now - _claude_live_fetched_at < CLAUDE_LIVE_TTL_SECONDS
        ):
            return dict(_claude_live_cache), None

        try:
            data = fetch_claude_usage(refreshed_token)
        except HTTPError as exc:
            return _handle_claude_http_error(exc, retry_epoch_now, retry_state_file)
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            _claude_live_retry_after = retry_epoch_now + CLAUDE_LIVE_BACKOFF_SECONDS
            return None, f"Live Claude usage unavailable after login refresh: {exc}"

        _claude_live_cache = data
        _claude_live_fetched_at = retry_monotonic_now
        _claude_live_retry_after = 0.0
        if retry_state_file is not None:
            persist_claude_retry_deadline(retry_state_file, 0.0)
        if claude_file is not None:
            _persist_claude_usage(claude_file, data)
        return dict(data), None


def attach_claude_usage(
    payload: dict[str, Any],
    claude_file: Path,
    enabled: bool,
    live: bool = True,
    retry_state_file: Path | None = None,
) -> None:
    if not enabled:
        return

    live_warning: str | None = None
    if live:
        claude, live_warning = get_live_claude_usage(claude_file, retry_state_file)
        if claude is not None:
            payload["claude"] = claude
            return

    try:
        claude = load_claude_usage(claude_file)
    except FileNotFoundError:
        payload.setdefault("claude", {
            "five_hour": {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""},
            "weekly": {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""},
            "warning": live_warning or f"Claude usage file not found: {claude_file}",
        })
        return
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        payload.setdefault("claude", {
            "five_hour": {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""},
            "weekly": {"used": -1, "limit": 100, "remaining": -1, "reset_at": ""},
            "warning": live_warning or f"Claude usage unavailable: {exc}",
        })
        return

    if claude:
        if live_warning:
            claude.setdefault("warning", live_warning)
        payload["claude"] = claude


def fetch_codex_usage(auth_file: Path) -> dict[str, Any]:
    access_token, account_id = load_codex_auth(auth_file)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    request = Request(CODEX_USAGE_URL, headers=headers)
    with urlopen(request, timeout=10, context=trusted_ssl_context()) as response:
        raw = response.read()

    upstream = json.loads(raw.decode("utf-8"))
    rate_limit = upstream.get("rate_limit") or {}
    primary = rate_limit.get("primary_window")
    secondary = rate_limit.get("secondary_window")
    now = datetime.now(timezone.utc).isoformat()

    return {
        "plan": str(upstream.get("plan_type", "unknown")).title(),
        "updated_at": now,
        "codex": {
            "five_hour": convert_window(primary),
            "weekly": convert_window(secondary),
        },
        "source": CODEX_USAGE_URL,
        "note": "Live Codex OAuth usage. Values are percentages because OpenAI returns used_percent.",
    }


def add_display_labels(payload: dict[str, Any], prefix: str, now: datetime) -> None:
    label = ""
    if prefix != "UPDATED":
        label = "FALLBACK"
    payload["display"] = {
        "status_label": label,
    }
    for service_name in ("codex", "claude"):
        windows = payload.get(service_name)
        if not isinstance(windows, dict):
            continue
        for key in ("five_hour", "weekly", "seven_day"):
            window = windows.get(key)
            if isinstance(window, dict):
                window["reset_in"] = reset_in_label(window.get("reset_at"), now)


class UsageHandler(BaseHTTPRequestHandler):
    usage_file: Path
    auth_file: Path
    claude_file: Path
    live: bool
    live_claude: bool
    claude: bool
    auth_token: str | None = None
    retry_state_file: Path

    def do_GET(self) -> None:
        if self.path == "/health":
            if not self._authorized():
                self.write_json(403, {"status": "unauthorized"})
                return
            self.write_json(200, {
                "status": "ok",
                "version": BRIDGE_VERSION,
                "build_id": BRIDGE_BUILD_ID,
                "pid": os.getpid(),
                "instance_id": BRIDGE_INSTANCE_ID,
                "started_at": BRIDGE_STARTED_AT,
            })
            return
        if self.path == "/claude/status":
            if not self._authorized():
                self.write_json(403, {"error": "Unauthorized"})
                return
            self.write_json(200, claude_token_status())
            return
        if self.path not in ("/", "/usage.json"):
            self.send_error(404)
            return

        try:
            status_prefix = "UPDATED"
            now = datetime.now(timezone.utc)
            if self.live:
                payload = fetch_codex_usage(self.auth_file)
            else:
                payload = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError, OSError) as exc:
            try:
                payload = json.loads(self.usage_file.read_text(encoding="utf-8"))
                payload["source"] = str(self.usage_file)
                payload["warning"] = f"Live Codex usage unavailable: {exc}"
                status_prefix = "FAILED"
                now = datetime.now(timezone.utc)
                codex_windows = payload.get("codex")
                if isinstance(codex_windows, dict):
                    for key in ("five_hour", "weekly", "seven_day"):
                        expire_passed_window(codex_windows.get(key), now)
            except Exception as fallback_exc:
                self.write_json(500, {"error": str(exc), "fallback_error": str(fallback_exc)})
                return
        except Exception as exc:
            self.write_json(500, {"error": str(exc)})
            return

        attach_claude_usage(
            payload,
            self.claude_file,
            self.claude,
            self.live_claude,
            self.retry_state_file,
        )
        add_display_labels(payload, status_prefix, now)
        payload["served_at"] = now.isoformat()
        self.write_json(200, payload)

    def do_POST(self) -> None:
        if self.path == "/claude/refresh":
            if not self._authorized():
                self.write_json(403, {"ok": False, "error": "Unauthorized"})
                return
            if not self._allow_manual_refresh():
                self.write_json(429, {"ok": False,
                                      "error": "Refresh requested too frequently; try again shortly"})
                return
            result = refresh_claude_token()
            self.write_json(200 if result.get("ok") else 502, result)
            return
        self.send_error(404)

    def _authorized(self) -> bool:
        """Constant-time check of the loopback token. Open if none is configured."""
        token = self.auth_token
        if not token:
            return True
        provided = self.headers.get(BRIDGE_AUTH_HEADER)
        return bool(provided) and secrets.compare_digest(provided, token)

    def _allow_manual_refresh(self) -> bool:
        global _last_manual_refresh_at
        with _manual_refresh_lock:
            now = time.monotonic()
            if now - _last_manual_refresh_at < MANUAL_REFRESH_MIN_INTERVAL_SECONDS:
                return False
            _last_manual_refresh_at = now
            return True

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        try:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        except Exception as exc:
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
            return
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Codex usage JSON for a LilyGO T-Display S3.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--file", default=str(Path(__file__).with_name("usage.json")))
    parser.add_argument("--auth-file", default="~/.codex/auth.json")
    parser.add_argument("--claude-file", default=str(CLAUDE_USAGE_FILE), help="Sanitized Claude usage JSON written by bridge/claude_statusline_usage.py.")
    parser.add_argument("--manual", action="store_true", help="Only serve --file; do not call Codex live usage.")
    parser.add_argument("--no-claude", action="store_true", help="Do not attach Claude usage from --claude-file.")
    parser.add_argument("--no-live-claude", action="store_true", help="Do not call Claude OAuth usage; only attach --claude-file.")
    parser.add_argument("--discovery-port", type=int, default=8788)
    parser.add_argument("--advertise-host", help="Host/IP to return to discovery clients. Defaults to the LAN IP used to reach each client.")
    parser.add_argument("--no-discovery", action="store_true", help="Disable UDP bridge discovery.")
    args = parser.parse_args()

    UsageHandler.usage_file = Path(args.file)
    UsageHandler.auth_file = Path(args.auth_file).expanduser()
    UsageHandler.claude_file = Path(args.claude_file).expanduser()
    UsageHandler.live = not args.manual
    UsageHandler.live_claude = UsageHandler.live and not args.no_live_claude
    UsageHandler.claude = not args.no_claude
    UsageHandler.retry_state_file = CLAUDE_RETRY_STATE_FILE
    # Loopback token gating the state-changing /claude/refresh endpoint. Stored
    # next to the script (the installed Bridge dir) so the app can read it.
    UsageHandler.auth_token = load_or_create_auth_token(Path(__file__).with_name(".bridge_token"))
    bridge_lock = acquire_bridge_lock(BRIDGE_LOCK_FILE)
    if bridge_lock is None:
        print("Another CodexBuddy bridge process already owns the runtime lock", file=sys.stderr)
        raise SystemExit(73)
    load_claude_retry_deadline(UsageHandler.retry_state_file)
    try:
        server = ThreadingHTTPServer((args.host, args.port), UsageHandler)
    except OSError:
        bridge_lock.close()
        raise
    write_runtime_pid(BRIDGE_PID_FILE)
    if not args.no_discovery:
        DiscoveryResponder(args.host, args.port, args.discovery_port, args.advertise_host).start()
    mode = "manual JSON" if UsageHandler.live is False else f"live Codex OAuth via {UsageHandler.auth_file}"
    print(f"Serving {mode} at http://{args.host}:{args.port}/usage.json")
    if not args.no_discovery:
        advertised = args.advertise_host or "auto LAN IP"
        print(f"Discovery on UDP {args.discovery_port}, advertising {advertised}:{args.port}")
    if UsageHandler.live:
        print(f"Falls back to {UsageHandler.usage_file} if live usage is unavailable")
    if UsageHandler.claude:
        if UsageHandler.live_claude:
            print(f"Attaching live Claude usage via {CLAUDE_USAGE_URL}, falling back to {UsageHandler.claude_file}")
            # Mandatory launch check: renew an expired token up front so live
            # usage works immediately instead of erroring until the first poll.
            status = claude_token_status()
            if status.get("present") and not status.get("valid") and status.get("can_refresh"):
                print("Claude token expired at launch; refreshing...")
                result = refresh_claude_token()
                print("Claude token refresh:", result.get("message") or result.get("error"))
            else:
                print(f"Claude token status at launch: {status.get('message')}")
        else:
            print(f"Attaching Claude usage from {UsageHandler.claude_file}")
    server.serve_forever()


if __name__ == "__main__":
    main()
