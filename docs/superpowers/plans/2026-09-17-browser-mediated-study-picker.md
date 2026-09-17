# Browser-mediated Study Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a coordinator authenticate in the Digital Brain website, choose one published integration assignment from a dropdown, pair the launcher without typing a study name, and rehearse one assigned subject safely.

**Architecture:** The website creates a short-lived device-authorization request, authenticates the coordinator in the existing browser session, and binds one selected immutable experiment to that request. The launcher proves possession of a random verifier, exchanges the approved request for the existing experiment-scoped device token, saves it under an automatic study-derived profile name, and continues through the current prepare/run/sync workflow.

**Tech Stack:** Python 3.10 FastAPI/SQLite server, dependency-free HTML/CSS/JavaScript website UI, Python 3.12 Tk launcher, standard-library HTTP and browser integration, `unittest`, Node UI tests, Bandit.

**Spec:** `docs/superpowers/specs/2026-09-17-browser-mediated-study-picker-design.md`

## Global Constraints

- The workstation never receives or stores the coordinator's website password or browser session.
- Every saved workstation credential remains bound to exactly one immutable published integration assignment.
- Published packages remain `mode=integration_test` and `pgl_ready=false`.
- Literal loopback HTTP is valid for local testing; every non-loopback origin must use HTTPS.
- The launcher must not start PGL automatically or preselect a subject.
- A completed or interrupted attempt must not replay silently; upload retry never presents stimuli.
- Keep the current manual one-time pairing-code route as a fallback.
- Do not expose drafts, archived collections, other owners' studies, media paths, credentials, participant responses, or runtime artifacts.
- Do not add a frontend build system, deployment action, paid service, or account-wide launcher credential.
- Do not commit media, databases, credentials, cookies, logs, native PGL results, or generated runtime state.

---

## File Structure

### Website repository: `../dbp-dataset-browser`

- Create `study_runner_authorization.py`: pending device-authorization state, study discovery, approval, denial, polling, and one-time exchange.
- Modify `study_runner_auth.py`: issue an experiment-scoped device credential from a server-validated approved authorization without duplicating SQL.
- Modify `study_runner_routes.py`: install bounded public device-authorization endpoints and authenticated owner approval endpoints.
- Modify `app.py`: serve the authenticated authorization page through the existing login gate.
- Create `static/runner-authorize.html`: focused study-selection page.
- Create `static/runner-authorize.js`: load the pending request and published assignments, approve or deny one selection.
- Modify `static/metric-explorer.css`: reuse the existing visual system for the small authorization page if shared classes suffice; otherwise create `static/runner-authorize.css`.
- Create `tests/test_study_runner_authorization.py`: state-machine and ownership tests.
- Modify `tests/test_study_runner_routes.py`: HTTP, CSRF, body-limit, and auth-envelope tests.
- Create `tests/test_runner_authorize_ui.cjs`: dependency-free DOM/controller tests.
- Modify `docs/pgl-integration.md`: coordinator authorization and fallback pairing guide.

### Runner repository: current repository

- Create `src/dbp_pgl_runner/browser_pairing.py`: verifier generation, device-authorization polling, browser opening, cancellation, and strict response validation.
- Modify `src/dbp_pgl_runner/api.py`: bounded start, poll, and exchange calls.
- Modify `src/dbp_pgl_runner/profiles.py`: automatic collision-safe profile names and atomic publication of an already-issued credential.
- Modify `src/dbp_pgl_runner/launcher.py`: website-origin controls, browser-mediated pairing state, verified study labels, and one-subject rehearsal flow.
- Modify `src/dbp_pgl_runner/runner.py`: expose verified pairing and study context without importing PGL.
- Modify `tests/test_api.py`: exact wire-contract tests.
- Create `tests/test_browser_pairing.py`: browser flow and poll-state tests.
- Modify `tests/test_profiles.py`: automatic naming and no-overwrite tests.
- Modify `tests/test_launcher.py`: real Tk study picker and single-subject guard tests.
- Modify `README.md`: novice coordinator instructions and local rehearsal checklist.

---

### Task 1: Server-side device-authorization state machine

**Files:**
- Create: `../dbp-dataset-browser/study_runner_authorization.py`
- Modify: `../dbp-dataset-browser/study_runner_auth.py`
- Test: `../dbp-dataset-browser/tests/test_study_runner_authorization.py`

