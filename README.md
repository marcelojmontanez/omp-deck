# OMP Deck

OMP Deck is a local-first mobile control surface for one developer's [Oh My Pi](https://github.com/can1357/oh-my-pi) sessions. It shows managed and terminal-owned sessions, live conversation state, unread responses, todos, subagents, usage limits, and pending input requests. From the browser you can launch managed sessions, continue conversations, attach images, interrupt work, and use capabilities advertised by each session.

Deck is not a Collab replacement. It does not expose rooms, invite links, participant identities, public sharing, or multi-user arbitration. A writable Collab link may be imported as a private server-side transport credential for one terminal-owned session.

## Supported platform and versions

- Linux with a systemd user session, macOS with a logged-in graphical user session, or native Windows 11 with Task Scheduler, for managed background service operation
- Python 3.11 or 3.12
- stock OMP 18.1.10, 18.1.14, or 18.1.16 on `PATH` or in its official per-user install location
- Bun 1.3.14 or newer for Linux and macOS OMP extension installation and removal
- Tailscale only for the optional supported phone-access path

The official Python `omp-rpc` client remains pinned to the OMP 18.1.10 source release. Managed RPC recovery and the extension lifecycle are qualified against the three stock OMP versions listed above; other versions are not claimed as supported until the same compatibility checks pass. Deck does not patch, fork, or replace OMP.

Tmux is not required. Deck does not ship a tmux keystroke controller or interactive-TUI launcher; managed launches use RPC and terminal-owned sessions use Collab or the OMP extension.

## Session transports

Python normalizes all transports into one session model. The browser uses advertised capabilities rather than guessing behavior from a backend name.

| Indicator | Ownership and behavior |
| --- | --- |
| Green, **RPC session** | Deck launches and supervises `omp --mode rpc`. Full managed controls, remote Ask responses, model/thinking controls, todos, subagents, and image prompts are available when advertised. |
| Amber, **Terminal session via Collab** | A terminal-owned OMP session connected through an imported writable Collab link. Published pi-wire permissions and request settlement rules apply. The link remains server-side. Todo reporting is unavailable on this transport. |
| Amber, **Terminal session via extension** | A terminal-owned session discovered through the bundled OMP plugin and a private Unix socket on Linux/macOS or same-user named pipe on Windows. Only extension-advertised controls are enabled. Ask is visible but must be answered in the terminal. |
| Red, **Disconnected** | The transport is unavailable. Session state remains visible when persisted, but mutating controls are disabled. |

Capabilities can differ by transport, authorization, connection state, platform, and protocol version. Unavailable controls are disabled rather than silently emulated. Extension disconnects never abort the terminal session. Collab failures affect only the imported Collab session.

The extension fallback runs only in interactive terminal mode. RPC, JSON, and print-mode OMP processes do not publish fallback endpoints or appear as additional terminal sessions.

Extension shutdown follows OMP's deferred shutdown request: it may wait for OMP's next idle boundary rather than immediately exit an idle terminal. Deck does not inject terminal input or force-kill that session.

## Install

### Linux and macOS

Install through a standard isolated Python tool manager:

```bash
uv tool install 'git+https://github.com/marcelojmontanez/omp-deck.git'
# or
pipx install 'git+https://github.com/marcelojmontanez/omp-deck.git'
omp-deck setup
```

`setup` installs and validates the bundled plugin through `omp plugin`, generates and starts the per-user background service, checks `/healthz`, and prints a one-time pairing code. Linux uses a systemd user service; macOS uses a LaunchAgent; Windows uses a least-privilege Task Scheduler logon task. Enter the code on the browser pairing screen. It is exchanged by POST and is never placed in a URL or browser storage.

To configure the supported Tailscale Serve route during setup:

```bash
omp-deck setup --tailscale
```

Deck refuses to modify Tailscale when any Serve route already exists. Review and configure a distinct route manually instead. Tailscale changes may require device or tailnet administrator authorization; the per-user Deck service itself does not require root or elevation.

#### macOS service behavior

Use Python 3.11 or 3.12, not an older Apple-provided Python. Install the supported OMP version and Bun separately; Deck does not replace your OMP installation.

Setup installs `~/Library/LaunchAgents/com.omp-deck.agent.plist` in the current user's graphical login domain. Run it as your normal user, without `sudo`, while that user is logged into the Mac desktop. An SSH shell can manage the service while the graphical login exists. Headless SSH-only startup is not supported. The service starts at graphical login, restarts after failures, and stops at logout. A sleeping or powered-off Mac is not remotely available; Deck does not prevent sleep.

