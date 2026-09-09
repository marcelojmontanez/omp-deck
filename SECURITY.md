# Security

OMP Deck controls local coding agents that can read and modify files and execute
commands. Treat authenticated Deck access as equivalent to terminal access to
the host.

## Supported trust model

The supported remote-access design is:

```text
Phone -> authenticated Tailscale HTTPS -> localhost Python service
```

Both layers are required: Tailscale authenticates access to the private network,
and OMP Deck must independently authenticate the application session. Tailnet
membership alone is not a sufficient authorization boundary.

OMP Deck requires a paired device cookie for every API, SSE, and blob request.
Pairing uses a one-time expiring code printed locally by `omp-deck pair`; the
code is exchanged by POST and never stored in a URL or browser storage. Device
credentials are opaque, stored server-side only as verifiers, and can be logged
out individually or revoked together.

For remote access:

- use Tailscale Serve to terminate authenticated HTTPS and proxy to Deck's
  localhost listener;
- restrict the Deck endpoint to its owner with tailnet ACLs or grants, rather
  than granting access to the entire tailnet;
- retain Deck's own application authentication even for an owner-restricted
  tailnet route; and
- review the effective Serve and tailnet policy after every configuration
  change.

A minimal ACL for a tagged Deck host is:

```json
{
  "tagOwners": {
    "tag:omp-deck": ["you@example.com"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["you@example.com"],
      "dst": ["tag:omp-deck:443"]
    }
  ]
}
```

Replace the identity and tag with your tailnet values. This limits the route to
the owner identity; use device posture rules as well when access must be limited
to one specific phone rather than every device owned by that identity.

Tailscale Serve configuration may require device or tailnet administrator
authorization. This is separate from Deck's per-user systemd service, macOS
LaunchAgent, or Windows scheduled task, which do not require root or elevation.

## Unsupported exposure

- Tailscale Funnel is unsupported.
- Direct public or internet exposure is unsupported.
- Binding Deck to `0.0.0.0` is unsupported unless a separate authenticated
  reverse proxy protects every route. This is not the default or recommended
  Tailscale deployment.
- An unauthenticated reverse proxy, TLS alone, firewall reachability, and broad
  tailnet membership do not replace application authentication.

Deck setup must never replace or remove unrelated Tailscale Serve routes. If a
device already has Serve configuration, preserve it and add a distinct route
only after an administrator reviews and authorizes the change.

## Product and transport boundary

OMP Deck is a private control plane for one owner's local OMP fleet. It is not
OMP Collab and does not provide rooms, invite links, guest identities,
participant authorization, public sharing, or a public relay. A writable
Collab link may be imported as a server-side credential for one terminal-owned
session. Deck never returns that link through HTTP, SSE, logs, or browser
storage, and deletes it when the session is removed.

Managed sessions use OMP's official RPC client. Terminal-owned sessions use the
published Collab protocol or the bundled extension's private Unix socket on
Linux/macOS and authenticated named pipe on Windows. Unix discovery requires a
private current-user directory and registration metadata, an owned socket, and
matching session/connection identities. Linux additionally checks socket peer
credentials; macOS relies on the private filesystem boundary. Windows connection tokens remain
in a protected current-user metadata directory, and Deck verifies that the
named-pipe server process belongs to the current user before authenticating.
Extension fallback cannot answer Ask remotely and never injects terminal keys.
Deck has no tmux or `/proc` control path. A bounded JSONL reader exists only for
explicit compatibility recovery; transcript text is not an authorization or
control protocol.

## Request and data protections

All state-bearing routes require an authenticated device cookie. Mutations also
require an exact allowed Origin and a session-bound CSRF token. Host headers,
body lengths, JSON shapes, image count/type/size, project paths, protocol
versions, request IDs, event sequences, and transport peer identity are
validated at ingress. Windows blob reads bind ACL, owner, reparse-point, size,
and final-path validation to the opened handles. Private responses use
`no-store`, a restrictive CSP, and browser hardening headers.

Server state contains stable session metadata, hashed device credentials,
last-read cursors, and imported Collab credentials. Sensitive files are written
atomically with restrictive permissions. Corrupt stores are rejected or
quarantined rather than silently trusted. OMP retains its own transcripts and
blobs. The browser retains drafts, optimistic requests, and rendered state only
in memory and revokes attachment object URLs when their owner is removed.

Removing the per-user background service does not delete configuration or
state. `purge` deletes only paths proven to be Deck-owned and refuses roots,
home directories, shallow ancestors, symlinks, and Windows reparse points.

## Reporting a vulnerability

When this repository is public, use **Security → Report a vulnerability** on
GitHub. That sends the report privately to this repository's maintainer; it does
not create a public issue or report the project to GitHub.

Do not include real credentials or private session transcripts.
