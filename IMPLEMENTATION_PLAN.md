# OMP Deck Implementation Plan

## Execution directive

When instructed to “read `IMPLEMENTATION_PLAN.md` and workflowz it,” execute this entire plan autonomously as the high-thinking primary agent. Preserve current behavior and user changes; create the complete phased todo list; define shared contracts before delegation; use workflowz only for genuinely independent work; personally integrate shared boundaries; and satisfy every phase gate and completion criterion without scaffolds, shims, placeholders, or deferred work.

Production remains untouched during implementation. Use the isolated runtime specified below, complete all isolated verification, prepare migration and rollback, then report readiness and wait for explicit authorization before production cutover.

## Purpose

OMP Deck is a local-first, mobile control plane for a developer's OMP sessions. It is not a replacement for OMP Collab and does not implement rooms, guest sharing, or a public relay.

The finished system should:

- show every local OMP session in one mobile dashboard;
- identify work, unread responses, pending input, todos, and active subagents;
- control sessions through documented OMP APIs rather than terminal guesses;
- support both Deck-launched RPC sessions and terminal-launched OMP sessions;
- keep the transport distinction internal except for a small status indicator;
- keep Python as the service, state, security, and orchestration layer;
- be safe to install, upgrade, use, and uninstall from a clean public repository.

Python remains central because it is relevant to the Evaboot application and is appropriate for authentication, concurrent process supervision, event ingestion, state projection, persistence, HTTP/SSE delivery, and recovery.

## Product boundary: OMP Deck versus Collab

OMP Collab shares one live session with other people. It provides rooms, participant identities, read-only and writable links, an encrypted relay protocol, transcript replication, interactive requests, and subagent control.

OMP Deck operates one person's local fleet. Its differentiators are:

- multi-session overview;
- persistent private access;
- mobile-first triage;
- unread and attention state;
- aggregate todos and active subagents;
- project and session launching;
- subscription usage;
- local ownership through Tailscale.

OMP Deck may consume Collab as an internal transport, but must not expose or recreate Collab's sharing product:

- no rooms, invite links, guest identities, or participant controls in Deck;
- no public sharing or multi-user arbitration;
- no Deck-specific fork of Collab's protocol;
- no assumption that every terminal session is hosting Collab.

Reuse the useful protocol and implementation work already provided by Collab:

- published `@oh-my-pi/pi-wire` schemas and protocol negotiation;
- authoritative snapshots followed by incremental entries and events;
- prompt, abort, subagent, and interactive UI request handling;
- request IDs, first-response-wins settlement, cancellation, and reconnect behavior;
- bounded state and transcript transfer.

Python normalizes Collab and RPC sessions behind one Deck interface. Browser users see capabilities, not rooms or transport internals.

Relevant upstream sources:

- `packages/coding-agent/src/collab/`
- `packages/collab-web/`
- published `@oh-my-pi/pi-wire`
- OMP RPC documentation and types
- bundled Python `omp_rpc.RpcClient`

OMP Deck must work with stock, frequently updated OMP releases. Do not patch, fork, rebuild, monkey-patch, or replace the OMP executable, and do not redirect or interfere with the normal `omp update` flow. Integrations must use documented RPC, extension, and published Collab protocol surfaces. Pin and negotiate supported protocol/package versions in Deck; fail with an explicit capability state when an installed OMP version is incompatible.

Collab's React components will not be reused. OMP Deck keeps its existing visual design and implements its own mobile rendering against stable structured data.

## Target architecture

```text
Terminal-started OMP
  -> Collab transport when explicitly active and compatible
  -> minimal OMP Deck extension otherwise
                         \
                          -> Python service -> authenticated HTTP + SSE -> browser
                         /
Deck-started OMP
  -> omp --mode rpc
  -> official omp_rpc Python client
```

The Python service owns:

- device authentication and revocation;
- session registry;
- RPC child-process supervision;
- Collab guest connections and credentials;
- fallback extension socket connections;
- normalized fleet and session state;
- event sequencing and bounded replay;
- unread cursors;
- attachment validation and storage;
- safe project launching;
- HTTP commands;
- SSE updates;
- static mobile assets;
- health reporting and recovery.

The browser sees one normalized session interface regardless of transport:

```text
Session
- id
- title
- project
- state
- capabilities
- messages
- todos
- subagents
- pending UI request
- send_message
- follow_up
- interrupt
- answer_ui_request
- cancel_ui_request
- shutdown
```

