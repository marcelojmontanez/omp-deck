# OMP Deck

A local-first mobile control surface for [Oh My Pi](https://github.com/can1357/oh-my-pi) sessions.

OMP Deck shows every local OMP session, its current work, unread responses, todos, and subagents. From a phone you can continue conversations, attach files, run slash commands, interrupt active work, and launch projects.

## Requirements

- Linux with a systemd user session
- Python 3.10 or newer
- OMP 18.x on `PATH`
- Tailscale on the computer and phone for secure remote access

OMP Deck is currently tested against OMP 18.1.2 on Linux and WSL2. Other OMP versions and operating systems are not yet supported.

## Install

Clone the repository and run the installer:

```bash
git clone https://github.com/marcelojmontanez/omp-deck.git
cd omp-deck
./install.sh
```

The installer installs OMP Deck's OMP extension, copies the server into your user data directory, creates and starts a user service, and configures Tailscale Serve when Tailscale has no existing Serve configuration. It does not require root.

The final line prints the URL to open on your phone. Restart OMP sessions that were already running during installation so they load the extension. If Tailscale already serves something else, the installer leaves that configuration untouched and prints the manual command instead. OMP Deck listens only on localhost by default; do not expose port 8787 directly to the internet.

## Use

Start or restart an OMP session. The installed extension connects it to OMP Deck automatically; no command is required. OMP Deck then discovers the session and refreshes the mobile interface as it works.

Useful commands:

```bash
omp-deck doctor
omp-deck serve
omp-deck uninstall
omp-deck uninstall --purge
```

Installation starts the background service automatically. `serve` is only for foreground debugging.

## Configuration

Optional configuration lives at:

```text
~/.config/omp-deck/config.json
```

Defaults work for a standard OMP installation. Example overrides:

```json
{
  "port": 8787,
  "project_root": "~/dev",
  "session_root": "~/.omp/agent/sessions",
  "socket_root": "~/.local/state/omp-deck/sockets"
}
```

Available keys are `host`, `port`, `project_root`, `session_root`, `socket_root`, `state_root`, and `omp_bin`.

Restart after changing configuration:

```bash
systemctl --user restart omp-deck
```

## Security

OMP Deck can instruct coding agents that can modify files and execute commands. Treat access to OMP Deck like terminal access to your computer.

- The default listener is `127.0.0.1` only.
- Tailscale Serve is the supported remote-access path.
- Do not bind to `0.0.0.0` unless another authenticated reverse proxy protects it.
- Uploaded files and read cursors stay under `~/.local/state/omp-deck` by default.
- OMP session transcripts remain in OMP's normal local session directory.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Architecture

The Python server projects OMP's structured session events into a small HTTP API and serves a dependency-free mobile web interface. It discovers sessions from OMP's local session store and OMP Deck socket registry. OMP-specific integration remains local; no hosted relay is involved.