macOS retains the same XDG defaults as Linux: configuration at `~/.config/omp-deck/config.json`, installation ownership records at `~/.local/share/omp-deck`, and state at `~/.local/state/omp-deck`. The LaunchAgent preserves your configured XDG directories and OMP profile directory. After changing those environment variables, rerun `omp-deck setup`; a plain service restart retains the installed environment. `service status` reports whether the LaunchAgent is loaded; `doctor` also checks actual HTTP health.

Managed RPC, Collab, Unix-socket terminal discovery, image attachments, setup rollback, service removal, and purge use the same commands as Linux. Legacy migration remains Linux-only. Each Deck installation owns its machine's sessions; an optional coordinator provides a shared multi-machine dashboard.

### Native Windows

Install from PowerShell with `uv tool` or `pipx`, then run setup:

```powershell
uv tool install 'git+https://github.com/marcelojmontanez/omp-deck.git'
omp-deck setup
```

`setup` installs the OMP extension fallback, starts Deck through a per-user Task Scheduler logon task, validates service and plugin health, and prints a pairing code. The defaults use `%APPDATA%\OMP Deck\config.json`, `%LOCALAPPDATA%\OMP Deck\install`, `%LOCALAPPDATA%\OMP Deck\state`, `%USERPROFILE%\dev`, and the official OMP executable at `%LOCALAPPDATA%\omp\omp.exe`. Managed RPC, Collab, named-pipe terminal discovery, image attachments, `service`, `plugin`, `purge`, and automatic setup are supported. Legacy `migrate` remains Linux-only.

## Use

```bash
omp-deck config validate
omp-deck doctor
omp-deck pair
omp-deck service status
omp-deck service restart
omp-deck plugin install
omp-deck plugin remove
omp-deck serve
```

`serve` runs Deck in the foreground. In the browser:

- choose a configured project and select **Start** for a managed RPC session;
- open **Connect terminal via Collab** and paste a writable link to connect a terminal-owned session;
- use the session menu to compact, interrupt, shut down, or remove a terminal transport only when that capability is available;
- answer pending RPC or Collab input requests in the request panel; extension-fallback requests remain terminal-only.

Unread sessions appear first. Running subagents appear beneath their parent session, and supported todo lists retain their phase grouping. Completed runs that report an error remain marked as failed, with the error visible in the conversation.

Drafts and selected images stay with their session when you switch conversations. Thinking changes are sent in order per session and reconcile to OMP's reported setting. Drafts and images remain memory-only: reloading, logging out, or clearing local data discards them.

## Multi-machine coordinator

The optional coordinator is a separate Python process. It serves the shared dashboard and owns only the machine directory, viewing-device authentication, and the Ed25519 ticket-signing key. It does not store node transcripts, run OMP, proxy node traffic, or act as a central session/process lifecycle authority. After pairing with the coordinator, the browser connects directly to every enrolled Deck node for sessions, commands, images, usage, and SSE.

Run it on your PC for loopback testing, or on an independently available Linux host for regular use. No particular hosting provider, container runtime, or web server is required. Installing the Python package is enough for a coordinator-only host; do **not** run `omp-deck setup` there unless you also want a local Deck node.

### Run on your PC

Keep coordinator configuration and state separate from the local Deck installation:

```bash
install -d -m 700 ~/.config/omp-deck-coordinator ~/.local/state/omp-deck-coordinator
```

Create `~/.config/omp-deck-coordinator/config.json` with:

```json
{
  "host": "127.0.0.1",
  "port": 8790,
  "origin": "http://localhost:8790",
  "state_root": "~/.local/state/omp-deck-coordinator"
}
```

Then run:

```bash
chmod 600 ~/.config/omp-deck-coordinator/config.json
omp-deck coordinator serve --config ~/.config/omp-deck-coordinator/config.json
```

Open the exact configured `origin`. An origin is only a scheme, hostname, and optional port—no path, query, or fragment—and must be the browser-reachable address actually used by viewers. HTTP is accepted only for `localhost` or a loopback IP and is intended for same-machine testing. A phone cannot use your PC's `localhost`; for remote use, give the coordinator and every node their own browser-reachable HTTPS origin, normally through owner-restricted Tailscale Serve. Every viewing device must be able to reach every node independently, and the proxy must preserve the advertised hostname because nodes require the exact enrolled `Origin` and `Host`. Deck does not change routing or install TLS for the coordinator.

`serve` stays in the foreground and stops on Ctrl+C or SIGTERM. Run this same command under your chosen process supervisor when hosting permanently. The existing `omp-deck service` commands manage the local Deck node, not the coordinator.

### Enroll a machine and pair a viewer