## OMP transports

### RPC transport

Deck-launched sessions use OMP's existing RPC API:

```bash
omp --mode rpc --cwd /path/to/project
```

Use the official bundled Python client:

```python
from omp_rpc import RpcClient
```

The RPC transport provides:

- protocol negotiation;
- correlated commands and responses;
- prompt, steer, and follow-up delivery;
- abort and abort-and-prompt;
- new, switch, branch, and resume operations;
- model and thinking controls;
- structured messages and agent events;
- paginated history;
- subagent subscriptions, events, and transcripts;
- todos, compaction, stats, and available commands;
- large-frame chunking and reconstruction.

RPC-managed sessions must fully proxy OMP's extension UI sub-protocol. Persist each outstanding `extension_ui_request` by session and request ID, publish it immediately through normalized state/SSE, and return the matching `extension_ui_response`. Support `select`, `confirm`, `input`, `editor`, and `cancel`, including option descriptions, custom answers, cancellation, timeouts, reconnects, and stale or duplicate responses. This makes the built-in `ask` tool usable from Deck instead of leaving the RPC session blocked behind an invisible terminal dialog.

The browser must show only the currently valid request, preserve multi-question `ask` progress across state refreshes, and disable ordinary sends only when OMP itself cannot accept them. Python remains authoritative: the first valid response wins, later responses fail cleanly, and disconnecting a browser does not cancel the OMP request.

Do not recreate these RPC commands in a Deck-specific language.

Pin the `omp-rpc` dependency to a tested OMP release until it is available through a stable package release channel. Test the minimum and current supported OMP versions.

The Python service supervises each RPC child. It persists the OMP session identity, project, and desired running state. After a service restart it resumes sessions that were intentionally left running.

### Collab transport for terminal sessions

When a terminal-owned OMP session is explicitly hosting Collab, Python joins it as one writable guest using the published `@oh-my-pi/pi-wire` protocol. Collab is the authoritative structured transport for that session: consume its welcome/snapshot chunks, entries, events, state, agent and subagent frames, prompts, aborts, and UI requests. Preserve Collab's request IDs, permissions, first-response-wins behavior, request-end notifications, and reconnect semantics.

Support normal Collab links and their configured relay, including the default `wss://my.omp.sh`, without exposing the room or write secret to the browser. Treat the full link as a credential: retain it only in protected server state, redact it from logs and API payloads, and discard it when the session disconnects or the user removes it. Do not make the public relay a requirement for Deck itself; RPC and extension-fallback sessions continue independently.

Do not automatically start Collab through terminal keystrokes, private imports, patched OMP internals, or unmerged OMP features. Initial activation may require the user to run `/collab` and provide the writable link to Deck. If a future stock OMP release exposes documented auto-start or secure write-link handoff, adopt it behind capability/version checks without making it mandatory.

### Extension fallback transport

A normal interactive TUI already owns its stdin and stdout, so OMP RPC cannot attach after startup. When Collab is not active, the Deck extension exposes only the documented Deck-required subset through a Unix socket:

- `pi.sendUserMessage(...)` for prompts;
- `pi.sendUserMessage(..., {deliverAs: "followUp"})` for queued messages;
- `ctx.abort()` for interruption;
- `ctx.shutdown()` for shutdown;
- `ctx.isIdle()` and lifecycle hooks for status;
- message and tool lifecycle hooks for structured events;
- `ctx.getAsyncJobSnapshot()` and task events for supported subagent state;
- `ctx.setInterval()` and `ctx.clearTimer()` for managed timers.

The extension protocol must use versioned, validated request and event envelopes. It is a degraded fallback, not a second implementation of RPC or Collab.

The extension can observe a terminal `ask` tool call but stock OMP does not expose a documented extension response API for its local modal. Advertise `answer_ui_request: false`; show `Answer this request in the terminal`; never queue a fake answer, inject terminal keys, disable or replace the built-in tool, or patch OMP. The terminal session may remain blocked until answered or cancelled locally. Full phone-side `ask` support is required only for RPC-managed and Collab-connected sessions.

Example:

```json
{
  "version": 1,
  "id": "request-42",
  "type": "follow_up",
  "message": "Run the focused test too"
}
```

Every extension registration reports:

- protocol version;
- OMP version;
- session ID;
- project and title;
- supported capabilities;
- connection instance ID.

### Transport indicator

The transport distinction remains mostly internal, but the active chat shows a small status light beside the kebab menu:

