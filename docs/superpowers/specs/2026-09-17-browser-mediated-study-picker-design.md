# Browser-mediated study picker and single-subject rehearsal

## Purpose

A research coordinator should be able to launch one saved Digital Brain study
without editing Python or copying a study name. The coordinator enters the
website URL, signs in through the existing website, selects a published study,
chooses one assigned subject, prepares the videos, and starts a non-participant
rehearsal.

This design keeps the current security boundary: every workstation credential
belongs to one immutable published assignment. The launcher never receives the
coordinator's website password or browser session.

## Current behavior

The website already lets an authenticated owner publish an integration
assignment and create a short-lived pairing code for that assignment. The
launcher exchanges the code for a study-scoped device credential. It then
fetches the paired study name and subject roster.

The launcher can save several named connections, but the coordinator must type
a local connection name and paste a pairing code. A URL alone cannot reveal
private study names. The website must authenticate the coordinator before it
shows the study inventory.

The runner remains an integration system. Published packages use
`mode=integration_test` and `pgl_ready=false`. A real saved assignment may be
used for a non-participant rehearsal, but this software does not certify a
participant protocol, display, response device, eye tracker, or interruption
policy.

## Coordinator flow

1. The launcher remembers one or more website origins. The coordinator selects
   an origin or enters a new HTTPS origin; literal loopback HTTP remains valid
   for local testing.
2. **Choose study in browser** opens an authorization page on that website.
3. The website uses its normal named-account session. If the coordinator is not
   signed in, it sends them through the existing login flow and returns them to
   the authorization page.
4. The authorization page lists only the coordinator's published integration
   assignments. Each option shows the study name, subject count, trial count,
   publication time, and immutable assignment ID. Draft studies do not appear.
5. The coordinator selects one assignment and approves the named workstation.
6. The launcher receives a short-lived, single-use authorization result and
   exchanges it for the existing experiment-scoped device credential. It then
   fetches the authoritative study context.
7. The launcher saves the connection under the study name plus a short
   assignment-ID suffix. It does not ask the coordinator to invent a name. If
   that assignment is already paired, the launcher selects the existing
   connection instead of replacing its credential.
8. The launcher shows paired studies in a dropdown. Selecting one displays its
   server, immutable assignment ID, subject count, and trial count.
9. The coordinator selects one assigned subject from a second dropdown. No
   subject is preselected.
10. **Prepare videos** downloads and verifies the exact assignment. **Start
    rehearsal** remains disabled until preparation succeeds, the workstation
    runtime is available, and the coordinator acknowledges non-participant use.
11. The final confirmation names the website, study, immutable assignment,
    subject, and trial count. Starting launches the existing runner in a bound
    child process. Upload retries never replay videos.

## Authorization protocol

The flow follows the shape of an OAuth device authorization without adding an
OAuth provider.

### Start

The launcher sends a bounded request to a new unauthenticated endpoint with its
printable workstation name and a random verifier challenge. The server stores
only the challenge digest and returns:

- an opaque request ID;
- a short, non-secret user code;
- an authenticated verification URL;
- an expiration time and minimum polling interval.

The request expires after fifteen minutes. Starting a request grants no access
to studies, media, or results.

### Approve

The launcher opens the verification URL in the system browser. The website
requires the existing named-account session and CSRF protection. It resolves
the request ID on the server, lists that account's published integration
assignments, and asks the coordinator to select one.

Approval stores the selected experiment ID, owner ID, and approval time against
the pending request. The browser never receives a device credential. Another
account cannot approve the request for a study it does not own.

### Exchange

The launcher polls no faster than the server's interval. It sends the request
ID and original verifier. Before approval, the endpoint returns `pending`.
After approval, one atomic exchange consumes the request and returns the same
experiment-scoped device identity and token used by the existing pairing flow.
Expired, denied, consumed, or mismatched requests return generic errors and
cannot be retried with the same verifier.

The launcher immediately calls the existing identity and study-context
endpoints. It accepts the connection only if all three experiment identities
match: the approval response, device identity, and immutable study context.

### Stored authority

The launcher stores only the experiment-scoped device credential. It does not
store an owner-wide study-list token. To pair another study, the coordinator
repeats the browser approval. Previously paired studies remain available in the
launcher dropdown.

This is slightly more interaction than an account-wide launcher token, but it
keeps a lost workstation from enumerating or pairing every study owned by the
account.