**Interfaces:**
- Consumes: `AccountStore.connect()`, `_check_save_session(...)`, and the immutable `study_experiments` records.
- Produces: `RunnerAuthorization.start(device_name, challenge, request_ip) -> dict`, `view(request_id, owner_id, session_token) -> dict`, `approve(request_id, experiment_id, owner_id, session_token) -> dict`, `deny(request_id, owner_id, session_token) -> dict`, `poll(request_id, verifier) -> dict`, and `exchange(request_id, verifier) -> dict`.
- Produces: `RunnerAuth.issue_device(experiment_id, owner_id, device_name, now) -> dict`, called only inside a validated transaction.

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_approval_exposes_only_owned_published_integration_assignments(self):
    pending = self.authorizations.start("lab-mac", self.challenge, "127.0.0.1")
    view = self.authorizations.view(pending["request_id"], self.owner, self.session)
    self.assertEqual([row["experiment_id"] for row in view["studies"]], [self.experiment_id])
    self.assertEqual(view["studies"][0]["study_name"], "Runner pairing")

def test_exchange_is_verifier_bound_single_use_and_experiment_scoped(self):
    pending = self.start_and_approve(self.experiment_id)
    with self.assertRaises(ValueError):
        self.authorizations.exchange(pending["request_id"], "wrong-verifier")
    device = self.authorizations.exchange(pending["request_id"], self.verifier)
    self.assertEqual(device["experiment_id"], self.experiment_id)
    with self.assertRaises(ValueError):
        self.authorizations.exchange(pending["request_id"], self.verifier)
```

Also test expiry, denial, archived owner, archived collection, draft exclusion, another owner's experiment, two concurrent exchanges, minimum polling interval, cleanup of expired requests, global active-request cap, and challenge/verifier length and alphabet.

- [ ] **Step 2: Run the focused tests and verify the missing module failure**

Run: `PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_authorization -v`

Expected: FAIL because `study_runner_authorization` does not exist.

- [ ] **Step 3: Implement the private state table and strict validators**

Create `study_runner_device_authorizations` with these immutable identity fields and mutable state fields:

```sql
request_id TEXT PRIMARY KEY,
user_code_hash TEXT NOT NULL UNIQUE,
verifier_challenge TEXT NOT NULL,
device_name TEXT NOT NULL,
created_at REAL NOT NULL,
expires_at REAL NOT NULL,
next_poll_at REAL NOT NULL,
owner_id TEXT REFERENCES accounts(id),
experiment_id TEXT REFERENCES study_experiments(id),
approved_at REAL,
denied_at REAL,
consumed_at REAL
```

Validate request IDs as 32 lowercase hexadecimal characters, verifier and challenge values as bounded URL-safe ASCII, and device names through `RunnerAuth._device_name`. Store only a SHA-256 challenge derived from a 32-byte random verifier. Delete expired unapproved rows during `start`, reject starts when 256 live rows remain, and enforce a per-process request-IP token bucket before inserting.

- [ ] **Step 4: Implement owned study discovery and state transitions**

Return studies in deterministic name/ID order with exactly:

```python
{
    "experiment_id": row["id"],
    "study_name": row["name"],
    "subject_count": row["subject_count"],
    "trial_count": row["trial_count"],
    "published_at": row["created_at"],
}
```

Join the experiment, owner, collection, saved study, and subject tables. Require an active owner, accessible non-archived collection, immutable source-study digest match, `mode="integration_test"`, and `pgl_ready=false`. Use `BEGIN IMMEDIATE` for approve, deny, and exchange. Never return a device token from `view`, `approve`, `deny`, or `poll`.

- [ ] **Step 5: Extract credential issuance and implement one-time exchange**

Move the insert-and-response portion of `RunnerAuth.exchange_pairing` into:

```python
def issue_device(self, database, experiment_id, owner_id, device_name, now):
    ...
    return {"device_id": device_id, "token": token, "experiment_id": experiment_id,
            "owner_id": owner_id, "device_name": device_name, "created_at": now}
