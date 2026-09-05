# Development Rules

## Product and scope

OMP Deck is a local-first, mobile control plane for one developer's OMP sessions. Python owns orchestration, security, persistence, transport adaptation, and HTTP/SSE delivery. The browser is a dependency-free mobile client. The OMP extension is a documented-API fallback for terminal-owned sessions.

For the planned architecture and completion gates, read `IMPLEMENTATION_PLAN.md` in full. Do not reduce its scope silently. Preserve behavior added after the plan was written.

Deck is not a Collab replacement. It may consume the published Collab protocol internally, but it does not expose rooms, invite links, guest identities, public sharing, or multi-user arbitration.

## Ownership boundaries

Use the strongest native transport for each ownership model:

- Deck-launched sessions: official `omp --mode rpc` plus the bundled Python `omp_rpc.RpcClient`.
- Terminal-owned sessions with Collab active: published `@oh-my-pi/pi-wire` Collab protocol.
- Terminal-owned sessions without Collab: minimal OMP Deck extension over its Unix socket.
- Browser delivery: authenticated HTTP actions plus SSE snapshots/replay.

Normalize every transport behind one Python session model. Browser code consumes normalized state and advertised capabilities; it never branches on backend implementation names merely to infer behavior.

Do not patch, fork, rebuild, monkey-patch, or replace OMP. Do not interfere with `omp update`. Do not use terminal keystrokes, pane scraping, `/proc`, or full-transcript polling where a documented structured API exists.

One component owns each state transition:

- OMP owns the agent/tool lifecycle.
- A transport adapter owns protocol translation and correlation.
- Python owns normalized fleet state, authentication, cursors, persistence, and replay.
- The browser owns only ephemeral presentation and user interaction.

Never create a second authority for the same state.

## Research before implementation

Before adding a helper, parser, transport, cache, process wrapper, path validator, or renderer:

1. Search the repository for an existing implementation.
2. Check the domain module next to the call site.
3. Check the official OMP RPC client/types and published Collab schemas.
4. Extend the central implementation when a capability is missing; do not fork its logic locally.

Two implementations of the same operation are a bug even when both currently work.

Inspect external API source or installed types rather than guessing signatures, event shapes, defaults, or error behavior. Private or inferred behavior is not a stable contract. If compatibility requires inference, isolate it behind one adapter, test it explicitly, and expose a degraded capability when it fails.

## Engineering standard

Correctness first, then maintainability.

- Prefer boring, explicit code over abstraction without demonstrated reuse.
- Keep changes focused. No unrelated cleanup or feature additions.
- Fix the ownership boundary or source of a bug; do not suppress its symptom.
- Migrate every caller during a cutover. Remove obsolete aliases, paths, comments, and tests.
- Make invalid states unrepresentable where practical; otherwise reject them at the boundary.
- Validate untrusted values once at ingress and pass typed/normalized values internally.
- Do not add silent fallback behavior. Degradation must be explicit in state and UI.
- Comments explain invariants, failure history, races, or rejected alternatives—not syntax.
- Record measured rejected approaches when future agents are likely to propose them again.
- Avoid avoidable allocation, copying, polling, reparsing, and repeated filesystem traversal.

Keep modules below the repository's metric limits. Split by cohesive ownership rather than compressing formatting or moving complexity into unnamed helpers.

## Python

Python is the primary service language.

- Use standard-library APIs unless a dependency materially improves a required contract.
- Keep blocking filesystem and subprocess work out of asynchronous/event-delivery critical sections.
- Supervise every long-lived child explicitly: startup, readiness, exit, cancellation, restart policy, and cleanup.
- Use argument arrays, never shell interpolation, for process execution.
- Resolve and validate filesystem paths against their configured root before reading, writing, creating, deleting, or launching.
- Treat symlinks, `..`, missing parents, malformed configuration, and concurrent replacement as hostile boundary cases.
- Write durable state atomically. Define ownership, permissions, schema/version, migration, corruption recovery, and retention.
- Never hold a global lock across network I/O, subprocess waits, or callbacks.
- Bound queues, replay buffers, uploads, frames, command output, and retained history.
- Distinguish timeout, cancellation, disconnect, process exit, protocol error, and user rejection.