On the coordinator host, register the address of an already-running Deck node. This example uses a separate local test node listening on port `8791`:

```bash
omp-deck coordinator machine add Desktop \
  --origin http://127.0.0.1:8791 \
  --out ~/.config/omp-deck-coordinator/desktop.json \
  --config ~/.config/omp-deck-coordinator/config.json
```

The output directory and bundle must remain private and owned by you. On POSIX, an existing output directory must be mode `0700`; the new bundle is mode `0600`. An existing output file is never overwritten. The bundle contains the machine identity, exact coordinator and machine origins, and the coordinator's **public** verification key—not its signing key—but installing it is an explicit authorization to trust that coordinator.

Transfer that bundle to the owning machine through a trusted channel such as SSH. On that machine, explicitly accept it:

```bash
omp-deck fleet join --file /path/to/desktop.json --config /path/to/node-config.json
omp-deck fleet status --config /path/to/node-config.json
```

Omit `--config` to use that node's normal Deck configuration. Keep the bundle owner-only, transfer it only through a trusted channel, and delete working copies after a successful join unless they are in a protected backup. On POSIX, the input file must have no group or world permissions and its parent must be owned by you and not group- or world-writable. The node's `allowed_hosts` must include the hostname in its advertised origin. Joining is live: no service restart or plugin reinstall is required. Repeat enrollment for your Mac or other computers. Replacing an existing trust relationship requires `fleet leave` first.

Back on the coordinator host, create a one-time code for each viewing device:

```bash
omp-deck coordinator pair --config ~/.config/omp-deck-coordinator/config.json
```

Enter it at the coordinator dashboard—not at an individual node. Successful pairing reloads the authenticated dashboard so its connection policy includes the enrolled origins. Additional tabs in the same paired browser reuse its cookie. Coordinator and local-node cookies have distinct names, so hosting both on one PC does not overwrite local pairing.

### Availability and revocation

- Each node remains authoritative for its sessions and OMP processes. Switching viewers does not move work between computers, and the coordinator cannot cancel node lifecycle work.
- An offline machine does not block access to other reachable machines. Its previous browser snapshot is stale presentation, not a claim that its OMP process stopped.
- A coordinator outage does not stop OMP or cancel work already accepted by a node. The browser can use already-issued tickets only until they expire, no later than 60 seconds after issue; it cannot renew access while the coordinator is unavailable. Each node's independent local pairing remains available. Keep all machine clocks synchronized.
- Coordinator device logout or revoke-all, and coordinator machine removal, prevent new tickets but cannot retract a ticket already issued to a browser. The maximum remaining authorization window is 60 seconds from ticket issue. An idle SSE connection rechecks authorization before delivering newly available events and closes after expiry.
- Adding a machine or changing its origin requires an already-open dashboard to reload before the browser policy permits that origin. Reloading clears browser-memory state: manually copy any unsent draft text and retain the original attachment files so you can reattach them afterward.

List or remove directory entries on the coordinator:

```bash
omp-deck coordinator machine list --config ~/.config/omp-deck-coordinator/config.json
omp-deck coordinator machine remove MACHINE_ID --config ~/.config/omp-deck-coordinator/config.json
```

To revoke coordinator access immediately at the owning node, remove its local trust:

```bash
omp-deck fleet leave --config /path/to/node-config.json
```

Omit `--config` to use the normal node configuration. The node reloads trust on every request, so this rejects even an otherwise-unexpired fleet ticket without a service restart; a live event stream closes before delivering another event after the failed check. Neither local leave nor coordinator removal deletes sessions or aborts OMP work.

### State, backup, and key-loss recovery

The coordinator state root contains `fleet-coordinator.json` (the private signing key and up to 32 machine directory records), `auth.json` (pairing state and hashed viewing-device credentials), and adjacent lock files. For fleet enrollment, each node adds `fleet-node.json` and its lock alongside its existing Deck state; that record pins the machine identity, exact origins, and coordinator public key. The browser sends its coordinator cookie only to the coordinator, sends direct node requests with a 60-second bearer ticket and no node cookies, and keeps the roster, tickets, drafts, attachments, and rendered transcripts in memory only.

For backup or host migration, stop the coordinator and its supervisor, then securely copy its owner-only configuration and the **complete** state directory. Restore both while the service is stopped, retain owner-only permissions, and keep the exact configured origin. Preserving `fleet-coordinator.json` preserves node trust; preserving `auth.json` preserves paired viewers. Start the restored service with the same command:

```bash
omp-deck coordinator serve --config ~/.config/omp-deck-coordinator/config.json
```

