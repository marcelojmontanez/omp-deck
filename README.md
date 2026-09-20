# OMP Deck

OMP Deck is a local-first mobile control surface for one developer's [Oh My Pi](https://github.com/can1357/oh-my-pi) sessions. It focuses on ongoing work: live conversation state, unread responses, todos, subagents, usage limits, and pending input requests. History is hidden by default and can be enabled in configuration without deleting or migrating saved conversations. From the browser you can launch managed sessions, continue conversations, attach images, interrupt work, and use capabilities advertised by each session.

Deck is not a Collab replacement. It does not expose rooms, invite links, participant identities, public sharing, or multi-user arbitration. A writable Collab link may be imported as a private server-side transport credential for one terminal-owned session.

## Supported platform and versions

- Linux with a systemd user session, macOS with a logged-in graphical user session, or native Windows 11 with Task Scheduler, for managed background service operation
- Python 3.11 or 3.12
- stock OMP on `PATH` or in its official per-user install location; compatibility is checked through its documented protocols and advertised capabilities, not an exact-version allowlist
- Bun 1.3.14 or newer for Linux and macOS OMP extension installation and removal
- Tailscale only for the optional supported phone-access path

Keep updating OMP normally. A new version number does not block Deck or make doctor fail. The official Python `omp-rpc` client remains pinned to a reviewed upstream source revision; native OMP updates do not silently replace that dependency. RPC startup rejects incompatible protocol versions, and terminal adapters validate their wire format and advertised capabilities. A genuine connection or command failure is reported for the affected session rather than disabling every session.

Doctor shows installed OMP and already-running extension versions separately. An unreadable version is an optional diagnostic warning, not proof of incompatibility. Updating the executable does not replace code already loaded in a terminal process: restart and resume it when idle to load the new executable or extension. Doctor only reads validated extension metadata; it does not connect to or restart those sessions. Deck does not patch, fork, or replace OMP.

Native 18.2.1 image delivery, todos, recovery, and shutdown were exercised on macOS; 18.2.4 additionally passed isolated RPC startup, identity-preserving recovery, and independent shutdown. These are observations, not a permanent support list or proof of every future feature. The latest-release CI workflow checks native RPC startup/recovery/shutdown on Linux and macOS every six hours and on main-branch pushes; it becomes active when the workflow is published to GitHub. Windows retains its separate pinned native gate.

Tmux is not required. Deck does not ship a tmux keystroke controller or interactive-TUI launcher; managed launches use RPC and terminal-owned sessions use Collab or the OMP extension.

## Session transports

Python normalizes all transports into one session model. The browser uses advertised capabilities rather than guessing behavior from a backend name.

| Indicator | Ownership and behavior |
| --- | --- |
| **RPC session** | Deck launches and supervises `omp --mode rpc`. Full managed controls, remote Ask responses, model/thinking controls, todos, subagents, and image prompts are available when advertised. |
| **Terminal session via Collab** | A terminal-owned OMP session connected through an imported writable Collab link. Published pi-wire permissions and request settlement rules apply. The link remains server-side. Todo reporting is unavailable on this transport. |
| **Terminal session via extension** | A terminal-owned session discovered through the bundled OMP plugin and a private Unix socket on Linux/macOS or same-user named pipe on Windows. Only extension-advertised controls are enabled. Ask is visible but must be answered in the terminal. |
| **Disconnected** | The transport is unavailable. Managed-session shutdown can still cancel automatic recovery; other controls require their advertised availability. Retained records remain available in History when there is no live owner or managed recovery intent. |

Capabilities can differ by transport, authorization, connection state, platform, and protocol version. Unavailable controls are disabled rather than silently emulated. Extension disconnects never abort the terminal session. Collab failures affect only the imported Collab session.

Deck accepts HTTP requests while managed and imported terminal connections recover in the background. A healthy service does not mean every OMP session is connected: each session reports its own connection state, and slow recovery does not block the dashboard.

Managed child exits and failed RPC readers are supervised even while idle. Sessions whose managed-running intent is still enabled retry recovery; explicit shutdown cancels that intent. Imported Collab sessions also keep retrying after a startup relay outage. Live RPC messages update incrementally, with authoritative snapshots on recovery and native turn completion rather than full-fleet transcript polling.

The extension fallback runs only in interactive terminal mode. RPC, JSON, and print-mode OMP processes do not publish fallback endpoints or appear as additional terminal sessions.

Quit requests OMP's native shutdown; acceptance is not proof that the process has exited. OMP controls graceful-exit timing, which can differ between supported versions. Deck moves an unattached record to History rather than deleting it, and never injects terminal input or force-kills a terminal-owned session.

The green session connection indicator means the session transport is connected, regardless of transport type. The separate **Deck connected** label describes the browser's dashboard connection, not the health of every OMP session.

Large tool arguments, tool results, and provider-only message content are not sent as conversation bubbles. The extension preserves conversational text, supported image references, and explicit errors; unavailable images remain visible as placeholders. Wire and retained-history limits still apply.

