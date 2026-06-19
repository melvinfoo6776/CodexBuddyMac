#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
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


def load_claude_oauth_token() -> str | None:
    """Resolve the Claude Code OAuth token: env override, macOS Keychain, then file."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            token = find_value(json.loads(result.stdout), ("accessToken", "access_token"))
            if token:
                return token
    except (OSError, json.JSONDecodeError, ValueError):
        pass

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


def get_live_claude_usage() -> tuple[dict[str, Any] | None, str | None]:
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

    now = time.monotonic()
    with _claude_live_lock:
        # Serve the cached live value while it is still fresh.
        if _claude_live_cache is not None and now - _claude_live_fetched_at < CLAUDE_LIVE_TTL_SECONDS:
            return dict(_claude_live_cache), None

        # In a backoff window after a recent failure: serve last-known-good live
        # rather than hammering the endpoint (and rather than the stale file).
        if now < _claude_live_retry_after and _claude_live_cache is not None:
            stale = dict(_claude_live_cache)
            now_dt = datetime.now(timezone.utc)
            for key in ("five_hour", "weekly", "seven_day"):
                expire_passed_window(stale.get(key), now_dt)
            eta = _format_backoff_eta(_claude_live_retry_after - now)
            stale["warning"] = f"Live Claude usage rate-limited; showing last known live values (retry in {eta})"
            return stale, None

        try:
            data = fetch_claude_usage(token)
            _claude_live_cache = data
            _claude_live_fetched_at = now
            _claude_live_retry_after = 0.0
            return dict(data), None
        except HTTPError as exc:
            # Honor the server's Retry-After (the OAuth usage endpoint returns a
            # long one, e.g. ~46m). Retrying earlier just re-trips the limit.
            backoff = _retry_after_seconds(exc, CLAUDE_LIVE_BACKOFF_SECONDS)
            _claude_live_retry_after = now + backoff
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
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            _claude_live_retry_after = now + CLAUDE_LIVE_BACKOFF_SECONDS
            if _claude_live_cache is not None:
                stale = dict(_claude_live_cache)
                now_dt = datetime.now(timezone.utc)
                for key in ("five_hour", "weekly", "seven_day"):
                    expire_passed_window(stale.get(key), now_dt)
                stale["warning"] = f"Live Claude usage unavailable ({exc}); showing last known live values"
                return stale, None
            return None, f"Live Claude usage unavailable: {exc}"


def attach_claude_usage(payload: dict[str, Any], claude_file: Path, enabled: bool, live: bool = True) -> None:
    if not enabled:
        return

    live_warning: str | None = None
    if live:
        claude, live_warning = get_live_claude_usage()
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

    def do_GET(self) -> None:
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

        attach_claude_usage(payload, self.claude_file, self.claude, self.live_claude)
        add_display_labels(payload, status_prefix, now)
        payload["served_at"] = now.isoformat()
        self.write_json(200, payload)

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
    server = ThreadingHTTPServer((args.host, args.port), UsageHandler)
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
        else:
            print(f"Attaching Claude usage from {UsageHandler.claude_file}")
    server.serve_forever()


if __name__ == "__main__":
    main()