Centralize settings, session projection, process discovery, attachments, cursors, project paths, and transport adapters. Route handlers should validate and delegate rather than contain domain logic.

## JavaScript and browser UI

The browser remains dependency-free unless a measured requirement justifies a framework or package.

- Escape untrusted text before producing HTML.
- Sanitize and validate URLs before creating links or media elements.
- Keep transport concepts out of presentation logic; use normalized capabilities.
- Do not persist transcripts, paths, identifiers, usage data, or credentials in browser storage.
- Optimistic state must reconcile to an authoritative server event or roll back visibly.
- Queue behavior must preserve order per session and isolate failures between sessions.
- Every pending operation needs a stable identity and explicit accepted, durable, failed, cancelled, or stale state.
- Preserve draft text and attachments across session switching and failed delivery.
- Revoke object URLs and release event listeners when their owning UI is removed.
- Use semantic controls, labels, focus behavior, and non-color status text.
- Long text, paths, links, tables, code, and translated strings must remain contained at narrow and wide viewports.

Do not use source text or Markdown as an authorization or state protocol.

## OMP extension

The extension runs in the OMP process and can bring down the user's session.

- Use documented extension APIs only.
- Register during load; perform runtime actions from initialized events and contexts.
- Use `ctx.setInterval`, `ctx.setTimeout`, and `ctx.clearTimer`; never leave raw timers or detached rejections.
- Catch and report background errors through structured extension events.
- Advertise capabilities explicitly at registration. Never infer a capability merely because the bridge connected.
- Version and validate every Unix-socket envelope.
- Correlate requests and responses with IDs.
- Bound frame size, reconnect attempts, retained state, and heartbeat tolerance.
- A transport disconnect must not crash or abort the local OMP session.

For extension-fallback terminal sessions, Ask is terminal-only. The extension may report the pending tool but must not inject keys, queue a fake answer, replace the tool, or claim it was answered remotely.

## RPC and Collab adapters

Do not recreate official commands or wire schemas in a Deck-specific language.

For RPC:

- Negotiate the supported protocol version.
- Correlate every command response by ID; never rely on output order.
- Implement chunk validation and bounded reassembly exactly as specified.
- Treat snapshots as authoritative and incremental events as ordered updates.
- Supervise child process ownership separately from protocol connection state.
- Persist enough identity and desired state to recover intentionally running managed sessions.

For Collab:

- Use published `@oh-my-pi/pi-wire` shapes and protocol versions.
- Treat the full writable link as a credential. Keep it server-side, redact it from logs/API payloads, and discard it when removed.
- Preserve read-only/write authorization, request IDs, first-response-wins settlement, request-end notification, late-join replay, and unavailable-versus-cancelled semantics.
- Collab relay failure must affect only Collab-backed sessions.
- Do not expose room or participant concepts in the Deck UI.

For all asynchronous request/response protocols, define and test:

1. identity and correlation;
2. validation and authorization;
3. first and duplicate response behavior;
4. cancellation and timeout;
5. transport disconnect;
6. reconnect and replay;
7. stale response rejection;
8. service/process restart;
9. bounded memory and frame size;
10. observability without secret leakage.

## Security

Treat Deck access as terminal access to the machine.

- Bind locally by default.
- Authenticate every state-bearing or mutating browser route.
- Protect browser mutations against CSRF and hostile origins.
- Pair devices explicitly; support revocation.
- Never put credentials, Collab secrets, session contents, or private paths in URLs, logs, toasts, browser persistence, or cacheable responses.
- Apply `no-store` where responses may contain private state or blobs.
- Validate attachment type, size, count, ownership, and path.
- Refuse project paths that escape configured roots through traversal or symlinks.
- Uninstall and purge only paths proven to be owned by Deck.
- Use timing-safe comparison for secrets where applicable.
- Default to denial when capability, identity, version, or ownership is uncertain.