Do not edit, partially reconstruct, or generate a replacement key inside either state file. If the signing key or configured origin cannot be restored, select a new empty owner-only state root in the coordinator configuration and explicitly re-enroll every node:

```bash
# Coordinator host: create a fresh bundle for each node.
omp-deck coordinator machine add Desktop \
  --origin https://desktop.example \
  --out ~/.config/omp-deck-coordinator/desktop-recovery.json \
  --config ~/.config/omp-deck-coordinator/config.json

# Owning node: remove the old public-key trust, then install the fresh bundle.
omp-deck fleet leave --config /path/to/node-config.json
omp-deck fleet join \
  --file /path/to/desktop-recovery.json \
  --config /path/to/node-config.json

# Coordinator host: pair each viewing device again if auth.json was not restored.
omp-deck coordinator pair --config ~/.config/omp-deck-coordinator/config.json
```

Reload the dashboard after rebuilding the directory, preserving unsent drafts and attachments manually first. Deleting a bundle after a successful join is safe because the node retains its installed trust record. If that node record is later lost, remove the machine's old coordinator entry, add it again to create a new bundle, then run `fleet join`; an existing enrollment cannot be re-exported. If an enrollment export fails and rollback also fails, remove the resulting directory entry before enrolling again. Local Deck `purge` does not administer a separately configured coordinator; stop its supervisor and remove only the deliberately chosen coordinator configuration and state when retiring it.

## Configuration

Optional configuration lives at `~/.config/omp-deck/config.json` on Linux/macOS or `%APPDATA%\OMP Deck\config.json` on Windows. The example below shows Unix defaults; Windows uses the paths listed in the installation section.

```json
{
  "host": "127.0.0.1",
  "port": 8787,
  "project_root": "~/dev",
  "project_discovery_depth": 2,
  "session_root": "~/.omp/agent/sessions",
  "socket_root": "~/.local/state/omp-deck/sockets",
  "state_root": "~/.local/state/omp-deck",
  "omp_bin": "~/.local/bin/omp",
  "allowed_hosts": []
}
```

Project names are resolved beneath `project_root`; traversal, symlink escapes, broken links, and non-directories are rejected. `project_discovery_depth` limits filesystem traversal to 1–8 directory levels and defaults to `2`: direct children and grandchildren are listed, while configured depths beyond two discover only directories containing `.git`, `AGENTS.md`, `Cargo.toml`, `go.mod`, `package.json`, or `pyproject.toml`. Keep `project_root` near the common parent of your projects and increase the depth only when nested repositories must appear.

Restart Deck after a configuration change with `omp-deck service restart`.

On Linux/macOS, the extension and Deck must share the same `XDG_STATE_HOME`; the extension publishes under `omp-deck/sockets` within that directory. A custom `socket_root` must point there. The complete Unix socket pathname must fit within 103 UTF-8 bytes on macOS or 107 on Linux. An oversized path leaves the extension unavailable with an explicit warning; use a shorter shared state directory.

When upgrading from Deck 1.1.x, rerun `omp-deck setup` and restart terminal-owned OMP sessions to load the new plugin. Older Unix endpoint names are no longer discovered.

## Pairing and remote access

Treat authenticated Deck access as terminal access to the host. The supported remote path is:

```text
Phone -> owner-restricted Tailscale HTTPS -> localhost OMP Deck
```

Both layers are required. Keep the listener on `127.0.0.1`, use Tailscale Serve rather than Funnel, restrict the endpoint with tailnet ACLs or grants, and retain Deck pairing. TLS, firewall reachability, or broad tailnet membership does not replace application authentication. Direct public exposure is unsupported.

Pairing uses a six-digit code that expires after one minute. The code is single-use, and the first failed submission invalidates it; run `omp-deck pair` again after a typo or failed attempt.

Every private API, SSE, and blob request requires a paired device cookie, or an explicitly enrolled coordinator's short-lived machine-specific access ticket. Cookie-authenticated mutations require an exact trusted Origin and session-bound CSRF token; fleet tickets require the exact pinned coordinator Origin and never use ambient node cookies. Devices can log out individually or revoke all paired devices from the session menu.

## Retained data

Server state defaults to `~/.local/state/omp-deck` on Linux/macOS and `%LOCALAPPDATA%\OMP Deck\state` on Windows:

- `registry.json`: stable Deck session identities and non-secret recovery metadata;
- `auth.json`: installation verifier and hashed device credentials;
- `cursors.json`: last-read message cursors;
- `collab.json`: protected Collab credentials while imported;
- `sockets/`: extension registration metadata plus Unix sockets on Linux/macOS or same-user named-pipe endpoints on Windows.

