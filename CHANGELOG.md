# Changelog

## Unreleased

### Changed

- Bound project-picker discovery to a configurable directory depth, defaulting to two levels, and group nested paths with explicit loading and result feedback on mobile.

## 1.0.1 — 2026-09-05

### Fixed

- Prevent queued duplicate or stale Collab UI-request frames from restoring a request after it has been answered or ended.

### Changed

- Refresh the pinned official `omp-rpc` source to the current tested OMP revision while retaining stock OMP 18.1.10 compatibility.
- Enforce Ruff linting and formatting in contributor and CI checks, with transport internals reorganized around explicit protocol and runtime ownership boundaries.

## 1.0.0 — 2026-09-04

### Added

- Ship OMP Deck as an installable Python application with an `omp-deck` command, systemd user service, health diagnostics, optional Tailscale Serve setup, and ownership-safe removal and purge operations.
- Manage Deck-launched sessions through the official `omp_rpc.RpcClient`, including stable Deck identities, persisted desired-running state, OMP session resume, correlated commands, and complete built-in Ask handling.
- Connect terminal-owned sessions through imported writable Collab links or the bundled versioned Unix-socket extension fallback. Collab supports remote pending-request responses; extension fallback keeps Ask terminal-only.
- Present RPC, Collab, and extension sessions through one normalized mobile fleet dashboard with transport status, capabilities, messages, todos, subagents, usage, unread state, and pending requests.
- Support project launch and creation, ordered prompts and follow-ups, interrupt, compact, model and thinking controls, image attachments, session removal, logout, revoke-all, and local browser-data clearing when the active transport advertises each capability.

### Security

- Require explicit device pairing for every API, SSE, and blob request, with opaque verifier-backed credentials, individual logout, revoke-all, CSRF enforcement, trusted Origin and Host validation, and private no-store responses.
- Keep writable Collab links in protected server-side storage and exclude them from browser state, events, logs, and persisted recovery metadata.
- Enforce rooted project resolution, hostile-symlink rejection, canonical project names, authenticated constant-time blob lookup, verified image signatures, and bounded request and attachment sizes.
- Store authentication, registry, request, cursor, and Collab state atomically with strict ownership, permissions, schemas, size limits, retention bounds, and explicit corruption recovery.

### Reliability

- Provide generation- and sequence-aware fleet ownership, stale-event rejection, bounded process-scoped SSE replay, authoritative replacement snapshots, failed-stream replacement, and immediate browser recovery from event gaps.
- Close live event streams after credential revocation and preserve server-authoritative unread cursors across browser and service restarts.
- Deduplicate launches and commands with durable request IDs, preserve ambiguous browser request IDs for safe retry, and serialize per-session operations without blocking unrelated sessions.
- Supervise RPC, Collab, and extension transports through bounded startup, refresh, reconnect, shutdown, and cleanup paths with explicit degraded and disconnected states.
- Preserve extension-removal tombstones across restarts, enforce concurrent attachment reservations, and retain newly created project data when OMP startup fails.

### Compatibility and operations

- Support Linux with Python 3.11 or 3.12, Bun 1.3.14 or newer, and stock OMP 18.1.10 through pinned and independently validated RPC, Collab, and extension integrations.
- Include read-only legacy migration planning plus ownership-checked application, rollback boundaries, isolated plugin backup recovery, and safe handling of interrupted writes and quarantine artifacts.
- Exclude plugin development dependencies from Python packages and use a vulnerability-free pinned Puppeteer release for browser verification.
- Remove tmux keystroke control, `/proc` discovery, full-transcript live polling, direct plugin copying, legacy HTTP routes, slash-command emulation, persistent browser session data, and obsolete compatibility paths.
