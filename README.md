# CodexBuddyMac

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

CodexBuddyMac is a macOS menu bar app that displays Codex and Claude Code usage.
It runs a local Python bridge, polls the providers' usage endpoints, and keeps the
latest snapshot on the Mac.

<p align="center">
  <img src="docs/images/codexbuddy-menu.png?v=3" width="336" alt="CodexBuddyMac menu bar popover showing Codex weekly usage and Claude usage with reset times">
</p>

See both services at a glance without interrupting your workflow. The compact
menu shows each provider's currently available usage windows, remaining
capacity, connection status, and one-click refresh controls.

## Highlights

- Codex and Claude Code usage in one menu bar view
- Provider usage windows, including weekly-only Codex plans
- Next provider usage reset time and combined capacity battery indicator
- Local-only bridge bound to `127.0.0.1`
- Automatic refresh with offline fallback
- Automatic Claude OAuth token refresh (self-healing when the login expires)
- Settings panel with bridge controls, diagnostics, and a Claude login refresh
- Optional launch at login

## Latest Release

Version 1.1.2 detects Codex plans that provide a weekly-only usage window. It
shows that live weekly usage without a misleading five-hour bar, while Claude
continues to show its available five-hour and weekly windows.

See [CHANGELOG.md](CHANGELOG.md) for recent fixes and changes.

Possible longer-term improvements, including a native Swift 2.0 architecture,
are tracked in [Future Considerations](docs/FUTURE_CONSIDERATIONS.md). These are
ideas, not committed release plans.

## Requirements

- macOS 26.5 or later
- Xcode 26.5 or later
- Python 3 from Xcode Command Line Tools
- Codex CLI signed in for Codex usage
- Claude Code signed in for Claude usage

## Download And Run

1. Download the repository ZIP from GitHub or clone the repository.
2. Open `CodexBuddyMac/CodexBuddyMac.xcodeproj` in Xcode.
3. Replace `com.example.CodexBuddyMac` with a bundle identifier registered to
   your Apple Developer account.
4. Replace `group.com.example.CodexBuddyMac` in the app, widget, entitlements,
   and `UsageStore.swift` files with your registered App Group.
5. Select your development team and run the `CodexBuddyMac` scheme.

Sign in to the command-line tools when required:

```bash
codex login
claude auth login
```

The app starts its bundled bridge at `http://127.0.0.1:8789/usage.json`.

## Build A Local Release Candidate

Run `./scripts/build-release.sh`. It creates the app, ZIP archive, and SHA-256
checksum under `Dist/Release`, then verifies the app again after extracting the
ZIP. The generated app is ad-hoc signed for local testing. Public distribution
still requires a Developer ID Application certificate and Apple notarization.

## Settings

Open Settings from the menu to:

- Start, restart, and refresh the local bridge.
- Check the Codex and Claude account status, including the Claude login token's
  validity, expiry, and automatic refresh timing.
- Check the installed app version under Diagnostics.
- **Refresh Claude Login** — renew the Claude OAuth token on demand if usage
  shows a `401` / login-expired error. The token is also refreshed
  automatically at launch and when it nears expiry, so this is rarely needed.
- Adjust the refresh interval, labels, and launch-at-login, and open the logs.

## Privacy And Security

- Credentials are not stored in this repository or copied into the app bundle.
- Codex credentials are read locally from the Codex CLI authentication file.
- Claude credentials are read locally from Claude Code's macOS Keychain entry
  or credentials file.
- The bridge binds to `127.0.0.1` by default and is not exposed to the local
  network.
- The bridge's control endpoints (`/health`, `/claude/status`, `/claude/refresh`)
  require a private loopback token written to a `0600` file, and login refresh is
  rate-limited, so a stray local process or browser cannot trigger a token
  refresh. Usage responses are cached locally and are not uploaded by CodexBuddyMac.
- Do not commit local logs, build products, user data, credentials, or signing
  profiles. The included `.gitignore` excludes these files (including the runtime
  token, lock, PID, and retry-state files).
- Run `./scripts/security-check.sh` for an on-demand audit (no leaked secrets or
  PII, loopback-only bind, endpoint auth, safe file permissions, dependencies,
  GitHub protections). `./scripts/check-secrets.sh` is a focused pre-push scan,
  complementing GitHub's server-side secret-scanning push protection.

The bridge calls provider usage endpoints using the user's existing CLI OAuth
session. These endpoints may change because they are controlled by their
respective providers.

CodexBuddyMac is an independent, unofficial project. It is not affiliated with,
endorsed by, or sponsored by OpenAI or Anthropic. Codex, ChatGPT, Claude, and
Claude Code are trademarks of their respective owners.

## Troubleshooting

- If Claude shows a `401` or login-expired error, use **Refresh Claude Login**
  in Settings (the app also refreshes the token automatically).
- If Claude remains at zero, run `claude auth status` and sign in if necessary.
- If usage shows a rate-limit warning, it clears on its own once the provider's
  `Retry-After` window elapses; repeated manual refreshes can prolong it.
- If the bridge is stale, quit the app, stop any process using port `8789`, and
  relaunch the app.
- App logs are stored under `~/Library/Application Support/CodexBuddy/Logs`.

## Repository Layout

- `CodexBuddyMac/CodexBuddyMac`: menu bar app and bundled bridge
- `CodexBuddyMac/CodexBuddyWidgetExtension`: widget source
- `CodexBuddyMac/CodexBuddyMac.xcodeproj`: Xcode project
- `scripts/`: release build, security check, and pre-push secret scan
- `tests/`: Python bridge tests (`python3 tests/test_claude_auth_recovery.py`)

## License

Licensed under the [MIT License](LICENSE).