Sensitive files are atomic and bounded. Linux/macOS enforce owner-only mode `0600`; Windows enforces a protected current-user ACL and uses named mutexes for cross-process locking. OMP transcripts and blobs remain under OMP's own directories. The browser keeps drafts, optimistic sends, object URLs, and rendered state in memory only; it does not persist transcripts, paths, identifiers, usage, Collab links, or credentials in Web Storage. Object URLs are revoked when their owning draft or session is removed.

Live browser state uses authenticated SSE with bounded replay. Low-frequency snapshot recovery replaces state when a cursor is stale. The compatibility JSONL reader is isolated and bounded; it is not a discovery mechanism, live event source, or control protocol.

Dashboard assets are served without browser caching so an upgrade or rollback replaces the previous release's scripts and styles.

## Legacy migration and rollback

Legacy migration is available on Linux only.

`migrate` is read-only unless `--apply` is supplied:

```bash
omp-deck migrate
omp-deck migrate --apply
```

The plan reports only counts and safety state; it does not print session paths
or cursor values. Apply imports securely owned legacy extension registrations
as disconnected stable terminal sessions, maps accepted legacy read cursors to
those stable IDs, and marks only a validated Deck state root as owned. Legacy
cursor files that are symlinks, world-writable, unowned, oversized, or malformed
are rejected and reported instead of being trusted. Accepted legacy values are
rewritten into a mode-`0600` normalized cursor store.
A migrated terminal session reconnects after that OMP process loads the new
plugin. Wait until the session is idle, then restart OMP and resume it normally.
`/reload-plugins` does not reload extension modules in OMP 18.1.14 or 18.1.16;
Deck never injects code or keystrokes into an already-running OMP process.

For a copied-file legacy installation, before an authorized production
cutover, stop the old service and place these artifacts in a mode-`0700`
backup directory outside `~/.local/state/omp-deck`:

- `~/.local/share/omp-deck`;
- `~/.local/bin/omp-deck`;
- `~/.config/systemd/user/omp-deck.service`;
- `~/.config/omp-deck`;
- `~/.local/state/omp-deck`; and
- `~/.omp/agent/extensions/omp-deck.ts`.

After the backup, move the old install root, command, service unit, and direct
extension out of their live paths. Keep configuration and state in place, run
`omp-deck migrate --apply`, then run `omp-deck setup`. This ordering prevents
the new ownership manifest from claiming an unverified copied installation.

Rollback disables the new service, removes the package-managed plugin through
`omp plugin uninstall omp-deck`, moves the failed new state aside, restores all
six artifacts from the backup with their original permissions, runs
`systemctl --user daemon-reload`, and re-enables the restored service. Do not
delete the backup until the phone surface, existing terminal sessions, managed
RPC launch, unread state, and plugin/service health have been verified.

## Service, update, and removal

```bash
omp-deck service install
omp-deck service status
omp-deck service restart
omp-deck service remove
```

Upgrade with the same tool manager, then run `omp-deck setup`. Setup snapshots
the active OMP plugin and service unit before replacement, validates the new
service twice, and restores those components if any later setup stage fails.
When `--tailscale` creates a new Serve route, a later failure removes that route
without changing configuration that existed before setup.
The Python environment remains owned by `uv` or `pipx`; record the installed
version before upgrading so that package-manager rollback remains explicit:

```bash
omp-deck config validate
uv tool upgrade omp-deck
# or: pipx upgrade omp-deck
omp-deck setup

# If setup reports rollback and the Python package must also be restored:
uv tool install --force 'git+https://github.com/marcelojmontanez/omp-deck.git@PREVIOUS_REVISION'
# or: pipx install --force 'git+https://github.com/marcelojmontanez/omp-deck.git@PREVIOUS_REVISION'
```

`service remove` removes only Deck's generated user service. `purge` removes the service, official plugin registration, configuration, and state only after strict ownership and path checks:

```bash
omp-deck purge
uv tool uninstall omp-deck
# or: pipx uninstall omp-deck
```

## Architecture and limitations

OMP owns agent and tool lifecycles. Each transport adapter owns protocol translation and request correlation. Python owns process supervision, normalized fleet state, authentication, persistence, unread cursors, HTTP actions, and SSE replay. The dependency-free browser owns only ephemeral presentation and user interaction.

Managed RPC sessions are recoverable only when intentionally left running and when OMP's recorded session identity remains valid. Collab availability depends on its relay and the imported link's permissions. Extension fallback is intentionally smaller than RPC and cannot answer Ask remotely. JSONL compatibility recovery does not authorize control. Deck remains single-owner and local-first.

See [SECURITY.md](SECURITY.md) for the trust boundary and private vulnerability reporting process.