- green: RPC session controlled through OMP's full RPC API;
- amber: terminal session connected through the Deck extension rather than RPC;
- red: disconnected or degraded session.

Requirements:

- accessible text/title, not color alone;
- exact labels such as `RPC session`, `Terminal session via extension`, and `Disconnected`;
- controls are enabled from advertised capabilities, not inferred from the color;
- home cards remain uncluttered unless later testing demonstrates a need for the indicator there.

## Browser transport

Use Server-Sent Events for continuous Python-to-browser updates:

```text
GET /api/events
Content-Type: text/event-stream
```

Each event carries a monotonic ID, type, and JSON payload. The browser reconnects with `Last-Event-ID`. Python replays missed bounded events or sends a replacement snapshot.

Browser-to-Python actions remain authenticated HTTP requests:

- `POST /api/sessions/:id/messages`
- `POST /api/sessions/:id/follow-ups`
- `POST /api/sessions/:id/interrupt`
- `POST /api/sessions/:id/ui-requests/:request-id/respond`
- `POST /api/sessions/:id/ui-requests/:request-id/cancel`
- `POST /api/sessions/:id/shutdown`
- `POST /api/projects/:name/sessions`

This is bidirectional at the application level without requiring a browser WebSocket. A WebSocket should be introduced only if measured requirements demand high-volume two-way streaming, binary multiplexing, or peer-style coordination.

## Remove fragile integration paths

### Remove `/proc` discovery

`/proc` is Linux's virtual process filesystem. OMP Deck currently inspects process commands, working directories, environments, and open file descriptors to infer OMP sessions.

Replace it with authoritative registration:

- RPC children are known because Python starts and supervises them;
- terminal sessions register through the extension;
- registrations include heartbeats and connection instance IDs;
- stale registrations expire;
- disconnected persisted sessions remain visible with an explicit state.

Do not retain `/proc` as the normal source of truth.

### Remove JSONL polling as the primary event source

Do not repeatedly parse complete OMP transcripts every second. Consume structured RPC or extension events incrementally.

If JSONL remains as a compatibility/recovery fallback:

- isolate it behind an adapter;
- track inode, size, and byte offset;
- process only appended records;
- detect truncation and replacement;
- bind support to tested OMP versions;
- never let fallback inference override authoritative live state.

### Remove tmux from session control

Do not send terminal keystrokes for:

- messages;
- queued messages;
- interruption;
- shutdown;
- model or thinking controls.

Use RPC or documented extension APIs.

Tmux may remain temporarily as an optional launcher for a persistent interactive TUI session. It is not required for RPC sessions or extension-connected existing sessions. If tmux is absent, only the optional interactive-TUI launch capability is unavailable.

Do not build a custom PTY supervisor merely to remove tmux.

## Service model

Keep Python as one persistent service. On Linux, a systemd user service remains the default and appropriate supervisor because it provides startup, restart, logs, and stable availability when no OMP TUI is open.

Systemd is an integration, not a core application assumption:

```text
omp-deck serve
omp-deck service install
omp-deck service status
omp-deck service restart
omp-deck service remove
```

Future service adapters may support `launchd` or another platform without changing the server.

## Packaging and installation

Do not maintain an independent package manager or updater.

### Python package

Create a standard Python package with:

- `pyproject.toml`;
- console entry point `omp-deck`;
- declared Python version and dependencies;
- version metadata;
- installation through `uv tool install` or `pipx install`;
- upgrades through the selected Python tool manager.

### OMP plugin

Package the extension using OMP's official plugin format. Install, validate, upgrade, and remove it through OMP's plugin manager rather than copying TypeScript directly into `~/.omp/agent/extensions`.

### Setup orchestration

`omp-deck setup` may orchestrate:

- initial configuration;
- authentication secret generation;
- service installation;
- official OMP plugin installation and validation;
- optional Tailscale Serve configuration;
- service start and health verification.

It must not act as a custom updater.

## Security requirements

### Application authentication

Tailscale is not sufficient as the only authorization boundary.

Implement:

1. a high-entropy installation secret;
2. a one-time setup URL or explicit pairing flow;
3. exchange for an `HttpOnly`, `Secure`, `SameSite=Strict` session cookie;
4. hashed server-side session credentials;
5. logout and revoke-all-devices controls;
6. authentication on every API, SSE, and blob request;
7. rate limiting for authentication attempts;
8. no durable credentials in localStorage or URLs after pairing.

Treat authenticated Deck access as equivalent to terminal access.