```

Keep `exchange_pairing` behavior unchanged. `RunnerAuthorization.exchange` verifies the original verifier against the stored challenge, atomically marks the request consumed, and calls `issue_device` in the same transaction.

- [ ] **Step 6: Run focused and existing auth tests**

Run: `PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_authorization tests.test_study_runner_auth -v`

Expected: PASS with no token in non-exchange responses.

- [ ] **Step 7: Commit the server state machine**

```bash
git add study_runner_authorization.py study_runner_auth.py tests/test_study_runner_authorization.py
git commit -m "Add browser-mediated runner authorization"
```

### Task 2: Website authorization API and study-selection page

**Files:**
- Modify: `../dbp-dataset-browser/study_runner_routes.py`
- Modify: `../dbp-dataset-browser/app.py`
- Create: `../dbp-dataset-browser/static/runner-authorize.html`
- Create: `../dbp-dataset-browser/static/runner-authorize.js`
- Create: `../dbp-dataset-browser/static/runner-authorize.css`
- Modify: `../dbp-dataset-browser/tests/test_study_runner_routes.py`
- Create: `../dbp-dataset-browser/tests/test_runner_authorize_ui.cjs`

**Interfaces:**
- Consumes: all `RunnerAuthorization` methods from Task 1.
- Produces: `POST /api/runner-device/authorizations`, `POST /api/runner-device/authorizations/{request_id}/poll`, `POST /api/runner-device/authorizations/{request_id}/exchange`, `GET /api/runner/authorizations/{request_id}`, `POST /api/runner/authorizations/{request_id}/approve`, and `POST /api/runner/authorizations/{request_id}/deny`.
- Produces: authenticated `GET /runner/authorize?request=<request_id>`.

- [ ] **Step 1: Write failing HTTP tests**

```python
def test_browser_authorization_requires_session_and_csrf_for_approval(self):
    pending = self.anonymous.post("/api/runner-device/authorizations", json=self.start_body).json()
    path = f"/api/runner/authorizations/{pending['request_id']}/approve"
    self.assertEqual(self.anonymous.post(path, json={"experiment_id": self.experiment_id}).status_code, 401)
    self.assertEqual(self.client.post(path, json={"experiment_id": self.experiment_id}).status_code, 403)
    response = self.client.post(path, headers=self.csrf, json={"experiment_id": self.experiment_id})
    self.assertEqual(response.status_code, 200)

def test_public_flow_cannot_choose_experiment_or_read_study_inventory(self):
    pending = self.anonymous.post("/api/runner-device/authorizations", json=self.start_body).json()
    self.assertNotIn("studies", pending)
    poll = self.anonymous.post(f"/api/runner-device/authorizations/{pending['request_id']}/poll",
                               json={"verifier": self.verifier})
    self.assertEqual(poll.json(), {"status": "pending", "expires_at": pending["expires_at"]})
```

Cover exact fields, body-size limits, malformed IDs, unsafe Host/path handling, approval isolation, denial, expiry, poll throttling, and one-time exchange.

- [ ] **Step 2: Run HTTP tests and verify route failures**

Run: `PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_routes -v`

Expected: FAIL with 404 responses for the new routes.

- [ ] **Step 3: Install the API routes with existing guards**

Use `strict_json` for every body, `run_in_threadpool` for database work, the existing runner bearer-auth boundary for no owner endpoint, and the existing named-account session plus CSRF middleware for owner writes. Return `Cache-Control: no-store` on every authorization response. Build `verification_path` as a fixed relative path plus URL-encoded request ID; never derive an absolute URL from `Host`.

- [ ] **Step 4: Write failing authorization-page controller tests**

Test that the page shows the user code, renders only returned studies, requires a selected study, disables duplicate submission, supports denial, clears request data on terminal states, and never stores the pairing verifier or device token.

Run: `node --test tests/test_runner_authorize_ui.cjs`

Expected: FAIL because `runner-authorize.js` does not exist.

- [ ] **Step 5: Implement the focused authenticated page**

The page contains one title, workstation name, user code, a study `<select>`, a concise immutable-assignment summary, **Approve workstation**, and **Deny**. It fetches only `GET /api/runner/authorizations/{request_id}` and submits with the existing CSRF header. It displays completion and tells the coordinator to return to the launcher. It contains no media, subject controls, PGL controls, account password fields, or account-wide token.

- [ ] **Step 6: Run website UI and route tests**

Run:

```bash
node --test tests/test_runner_authorize_ui.cjs
PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_routes tests.test_study_runner_authorization -v
```

Expected: PASS.

- [ ] **Step 7: Commit the website authorization surface**

```bash
git add study_runner_routes.py app.py static/runner-authorize.html static/runner-authorize.js \
  static/runner-authorize.css tests/test_study_runner_routes.py tests/test_runner_authorize_ui.cjs