An older extension already loaded in a terminal may disconnect while synchronizing a large history. Deck makes one control-only reconnect after a verified handshake, keeps that session in Ongoing, and displays a history warning without treating its control channel as disconnected. It does not repeatedly request the failed history while that connection remains healthy. Updating Deck does not replace extension code inside running OMP processes: wait until idle, then restart OMP and resume the session to load the update. Deck does not remotely restart or interrupt ongoing terminal work.

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

The registered extension lives outside the Python tool environment: `$XDG_DATA_HOME/omp-deck-plugin` (default `~/.local/share/omp-deck-plugin`) on Linux/macOS, or `%LOCALAPPDATA%\OMP Deck\plugin` on Windows. Setup retains the previous owned source until replacement succeeds. Linux service generation also pins the resolved OMP executable; rerun setup after moving or replacing that executable path.

To configure the supported Tailscale Serve route during setup:

```bash
omp-deck setup --tailscale
```

Deck refuses to modify Tailscale when any Serve route already exists. Review and configure a distinct route manually instead. Tailscale changes may require device or tailnet administrator authorization; the per-user Deck service itself does not require root or elevation.

#### macOS service behavior

Use Python 3.11 or 3.12, not an older Apple-provided Python. Install OMP and Bun separately; Deck does not replace your OMP installation.

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
- open **Deck settings → Connect a terminal** and paste a writable Collab link to connect a terminal-owned session on the selected machine;
- use the session menu to compact, interrupt, shut down, or remove a terminal transport only when that capability is available;
- rename or reset a session’s Deck display name and pin or unpin it from the session menu, independently of whether its OMP transport supports native renaming;
- answer pending RPC or Collab input requests in the request panel; extension-fallback requests remain terminal-only.

**Machine for new sessions and usage** chooses where to start a new OMP, connect a writable Collab link, and read usage. It is not a session filter: the list still includes every enrolled machine. Each session's machine label identifies its actual owner, independently of this selector.

**Ongoing** is the default view. It includes sessions with a current transport owner and managed sessions whose recovery is still requested. **Needs attention** includes pending input, failures, unavailable machines, connection recovery, and incomplete history. Summary counts and filters apply to ongoing sessions, not old records.

When History is enabled, matching session names are distinguished as **Live session** or **Saved conversation**. Numbering appears only when the same machine and project have multiple matching sessions of the same kind. These labels are hidden when History is disabled.

**History** is hidden by default. Set `"show_history": true` in the configuration of the service whose dashboard you open to restore it. History contains retained records without a current attachment or managed recovery intent. Conversations are read-only; their Deck names and pins remain editable. Hiding History only changes presentation: records and transcripts remain available to the authenticated API, so this is not an access-control or deletion setting. A restored live attachment returns a record to Ongoing. Machine outages preserve the browser's last-known grouping rather than reclassifying every session as history.

Unread sessions appear first unless pinned sessions are present; pinned sessions stay ahead within the current Ongoing/History view and active filter, while the existing unread, attention, status, and name ordering remains unchanged within each group. Only meaningful assistant content creates unread state. Tool-only, thinking-only, and whitespace-only records do not create empty conversation bubbles. Image-only replies and explicit errors remain visible. Running subagents appear beneath their parent session, and supported todo lists retain their phase grouping. Completed runs that report an error remain marked as failed.

Session tiles show the effective session display name and explicitly label its project and machine. Sessions with otherwise identical machine, project, and name labels receive a deterministic ordinal that stays stable when pinning, activity, or filters change. A custom Deck name does not rename the OMP conversation; resetting it reveals OMP’s current native/default title. Custom names and pins are stored in the owning node’s server registry, survive browser and service restarts, and reconcile through normal session state updates without browser storage.

The conversation toolbar stays compact: provider-qualified model, effective thinking level, tasks, connection light, and session menu. Use browser Back or **All sessions** in that menu to return to the list. **Session details** shows the effective name, native/default name when customized, project directory, machine, and availability details. Links to saved conversations open only when History is enabled; otherwise the browser stays with ongoing sessions. A direct connection identifies the machine by its Deck address; the multi-machine coordinator uses enrolled machine names.

The session light is green for connected managed RPC, amber for connected terminal transports, red for an unavailable ongoing connection, and neutral for history. Its accessible label and **Session details** identify the connection without relying on color. Actual machine, transport, and history failures remain visible. **Deck settings**, available from the dashboard or session menu, holds terminal connection setup, local-draft clearing, logout, and device revocation.

Unsent drafts and selected images stay with their session when you switch conversations. By default, the owning Deck machine saves them automatically for recovery after a browser reload or Deck restart, shared with your other authenticated devices.

Nothing is stored in browser storage or sent to OMP automatically. Saving is delayed briefly while you edit; leaving before a save finishes can lose the latest changes. Save failures remain visible. A conflicting device edit preserves your local draft and offers **Restore saved draft** or **Keep local and overwrite**.