Security behavior must be tested through the public boundary, including negative cases.

## State and event invariants

- Each session has one stable Deck identity independent of browser connection.
- Sequence IDs are monotonic within their documented scope.
- A snapshot establishes the base state; events apply only to the matching base/generation.
- Reconnect uses bounded replay when possible and an authoritative replacement snapshot otherwise.
- Duplicate delivery is harmless.
- Out-of-order or stale input is rejected rather than guessed into place.
- Unread state is server-authoritative and based on explicit cursors.
- Browser disconnect does not cancel an OMP operation unless the user explicitly requested cancellation.
- One session's slow client or oversized history cannot stall the fleet.
- Transport state and agent execution state are separate facts.

## Testing guidance

Test the contract the system exposes—not the easiest internal detail.

Every new test must defend a concrete observable behavior, boundary, invariant, transition, precedence rule, wire shape, or real regression. If the consumer-visible failure cannot be named, do not add the test.

Good tests:

- reproduce a reported bug and fail on the former implementation;
- exercise distinct valid, invalid, empty, timeout, cancellation, and reconnect branches;
- assert protocol bytes/shapes when an external consumer depends on them;
- test path traversal and symlink escape through the public function/route;
- test state transitions and race winners;
- run deterministic, isolated, and safely in the complete suite.

Bad tests:

- `assert True`, bare “does not throw,” non-empty, or length-growth assertions;
- checking that a constructor copied its input;
- source-grepping for an import, call, comment, or removed name;
- testing prompt wording or incidental formatting without a parser/consumer contract;
- duplicating an integration contract in several lower-level tests;
- globally mutating environment/module state without restoring it.

For errors, trigger the real failure path and assert what the caller observes. For compile-time guarantees, use compilation/type checks rather than runtime placeholders.

Do not add tests for trivial low-risk edits unless they protect a regression-prone contract.

## Behavioral verification

Automated checks are necessary but not sufficient.

- Bug fix: reproduce, implement, rerun the same reproduction.
- Web UI: launch the deployed/isolated surface, interact with it in a real browser, and inspect the target viewport.
- Service/lifecycle change: run the actual command against isolated config/state and verify process health and cleanup.
- RPC/Collab change: exercise a real protocol session, including disconnect/reconnect where relevant.
- Packaging change: test clean install, upgrade, rollback, uninstall, and purge in isolation.

Report exactly what was exercised. Do not claim unobserved behavior.

The complete repository check is:

```bash
python3 tools/check.py
```

It runs Ruff linting and formatting checks, text/encoding checks, Python compilation, unit tests, metrics, and JavaScript syntax checks. Run focused behavioral verification first, then this check once at the end.

## Production isolation

During major implementation, never use the live deployment as a test environment.

Do not modify or restart production installation, config, state, extension, Tailscale routing, or port `8787` until the cutover gate in `IMPLEMENTATION_PLAN.md` passes.

Use an isolated localhost port and temporary `HOME`, XDG directories, Deck configuration, state, sockets, sessions, uploads, projects, and OMP plugin/profile data. Test generated service files without installing them.

Production cutover is one final operation with a prepared rollback. If health or phone-surface verification fails, roll back immediately.

## Git and change discipline

- Commit completed, verified feature and bug-fix work when it forms a coherent change; explicit user permission is not required. Do not commit exploratory or incomplete work.
- Never publish, push, open an issue, or open a pull request unless explicitly requested.
- Preserve user changes and untracked files.
- Keep commits focused and use the repository's Conventional Commit hook.
- Review every changed file before committing.
- Do not mix generated noise, unrelated cleanup, or separate features.
- A clean working tree is not proof of correctness; verification precedes commit.

## Documentation

Documentation describes supported behavior, exact trust boundaries, data retention, configuration ownership, failure modes, and recovery. Do not claim support based only on inferred JSONL, terminal, or private OMP behavior.

Update user-facing documentation and changelog entries when a permanent feature changes behavior. Keep implementation history and internal mechanics out of user-facing release notes unless users must act on them.