git commit -m "Add authenticated workstation study picker"
```

### Task 3: Runner API and automatic profile publication

**Files:**
- Create: `src/dbp_pgl_runner/browser_pairing.py`
- Modify: `src/dbp_pgl_runner/api.py`
- Modify: `src/dbp_pgl_runner/profiles.py`
- Modify: `tests/test_api.py`
- Create: `tests/test_browser_pairing.py`
- Modify: `tests/test_profiles.py`

**Interfaces:**
- Consumes: Task 2 public authorization endpoints.
- Produces: `BrowserPairing.start(origin, device_name) -> PendingAuthorization`, `BrowserPairing.wait(pending, cancel_event) -> dict`, and `ConnectionProfiles.publish(origin, device_response, study_context) -> str` returning the profile name.

- [ ] **Step 1: Write failing strict API tests**

```python
def test_browser_authorization_contract_and_single_exchange(self):
    pending = api.start_authorization("lab-mac", challenge)
    self.assertEqual(pending["status"], "pending")
    self.assertTrue(pending["verification_path"].startswith("/runner/authorize?request="))
    self.assertEqual(api.poll_authorization(pending["request_id"], verifier)["status"], "approved")
    device = api.exchange_authorization(pending["request_id"], verifier)
    self.assertEqual(device["experiment_id"], "b" * 32)
```

Reject unknown fields, non-relative verification paths, invalid intervals, mismatched experiment IDs, redirects, oversized JSON, and tokens in poll responses.

- [ ] **Step 2: Run API tests and verify missing methods**

Run: `PYTHONPATH=src python3.12 -m unittest tests.test_api -v`

Expected: FAIL because the authorization methods are undefined.

- [ ] **Step 3: Implement verifier generation and bounded polling**

`BrowserPairing.start` generates 32 random bytes, encodes a URL-safe verifier, sends its SHA-256 challenge, validates the response, and opens `origin + verification_path` with `webbrowser.open`. `wait` polls no faster than the returned interval, stops on a caller-owned `threading.Event`, refuses to run beyond `expires_at`, and exchanges exactly once after `approved`. It does not log or return the verifier.

- [ ] **Step 4: Write failing profile-publication tests**

```python
def test_publish_uses_verified_study_name_and_never_overwrites(self):
    name = profiles.publish(origin, device_response, study_context)
    self.assertEqual(name, "practice-study-bbbbbbbb")
    self.assertEqual(profiles.publish(origin, device_response, study_context), name)
    self.assertEqual(RunnerConfig.load(profiles.directory(name)).experiment_id, "b" * 32)
```

Also test two studies with the same name, non-ASCII names, 48-character bounds, an existing conflicting credential, interrupted publication, private permissions, and no token in display metadata.

- [ ] **Step 5: Implement automatic profile naming and atomic publication**

Normalize the verified study name to lowercase ASCII words separated by hyphens, cap the stem so `-<first eight experiment hex>` fits the 48-character profile limit, and fall back to `study-<suffix>`. Verify that device response and study context carry the same experiment ID before publishing. Reuse an existing profile only when origin, device ID, and experiment ID match exactly; otherwise reject the conflict.

- [ ] **Step 6: Run runner API/profile tests**

Run: `PYTHONPATH=src python3.12 -m unittest tests.test_api tests.test_browser_pairing tests.test_profiles -v`

Expected: PASS.

- [ ] **Step 7: Commit browser pairing and profile publication**

```bash
git add src/dbp_pgl_runner/browser_pairing.py src/dbp_pgl_runner/api.py \
  src/dbp_pgl_runner/profiles.py tests/test_api.py tests/test_browser_pairing.py tests/test_profiles.py
git commit -m "Pair launcher through authenticated study selection"
```

### Task 4: Launcher study dropdown and single-subject rehearsal controls

**Files:**
- Modify: `src/dbp_pgl_runner/launcher.py`
- Modify: `src/dbp_pgl_runner/runner.py`
- Modify: `tests/test_launcher.py`

**Interfaces:**
- Consumes: `BrowserPairing` and `ConnectionProfiles.publish` from Task 3.
- Produces: origin selection, browser-mediated pairing, verified paired-study labels, explicit subject selection, and the existing prepare/run/status/sync actions.

- [ ] **Step 1: Write failing launcher-model and Tk tests**

```python
def test_url_to_browser_selection_populates_study_without_typed_name(self):
    window.origin.set("http://127.0.0.1:8769")
    window.choose_study_button.invoke()
    wait_for_background_work(window)
    self.assertEqual(window.study["values"], ("Practice study · bbbbbbbb",))
    self.assertEqual(window.subject.get(), "")
    self.assertIn("subject-001", window.subjects["values"])