Saved drafts expire seven days after their last save; expired files are reclaimed on the next draft access. Set `"save_drafts": false` in the owning node's configuration to keep drafts browser-only. This prevents new saves and deletes each previously saved draft when that session's draft is next accessed. Logout and **Clear browser-only drafts** do not delete server copies, which can restore later. Removing a session also removes its saved draft, with cleanup failures reported explicitly.

Send shows a greyed-out message preview immediately while saved-draft clearance and submission are pending. This is local feedback, not delivery proof. An enabled saved draft must still be cleared successfully before delivery proceeds; a failed clearance removes the preview and preserves the local draft. Submitted messages are separate: once the owning node saves the request, its private recovery receipt can survive a browser reload or Deck restart.

Send waits for images already selected in the file picker to finish loading, preserves their selection order, and reports read failures without sending a partial attachment set. Switching sessions does not move those images to another draft. Clearing local drafts also clears the visible composer and cancels pending image reads.

Use the image button to open the browser's native photo picker, or paste copied images into the message field. Tap a thumbnail to preview the whole image; its remove button stays beside it. Cancelling or reopening the picker preserves your draft. Images can be sent without text. Each message accepts up to four images, totaling **20 MiB** for managed RPC and extension 1.3.4 or later, or **10 MiB** for writable Collab. Deck submits the original files; OMP applies its own image processing for the selected model.

Extensions older than 1.3.4 retain their **700 KiB** total limit, with an explanation in the composer. Restart OMP and resume the session when idle to load the installed extension. Installing the update or running `/reload-plugins` does not replace extension code inside a running process.

Extension **1.3.5 or later** also makes supported inline images in terminal conversations previewable, using OMP's private image storage. After upgrading, restart and resume existing terminal sessions when idle so their retained history can be replayed with image references. Refreshing the dashboard alone cannot recover image references omitted by an older running extension.

Extension **1.3.6** restores running and recent background-job visibility and rejects delayed commands after the terminal changes conversations. Existing terminal sessions need an idle restart and resume to load these changes.

The composer stays below the conversation; its text field grows to at most 130px. Enter inserts a newline on phones; desktop Enter sends, and Shift+Enter inserts a newline. Sending keeps the text field focused and lets you write the next draft immediately. Delayed acceptance does not erase newer edits, and a delivery failure restores the unsent content without pulling focus from another control. History has a read-only notice instead of disabled composer controls.

On capable working sessions, choose **Steer** to send at the next native interruption point during the current run, or **Queue** to wait until it finishes. The choice is remembered per session for this browser tab. Idle sessions use **Send**; a queued message that reaches OMP after the run finishes starts normally instead of becoming stranded. An uncertain retry preserves its original delivery mode and request identity even if you change the current choice.

**Accepted** means the native transport accepted the request, not that OMP displayed, consumed, or answered it. Successful sends show a compact status, not a duplicate receipt card, and need no manual cleanup. **Delivery uncertain** means the response was lost or the outcome could not be established. Failed or uncertain sends retain recovery controls; matching conversation text is not delivery proof. Check the conversation before retrying. An unchanged uncertain retry keeps its request identity while the node retains its deduplication record. Reconnecting never resends automatically. **Restore draft** copies an unresolved receipt into the current draft without sending; **Dismiss receipt** removes its recovery copy without immediately removing its deduplication record. Recovery content remains private on the owning node for up to seven days; older fingerprint-only records cannot recover missing content.

The Accepted indicator clears after three seconds, or immediately when you edit a draft, change attachments, or switch sessions. Earlier acknowledgements and recovered successes do not mark a newer draft as accepted. The success timer never clears a newer send's progress or failure warning.

OMP can report a prompt failure after initial acceptance. Deck displays a session-level warning when the official RPC client reports such a failure. That client does not expose the successful native request ID, so Deck cannot reliably associate the later failure with an individual receipt or durably change that receipt's outcome. The warning survives browser reconnects while Deck remains running, but not a Deck service restart. Check the native conversation before retrying; Deck does not guess receipt identity from message text or internal SDK counters.

Each owning-node ledger retains at most 1,000 active request records and 1,000 compact deduplication records, with a 4 MiB metadata limit and a separate 128 MiB recovery-content limit including base64 images. Accepted recovery content is reclaimed first when space is needed. Dismissed or expired requests retain their identity and outcome in the compact records until that bounded history evicts them; deduplication is not permanent. Unresolved recovery content is not discarded to admit new requests before its seven-day retention expires. Native submissions reserve completion capacity before dispatch and cannot be dismissed while still running; a crashed unfinished submission becomes uncertain rather than being sent again automatically.

Dismissing a receipt does not cancel OMP or delete conversation history. If an uncertain request stays unresolved, check OMP first. Restoring its content, dismissing its receipt, and then sending creates a new request and can duplicate work if the original actually arrived.

