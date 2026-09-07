# OMP Deck

OMP Deck is a local-first mobile control surface for one developer's [Oh My Pi](https://github.com/can1357/oh-my-pi) sessions. It shows managed and terminal-owned sessions, live conversation state, unread responses, todos, subagents, usage limits, and pending input requests. From the browser you can launch managed sessions, continue conversations, attach images, interrupt work, and use capabilities advertised by each session.

Deck is not a Collab replacement. It does not expose rooms, invite links, participant identities, public sharing, or multi-user arbitration. A writable Collab link may be imported as a private server-side transport credential for one terminal-owned session.

## Supported platform and versions

- Linux with a systemd user session for managed background service operation
- Native Windows 11 for foreground operation
- Python 3.11 or 3.12
- stock OMP 18.1.10 on `PATH` or in its official per-user install location
- Bun 1.3.14 or newer for Linux OMP extension installation and removal
- Tailscale only for the optional supported phone-access path

The official Python `omp-rpc` client and extension peer dependency are pinned to the OMP 18.1.10 source release. Other OMP versions are not claimed as supported until the RPC, extension, and Collab compatibility suites pass for them. Deck does not patch, fork, or replace OMP.

Tmux is not required. Deck does not ship a tmux keystroke controller or interactive-TUI launcher; managed launches use RPC and terminal-owned sessions use Collab or the OMP extension.

## Session transports

Python normalizes all transports into one session model. The browser uses advertised capabilities rather than guessing behavior from a backend name.

| Indicator | Ownership and behavior |
| --- | --- |
| Green, **RPC session** | Deck launches and supervises `omp --mode rpc`. Full managed controls, remote Ask responses, model/thinking controls, todos, and subagents are available when advertised. Image prompts are available on Linux. |
| Amber, **Terminal session via Collab** | A terminal-owned OMP session connected through an imported writable Collab link. Published pi-wire permissions and request settlement rules apply. The link remains server-side. |
| Amber, **Terminal session via extension** | On Linux, a terminal-owned session discovered through the bundled OMP plugin and private Unix socket. Only extension-advertised controls are enabled. Ask is visible but must be answered in the terminal. |
| Red, **Disconnected** | The transport is unavailable. Session state remains visible when persisted, but mutating controls are disabled. |

Capabilities can differ by transport, authorization, connection state, platform, and protocol version. Unavailable controls are disabled rather than silently emulated. Native Windows disables image attachments until OMP blob paths can be validated against Windows ACLs, and terminal-owned sessions require Collab because the Unix-socket extension is not installed there. Extension disconnects never abort the terminal session. Collab failures affect only the imported Collab session.

## Install

### Linux

Install through a standard isolated Python tool manager:

```bash
uv tool install 'git+https://github.com/marcelojmontanez/omp-deck.git'
# or
pipx install 'git+https://github.com/marcelojmontanez/omp-deck.git'
omp-deck setup
```

`setup` installs and validates the bundled plugin through `omp plugin`, generates and starts the systemd user service, checks `/healthz`, and prints a one-time pairing code. Enter the code on the browser pairing screen. It is exchanged by POST and is never placed in a URL or browser storage.

To configure the supported Tailscale Serve route during setup:

```bash
omp-deck setup --tailscale
```

Deck refuses to modify Tailscale when any Serve route already exists. Review and configure a distinct route manually instead. Tailscale changes may require device or tailnet administrator authorization; the systemd user service itself does not require root.

### Native Windows

Install from PowerShell with `uv tool` or `pipx`, then run Deck in the foreground:

```powershell
uv tool install 'git+https://github.com/marcelojmontanez/omp-deck.git'
omp-deck config validate
omp-deck serve
```

In another PowerShell window, run `omp-deck pair` to issue a browser pairing code. The defaults use `%APPDATA%\OMP Deck\config.json`, `%LOCALAPPDATA%\OMP Deck\state`, `%USERPROFILE%\dev`, and the official OMP executable at `%LOCALAPPDATA%\omp\omp.exe`. `service`, `plugin`, `migrate`, `purge`, and automatic `setup` are intentionally unavailable on Windows; use managed RPC sessions or Collab links and uninstall the Python tool with its tool manager. Configure Tailscale Serve separately when phone access is required.

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

## Configuration

Optional configuration lives at `~/.config/omp-deck/config.json` on Linux or `%APPDATA%\OMP Deck\config.json` on Windows. The example below shows Linux defaults; Windows uses the paths listed in the installation section.

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

Restart Deck after a configuration change with `omp-deck service restart` on Linux. On Windows, stop the foreground process with Ctrl+C and run `omp-deck serve` again.

## Pairing and remote access

Treat authenticated Deck access as terminal access to the host. The supported remote path is:

```text
Phone -> owner-restricted Tailscale HTTPS -> localhost OMP Deck
```

Both layers are required. Keep the listener on `127.0.0.1`, use Tailscale Serve rather than Funnel, restrict the endpoint with tailnet ACLs or grants, and retain Deck pairing. TLS, firewall reachability, or broad tailnet membership does not replace application authentication. Direct public exposure is unsupported.

Pairing uses a six-digit code that expires after one minute. The code is single-use, and the first failed submission invalidates it; run `omp-deck pair` again after a typo or failed attempt.

Every API, SSE, and blob request requires a paired device cookie. Mutations also require an exact trusted Origin and session-bound CSRF token. Devices can log out individually or revoke all paired devices from the session menu.

## Retained data

Server state defaults to `~/.local/state/omp-deck` on Linux and `%LOCALAPPDATA%\OMP Deck\state` on Windows:

- `registry.json`: stable Deck session identities and non-secret recovery metadata;
- `auth.json`: installation verifier and hashed device credentials;
- `cursors.json`: last-read message cursors;
- `collab.json`: protected Collab credentials while imported;
- `sockets/`: Linux extension registration metadata and Unix sockets.

Sensitive files are atomic and bounded. Linux enforces owner-only mode `0600`; Windows enforces a protected current-user ACL and uses named mutexes for cross-process locking. OMP transcripts and blobs remain under OMP's own directories. The browser keeps drafts, optimistic sends, object URLs, and rendered state in memory only; it does not persist transcripts, paths, identifiers, usage, Collab links, or credentials in Web Storage. Object URLs are revoked when their owning draft or session is removed.

Live browser state uses authenticated SSE with bounded replay. Low-frequency snapshot recovery replaces state when a cursor is stale. The compatibility JSONL reader is isolated and bounded; it is not a discovery mechanism, live event source, or control protocol.

## Legacy migration and rollback

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
plugin; Deck never injects code into an already-running OMP process.

The currently deployed copied-file version corresponds to source revision
`051410ee43e4d32fd60a9044a8c8873cf93a4365`. Before an authorized production
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
