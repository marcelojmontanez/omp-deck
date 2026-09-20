# Security

OMP Deck controls local coding agents that can read and modify files and execute
commands. Treat authenticated Deck access as equivalent to terminal access to
the host.

## Supported trust model

The supported remote-access design is:

```text
Phone -> authenticated Tailscale HTTPS -> localhost Python service
```

Tailscale protects network reachability; Deck separately authorizes application
sessions. Tailnet membership alone is not sufficient. Public installations
default to one-time pairing codes. An explicit `auth_mode: "tailscale"` option
instead authorizes one configured Tailscale account through Serve's identity
headers; it does not disable application authentication.

A standalone Deck node requires a device cookie for every private API, SSE,
and blob request. The fleet coordinator uses its own distinct device cookie
for its dashboard and ticket endpoint. In the default mode, a one-time expiring
code printed locally by `omp-deck pair` or `omp-deck coordinator pair` is
exchanged by POST and never stored in a URL or browser storage. Device
credentials are opaque, stored server-side only as verifiers, and can be logged
out individually or revoked together.

Device cookies are Secure, HttpOnly, SameSite=Strict, and persistent only until
their existing server-side expiry (90 days by default). Successful session
verification upgrades an older session-only cookie without rotating its
credential or extending that expiry. Logout and revocation take effect
server-side even if a browser retains an old cookie. Deleting site data or
losing a cookie requires pairing again in the default mode; an approved
Tailscale-account client can sign in again without a code.

Connection errors and generic forbidden responses do not establish that a
device is unpaired. The browser offers connection recovery until the server
confirms authentication status; it does not bypass authentication to reconnect.

For remote access:

- use Tailscale Serve to terminate authenticated HTTPS and proxy to Deck's
  localhost listener;
- restrict the Deck endpoint to its owner with tailnet ACLs or grants, rather
  than granting access to the entire tailnet;
- retain Deck's application authentication, using either default pairing or
  explicitly configured Tailscale-account sign-in; and
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

### Optional Tailscale-account authentication

Both standalone nodes and coordinators accept `auth_mode: "tailscale"` with
one exact `tailscale_user` account login. This requires a loopback listener and
a loopback request peer. Tailscale Serve must be the trusted immediate proxy:
it removes incoming identity headers and supplies `Tailscale-User-Login` from
the authenticated connection. Missing, duplicate, or nonmatching identity
headers are rejected, including when the request contains an otherwise-valid
Deck cookie. Tagged clients and Funnel do not supply a user identity.

The headers are **not signed**. Any local process that can connect to the
backend can impersonate the configured account. This opt-in mode therefore
trusts the local host and its users/processes; it is inappropriate when those
are outside the trust boundary. Never expose its backend to the LAN or public
internet, or use a proxy that forwards unverified client identity headers.
Pairing remains available when this local trust assumption is unsuitable.

The explicit sign-in POST requires the trusted browser Origin plus the
configured proxy identity before issuing a cookie. GET session checks do not
issue new credentials. Other cookie-authenticated mutations still require
session-bound CSRF tokens. The configured login is not returned in public
configuration or errors. Pairing endpoints and commands are disabled in this
mode; identity failure never falls back to pairing.

This authorizes an account, not a particular phone or browser. Logout and
revoke-all invalidate Deck cookies but cannot prevent an approved Tailscale
device from signing in again. Revoke persistent access through Tailscale
device/access policy or change the configured account. Existing signed fleet
tickets remain usable for their bounded lifetime. Switching authentication
modes preserves unexpired cookies; revoke them explicitly when a fresh login
is required.

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
node authentication remains independent. An enrolled coordinator's signed
ticket is an explicit delegated authority, independent of the node's local
cookie authentication mode. Back up the stopped coordinator's complete state
directory and configuration with owner-only permissions: retaining the signing
key preserves node enrollment, and retaining `auth.json` preserves browser
credentials. If the key or exact coordinator origin cannot be restored, use a
fresh state root, explicitly leave and re-enroll every node, and authenticate
every viewer again rather than editing state files.

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
Authorization is checked again after reading mutation bodies, before dispatching
an action or issuing new fleet tickets.
Only packaged public JavaScript and CSS use content-based cache revalidation.
HTML, authentication, API, SSE, and blob responses remain `no-store`.

Each HTTP listener admits at most 64 simultaneous connections and closes
connections whose request headers do not complete within ten seconds, including
clients that keep sending partial headers. Request bodies have a thirty-second
absolute deadline and a ten-second idle timeout. Excess connections are closed
before allocating a handler. Completed requests, including authenticated SSE
streams, are not subject to these read deadlines. These limits do not replace
the owner-restricted network access boundary.

Node state contains stable session metadata, hashed local-device credentials,
last-read cursors, imported Collab credentials, and—when enrolled—the
coordinator public-key trust record. Coordinator state separately contains its
private signing key, machine directory, and hashed viewing-device credentials.
Sensitive files are written atomically with restrictive permissions. Corrupt
stores are rejected or quarantined rather than silently trusted. OMP retains
its own transcripts and blobs. The browser retains rosters, short-lived
tickets, drafts, attachments, optimistic requests, and rendered state only in
memory and revokes attachment object URLs when their owner is removed.

Unsent draft persistence is disabled by default for each session. Opting in
stores its text and selected images under the owning node's private `drafts/`
directory, not on the coordinator or in browser storage. All of the owner's
authenticated devices with access to that session can read and replace the
same draft; revisions reject stale overwrites. Draft routes retain normal
authentication, origin/CSRF or fleet-ticket checks, and `no-store` responses.
The store is bounded to 1,000 records, a 32 MiB manifest, and 128 MiB of image
payloads per node. Each draft accepts at most 4,000 text characters and four
images totaling 20 MiB; sending remains subject to the session's advertised
image limit. Drafts expire seven days after the last save, with expired content
reclaimed on the next draft access. Disabling saving removes its stored text
and images. Logout, device revocation, and browser-only clearing do not erase
server drafts. Session removal also removes its draft; a cleanup failure is
reported rather than silently claiming deletion.

Removing the per-user background service does not delete configuration or
state. `purge` deletes only paths proven to be Deck-owned and refuses roots,
home directories, shallow ancestors, user-controlled symlink ancestors, and
Windows reparse points. POSIX purge also refuses roots that are no longer
current-user-owned or have become group/world-writable.

## Reporting a vulnerability

When this repository is public, use **Security → Report a vulnerability** on
GitHub. That sends the report privately to this repository's maintainer; it does
not create a public issue or report the project to GitHub.

Do not include real credentials or private session transcripts.
