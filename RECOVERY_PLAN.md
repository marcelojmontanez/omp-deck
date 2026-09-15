# Recovery plan

## Baselines and evidence

- Investigated checkout: `4bb6840`, OMP Deck 1.3.1; correction version: 1.3.2. First supported-API public release: `v1.0.0` (`1cfc278`).
- The earlier private dashboard is preserved in the initial public import, `a75fb754`, and the private checkout at `803cadf` identified by the old development service. The private OMP conversation dated 2026-08-31, continued on 2026-09-02, identifies that import. It is distinct from the later RPC/Collab rewrite. Historical requests are not proof that a feature was implemented.
- `AuthManager` retains credentials for 90 days but issues session-only cookies. The browser can lose its credential while the server still retains the device. This defect also exists in v1.0.0.
- Browser bootstrap maps network/auth-service failures to the pairing form. It also treats generic forbidden responses as loss of authentication.
- Opening the dashboard serializes client-mode discovery and authentication. All assets are uncacheable, including public CSS and JavaScript. The SSE parser repeatedly scans an increasingly large incomplete frame. These add avoidable connection work; a measured result must distinguish them from network latency.
- Returning to a visible tab refreshes a snapshot but does not replace a potentially stalled event stream. Reconnect backoff and a pending fetch can survive browser suspension.
- Direct inspection corrected an initial transcript-only conclusion: both the imported and deployed legacy source already put the composer below the transcript and cap its height at 130px. Those are not regressions introduced by this branch; retain that layout. Newest-first messages and mobile Enter-as-newline remain contracts to protect.
- Legacy rendered cached session summaries immediately from localStorage and then polled every second. The rewrite removed persisted private session metadata and added authentication/SSE, but did not adequately replace its startup and resume UX. Restore responsiveness without restoring sensitive browser persistence or transcript polling.

The previous checks covered authentication within an existing browser context, not the device-cookie persistence policy and failure/recovery lifecycle. They also lacked a release-level requirement to run the real browser scenarios. The mistake was treating implementation and in-session checks as proof of end-to-end parity, rather than recording executable consumer contracts and verifying the actual surface.

## Corrections

1. **Keep paired devices paired.** Issue persistent Secure, HttpOnly, SameSite cookies bounded by the existing server-side 90-day expiry. Upgrade an existing valid session cookie during session verification without extending its original expiry. Preserve logout, revocation, CSRF, origin checks, and distinct coordinator/node credentials. Do not put credentials in web storage or remove pairing.
2. **Separate connection recovery from authentication.** Show checking/retry state until the server confirms whether pairing is needed. Network failure and generic 403 are not revocation. Bound bootstrap waits, remove the unnecessary serial mode/auth dependency, and prevent stale responses from undoing newer authentication.
3. **Recover a suspended browser deliberately.** On a genuine resume or network restoration, cancel obsolete reads/reconnect timers and re-establish authenticated SSE with its replay cursor. Keep in-memory drafts and attachments. Isolate unavailable fleet nodes rather than clearing the whole dashboard or replaying mutations.
4. **Reduce avoidable opening work.** Use content-based revalidation for public assets, while keeping HTML, authentication, API, SSE, and blobs no-store. Verify upgrade and rollback with older mtimes. Parse fragmented SSE incrementally with bounded linear work, retaining size limits and generation checks. Measure cold/reopen behavior in an isolated browser rather than claiming a universal phone latency.
5. **Protect confirmed interaction parity without redesigning it.** Retain the actual legacy composer layout, newest-first messages, mobile Enter as newline, session-specific drafts, failed-delivery recovery, and narrow-viewport containment. Do not convert old unverified requests into supposed regression fixes.
6. **Make these release contracts executable.** Add regressions for browser-process restart with a real persistent profile, existing-cookie adoption and expiry, offline startup/retry, suspend/resume and stale stream replacement, node outage isolation, fragmented SSE, cache revalidation across rollback, newest-first messages, and mobile Enter. Provide one fail-closed release-check command that runs repository, real-browser, and plugin checks. Keep wall-clock measurements separate from deterministic ordering and state assertions.

## Execution and acceptance

- Work on the Mac checkout with isolated HOME/XDG/profile/config/state/projects and a temporary localhost port. Do not modify production, the old Linux deployment, Tailscale routing, or running OMP terminals during implementation.
- Split ownership between durable authentication, browser recovery, composer parity, and integration/performance/release gates. Shared HTML and HTTP-handler edits have one integration owner.
- Each regression must identify a concrete consumer-visible failure, not merely assert wiring or source text. Run the actual browser lifecycle and inspect phone/desktop layouts after implementation.
- Preserve the supported RPC/Collab/extension ownership boundaries. Do not restore legacy keystroke injection, transcript polling as a live protocol, or browser-stored secrets to recover its UX.
- Keep new tests on the existing CI browser path and document exactly what the local release gate exercises. The presence or count of tests is not evidence that Android lifecycle behavior was covered.
- Commit only after the complete release gate and isolated behavioral verification pass. A deployment requires a reachable target, an installation/state-preserving rollback, and live health plus phone-surface verification; do not silently redirect the user's existing Tailscale link to this Mac.

## Isolated verification results

- Native Chromium reproduced the old false-PIN path: abort one `/api/auth/session` request while retaining a valid cookie. Version 1.3.1 showed the PIN form even though a subsequent server check authenticated the device. The candidate showed Retry, then recovered without pairing using that same pre-upgrade device.
- A real browser-profile close/relaunch retained authentication with the candidate and its persistent cookie. This Chromium configuration also retained the old session-only cookie across its restart; restart alone is therefore not the regression oracle. Cookie expiry and the failed-request scenario are separate assertions.
- Under an explicit 150 ms latency / 250,000 bytes-per-second download emulation, one paired reload measured 1,745 ms and 171,452 public-asset transfer bytes before, versus 1,379 ms and 8,400 bytes after. Profile reopen measured 2,022 ms versus 1,940 ms. These are isolated samples, not a phone latency guarantee.
- Running the actual SSE consumers without rendering, using 256-byte fragments, took 35.3 ms / 449.8 ms for 256 KiB / 1 MiB payloads before, versus 1.6 ms / 5.1 ms after. Repeatedly searching the whole unfinished frame is a measured rejected approach; bounded block assembly avoids that superlinear work.
- Inspected the actual retry surface at 390×844 and 1200×900, and the connected dashboard at 390×844. Local Chromium scenarios exercised startup interruption, recovery, stale-response rejection, draft retention, newest-first presentation, and mobile Enter/Send behavior.
- Exercised a bounded JSON read against a real open HTTP response: it received headers, timed out while waiting for the body to finish, and left the device paired.
- A real TCP reset before request headers failed the new regression before the correction, then closed quietly and released its connection slot afterward. Backend failures after headers are not swallowed.
- The complete release gate passed: 405 Python tests (13 platform/opt-in skips), both real Chromium scenarios, TypeScript checking, and 11 plugin cases (one Windows-only skip). Ruff, formatting, compilation, text checks, metrics, and JavaScript syntax checks also passed.

## Limits of the evidence

The Android report is accepted as real. A Chromium restart/suspension scenario can deterministically exercise Deck's responsibilities, but does not certify every Brave/Android eviction policy. A browser explicitly configured to delete all site data cannot retain pairing. Already-lost cookies need one new pairing; an existing cookie can be adopted without re-pairing. Unspecified regressions are not declared fixed: the legacy comparison separates confirmed behavior from guesses and from intentionally unsupported mechanisms.
