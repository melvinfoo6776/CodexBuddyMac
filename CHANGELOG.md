# Changelog

All notable changes to CodexBuddyMac are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.2] — 2026-07-14

### Fixed

- **Weekly-only Codex usage.** Codex plans that no longer return a five-hour
  window now show their live weekly utilization. The unavailable 5H bar is
  hidden in the menu and widget instead of being treated as zero usage.

- **Reliable local release packaging.** The release script clears Finder
  metadata before signing and after archiving, so both the local `.app` and
  the extracted ZIP pass macOS code-sign verification.

## [1.1.1] — 2026-06-26

### Fixed

- **Actionable message when the Claude session must be re-authenticated.** When
  the stored Claude refresh token is permanently invalid (revoked, rotated away,
  or expired — the OAuth endpoint returns `invalid_grant`), **Refresh Claude
  Login** now reports *Claude session expired. Run `claude auth login` to sign in
  again* instead of a generic "backing off after a recent failure". The bridge no
  longer starts a five-minute retry backoff for this permanent failure and skips
  further refresh calls against the dead token, so a fresh `claude auth login` is
  picked up immediately. The re-auth state self-heals once a new credential is
  stored, and `GET /claude/status` exposes a `reauth_required` flag.

## [1.1] — 2026-06-19

### Added

- **Provider reset timing.** The menu popover shows the next five-hour or
  weekly reset for Codex and Claude as both local clock time and a countdown.
- **Version and token schedule in Settings.** Settings now shows the installed
  app version and when Claude's token will be refreshed automatically.

- **Automatic Claude token refresh.** When the Claude OAuth access token is
  expired (or about to expire), the bridge renews it using the stored refresh
  token and continues serving live usage instead of failing with `401`. The
  rotated credentials are written back to the same Keychain item so Claude Code
  stays in sync, with the rest of the credential (including the `mcpOAuth`
  connector tokens) preserved untouched, plus a backup/verify/restore guard
  around the write.
- **Mandatory token check at launch and on poll.** The bridge checks token
  validity at startup and before each live fetch, refreshing proactively so the
  usage display does not silently go to `401`.
- **Settings: "Refresh Claude Login" button and "Claude login" status row.**
  Manually renew the token from the app, and see whether it is valid and how
  long until it expires.
- **Bridge endpoints** `GET /claude/status` and `POST /claude/refresh` backing
  the new Settings controls.
- **Security tooling.** `scripts/security-check.sh` (on-demand audit) and
  `scripts/check-secrets.sh` (pre-push secret scan); GitHub secret-scanning push
  protection enabled on the repository.

### Fixed

- **Menu bar battery direction.** The battery now becomes emptier as either
  provider's five-hour usage rises, using the higher Codex or Claude usage so a
  nearly exhausted provider is never hidden by the other provider's balance.

- **Persistent Claude `429` recovery.** The bridge stores the upstream retry
  deadline on disk and restores it after an app or bridge restart, preventing a
  restart or manual refresh from immediately calling Claude again.
- **Verified bridge start and restart.** The app now waits for an authenticated
  health response containing the expected script build and process ID before
  reporting success.
- **Single bridge ownership.** An exclusive runtime lock prevents competing
  Python bridge processes, while a second Mac app launch activates the existing
  app instead of starting another bridge.
- **Protected control endpoints.** Bridge health, Claude login status, and
  Claude login refresh require the private loopback token. Runtime token, lock,
  PID, and retry-state files are excluded from source control.

- **Claude `401` recovery.** The bridge now uses Claude Code's current OAuth
  token endpoint and, when a usage request returns `401`, refreshes the shared
  credential and retries the usage request exactly once. Authentication errors
  no longer create a misleading rate-limit countdown.
- **Claude login refresh feedback.** The Settings action now checks bridge
  availability, refreshes usage after a successful login refresh, preserves a
  genuine Claude rate-limit deadline, and recommends **Restart Bridge** only
  when the bridge is outdated or not responding.
- **Claude usage stuck on a rate-limit warning.** The bridge now honors the
  server's `Retry-After` header on `429` responses instead of retrying on a
  fixed five-minute timer. Retrying inside the rate-limit window had been
  re-tripping the limit so it never cleared.
- **Backoff bypass with no cached value.** During a rate-limit backoff the
  bridge no longer calls the upstream endpoint when it has no in-process cache
  (for example right after a restart); it falls back to the on-disk file
  instead, so it stops renewing the `429`.
- **Orphaned bridge / port `8789` conflict.** The bridge is now terminated when
  the app quits, and a bridge left over from a previous run is reclaimed on the
  next launch (tracked via a pid file). This removes the
  `Address already in use` failures that could leave the app unable to start
  its bridge.
- **Last-known-good usage persists across restarts.** Successful live Claude
  usage is written to disk, so a restart falls back to the last real values
  instead of the bundled placeholder zeros.
- **Reduced disk churn.** The bundled bridge script is reinstalled only when its
  contents actually change, instead of being rewritten on every refresh.

### Changed

- Live Claude usage cache TTL increased from 90s to 300s to reduce polling
  against the shared usage endpoint.