Conversation images load as you scroll, using a bounded in-memory cache rather than a limit on how many images you can view. Off-screen images may be unloaded and fetched again when revisited; an open enlarged preview is protected from cache eviction. If several visible images exceed the cache budget, tap **Load image** to prioritize the one you want.

Image downloads and delivery-recovery reads have a two-minute deadline, including the response body. Image uploads also have a bounded two-minute body allowance; normal authorization and connection limits still apply. A failed thumbnail offers **Retry**; returning to the browser or reconnecting also retries needed images. Images still held in memory remain previewable during a temporary outage; unloaded images need a connection to fetch again. The local photo picker and supported draft attachments remain available offline, while sending waits for reconnection.

Routine machine-access renewal keeps the composer available. Stale requests cannot override newer access checks, and an incidental failed refresh does not declare a healthy event stream unavailable. During a genuine outage, Send remains unavailable but the draft stays editable and retains focus and text selection. Browser-offline status is distinct from machine availability. Reconnection never submits a draft automatically.

Assistant replies can display an existing private OMP image blob with a standalone Markdown line, `![Portrait](blob:sha256:<64 lowercase hex digest>)`. The image must already exist in that machine's active OMP blob store. Deck uses its authenticated image viewer; local file paths and external image URLs are not loaded. References inside code blocks or user text remain text.

Type `/` to pick an available structured session action: `/compact`, `/interrupt`, `/quit`, or `/thinking <level>` using OMP's advertised choices. Arrow keys and Tab select a suggestion; Escape dismisses it. An exact action changes **Send** to **Run**. Compact and quit require confirmation. Cancelled or failed actions preserve the command draft, and delayed completion does not erase newer edits. Actions cannot be combined with image attachments. This is not a terminal command console and does not emulate unsupported OMP commands.

Choose **Start fresh session…** in the session menu to open a separate, empty Deck-managed session on the same machine, using the source project's directory, provider, model, and thinking level. Confirmation keeps the original session, conversation, and text/image draft intact. Starting from a terminal-owned session does not replace, restart, or send commands to that terminal.

The action requires known source settings and an existing canonical project directory beneath the node's configured `project_root`; symlink aliases and paths outside that root are rejected. The new process uses Deck's normal OMP configuration and credentials, not a clone of the terminal's environment or custom launch flags. Deck checks the new native identity, empty conversation, and exact model/thinking settings before accepting; unavailable settings fail rather than silently using defaults. Retry reuses the request identity, including after a service restart, within the ledger's bounded retention. If you navigate elsewhere while creation is pending, the new session appears without taking focus.

Compact, Interrupt, and Quit show pending and accepted state for the selected session without disabling other sessions. If an action response or body stalls for eight seconds, Deck reports uncertainty; retrying checks the same request identity. A browser timeout does not cancel native work. Task progress from native todo-tool results updates during the run rather than waiting for a later reminder.

The **thinking** control shows OMP's effective level. Tap it to cycle through the current model's supported choices when control is available. Missing state is shown as unavailable, not assumed to be `auto`. Changes are sent in order per session and reconcile to OMP's reported level. With the updated terminal extension, changes made directly in OMP also reach Deck on the next heartbeat, normally within 15 seconds. Already-running terminals need an idle restart and resume to load the updated extension.

## Multi-machine coordinator

The optional coordinator is a separate Python process. It serves the shared dashboard and owns only the machine directory, viewing-device authentication, and the Ed25519 ticket-signing key. It does not store node transcripts, run OMP, proxy node traffic, or act as a central session/process lifecycle authority. After signing in to the coordinator, the browser connects directly to every enrolled Deck node for sessions, commands, images, usage, and SSE.

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

With the default pairing authentication, create a one-time code on the coordinator host for each viewing device:

```bash
omp-deck coordinator pair --config ~/.config/omp-deck-coordinator/config.json
```

Enter it at the coordinator dashboard—not at an individual node. Successful pairing reloads the authenticated dashboard so its connection policy includes the enrolled origins. Additional tabs in the same paired browser reuse its cookie. Coordinator and local-node cookies have distinct names, so hosting both on one PC does not overwrite local pairing.

