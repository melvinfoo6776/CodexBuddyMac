# Changelog

All notable changes to CodexBuddyMac are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2026-06-19

### Added

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

### Fixed

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
