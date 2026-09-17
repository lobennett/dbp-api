# DBP PGL runner — integration STARTED

**Non-participant preparation only. `pgl_ready` is always false.** This separate
wrapper pairs a workstation, downloads the exact server-sealed media, builds a
fixed-order PGL manifest, and verifies the prepared files offline. It does **not**
run an experiment or synchronize results. Both `run` and `sync` return nonzero
before accessing credentials, making requests, importing PGL, or opening devices.

## Install and test

Use Python **3.11+** on macOS or Linux. The runtime uses only Python's standard
library; file locking and private-file checks require POSIX. No deployment, billing,
or paid service is involved. Use the wrapper interpreter, not a Python 3.10 website
environment.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Without installation:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m dbp_pgl_runner --help
```

The optional `experiment` extra pins package `pgl` to reviewed fork commit
`4ffe661e7bdf4a5f0f02e84af54c02416d021261` on macOS. Installing it does **not** enable
execution. PGL's build requires its native macOS toolchain and additional library
prerequisites; none is needed for preparation or these tests. No PGL import occurs
in the wrapper. The fork's `pgl/pglImage.py` defines `pglMovieDatabase` and
`useManifest`; the manifest must name the loaded files by **basename**, not absolute
path. Each trial therefore gets a unique basename, including repeated clips.

## Coordinator workflow

Create a one-time pairing code in the website's saved-experiment runner panel.
Use a synthetic integration subject, not real participant data.

```sh
dbp-pgl connect
dbp-pgl prepare s001
dbp-pgl status s001
```

`connect` prompts for the server origin, consent to private-file credential storage,
and a **hidden** pairing code. `--server https://example.org`, `--device-name lab-mac`,
and `--allow-file-token` are optional connect flags; there is intentionally no
command-line token or pairing-code option. Hidden input fails closed if a terminal
cannot disable echo. Do not pipe secrets through shell commands or paste them into
logs. Pairing exchange is one-shot, with no automatic retry.

HTTPS is required except for `localhost` or a literal loopback IP, for example
`http://127.0.0.1:8000`. HTTP loopback is for the isolated local integration harness,
not remote workstation operation. Origins cannot contain userinfo, query strings,
fragments, or non-root paths. All redirects are refused, including same-origin
redirects; environment HTTP proxies are disabled. Signed external media URLs and
GCS delivery are not supported by this rollout.

Configuration defaults to `~/.config/dbp-pgl/config.json` (0600). The secret lives
in a separate random `token-<hex>` file (0600) under the private 0700 directory.
**The macOS Keychain backend is pending.** File storage requires explicit flag or
interactive consent and is suitable only for this integration harness. Existing
insecure permissions and symlink files are rejected rather than silently repaired.
Re-pairing retains old token files to avoid destroying credentials still referenced
by a prior config; revoke obsolete devices on the website and remove obsolete
token files deliberately. Never commit configuration or credentials.

The global options `--config-dir`, `--cache-root`, and `--work-root` precede the
subcommand. Cache and work roots must be private, distinct, and non-nested:

```sh
dbp-pgl --cache-root /private/tmp/dbp-cache --work-root /private/tmp/dbp-work prepare s001
```

Aliases are exactly `s001` through `s100`, or `subject-001` through `subject-100`.
The server response must match both the configured experiment and that canonical
subject. The server owns trial order and conditions; the wrapper never constructs
an assignment, shuffles trials, or substitutes a parent for a foil interval.

## Preparation and status semantics

`prepare` checks the device identity, fetches a sealed block, validates its exact
schema and `package_sha256`, checks free space, and streams media in chunks of at
most 1 MiB. Downloads are sequential. Unique content is bounded to 32 MiB by the
server contract. A cache entry is named by SHA-256 and becomes visible only after
length/digest verification, fsync, and atomic replacement of its partial name.

Interrupted downloads retain private `.partial` prefixes. A later preparation
requests the remaining range and checks its exact `Content-Range`. If the server
ignores Range, preparation truncates the partial and restarts at zero. The full
result must still match the sealed checksum. A corrupt completed cache entry is
never silently overwritten or deleted; investigate and remove it manually before
retrying. Previously verified cache files survive a failed preparation.

Prepared blocks are stored under:

```text
<work-root>/<experiment-id>/<device-id>/<canonical-subject>/<package-id>/
  block.json
  manifest.csv
  readiness.json
  trial-00000-<media-sha256>.mp4
  trial-00001-<media-sha256>.mp4
```

The media basenames correspond one-to-one to trials. Repeated media use distinct
hardlinks to the verified cache, or verified copies if the roots are on different
filesystems. The CSV columns are `filename,trial_index,condition`; indices and
integration condition labels come unchanged from the server. Never edit the media,
manifest, block, or receipt. Files are published read-only, but the workstation
owner can still change them; this is not an OS-enforced immutable archive.