Alternatively, configure [Tailscale-account sign-in](#optional-tailscale-account-sign-in) on the coordinator to authorize your own Tailscale account without pairing codes. This does not change node authentication or enrollment.

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
  "show_history": false,
  "save_drafts": true,
  "session_root": "~/.omp/agent/sessions",
  "socket_root": "~/.local/state/omp-deck/sockets",
  "state_root": "~/.local/state/omp-deck",
  "omp_bin": "~/.local/bin/omp",
  "allowed_hosts": []
}
```

`show_history` accepts only JSON `true` or `false`; omitting it means `false`. For the shared dashboard, set it in the coordinator's configuration, not the individual machines' files. Restart the service whose configuration changed, then refresh its dashboard. Save unsent drafts before refreshing. No OMP restart is needed for this presentation setting.

`save_drafts` accepts only JSON `true` or `false`; omitting it means `true`. Configure it on each node, not the coordinator. Restart the node's Deck service and refresh the dashboard after changing it; terminal-owned OMP sessions do not need restarting. With saving disabled, reload, logout, or **Clear browser-only drafts** discards the browser's unsent text and images.

Project names are resolved beneath `project_root`; traversal, symlink escapes, broken links, and non-directories are rejected. `project_discovery_depth` limits filesystem traversal to 1–8 directory levels and defaults to `2`: direct children and grandchildren are listed, while configured depths beyond two discover only directories containing `.git`, `AGENTS.md`, `Cargo.toml`, `go.mod`, `package.json`, or `pyproject.toml`. Keep `project_root` near the common parent of your projects and increase the depth only when nested repositories must appear.
Use real directories for the project root and its user-controlled ancestors, not symlink aliases. Empty XDG directory variables use the normal home-directory defaults rather than the current working directory.

To create a project inside an existing directory, choose **New directory in active/…** (or the corresponding parent) in the project dropdown, enter the child directory name, and choose **Create & start**. The root-level action still creates directly beneath `project_root`. Parent directories must already exist. Discovery depth limits the picker scan, not which safe paths may be launched: a directory just created below that depth remains selected until the picker is reloaded. If OMP fails to start after creation, the directory is preserved and selected as an existing project so **Start** can retry without creating it again.

Restart Deck after a configuration change with `omp-deck service restart`.

On Linux/macOS, generated services preserve the configured XDG directories, `PI_CODING_AGENT_DIR`, and the `OMP_PROFILE`/`PI_PROFILE` selectors. After changing these environment variables, rerun `omp-deck setup`; restarting alone retains the installed environment. Image blobs follow OMP's active profile and existing XDG data layout, so OMP and Deck must use matching profile settings. A configured `omp_bin` beginning with `~` expands to your home directory; a bare command name is resolved through `PATH`.

On Linux/macOS, the extension and Deck must share the same `XDG_STATE_HOME`; the extension publishes under `omp-deck/sockets` within that directory. A custom `socket_root` must point there. The complete Unix socket pathname must fit within 103 UTF-8 bytes on macOS or 107 on Linux. An oversized path leaves the extension unavailable with an explicit warning; use a shorter shared state directory.

When upgrading from Deck 1.1.x, rerun `omp-deck setup` and restart terminal-owned OMP sessions to load the new plugin. Older Unix endpoint names are no longer discovered.

## Pairing and remote access

Treat authenticated Deck access as terminal access to the host. The supported remote path is:

```text
Phone -> owner-restricted Tailscale HTTPS -> localhost OMP Deck
```

Keep the listener on `127.0.0.1`, use Tailscale Serve rather than Funnel, and restrict the endpoint with tailnet ACLs or grants. Pairing is the default application authentication; the optional Tailscale-account mode below replaces the code, not authentication. TLS, firewall reachability, or broad tailnet membership alone does not authorize Deck access. Direct public exposure is unsupported.

Pairing uses a six-digit code that expires after one minute. The code is single-use, and the first failed submission invalidates it; run `omp-deck pair` again after a typo or failed attempt.

Paired-device cookies persist for the existing 90-day server-side lifetime; checking a session does not extend that expiry. A still-valid cookie from an older version becomes persistent on the next successful connection without a new code. Expired, revoked, or already-lost cookies still require pairing. Private browsing or settings that delete site data can remove even a persistent cookie.

A failed connection check shows **Retry connection**, not the PIN form. In an already loaded writable session, you can keep editing drafts and adding supported attachments while the machine or session connection is unavailable; **Send** stays disabled until it reconnects. Reconnection does not send your draft automatically. Returning to a tab or restoring connectivity replaces stale reads and event streams while preserving in-memory drafts and attachments; reconnecting does not replay accepted actions or actions with an uncertain outcome. Drafts are not persisted across a full page reload or browser-process loss.

Browser connection checks, session/project reads, and event-stream handshakes have an eight-second deadline; JSON reads include the response body. A cancelled or timed-out read stops blocking recovery even if the browser's suspended network operation has not settled. The deadline does not limit a healthy event stream or cancel an OMP operation. Returning to the browser through visibility, focus, or supported page-resume events replaces stale connections and retries failed project loading without replacing a usable project selection. Connection-state updates preserve composer focus and text selection.

Every private API, SSE, and blob request requires an authenticated device cookie, or an explicitly enrolled coordinator's short-lived machine-specific access ticket. Cookie-authenticated mutations require an exact trusted Origin and session-bound CSRF token; fleet tickets require the exact pinned coordinator Origin and never use ambient node cookies. Devices can log out individually or revoke all browser sessions from the session menu.

### Optional Tailscale-account sign-in

To skip pairing codes, add these fields to the configuration of the **service being opened**: the node configuration for standalone use, or the separate coordinator configuration for a shared dashboard:

```json
{
  "auth_mode": "tailscale",
  "tailscale_user": "you@example.com"
}
```

Use your exact Tailscale account login, not a device name or display name. Keep the other configuration fields, including `"host": "127.0.0.1"`, and restart that service. For a coordinator, restart its own supervisor—not `omp-deck service`, which manages the node. Changing coordinator authentication does not require restarting nodes or OMP sessions.

This mode requires **Tailscale Serve directly proxying to Deck's loopback listener**. Serve strips supplied identity headers and inserts the authenticated account's `Tailscale-User-Login`. Deck accepts only the configured account, rejects missing or duplicate identity headers, and checks the identity again on every cookie-authenticated request. Tagged clients and Funnel do not supply a user identity and cannot sign in. This authorizes every reachable device belonging to that account; use Tailscale device posture or access policy for narrower access.

The loopback listener is a trust boundary, not proof that a request came from Serve: a local process can forge those headers. Enable this option only on a host where local users and processes are trusted. Do not expose the backend on a LAN address, accept these headers from an arbitrary reverse proxy, or forward unverified client-supplied identity headers. Keep pairing on hosts where that local trust assumption is inappropriate. See [Tailscale's identity-header contract](https://tailscale.com/docs/features/tailscale-serve#identity-headers).

An authorized browser signs in automatically and receives the same Secure, HttpOnly, 90-day cookie used by pairing. Cookies, CSRF protection, and signed fleet tickets remain required. Pairing commands and the pairing endpoint are disabled in this mode; denied identity does not fall back to a code.

Logging out or revoking browser sessions invalidates existing credentials, but does **not** revoke the Tailscale account: an approved device can sign in again or reload the dashboard. For lasting revocation, remove the device/account's Tailscale access or change the configured account. Already-issued fleet tickets retain their existing maximum 60-second lifetime.

To return to pairing, set `"auth_mode": "pairing"` (or omit it), remove `tailscale_user`, and restart that service. Existing unexpired browser cookies remain valid; switching modes does not reset authentication state. Revoke browser sessions before switching if you want to require a fresh login.

## Retained data

Server state defaults to `~/.local/state/omp-deck` on Linux/macOS and `%LOCALAPPDATA%\OMP Deck\state` on Windows:

- `registry.json`: stable Deck session identities, custom display names and pins, and non-secret recovery metadata;
- `auth.json`: installation verifier and hashed device credentials;
- `cursors.json`: last-read message cursors;
- `requests.json`: schema-3 request identities and outcomes, bounded to 4 MiB, with up to 1,000 active records and 1,000 compact deduplication records;
- `.requests.json.recoveries/`: private submitted text and image recovery files, bounded to 128 MiB in total and normally retained until dismissed or seven days old; accepted content may be reclaimed sooner;
- `.requests.json.admissions/`: private process-owned submission locks; operating-system lock release distinguishes a live native operation from a crashed unfinished request;
- `collab.json`: protected Collab credentials while imported;
- `sockets/`: extension registration metadata plus Unix sockets on Linux/macOS or same-user named-pipe endpoints on Windows.

Sensitive files are atomic and bounded. Linux/macOS enforce owner-only mode `0600`; Windows enforces a protected current-user ACL and uses named mutexes for cross-process locking. OMP transcripts and blobs remain under OMP's own directories. The browser keeps drafts, optimistic sends, object URLs, and rendered state in memory only; it does not persist transcripts, paths, identifiers, usage, Collab links, or credentials in Web Storage. Object URLs are revoked when their owning draft or session is removed.

Back up the complete state root, including hidden recovery directories, with Deck stopped. Copying only `requests.json` does not preserve submitted text or images. Expired recovery content and validated crash remnants are reclaimed on subsequent ledger access; an operation still holding its submission lock retains its completion capacity. Do not manually remove live lock files.

Live browser state uses authenticated SSE with bounded replay. Low-frequency snapshot recovery replaces state when a cursor is stale. The compatibility JSONL reader is isolated and bounded; it is not a discovery mechanism, live event source, or control protocol.

Public JavaScript and CSS are revalidated by content so upgrades and rollbacks replace stale scripts and styles. HTML and private responses remain `no-store`.

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
`/reload-plugins` does not reload extension modules in OMP 18.1.14, 18.1.16, 18.1.17, or 18.2.1;
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

The first start of a release with Deck display names upgrades `registry.json` from schema 1 to schema 2. Older releases reject and quarantine schema 2, so reverting the package alone is not a safe rollback after deployment. With Deck stopped, preserve a private backup and convert the current registry to schema 1 by removing only `displayName`, `pinned`, and `presentationRevision` from each session and changing the envelope version to `1`; preserve current session identities, recovery metadata, and generations, and replace the file atomically with its original private permissions. This discards custom names and pins, not the sessions. Do not restore old authentication, trust, or request-ledger files to undo this feature: doing so can revive revoked access or lose newer request outcomes.

The request ledger upgrades schemas 1 and 2 to schema 3, preserving current request identities and outcomes while moving recoverable content into private sidecar files. Schema-1 and schema-2 releases cannot read this format. A package-only downgrade, editing the version field, or restoring an older ledger is unsafe and can lose outcomes or permit duplicate work. Keep a private backup of the complete current state and use a rollback artifact that can read schema 3. There is no supported automatic downgrade of the new ledger.

Upgrade with the same tool manager, then run `omp-deck setup`. Setup snapshots
the active OMP plugin and service unit before replacement, validates the new
service twice, and restores those components if any later setup stage fails.
When `--tailscale` creates a new Serve route, a later failure removes that route
without changing configuration that existed before setup.
For the first upgrade from an older installation whose plugin still points inside the Python environment, retain its plugin bytes **before** the package manager replaces that environment. Setup run afterward cannot reconstruct already-overwritten code. Guarded release preparation retains the original environment and plugin before cutover; subsequent upgrades use the separate durable plugin store.
The Python environment remains owned by `uv` or `pipx`; record the installed
version before upgrading so that package-manager rollback remains explicit:

```bash
omp-deck config validate
uv tool upgrade omp-deck
# or: pipx upgrade omp-deck
omp-deck setup

