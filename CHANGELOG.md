# Changelog

## Unreleased

### Added

- Support native macOS with a per-user LaunchAgent, graphical-login startup, failure restarts, setup rollback, health diagnostics, and ownership-checked removal and purge. The service requires a logged-in desktop user and does not prevent Mac sleep.
- Add a native macOS CI gate for LaunchAgent lifecycle, Python portability contracts, and the Unix-socket plugin.
- Add an optional standalone multi-machine coordinator with explicit node enrollment, an authenticated shared dashboard, direct browser-to-node connections using 60-second Ed25519 bearer tickets, per-machine outage isolation, bounded revocation behavior, and documented backup and re-enrollment recovery.
- Make ongoing sessions the default dashboard, with a quiet secondary History view for retained unattached records and ongoing-only summary counts.
- Add optional owner-account sign-in through Tailscale Serve for standalone nodes and shared dashboards. Pairing remains the default; opting in replaces codes with an explicit account allowlist while retaining browser cookies, CSRF protection, and fleet ticket checks.
- Add server-persisted Deck display names and pinning for managed RPC, Collab, and extension sessions, with explicit machine/project labels and duplicate-session disambiguation.

### Changed

- Shorten Unix extension endpoint names to fit macOS socket limits. After upgrading, rerun `omp-deck setup` and restart terminal-owned OMP sessions; older Unix endpoints are no longer discovered.
- Reject legacy migration outside Linux. macOS and Windows use the normal setup commands.
- Qualify managed RPC recovery and the extension lifecycle against stock OMP 18.1.14 on macOS and 18.1.16 on Linux, alongside 18.1.10.
- Keep session execution, transport connectivity, and history availability distinct; identify Deck connectivity separately and preserve last-known grouping during machine outages.
- Identify a directly connected machine by its Deck address instead of `This machine`; preserve enrolled machine names.

### Fixed