def test_start_names_exact_study_subject_and_trial_count(self):
    window.select_study("practice-study-bbbbbbbb")
    window.select_subject("subject-001")
    window.prepare_button.invoke()
    window.ack.set(True)
    with patch("tkinter.messagebox.askokcancel", return_value=False) as confirm:
        window.start_button.invoke()
    prompt = confirm.call_args.args[1]
    self.assertIn("Practice study", prompt)
    self.assertIn("subject-001", prompt)
    self.assertIn("2 assigned trials", prompt)
```

Also test cancellation, denial, expiry, browser-open failure, a revoked saved study, duplicate profile reuse, no subject preselection, no PGL import during pairing, disabled start when runtime is missing, and no automatic replay after any attempt.

- [ ] **Step 2: Run launcher tests and verify UI failures**

Run: `DBP_TEST_GUI=1 PYTHONPATH=src python3.12 -m unittest tests.test_launcher -v`

Expected: FAIL because the URL and browser-selection controls do not exist.

- [ ] **Step 3: Replace typed profile-name pairing with the browser flow**

The first section becomes:

```text
Website: [http://127.0.0.1:8769 ▼] [Choose study in browser]
Study:   [Practice study · bbbbbbbb ▼]
```

Keep **Manual pairing…** under an advanced/recovery disclosure. Run authorization polling on the existing background worker, provide **Cancel**, and show the non-secret user code so the coordinator can compare it with the browser page. On completion, refresh profiles and select the newly verified study.

- [ ] **Step 4: Show verified context and preserve rehearsal safeguards**

Display the origin, immutable assignment ID, subject count, selected-subject trial count, and preparation state. Keep the runtime preflight message visible. Require preparation and the non-participant checkbox. The final confirmation includes study name, assignment ID, subject ID, trial count, day 1/block 1 labels, and the warning that this is not an approved participant schedule.

- [ ] **Step 5: Run all launcher tests**

Run: `DBP_TEST_GUI=1 PYTHONPATH=src python3.12 -m unittest tests.test_launcher tests.test_runner -v`

Expected: PASS without opening real PGL.

- [ ] **Step 6: Commit the launcher flow**

```bash
git add src/dbp_pgl_runner/launcher.py src/dbp_pgl_runner/runner.py tests/test_launcher.py
git commit -m "Add coordinator study and subject picker"
```

### Task 5: End-to-end local authorization and preparation test

**Files:**
- Modify: `../dbp-dataset-browser/tests/test_study_runner_integration.py`
- Modify: `README.md`
- Modify: `../dbp-dataset-browser/docs/pgl-integration.md`

**Interfaces:**
- Consumes: complete website and runner flows from Tasks 1–4.
- Produces: an automated real-HTTP integration test and a reproducible human rehearsal checklist.

- [ ] **Step 1: Write the failing real-HTTP integration test**

Extend `WrapperIntegrationTests` to start authorization, approve the first owned published assignment through the authenticated client, poll and exchange through the runner client, publish the profile, load the exact study context, choose `subject-001`, prepare its package, reserve an attempt, append the expected integration milestones, upload native-shaped test artifacts, finalize, and verify offline status. Assert that the selected experiment ID remains identical at every boundary and that retrying exchange or reservation cannot create a second exposure.

- [ ] **Step 2: Run the integration test and verify the first missing boundary**

Run: `PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_integration.WrapperIntegrationTests -v`

Expected: FAIL until all new wire contracts are connected.

- [ ] **Step 3: Complete integration wiring without bypasses**

Use only the public runner authorization API, authenticated owner approval API, and existing runner APIs. Do not inject a token, experiment ID, subject ID, media path, or result directly into the wrapper. Keep synthetic native-shaped artifacts clearly labelled as an integration test, not actual PGL evidence.

- [ ] **Step 4: Update coordinator documentation**

Document URL entry, browser sign-in, study selection, subject selection, preparation, runtime checks, final confirmation, local result location, upload state, interruption recovery, manual pairing fallback, and revocation. State that the automated test does not certify display timing, input hardware, eye tracking, or the scientific schedule.

- [ ] **Step 5: Run the complete software verification**

Runner repository:

```bash
DBP_TEST_GUI=1 PYTHONPATH=src python3.12 -m unittest discover -s tests -v
python3.12 -m compileall -q src/dbp_pgl_runner
git diff --check
```

Website repository:

```bash
PYTHONPATH=.:tests .venv/bin/python -m unittest discover -s tests -v
node --test tests/*.cjs
.venv/bin/python -m py_compile app.py accounts.py portal.py request_safety.py \
  study_runner_auth.py study_runner_authorization.py study_runner_routes.py
.venv/bin/python -m bandit -q -ll -ii study_runner_auth.py \
  study_runner_authorization.py study_runner_routes.py
git diff --check
```

Expected: all tests pass; no credentials, databases, media, results, or runtime logs are staged.

- [ ] **Step 6: Commit integration coverage and docs**

Runner repository:

```bash
git add README.md
git commit -m "Document browser-mediated PGL rehearsal"
```

Website repository:

```bash
git add tests/test_study_runner_integration.py docs/pgl-integration.md
git commit -m "Test browser-selected single-subject preparation"
```

### Task 6: Install the local PGL runtime and conduct one rehearsal

**Files:**
- Runtime only: `.venv-pgl/` or another ignored private Python 3.12 environment.
- Runtime only: `~/.config/dbp-pgl/`, `~/.local/share/dbp-pgl/cache/`, and `~/.local/share/dbp-pgl/work/`.
- No committed media, credentials, databases, responses, or native outputs.

**Interfaces:**
- Consumes: the pinned PGL fork commit from `pyproject.toml`, a local FFmpeg executable, the updated local website, one published integration assignment, and the launcher from Task 4.
- Produces: one locally saved and synchronized non-participant attempt for one assigned subject.

- [ ] **Step 1: Build an isolated Python 3.12 environment**

Run:

```bash
python3.12 -m venv .venv-pgl
.venv-pgl/bin/python -m pip install -e '.[experiment]'
```

Expected: importing `pgl` succeeds. If native build prerequisites fail, record the exact missing prerequisite and stop before attempting presentation.

- [ ] **Step 2: Verify runtime tools without opening experiment hardware**

Run:

```bash
.venv-pgl/bin/python -c 'import pgl, tkinter; print("runtime imports ready")'
DBP_PGL_PYTHON="$PWD/.venv-pgl/bin/python" ./launch-pilot.command
```

Expected: the launcher shows PGL available. It must not create a PGL engine until **Start rehearsal** is confirmed.

- [ ] **Step 3: Start the updated local website with development data**

Use the existing local run script and runner environment variables, including an absolute macOS FFmpeg path with a compatible sibling `ffprobe`. Confirm `/api/health`, login, study publication, and the authorization page before pairing. Do not change production deployment.

- [ ] **Step 4: Pair and prepare one real saved assignment**

Enter the loopback website URL, choose the study in the browser, select one subject in the launcher, and click **Prepare videos**. Record the displayed study name, assignment ID, subject ID, trial count, package hash, and prepared path in private local notes. Do not start if any identity differs from the website.

- [ ] **Step 5: Conduct the non-participant rehearsal with the coordinator present**

Confirm installed PGL settings and display profiles. The coordinator checks the acknowledgement and clicks **Start rehearsal**. Complete the task as the tester. If interrupted, inspect status and use the explicit recovery workflow; never click Start again to continue.

- [ ] **Step 6: Verify durable outputs and synchronization**

Check that status reports the exact completed-trial count, native PGL files exist under the private attempt root, the journal is complete, `sync-receipt.json` matches the local artifact manifest, and the website PGL-runs view shows the same attempt. Use **Retry upload** only if synchronization is pending, and confirm that it does not open PGL.

- [ ] **Step 7: Record the validation boundary**

Document whether preparation, playback, responses, native saving, interruption controls, and synchronization worked. Keep the result private. Explicitly leave `pgl_ready=false` until Justin and the study team approve the scientific schedule, timing, display, response device, eye tracker, and participant interruption policy.

---

## Self-review

- Spec coverage: every coordinator-flow, authorization, storage, failure, testing, and out-of-scope requirement maps to Tasks 1–6.
- Placeholder scan: the plan contains no deferred implementation placeholders; each failure case names its expected behavior and test surface.
- Type consistency: website authorization methods, API paths, runner client methods, pairing objects, profile publication, and launcher controls retain the same names across producing and consuming tasks.
- Scope: server authorization, website picker, runner pairing, launcher UI, integration verification, and local rehearsal form one dependent delivery; none can safely ship as the requested workflow without the preceding tasks.
