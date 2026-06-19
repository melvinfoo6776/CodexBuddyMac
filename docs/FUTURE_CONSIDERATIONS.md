# Future Considerations

This document records possible future work. It is not a release commitment or
schedule. Items can be added, changed, split, or removed as the product evolves.

## Possible Version 2.0

A future major release could replace the bundled Python bridge with a native
Swift service inside the macOS app. This would be a breaking architectural
change and should be developed separately from 1.x maintenance work.

### Goals

- Remove the Python 3 runtime requirement.
- Remove the localhost HTTP server, port `8789`, PID files, and child-process
  lifecycle management.
- Use one native refresh controller for the timer, **Refresh Now**, and account
  recovery actions.
- Read Codex and Claude credentials using appropriate macOS security APIs.
- Refresh provider authentication when supported and report expired login,
  offline, and rate-limited states clearly.
- Persist last-known-good usage and provider retry deadlines across app
  restarts.
- Support signing, notarization, and normal macOS distribution.

### Suggested Architecture

- A Swift actor owns provider requests, cache state, and refresh scheduling.
- Provider-specific clients handle Codex and Claude responses independently.
- The menu bar, Settings, and widget read a shared usage model instead of
  polling a local web server.
- Manual refresh respects active provider `Retry-After` deadlines.
- Authentication errors are kept separate from network and rate-limit errors.

### Migration

- Keep 1.x available while 2.0 is developed and tested.
- Preserve compatible display settings, refresh preferences, and cached usage.
- Remove installed bridge files and PID data only after a successful migration.
- Provide a clear fallback or rollback path during early 2.0 releases.

### Verification

Automated tests should cover:

- Successful Codex and Claude responses.
- Expired credentials and failed authentication refresh.
- HTTP `401`, `429`, provider outages, malformed responses, and timeouts.
- App restart during a provider backoff window.
- Timer and manual refresh concurrency.
- Offline startup with last-known-good data.
- Cache migration from 1.x.

### Risks And Prerequisites

- Provider usage endpoints are not stable public APIs and may change.
- Authentication behavior must be reviewed before implementing a native client.
- Apple Developer signing identities and notarization credentials are required
  for a public binary release.
- The widget target and App Group configuration must be complete before shared
  usage data can be considered reliable.

## Additional Ideas

Add future feature ideas below with their user value, dependencies, risks, and
acceptance criteria. Keep exploratory ideas here until they are approved for a
specific release.

