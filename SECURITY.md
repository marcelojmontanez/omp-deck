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

A standalone Deck node requires a paired device cookie for every private API,
SSE, and blob request. The fleet coordinator uses its own distinct paired-device
cookie for its dashboard and ticket endpoint. Pairing uses a one-time expiring
code printed locally by `omp-deck pair` or `omp-deck coordinator pair`; the code
is exchanged by POST and never stored in a URL or browser storage. Device
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

## Fleet coordinator trust boundary

The optional coordinator is a directory, viewing-device authenticator, and
Ed25519 ticket signer. It serves the dashboard but never stores node
transcripts, proxies node API traffic, runs OMP, or owns node process/session
lifecycle. Each enrolled Deck installation remains an independent authority for
the sessions and credentials on that machine.

Fleet access has two separate network and authentication paths:

```text
Browser -> owner-restricted coordinator HTTPS -> coordinator device cookie
Browser -> owner-restricted node HTTPS        -> 60-second bearer ticket
```

The second path is direct; the browser must be able to reach every enrolled
node. It obtains machine-specific tickets from the coordinator, then sends them
to node API, SSE, and blob routes with `Authorization: Bearer`. Direct requests
use `credentials: omit`, so coordinator and local-node cookies are not sent to
nodes. A malformed or rejected bearer request never falls back to a valid local
node cookie.

Each node pins its machine ID, machine origin, coordinator origin, and the
coordinator public key in `fleet-node.json`. It accepts a ticket only when its
Ed25519 signature, issuer, audience, device ID, issuance time, and expiry are
valid and the request has the exact enrolled coordinator `Origin` and machine
`Host`. Origins contain only scheme, hostname, and optional port. HTTP origins
are limited to loopback testing; regular remote coordinator and node origins
must be browser-reachable HTTPS endpoints protected for the owner.

The enrollment bundle contains those pinned identities and the public
verification key, not the coordinator signing key. Nevertheless, installing it
grants trust to that coordinator: create it only in an owner-controlled private
directory, transfer it over a trusted channel, and remove working copies after
joining. The signing key and machine directory remain in the coordinator's
private `fleet-coordinator.json`; viewing-device verifiers remain in its
`auth.json`.

Tickets expire no more than 60 seconds after issue. Coordinator logout,
revoke-all, and machine removal prevent renewal but do not revoke tickets
already held by a browser; this is the bounded revocation delay. Removing
`fleet-node.json` through `omp-deck fleet leave` takes effect locally without a
restart and rejects even unexpired tickets. Live streams reauthorize before
delivering newly available events.

A node outage is isolated to that machine. A coordinator outage does not stop
OMP or cancel work already accepted by nodes, but existing browser access lasts
only until its tickets expire and no new tickets can be issued. Direct local
node pairing remains independent. Back up the stopped coordinator's complete
state directory and configuration with owner-only permissions: retaining the
signing key preserves node enrollment, and retaining `auth.json` preserves
paired viewers. If the key or exact coordinator origin cannot be restored, use
a fresh state root, explicitly leave and re-enroll every node, and pair every
viewer again rather than editing state files.

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

Every private route requires either an authenticated cookie for that service or,
on an enrolled node route, a valid machine-specific fleet bearer ticket.
Cookie-authenticated mutations require an exact allowed Origin and a
session-bound CSRF token. Fleet requests instead require the signed ticket plus
the enrollment's exact coordinator Origin and machine Host; the node exposes
CORS only to that pinned coordinator. Host headers, body lengths, JSON shapes,
image count/type/size, project paths, protocol versions, request IDs, event
sequences, and transport peer identity are validated at ingress. Windows blob
reads bind ACL, owner, reparse-point, size, and final-path validation to the
opened handles. Private responses use `no-store`, a restrictive CSP, and browser
hardening headers.

Each HTTP listener admits at most 64 simultaneous connections and closes
connections whose request headers do not complete within ten seconds, including
clients that keep sending partial headers. Excess connections are closed before
allocating a handler. Completed requests, including authenticated SSE streams,
are not subject to the header deadline. These limits do not replace the
owner-restricted network access boundary.

Node state contains stable session metadata, hashed local-device credentials,
last-read cursors, imported Collab credentials, and—when enrolled—the
coordinator public-key trust record. Coordinator state separately contains its
private signing key, machine directory, and hashed viewing-device credentials.
Sensitive files are written atomically with restrictive permissions. Corrupt
stores are rejected or quarantined rather than silently trusted. OMP retains
its own transcripts and blobs. The browser retains rosters, short-lived
tickets, drafts, attachments, optimistic requests, and rendered state only in
memory and revokes attachment object URLs when their owner is removed.

Removing the per-user background service does not delete configuration or
state. `purge` deletes only paths proven to be Deck-owned and refuses roots,
home directories, shallow ancestors, symlinks, and Windows reparse points.

## Reporting a vulnerability

When this repository is public, use **Security → Report a vulnerability** on
GitHub. That sends the report privately to this repository's maintainer; it does
not create a public issue or report the project to GitHub.

Do not include real credentials or private session transcripts.