### Browser and request hardening

Add:

- strict Origin validation for mutations;
- CSRF protection;
- Host validation;
- `Content-Security-Policy`;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: no-referrer`;
- CSP `frame-ancestors 'none'`;
- restrictive `Permissions-Policy`;
- `Cache-Control: no-store` for APIs and attachment blobs;
- MIME verification for uploaded files;
- request, decoded-file, attachment-count, and aggregate-size limits.

### Browser data retention

Do not persist transcripts, usage reports, project paths, session identifiers, or dashboard summaries in localStorage by default.

Keep only non-sensitive preferences where necessary. Drafts may remain in sessionStorage but must be cleared on logout and through a `Clear local data` action.

Service workers must never cache API, SSE, authentication, or blob responses.

### Tailscale trust model

Document and enforce the supported deployment:

```text
Phone -> authenticated Tailscale HTTPS -> localhost Python service
```

Documentation must state:

- Funnel is unsupported;
- direct public exposure is unsupported;
- binding to `0.0.0.0` requires an authenticated reverse proxy and is not the default;
- tailnet ACLs/grants should restrict the Deck endpoint to the owner;
- application authentication remains required;
- existing unrelated Serve routes are never overwritten;
- Tailscale configuration may require administrator authorization even though the systemd user service does not require root.

### Safe project boundaries

For every listed, created, or launched project:

- resolve the configured root;
- resolve the candidate;
- prove containment with `relative_to`;
- reject symlink escapes, broken links, and non-directories;
- revalidate after creation;
- avoid shell interpolation.

### Safe uninstall and purge

A malformed configuration must not prevent uninstall.

Required changes:

- parse CLI commands before loading runtime settings;
- keep installation-owned paths in an installation manifest independent of arbitrary configuration;
- allow service removal with missing or invalid config;
- resolve every purge path;
- reject `/`, `$HOME`, XDG roots, shallow ancestors, and symlinks;
- require an OMP Deck ownership marker;
- restrict deletion to approved OMP Deck-owned roots;
- test malicious configurations and paths.

## State, sequencing, and recovery

Each normalized event carries:

- session ID;
- transport connection instance;
- monotonic sequence number;
- event type;
- structured payload;
- timestamp.

Python maintains:

- the latest authoritative snapshot per session;
- a bounded event replay buffer;
- unread/read cursors;
- pending command correlation;
- desired running state for RPC children;
- explicit disconnected and degraded states.

Required transition coverage:

- idle -> working -> completed;
- working -> interrupted;
- working -> needs input;
- queued follow-ups;
- multiple optimistic messages;
- duplicate message text;
- delivery failure in the middle of a queue;
- pending-to-durable reconciliation;
- refresh or reconnect while pending;
- branch, switch, resume, and compaction;
- subagent creation, progress, yield, failure, and disappearance;
- extension disconnect and reconnect;
- stale registration expiration;
- Python service restart with RPC session recovery.

## UI requirements

Preserve the existing visual design.

Continue supporting:

- current mobile cards and chat layout;
- Markdown rendering;
- semantic toast colors;
- immediate optimistic messages;
- sequential queued sends;
- active-only subagent summaries on the home page.

Add:

- the transport light beside the active chat kebab menu;
- capability-driven button visibility and disabled explanations;
- a mobile-native pending-request surface for OMP `ask` questions and other RPC or Collab UI requests, including option descriptions, recommended choices, multi-select, custom text, cancellation, and clear timeout/error states;
- precise disconnected/degraded states;
- configured project-root label instead of hard-coded `~/dev`;
- an authentication/pairing screen;
- logout, revoke devices, and clear local data controls.

Do not expose backend terms such as adapters unless needed to explain an unavailable capability.

## Release engineering

### Clean-checkout validation

The current local check runs 47 tests while full discovery runs 50. `tools/metrics.py` also requires untracked `tools/check_commit.py`, so a clean public checkout is not self-validating.

Required:

- track every required checker file or stop referencing it;
- use `python3 -m unittest discover`;
- validate from a clean exported checkout;
- ensure ignored and untracked local files cannot make checks pass;
- use executables from `PATH`, not a hard-coded `~/.local/bin/node`;
- include all maintained Python, JavaScript, TypeScript, and CSS sources.

### CI

Add clean Linux CI covering:

- text and encoding checks;
- Python compilation;
- all Python tests;
- JavaScript checks;
- TypeScript type checking against supported OMP APIs;
- OMP plugin validation;
- Python package build and install;
- setup/service generation in an isolated environment;
- upgrade and restart;
- uninstall and purge safety;
- browser behavioral tests.

### Frontend behavioral tests

Test observable behavior in a real browser:

- Markdown tables and lists;
- optimistic messages;
- several queued messages;
- pending-to-durable reconciliation;
- queue failure recovery;
- toast tones;
- subagent filtering;
- unread transitions;
- transport indicators;
- capability-driven controls;
- complete `ask` flows: one and multiple questions, single-select, multi-select, custom input, cancel, timeout, reconnect, stale response, and two-device response races;
- session switching;
- attachment recovery;
- mobile keyboard behavior.

### Service health

Add an authenticated or narrowly non-sensitive `/healthz` endpoint. Setup and upgrades must verify:

- configuration loads;
- port binds;
- static assets load;
- Python API responds;
- authentication state exists;
- OMP plugin is installed and compatible.

`doctor` should distinguish service, configuration, port, OMP, plugin, Tailscale, authentication, and optional tmux-launch problems.

## Documentation requirements

Document:

- supported operating systems and OMP versions;
- RPC versus extension behavior;
- transport indicator meanings;
- capability limitations;
- authentication and pairing;
- exact Tailscale trust assumptions;
- retained server and browser data;
- optional tmux launcher behavior;
- service management;
- safe update and uninstall procedures;
- fallback compatibility behavior;
- architecture boundaries and known limitations.

Do not claim support that is based only on inferred JSONL or terminal behavior.

## Primary-agent execution instructions

The implementation chat has exclusive ownership of this worktree while the migration is in progress. The maintenance chat will not edit it concurrently.

Run the work as one high-thinking primary agent using `workflowz` orchestration. The primary agent owns architecture, shared contracts, integration, migration, and final verification. Subagents may investigate or implement genuinely independent slices only after the shared contracts are written down.

Before editing:

1. Read this entire document.
2. Inspect the current repository and preserve all behavior added after this plan was written.
3. Inventory current uncommitted/user changes and do not overwrite them.
4. Create the complete phased todo list from this document without reducing scope.
5. Define the shared contracts for normalized sessions, capabilities, event envelopes, authentication, routes, RPC ownership, Collab adaptation, extension registration, and persistence.
6. Use those contracts in every delegated task.

Suitable parallel slices after contracts exist:

- clean-checkout validation and CI;
- authentication and browser security;
- uninstall, purge, and project-path safety;
- Python packaging and service commands;
- OMP RPC client integration research;
- extension protocol and capability research;
- browser tests and documentation.

The primary agent must personally integrate shared boundaries. Concurrent agents must not edit the same central files. In particular, serialize changes to the main Python service composition, normalized session model, route definitions, browser state model, packaging metadata, and final migration.

Every phase is a gate:

1. implement the complete phase;
2. run focused behavioral verification;
3. run affected contract tests;
4. review for obsolete code and callers;
5. proceed only when the phase is internally coherent.

Do not stop after scaffolding, partial adapters, placeholder methods, compatibility aliases, or narrowed tests. The final result must satisfy every completion criterion in this document.

### Protect the current deployment during development

Do not run `install.sh`, `omp-deck install`, production `systemctl` commands, or `tailscale serve` before final cutover.

Do not modify or delete these live resources during implementation:

- `~/.local/share/omp-deck`;
- `~/.local/bin/omp-deck`;
- `~/.config/systemd/user/omp-deck.service`;
- `~/.config/omp-deck`;
- `~/.local/state/omp-deck`;
- `~/.omp/agent/extensions/omp-deck.ts`;
- `~/.omp/agent/sessions`;
- `~/.omp/agent/blobs`;
- the existing Tailscale Serve configuration;
- port `8787`.

Use an isolated development runtime:

- bind only to `127.0.0.1` on a separate high port such as `8877`;
- use temporary `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME`;
- use a temporary `OMP_DECK_CONFIG`;
- configure separate state, socket, session, upload, and project roots;
- use a separate temporary OMP plugin/profile location;
- run the Python server in the foreground through the process supervisor;
- test generated service files without installing or starting them;
- never mutate Tailscale during isolated verification.

The current production installation runs from its copied deployment directory, so source edits do not require or justify early production installation.

### Cutover gate

Production cutover is the last operation. Before cutover:

1. all phases and clean CI pass;
2. package installation, upgrade, rollback, uninstall, and purge scenarios pass in isolation;
3. RPC and extension sessions pass behavioral tests;
4. authentication and project-boundary attacks are tested;
5. the isolated browser surface is visually verified;
6. migration preserves current configuration, cursors, and sessions;
7. a rollback procedure to the currently deployed version is prepared.

At cutover, install once, verify service health, verify the real phone surface through Tailscale, and roll back immediately if any completion criterion fails.

## Implementation order

### Phase 1: release and security blockers

1. Repair clean-checkout validation and run all tests.
2. Add clean CI.
3. Make configuration loading command-specific.
4. Make uninstall and purge path-safe.
5. Prevent project-root symlink escapes.
6. Add application authentication and device revocation.
7. Add browser security headers, CSRF/Origin protection, and blob `no-store`.
8. Remove sensitive browser persistence.
9. Correct Tailscale setup and trust documentation.

### Phase 2: packaging and service lifecycle

1. Convert the server to a standard Python package.
2. Package the extension through OMP's plugin manager.
3. Replace custom copying/updating with standard package tooling.
4. Add service install/status/restart/remove commands.
5. Add health verification and isolated lifecycle tests.
6. Declare systemd, Tailscale, stock OMP, optional tmux, and optional Collab relay requirements accurately.

### Phase 3: official RPC-managed sessions

1. Integrate the official Python `omp_rpc.RpcClient`.
2. Launch new sessions through `omp --mode rpc` by default.
3. Normalize RPC snapshots and events into Deck state.
4. Implement RPC process supervision and durable resume metadata.
5. Map prompt, follow-up, abort, shutdown, model, thinking, todos, subagents, and the complete extension UI request/response protocol.
6. Prove the built-in `ask` tool can be answered, cancelled, timed out, and resumed after browser reconnect without terminal interaction.
7. Add RPC compatibility tests against pinned OMP versions.

### Phase 4: terminal-session transports

1. Implement a versioned Python Collab guest adapter using published `@oh-my-pi/pi-wire` contracts.
2. Normalize Collab snapshots, entries, events, commands, subagents, reconnects, and UI requests into Deck state.
3. Protect Collab write links as server-side credentials and test read/write authorization boundaries.
4. Keep the extension as discovery plus degraded fallback when Collab is inactive.
5. Replace `/proc` discovery with extension registration and heartbeat.
6. Replace transcript polling with documented structured extension events where available.
7. Replace tmux interruption and shutdown with `ctx.abort()` and `ctx.shutdown()`.
8. Use managed extension timers and explicit capability negotiation.
9. Display terminal-local Ask as unavailable rather than attempting to answer it through the fallback extension.
10. Isolate any unavoidable JSONL fallback behind a tested compatibility adapter.

### Phase 5: live browser events and UI

1. Add the SSE snapshot/replay stream.
2. Replace one-second full polling with event-driven updates plus recovery polling.
3. Add the RPC/extension/disconnected status light beside the kebab.
4. Drive every control from capabilities.
5. Add the mobile pending-request and `ask` interaction surface.
6. Add browser behavior tests while preserving the current design.

### Phase 6: cleanup and publication

1. Remove obsolete `/proc`, tmux-control, transcript-reparse, and direct-extension-copy paths.
2. Remove stale tests, settings, documentation, and compatibility aliases.
3. Run clean package installation and lifecycle scenarios.
4. Verify the real phone surface through Tailscale.
5. Update README, security documentation, architecture, and changelog.
6. Publish only after a clean checkout passes the complete validation command.

## Completion criteria

OMP Deck is ready when a fresh supported machine can:

1. install from standard Python and OMP package mechanisms;
2. pair an authorized phone securely;
3. discover terminal sessions without `/proc`;
4. launch managed sessions through official OMP RPC;
5. stream structured state without reparsing complete transcripts;
6. send, queue, interrupt, and shut down through documented APIs;
7. answer the built-in OMP `ask` tool from the phone for RPC-managed and Collab-connected sessions, including after browser reconnect;
8. report extension-fallback Ask requests as terminal-only and leave the session awaiting local input without pretending they were remotely answered;
9. recover managed sessions after a service restart;
10. prevent unauthorized access and project-root escapes;
11. uninstall safely despite malformed or malicious configuration;
12. pass all Python, JavaScript, TypeScript, browser, package, and lifecycle checks from a clean checkout;
13. preserve the current mobile design;
14. remain clearly positioned as a private fleet control plane rather than a Collab replacement.
