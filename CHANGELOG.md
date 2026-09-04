# Changelog

## 0.1.0 — Unreleased

- Package OMP Deck as a standard Python application with an `omp-deck` entry point, official plugin-manager lifecycle, systemd user-service commands, health checks, isolated setup verification, and ownership-safe purge behavior.
- Add mandatory device pairing, revocation, CSRF and Origin enforcement, Host validation, private-response headers, bounded request parsing, safe project resolution, and authenticated blob delivery.
- Replace managed TUI/tmux control with supervised official `omp_rpc.RpcClient` sessions, stable Deck identities, persisted desired-running state, resume metadata, correlated commands, protocol-version coverage, and complete built-in Ask handling.
- Add terminal-owned session support through the published pi-wire Collab protocol and a versioned Unix-socket extension fallback with peer authentication, capability negotiation, structured events, managed timers, bounded frames, and terminal-only fallback Ask behavior.
- Add protected server-side Collab credential storage and browser import/removal controls. Writable links are never returned to the browser after import or included in API events and logs.
- Normalize RPC, Collab, and extension state behind one fleet model with generation/sequence ownership, stale-event rejection, bounded event replay, server-authoritative persistent unread cursors, explicit degradation, and per-session request deduplication.
- Replace browser snapshot polling with authenticated SSE plus bounded replay and low-frequency authoritative recovery. Add accessible transport status, capability-driven controls, mobile pending-request forms, ordered optimistic sends, in-memory drafts, and bounded image attachments.
- Remove `/proc` discovery, tmux keystroke control, full-transcript live polling, direct plugin copying, legacy HTTP routes, slash-command emulation, persistent browser session data, and obsolete compatibility tests.
- Document the single-owner architecture, transport limitations, Tailscale trust assumptions, retained data, supported OMP release, service operations, upgrades, rollback boundary, and safe uninstall behavior.
- Validate OMP 18.1.10 RPC and extension compatibility and update the pinned Python and plugin dependencies.
- Exclude plugin development dependencies from Python artifacts and update the browser test runtime to a vulnerability-free Puppeteer release.
- Validate the Deck plugin independently so unrelated OMP plugin-doctor warnings do not block setup.
- Harden setup rollback, malformed-config purge, RPC and Collab request idempotency, concurrent cursor persistence, transport shutdown/reconnect ownership, cleared browser queues, multi-select Ask responses, and oversized plugin-event sequencing.
- Keep rollback plugin backups alive while linked, retain failed-start transports until cleanup succeeds, and report unjoinable Collab reader shutdowns.
- Reject malformed Collab links before reserving durable request IDs.
- Surface oversized extension events as an explicit degraded transport state.
- Bound RPC refresh shutdown and isolate terminal and RPC cleanup failures.
- Reuse browser request IDs when Ask responses or session controls have ambiguous outcomes.
- Keep removed disconnected extension registrations hidden until their connection changes.