## Website components

The website adds:

- a private table for pending device authorizations, verifier challenges,
  expiration, approval, denial, and consumption;
- start, poll, and exchange endpoints under the runner-device namespace;
- an authenticated authorization page that uses the existing account session,
  collection ownership checks, streamed-body limit, CSRF checks, and Host/path
  validation;
- a server-side query that lists only immutable published integration
  assignments owned by the current account;
- an explicit approve action and a deny action;
- no CORS relaxation, localhost callback, public study-list endpoint, password
  endpoint, or credential in a URL.

The existing manual pairing-code route remains available as a recovery path and
for older launchers.

## Launcher components

The launcher adds:

- an origin dropdown with **Add website**;
- **Choose study in browser**, which starts authorization and opens the returned
  URL with the system browser;
- bounded background polling with cancellation and expiration handling;
- automatic connection naming from the verified study context;
- a study dropdown populated from saved, individually scoped pairings;
- the existing subject dropdown, preparation, confirmation, child-process
  execution, status, recovery, and upload controls.

The launcher does not import native PGL merely to list studies. It disables
**Start rehearsal** when macOS, Python 3.12, PGL, or required local settings are
missing, while leaving pairing, preparation, status, and synchronization
available.

## Single-subject rehearsal

The first end-to-end test uses one real published integration assignment and
one assigned subject. It is a non-participant rehearsal:

1. Publish or select a small assignment whose media are available locally to
   the website and whose foil rendering is configured when foils are present.
2. Pair through the browser-mediated flow.
3. Select one subject and inspect its role counts before download.
4. Prepare the exact videos and complete the full decode preflight.
5. Confirm the PGL settings and display profiles with the lab.
6. Run the assignment with the coordinator as the tester, not a research
   participant.
7. Verify the durable journal, native PGL files, completed trial count, and
   synchronized artifact receipt.
8. Confirm that **Retry upload** performs no presentation and that the same
   subject cannot start a second attempt silently.

The test stops before presentation if PGL is absent, FFmpeg cannot decode a
video, the study changes, the subject already has an attempt, or any identity
check fails.

## Failure behavior

- Closing either window leaves the request pending only until its short expiry.
- Denial and expiry return the launcher to its initial state without creating a
  device.
- A network interruption during approval can resume polling until expiry.
- A network interruption after credential exchange but before local storage
  does not automatically create another device. The website lets the owner
  revoke the orphaned device; the launcher starts a new authorization only after
  the coordinator requests it.
- A changed or revoked saved connection cannot prepare, run, or synchronize.
- A crash during presentation follows the existing explicit recovery path. The
  launcher never restarts the subject automatically.

## Testing

Server tests cover ownership, CSRF, expiry, denial, one-time exchange, verifier
binding, polling limits, archived accounts and collections, concurrent exchange,
and isolation between two owners and two experiments. The real HTTP integration
test covers authorization through study discovery, one-subject preparation,
reservation, journal upload, artifacts, and finalization.

Launcher tests cover URL validation, browser opening, pending and denied states,
automatic names, duplicate pairings, study and subject selection, stale identity
rejection, no automatic subject choice, no task launch during discovery, and a
real Tk control path with mocked presentation. The existing subprocess test
continues to prove bounded output draining and the bound configuration snapshot.

The final local rehearsal uses actual PGL and FFmpeg but no participant. It
records the assignment ID, subject ID, package hash, runtime versions, native
output location, and synchronization receipt. It does not commit media,
credentials, responses, databases, or runtime logs.

## Out of scope

- participant-ready certification;
- changing the scientific allocation, repeat, foil, condition, or block rules;
- account-wide launcher credentials;
- deploying the website or purchasing infrastructure;
- exposing draft studies or studies owned by another account;
- silently rerunning interrupted or completed subjects.

## Acceptance criteria

- A coordinator enters a website URL but never types a study or subject into
  code.
- The authenticated website presents the available published integration
  assignments as a dropdown.
- The launcher saves the selected study under its verified name and displays it
  in its study dropdown.
- Every saved credential remains restricted to one immutable assignment.
- The coordinator can prepare and rehearse exactly one selected subject, with a
  final identity confirmation and no automatic replay.
- A test verifies the whole local software path; actual participant use remains
  blocked until the study team approves the protocol and workstation hardware.