- Let the project dropdown create directories inside listed folders and retain a newly created directory for starting or retrying, including below the configured discovery depth.
- Prevent dashboard refreshes from interrupting a managed OMP while it starts, which could report a failed launch and replace the process even though a session later appeared connected.
- Escape quotes in rendered Markdown and session metadata without changing their displayed text, and preserve keyboard focus on session tiles during live updates.
- Recheck authorization after reading mutation bodies and bound slow body uploads so revoked access cannot dispatch a delayed action and trickling requests cannot occupy HTTP workers indefinitely.
- Reject user-controlled filesystem aliases in project roots, image-blob paths, and purge targets; refuse purging directories whose ownership or write permissions are no longer safe.
- Preserve nested project identities during legacy migration, close recovery file descriptors after path-revalidation failures, and treat empty platform-directory variables as unset.
- Keep the existing terminal transport owned when replacement cleanup fails, and report Collab pending-dialog overflow explicitly without fabricating a user cancellation.
- Fence terminal-extension reconnect retries to their originating session. The bundled extension is now 1.3.2; rerun setup after upgrading and restart/resume terminal-owned OMP sessions when idle to load this fix.
- Keep HTTP startup independent of reverse DNS, which could delay macOS readiness.
- Avoid concurrent private-state lock creation failures on APFS.
- Refuse purging configured XDG roots reached through filesystem aliases.
- Preserve the original setup error when plugin installation fails before creating a plugin.
- Persist a fresh managed session before exposing it so an unused empty OMP session resumes after Deck restarts; missing or replaced session files now fail explicitly instead of creating a different conversation.
- Keep the terminal extension inactive in RPC, JSON, and print modes so a managed session cannot also appear as a duplicate terminal session.
- Preserve images that finish loading after switching sessions, and serialize thinking changes so failed or older requests cannot undo a newer choice.
- Restore unread-first session ordering, running subagent details, grouped native todos, live session names, and error reporting. Refreshing unchanged state no longer resets its activity age.
- Keep project creation controls usable while another machine is launching a session, and discard stale action results after local data is cleared or a device is re-paired.
- Restore capability-gated terminal-session removal and stop advertising unsupported Collab todo reporting.
- Restore image-viewer focus cleanup and the selected conversation after re-pairing.
- Keep mobile dashboard counters within their columns.
- Replace cached dashboard assets after upgrades and rollbacks, including when the restored files have older modification times.
- Bind confirmation controls to the request actually displayed, preserve session links until initial state arrives, and reconcile repeated prompts without relying on synchronized device clocks.
- Restore live extension messages and images, retain a working terminal connection when a newer endpoint fails, and keep removed sessions hidden across failed reconnect attempts.
- Synchronize large extension histories without disconnecting readers, losing concurrent messages, or blocking interrupt and heartbeat controls; close pending Windows pipe connections during shutdown.
- Accept complete published Collab message and snapshot metadata, update live tool activity, clear settled requests, and close failed connections before reconnecting.
- Prevent delayed RPC updates from reviving stopped sessions or restoring old conversations, deliver confirmations while commands await them, and allow stopping recovery of disconnected managed sessions.
- Preserve unrelated services when setup is rejected, retain state when service shutdown fails, and distinguish an absent optional plugin from an invalid plugin inventory during purge.
- Preserve configured Linux/macOS profile environments, restore the previous macOS configuration after failed relocation, and contain Windows child processes before they can spawn descendants.
- Keep newer read cursors when migration is repeated, resolve image blobs from the active OMP profile, expand configured executable paths, and share concurrent usage refreshes.
- Bound HTTP connections and header waits, reject malformed session titles with validation errors, support IPv6 coordinator listeners, and render progress bars under the existing security policy.
- Remove empty assistant bubbles and false unread indicators from tool-only or thinking-only records while retaining image-only replies and explicit errors.
- Avoid sending oversized tool arguments, results, and redundant lifecycle payloads through the terminal extension. Already-running older extensions need an idle restart and resume to receive the producer fix.
- Keep a verified live terminal manageable through a bounded control-only reconnect when an older extension cannot synchronize history, with a visible history warning instead of hiding it as an old session.
- Keep connection warnings readable on narrow screens and identify transport connectivity separately from agent execution.
- Restore tappable thinking-level control for updated terminal extensions and show OMP's effective level instead of assuming missing state means `auto`. Offer only model-supported levels and refresh terminal-side changes on the next heartbeat. Already-running terminals need an idle restart and resume to load the updated extension.
- Keep paired devices authenticated with persistent cookies bounded by the existing 90-day expiry, and upgrade still-valid older cookies without requiring another code.
- Show connection retry instead of a misleading PIN prompt after network failures; forbidden actions no longer log out a paired device or discard its draft.
- Replace stale reads and event streams when returning to the browser or restoring connectivity, without resending actions.
- Reduce reopening work with parallel connection checks, revalidated public assets, and incremental processing of fragmented event streams. These connection fixes do not require restarting OMP terminals already using the 1.3.1 extension.
- Restore project-first, two-column phone tiles and the compact model/thinking/task toolbar. Move full session identity into Session details and keep list navigation in browser Back or All sessions.
- Move terminal connection setup and device administration into Deck settings, remove routine launcher counters and duplicate image controls, and enlarge primary touch targets.
- Restore a capability-gated slash picker for structured session actions and advertised thinking levels, preserving drafts after cancellation, unavailable actions, and delayed completion.
- Keep history read-only without a disabled composer, and distinguish managed, terminal, disconnected, and retained-history connection indicators.

## 1.1.0 — 2026-09-09

### Added

- Support native Windows with per-user defaults, protected ACLs and named-mutex state locking, project launch and creation, managed RPC supervision through kill-on-close Job Objects, Collab, ACL-validated image attachments, named-pipe extension fallback, and ownership-safe purge.
- Add a least-privilege per-user Task Scheduler logon service on Windows with bounded failure restarts, transactional setup rollback, health diagnostics, and explicit install, status, restart, and removal commands.
- Gate native Windows behavior in CI against the pinned official OMP 18.1.10 executable, including descendant-process cleanup, Task Scheduler lifecycle, named-pipe protocol behavior, and attachment ACL rejection.

### Fixed

- Preserve the entered pairing code and show explicit progress or secure-session errors instead of silently returning to an empty pairing form.

### Changed

- Bound project-picker discovery to a configurable directory depth, defaulting to two levels, and group nested paths with explicit loading and result feedback on mobile.
- Replace long pairing secrets with six-digit codes that expire after one minute and invalidate after the first failed submission.
- Normalize Windows and Linux behind the same advertised capabilities while keeping legacy migration Linux-only.

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