The complete block directory is staged privately, fsynced, and atomically renamed.
Only then is its subject's `current.json` pointer atomically updated. Existing
package IDs cannot be replaced with changed documents. Repeated preparation checks
the existing block without rewriting its files. Advisory locks prevent concurrent
wrapper writers; process death releases locks automatically. A power/process loss
may leave an unreferenced `.prepare-*` staging directory; it is never considered
ready and can be removed after confirming no preparation is active.

`status` does not contact the server or require the secret token. It revalidates
the block digest, exact manifest order and file set, receipt/workstation binding,
and **all media hashes**, not merely sizes. It returns nonzero for missing,
changed, linked, or inconsistent data. The receipt binds the package, manifest,
per-trial inventory, time, wrapper version, origin, and device identity. It is a
SHA-256 integrity checksum, **not a keyed signature** and not protection against a
malicious workstation owner replacing all files and checksums.

Successful output states `preparation_ready: true`, `integration_status: STARTED`,
and `pgl_ready: false`. This means byte-integrity preparation only. Offline status
does not prove current authorization, lease ownership, media decoding, display or
input readiness, approved protocol mapping, or participant readiness.

## Interfaces for integration tests

```python
from dbp_pgl_runner.api import RunnerApi
from dbp_pgl_runner.config import RunnerConfig, save_pairing
from dbp_pgl_runner.prepare import prepare_subject, status_subject

response = RunnerApi.pair(origin, pairing_code, device_name)
config = save_pairing(config_dir, origin, response, allow_file_token=True)
api = RunnerApi(config, config.read_token(config_dir))
api.identity()
prepared = prepare_subject(api, config, "s001", cache_root, work_root)
verified = status_subject(config, "s001", work_root)
assert verified == prepared
assert prepared.package.pgl_ready is False
```

`RunnerConfig(server_origin, device_id, experiment_id, token_ref)` is frozen and
validates its fields. `token_ref` must be `token-` plus 32 lowercase hex characters.
`RunnerConfig.load(config_dir)` reads the stored non-secret config.
`PreparedBlock` is frozen with `.root: pathlib.Path` and `.package: BlockPackage`.
`BlockPackage.to_dict()` returns an independent copy of the exact sealed document;
`.trials` is an immutable tuple of frozen `Trial` values.

`api.next_block(subject_alias) -> BlockPackage` requests
`GET /api/runner-device/subjects/{quoted-alias}/next`.
`api.iter_media(package, trial, *, offset=0)` yields bytes from
`GET /api/runner-device/blocks/{package_id}/media/{quoted-clip_id}`.
The pairing and identity routes are respectively
`POST /api/runner-device/pairings/exchange` and
`GET /api/runner-device/identity`. No attempt/event/upload methods are advertised.
Errors are `ContractError`, `ApiError`, or storage `OSError`; network exceptions
omit response bodies and credential-bearing detail. No requests retry implicitly.

The standalone validator mirrors the server contract without importing its
allocation/database dependencies. The optional sibling-fixture test compares
canonical UTF-8 sealing against `dbp-dataset-browser/tests/test_study_runner_contract.py`;
it skips when that checkout is absent. HTTP tests use generated bytes and ephemeral
local servers, not repository media or participants. The separate website test
suite owns the real cross-repository HTTP integration.

The actual website-to-wrapper loopback test has passed: pairing exchange, saved
credentials, authenticated identity, preparation, exact basename manifest and media
hash verification, stable repeated preparation, HTTP server shutdown, and offline
status verification. Reproduce from the sibling **website** checkout with the
wrapper checkout beside it:

```sh
PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_integration.WrapperIntegrationTests -v
```

The website uses its own interpreter and launches the Python 3.11+ wrapper as a
separate process. This proves preparation integration, **not** launch or result
synchronization; both remain disabled.

## Explicitly pending — execution remains blocked

- Approved trial-milestone journal, crash/restart semantics, and recovery after
  exposure; no silent replay after `stimulus_started`.
- Reviewed PGL adapter, actual decode checks, display/input/hardware validation,
  native artifact capture, safe stop, and interruption handling.
- Server-issued attempts, exclusive leases, heartbeat, append-only event ingestion,
  artifact sealing/upload, synchronization, and QA receipts.
- Study-team approval of real new/old conditions, day/block mapping, foil intervals,
  and interrupted-exposure policy. Integration labels are not scientific conditions.
- Keychain credential storage, signed external media support, and any production
  hosting/security review.

`dbp-pgl run s001` and `dbp-pgl sync s001` deliberately fail with exit code 2. There
is no override, experimental bypass flag, fake completion, or simulated upload.
Prepared media alone must never be used to claim the full design is implemented.
