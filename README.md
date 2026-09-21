# Digital Brain PGL runner

`dbp-pgl-runner` downloads a published DBP study assignment, verifies its media, runs the PGL task in the saved order, records trial progress, and synchronizes results to the website. The command line and [pilot notebook](examples/digital_brain_pilot.ipynb) use the same workflow.

This is an integration tool, not scientific or hardware certification. Participant use still requires approval of timing, display, response-device, eye-tracker, and interruption behavior.

## Install

PGL execution requires Python 3.12+ on macOS. Preparation and synchronization work on POSIX with Python 3.11+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[experiment]'
```

The experiment extra pins the PGL fork. Install FFmpeg for full media decoding. For preparation and synchronization without PGL, use `pip install -e .`.

Optional Jupyter kernel:

```bash
python -m pip install ipykernel
python -m ipykernel install --user --name dbp-pgl --display-name 'Python 3.12 (DBP PGL)'
```

## Run a study

1. Publish an integration assignment in the DBP website and choose **Pair workstation**.
2. Pair this computer: `dbp-pgl connect`.
3. Run a rehearsal: `dbp-pgl run s001 --integration-test`.
4. Inspect state: `dbp-pgl status s001`.
5. Retry upload without replaying videos: `dbp-pgl sync s001`.

`run` prepares and verifies media, reserves an attempt, launches PGL, writes local results, and then tries to synchronize. Use `dbp-pgl prepare s001` to prepare in advance or `run --no-sync` to upload later.

Equivalent Python:

```python
from dbp_pgl_runner.runner import StudyRunner
from dbp_pgl_runner.pgl_adapter import RunSettings

runner = StudyRunner()
runner.prepare("s001")
runner.run("s001", integration_test=True, settings=RunSettings(day=1, block=1))
runner.status("s001")
runner.sync("s001")
```

The website must expose the matching runner APIs. Updating this package does not deploy the website.

## Results and recovery

Each attempt is stored under:

```text
<work-root>/<experiment>/<device>/<subject>/attempts/<attempt-id>/
├── attempt.json
├── reservation.json
├── decode.json
├── journal.json
├── events.jsonl
├── native/
├── artifact-manifest.json
└── sync-receipt.json
```

Presentation and upload are separate states. No network requests occur during presentation. If a run stops unexpectedly:

```bash
dbp-pgl status s001
dbp-pgl recover s001 --terminate
dbp-pgl sync s001
```

An interrupted attempt is never replayed automatically. Failed uploads can be retried without rerunning PGL. Use `--repair-tail` only when status reports an incomplete final journal fragment.

## Integrity and limits

- The server owns trial order and conditions; the runner does not shuffle or assign subjects.
- Media use a SHA-256 cache, support resumed downloads, and are fully decoded before execution.
- Presentation uses local files only.
- Foils are rendered from assigned intervals; full parents are never substituted.
- Default limits are 32 MiB per video and 2 GiB of distinct media per subject package.
- Journals are flushed, sequence-numbered, and hash-chained.
- Uploads exclude stimuli and credentials and reject symlinks and path traversal.
- PGL hook times mark API boundaries, not measured visual onset.

Checksums detect alteration; they do not prove that an authorized workstation ran the experiment correctly.

## Credentials and storage

Use HTTPS for remote servers or HTTP only on loopback. Pairing secrets are entered through hidden input and stored only with explicit consent in private files. Keychain support is not implemented.

Global path options precede the command:

```text
--config-dir  ~/.config/dbp-pgl
--cache-root  ~/.local/share/dbp-pgl/cache
--work-root   ~/.local/share/dbp-pgl/work
```

Keep configuration, cache, and work directories private, separate, and non-nested. Never commit credentials, media, journals, participant responses, or databases.

## Verify

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests cover preparation, execution, interruption, synchronization, path safety, credentials, and adapter cleanup with test doubles. They do not verify real display, input-device, eye-tracker, or scanner timing.
