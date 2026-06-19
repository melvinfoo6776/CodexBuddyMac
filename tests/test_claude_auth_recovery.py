import importlib.util
import copy
import json
import os
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "CodexBuddyMac"
    / "CodexBuddyMac"
    / "Bridge"
    / "codex_usage_server.py"
)


def load_bridge(name: str):
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ClaudeAuthRecoveryTests(unittest.TestCase):
    def test_uses_current_claude_cli_refresh_endpoint(self):
        bridge = load_bridge("bridge_endpoint_test")
        self.assertEqual(
            bridge.CLAUDE_OAUTH_TOKEN_URL,
            "https://platform.claude.com/v1/oauth/token",
        )

    def test_401_refreshes_and_retries_usage_once(self):
        bridge = load_bridge("bridge_401_recovery_test")
        tokens = ["expired-token", "refreshed-token"]
        refresh_calls = []
        usage_calls = []

        bridge.load_claude_oauth_token = lambda: tokens.pop(0)

        def refresh(expected_access_token=None):
            refresh_calls.append(expected_access_token)
            return {"ok": True}

        def fetch(token):
            usage_calls.append(token)
            if token == "expired-token":
                raise HTTPError(bridge.CLAUDE_USAGE_URL, 401, "Unauthorized", Message(), None)
            return {
                "five_hour": {"used": 12, "limit": 100, "remaining": 88, "reset_at": ""},
                "weekly": {"used": 34, "limit": 100, "remaining": 66, "reset_at": ""},
                "updated_at": "2026-06-19T00:00:00+00:00",
                "source": "test",
            }

        bridge.refresh_claude_token = refresh
        bridge.fetch_claude_usage = fetch
        cache = Path(tempfile.mkdtemp()) / "claude.json"

        result, warning = bridge.get_live_claude_usage(cache)

        self.assertIsNone(warning)
        self.assertEqual(result["five_hour"]["used"], 12)
        self.assertEqual(refresh_calls, ["expired-token"])
        self.assertEqual(usage_calls, ["expired-token", "refreshed-token"])

    def test_failed_refresh_does_not_retry_usage_or_create_rate_backoff(self):
        bridge = load_bridge("bridge_401_failure_test")
        usage_calls = []
        bridge.load_claude_oauth_token = lambda: "expired-token"
        bridge.refresh_claude_token = lambda expected_access_token=None: {
            "ok": False,
            "error": "refresh rejected",
        }

        def fetch(token):
            usage_calls.append(token)
            raise HTTPError(bridge.CLAUDE_USAGE_URL, 401, "Unauthorized", Message(), None)

        bridge.fetch_claude_usage = fetch
        result, warning = bridge.get_live_claude_usage()

        self.assertIsNone(result)
        self.assertIn("refresh rejected", warning)
        self.assertEqual(usage_calls, ["expired-token"])
        self.assertEqual(bridge._claude_live_retry_after, 0.0)

    def test_token_refresh_preserves_usage_rate_limit_deadline(self):
        bridge = load_bridge("bridge_preserve_usage_backoff_test")
        credentials = {
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 0,
            },
            "mcpOAuth": {"example": {"token": "unchanged"}},
        }
        state = {"credentials": credentials}

        def read_credentials():
            value = copy.deepcopy(state["credentials"])
            return json.dumps(value), value

        class TokenResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                }).encode()

        class ProcessResult:
            returncode = 0
            stderr = ""

        def run_process(arguments, **kwargs):
            if "add-generic-password" in arguments:
                state["credentials"] = json.loads(arguments[-1])
            return ProcessResult()

        bridge._read_keychain_credentials = read_credentials
        bridge._keychain_account = lambda: "test-account"
        bridge.urlopen = lambda *args, **kwargs: TokenResponse()
        bridge.subprocess.run = run_process
        bridge._claude_live_retry_after = 12345.0

        result = bridge.refresh_claude_token()

        self.assertTrue(result["ok"])
        self.assertEqual(bridge._claude_live_retry_after, 12345.0)

    def test_429_deadline_survives_bridge_module_restart(self):
        first = load_bridge("bridge_persist_429_first")
        directory = Path(tempfile.mkdtemp())
        state_file = directory / "bridge_state.json"
        usage_file = directory / "claude.json"
        headers = Message()
        headers["Retry-After"] = "600"
        first.load_claude_oauth_token = lambda: "token"
        first.fetch_claude_usage = lambda token: (_ for _ in ()).throw(
            HTTPError(first.CLAUDE_USAGE_URL, 429, "Too Many Requests", headers, None)
        )

        result, warning = first.get_live_claude_usage(usage_file, state_file)

        self.assertIsNone(result)
        self.assertIn("rate-limited", warning)
        self.assertTrue(state_file.exists())

        second = load_bridge("bridge_persist_429_second")
        calls = []
        second.load_claude_oauth_token = lambda: "token"
        second.fetch_claude_usage = lambda token: calls.append(token)
        deadline = second.load_claude_retry_deadline(state_file)
        result, warning = second.get_live_claude_usage(usage_file, state_file)

        self.assertGreater(deadline, time.time())
        self.assertIsNone(result)
        self.assertIn("rate-limited", warning)
        self.assertEqual(calls, [])

    def test_bridge_lock_rejects_second_owner(self):
        bridge = load_bridge("bridge_lock_test")
        lock_file = Path(tempfile.mkdtemp()) / ".bridge.lock"
        first = bridge.acquire_bridge_lock(lock_file)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(bridge.acquire_bridge_lock(lock_file))
            self.assertEqual(oct(lock_file.stat().st_mode & 0o777), "0o600")
        finally:
            first.close()

    def test_control_auth_uses_configured_loopback_token(self):
        bridge = load_bridge("bridge_control_auth_test")
        handler = object.__new__(bridge.UsageHandler)
        handler.auth_token = "expected-token"
        handler.headers = Message()
        self.assertFalse(handler._authorized())
        handler.headers[bridge.BRIDGE_AUTH_HEADER] = "wrong-token"
        self.assertFalse(handler._authorized())
        handler.headers.replace_header(bridge.BRIDGE_AUTH_HEADER, "expected-token")
        self.assertTrue(handler._authorized())

    def test_bridge_auth_token_file_is_private(self):
        bridge = load_bridge("bridge_auth_permissions_test")
        token_file = Path(tempfile.mkdtemp()) / ".bridge_token"
        token = bridge.load_or_create_auth_token(token_file)
        self.assertTrue(token)
        self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)

    def test_token_status_reports_proactive_refresh_timing(self):
        bridge = load_bridge("bridge_token_refresh_timing_test")
        expires_at = (time.time() + 3600) * 1000
        credentials = {
            "claudeAiOauth": {
                "accessToken": "access",
                "refreshToken": "refresh",
                "expiresAt": expires_at,
            }
        }
        bridge._read_keychain_credentials = lambda: (json.dumps(credentials), credentials)

        status = bridge.claude_token_status()

        self.assertTrue(status["valid"])
        self.assertGreaterEqual(status["expires_in_seconds"], 3598)
        expected_refresh = 3600 - int(bridge.CLAUDE_TOKEN_REFRESH_SKEW_SECONDS)
        self.assertGreaterEqual(status["refresh_in_seconds"], expected_refresh - 2)
        self.assertLessEqual(status["refresh_in_seconds"], expected_refresh)


if __name__ == "__main__":
    unittest.main()