# If setup reports rollback, restore a schema-compatible previous package:
uv tool install --force 'git+https://github.com/marcelojmontanez/omp-deck.git@PREVIOUS_REVISION'
# or: pipx install --force 'git+https://github.com/marcelojmontanez/omp-deck.git@PREVIOUS_REVISION'
```

`service remove` removes only Deck's generated user service. `purge` removes the service, official plugin registration, configuration, and state only after strict ownership and path checks:

Setup rejects unsafe existing installation files before attempting replacement or rollback. Purge stops on a failed service shutdown or an unreadable plugin inventory rather than deleting state. An optional plugin that is confirmed absent does not prevent purge.

```bash
omp-deck purge
uv tool uninstall omp-deck
# or: pipx uninstall omp-deck
```

### Automatic build identities

There is no manual version bump for each update. Committed sources produce `1.5.N+gREVISION`, where `N` is the number of commits since the recorded release baseline and `REVISION` is the full Git commit ID. New commits on main increase the version; rebuilding the same sources keeps the same identity. Tags do not control it. `omp-deck --version` reports the installed identity, and **Deck settings → Installed Deck versions** checks the dashboard and enrolled machines on demand, showing differing builds without assuming they are incompatible.

The package build backend enforces this for ordinary pip, uv, wheel, and source-distribution builds—not only CI. It refuses incomplete Git history, dirty source (including edits hidden by Git index flags), untracked or ignored package inputs, symlinked source, and identity overrides. Builds use a fresh staging directory, verify its bytes against the committed source, and reject prepared metadata from another revision. A source distribution carries a content manifest; changed source or injected package files are rejected when building its wheel. This detects accidental source/identity mismatches, not a malicious publisher who rewrites the build system and manifest.

Build from a clean full checkout with `python3 -m build`, or from an unchanged source distribution. Editable installations are rejected; use `PYTHONPATH=src` to run changing source directly. An unbuilt checkout identifies itself as `0+unbuilt` with its revision unavailable, never as a previously installed release. Install development dependencies from a clean checkout before editing. Keep the exact previous wheel for rollback; older installations that report only `1.4.0` cannot be distinguished by that old version label alone.

### Guarded fleet releases

From a macOS or Linux operator checkout with the verification prerequisites below installed:

```bash
.venv/bin/python tools/release.py run
# Or commit already-staged changes through the normal hook first:
.venv/bin/python tools/release.py run --message "fix(ui): describe the change"
```

The operator inventory is an owner-only `~/.config/omp-deck-release/config.json` file, or a path supplied with `--config` before the subcommand. It is separate from Deck's service configuration and is never included in the package. Configure it once for the installed services; release IDs, package versions, hashes, and subsequent rollback selections are automatic.

The inventory has `version: 1`, an absolute private `state_root`, and a `targets` list. Each target supplies a unique `id`, `kind` (`node` or `coordinator`), `ssh` (an SSH alias or `null` for local execution), private absolute `work_root`, public `origin`, an absolute `bootstrap_rollback` wheel path on that target, and a `profile`. Coordinator targets also list the expected enrolled `machine_ids`.

The profile records the existing installation, not commands to execute: `platform` (`darwin` or `linux`), `user`, `home`, `uv`, `base_python`, `omp`, `tool_root`, `entrypoint`, `config`, `unit`, `state_root`, `plugin_expected`, `service_name`, `rollback_root`, and `port`. macOS uses `launch_domain` and `launch_label`; Linux supplies them as `null`. A Linux coordinator additionally supplies `releases_root` for its private immutable environments. Paths must be absolute. Nodes must be uv-managed installations; unsupported layouts fail before cutover. SSH must work non-interactively with an already-trusted host key. This operator automation does not deploy Windows services.

`run` validates the exact source, builds one immutable wheel, stages targets concurrently, then verifies one node before continuing to the remaining nodes and coordinator. The existing commit hook remains enforced. Its successful base-gate result is reused only when the staged source bytes, Python environment, and tool versions still match; additional release checks are not replaced by that result. Modified files invalidate reuse, including edits hidden by Git index flags.

For separate preparation and deployment:

```bash
.venv/bin/python tools/release.py prepare
.venv/bin/python tools/release.py deploy RELEASE_ID
.venv/bin/python tools/release.py status RELEASE_ID
.venv/bin/python tools/release.py resume RELEASE_ID
```

Preparation retains rollback artifacts but never restarts services. Deployment requires observed independent watchdog readiness, safe managed-session restart admission, exact installed identities, authenticated health checks, and a private 390px Chromium check. The browser may obtain coordinator access tickets but cannot send messages or other application mutations. An unchanged extension is not reinstalled. Updated extension bytes do not replace code inside existing terminal processes: those sessions still need an idle restart and resume.

Failed verification stops the rollout and attempts to restore the affected service. A detached watchdog also attempts rollback when the operator disconnects before verification. An unreachable host, changed ownership/configuration, busy managed session, or failed rollback is reported rather than forcibly overridden. Already verified machines reject stale rollback attempts; this is a guarded rolling release, not an atomic fleet transaction. Candidate and rollback wheels must expose `omp_deck.storage_compatibility()` and mutually read each other's request-ledger format. Older rollback artifacts without that API or a schema-3 reader are rejected before remote staging. Other persistence/protocol-sensitive source changes remain blocked without a reviewed compatibility migration.

`resume` reconciles actual target state before acting and gives a rolled-back target a fresh guarded attempt. If preparation ended before an immutable candidate was recorded, first run `resume OLD_RELEASE_ID` to release its interrupted preparation claim, then start a new `prepare` or `run`. Recovery of a missing journal is permitted only for an empty transaction directory; unknown retained assets require operator review rather than automatic deletion. Do not start a different release while a previous rollback remains unresolved.

Release journals and retained rollback packages/environments are owner-only and remain available for recovery; they are not browser storage. Do not delete an unresolved transaction or its rollback assets. Maintenance credentials are short-lived, stored only in private files, and revoked after success, rollback, or abandonment. Browser credential files and temporary verification output are removed automatically. Preserve the private operator state when moving the release checkout; it records the latest verified artifact for each target.


## Regression verification

In a development virtual environment, install the prerequisites and run the complete release gate:

```bash
python3 -m pip install '.[dev]'
npm ci
npx puppeteer browsers install chrome
npm --prefix src/omp_deck/plugin ci
python3 tools/check.py --release
```

`--release` requires the repository checks, isolated package-identity regression builds, actual Chromium scenarios, and extension checks to pass. Missing prerequisites fail the command; they are not silently skipped. `python3 tools/check.py` alone remains the repository-only gate. CI uses the complete release command.

Browser regressions exercise persistent-cookie expiry and process restart, failed and superseded startup reads, resume-time stream replacement, stale responses, forbidden mutations without logout or resend, per-machine outage isolation, fragmented events, newest-first messages, mobile Enter as newline, quoted Markdown attribute safety, and keyboard focus during live session updates. These contracts protect the tested behaviors; they do not certify every Android/Brave eviction policy or every historical UX request.

Run `PYTHONPATH=src python3 tools/check_omp.py --omp /path/to/omp` to exercise the actual installed executable through Deck's RPC adapter. It uses temporary profiles, projects, and state, sends no paid model prompts, and checks startup, identity-preserving recovery, and whether quitting one session leaves another connected. It does not certify image delivery, every tool, or terminal-extension behavior for that executable.

Browser server scenarios use temporary homes, OMP profiles, configuration, state, and local listeners; extension scenarios use temporary endpoint directories. Native Windows tests exercise process containment and named pipes; native macOS LaunchAgent lifecycle tests require a graphical login and `OMP_DECK_MACOS_LAUNCHD_SMOKE=1`. Run native gates on their actual operating systems; skipped native tests do not establish portability.

## Architecture and limitations

OMP owns agent and tool lifecycles. Each transport adapter owns protocol translation and request correlation. Python owns process supervision, normalized fleet state, authentication, persistence, unread cursors, HTTP actions, and SSE replay. The dependency-free browser owns only ephemeral presentation and user interaction.

Managed RPC sessions are recoverable only when intentionally left running and when OMP's recorded session identity remains valid. Collab availability depends on its relay and the imported link's permissions. Extension fallback is intentionally smaller than RPC and cannot answer Ask remotely. JSONL compatibility recovery does not authorize control. Deck remains single-owner and local-first.
If Collab exceeds Deck's pending-dialog capacity, the connection fails explicitly rather than silently dropping a question or sending a cancellation on your behalf. Answer outstanding questions in the owning terminal before reconnecting.

See [SECURITY.md](SECURITY.md) for the trust boundary and private vulnerability reporting process.
