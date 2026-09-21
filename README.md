# Digital Brain PGL runner

Fetch a saved study from the Digital Brain browser, prepare its exact videos,
run Justin's Digital Brain PGL task, save native results and a durable trial
journal, and synchronize results back to the website. Use the same workflow from
the command line or [the pilot notebook](examples/digital_brain_pilot.ipynb).

**Non-participant integration pilot.** The software can execute PGL, but this is
not certification of the scientific protocol, timing, display, response device,
or eye tracker. Packages remain `pgl_ready: false`. Hardware validation and study
approval are separate from the automated tests. No paid hosting is needed.

## Install

Use **Python 3.12+ on macOS for PGL execution**. Preparation, journals and sync
also work on POSIX with Python 3.11+. The wrapper uses the standard library.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[experiment]'
source .venv/bin/activate
```

The experiment extra pins the PGL fork to an exact commit in `pyproject.toml`.
PGL requires its native macOS build prerequisites and lab configuration.
FFmpeg must be installed locally for the full video-decode check. Pass
`--ffmpeg /absolute/path/to/ffmpeg` if it is not on `PATH`.
For preparation/synchronization without native PGL: `pip install -e .`.
Upstream PGL currently uses Python 3.12 syntax despite a less restrictive
package declaration; do not install the experiment extra into Python 3.11.

For Jupyter, install/register the environment once, then select its kernel:

```sh
.venv/bin/python -m pip install ipykernel
.venv/bin/python -m ipykernel install --user --name dbp-pgl --display-name 'Python 3.12 (DBP PGL)'
```

## Website → pilot → saved results

1. On the **updated** local browser or live website, save a study, publish its
   integration-test assignment, and choose **Pair workstation**.
2. Pair this computer: `dbp-pgl connect`.
3. Run one synthetic subject: `dbp-pgl run s001 --integration-test`.
4. Inspect results: `dbp-pgl status s001`.
5. Retry synchronization without presenting again: `dbp-pgl sync s001`.

`run` automatically prepares media, fully checks decoding, reserves an exclusive
attempt, launches real PGL, saves locally, and tries to synchronize afterward.
The notebook offers the same sequence. Alternatively:

```sh
python examples/digital_brain_pilot.py run s001 --integration-test
```

Preparation can be done ahead of time with `dbp-pgl prepare s001`.
`run --no-sync` retains results locally for later synchronization.
Use `--settings-name` and `--display-name` for installed PGL profiles;
`--day`, `--block`, `--description-seconds`, and `--display-width` configure the
integration pilot. Defaults match the notebook's 12-second description and
50-degree width; lab calibration determines whether they are appropriate.

**The live website must run the corresponding server integration.** Updating this
package does not update the website. Use a current local instance until the live
server has the runner APIs; deployment is not part of this package.

## Python / Jupyter

```python
from dbp_pgl_runner.runner import StudyRunner
from dbp_pgl_runner.pgl_adapter import RunSettings

runner = StudyRunner()
prepared = runner.prepare("s001")
result = runner.run("s001", integration_test=True,
                    settings=RunSettings(day=1, block=1))
runner.status("s001")
runner.sync("s001")
```

For a notebook with an already-configured PGL instance, import `PglAdapter` and
pass `adapter=PglAdapter(engine=your_pgl_instance)` to `runner.run`. The wrapper
still owns the experiment, manifest, output directory, journal and cleanup;
do not also call the original notebook's `e.run()` independently.

The returned `attempt_root` contains:

```text
<work-root>/<experiment>/<device>/<subject>/attempts/<attempt-id>/
  attempt.json
  reservation.json
  decode.json
  journal.json
  events.jsonl
  native/<experiment>/<subject>/dayN/<attempt-id>/...
  artifact-manifest.json
  sync-receipt.json
```

Native PGL files include settings, state, responses, task parameters, and
eye-tracker output when configured. Required native files must exist before
completion is recorded. `sync-receipt.json` appears only after the server confirms
the exact result inventory. Completed presentation and successful upload are
separate states. Refresh **PGL runs** in the website for received progress;
there are deliberately no network requests during presentation.

## Interrupted sessions

- Escape or an exception does not imply every trial completed. The journal
  distinguishes loading, possible exposure, playback return, response collection,
  persisted responses and completed trials.
- Interrupt handling attempts native saving and display/device cleanup. A hard
  process kill or power loss cannot guarantee native output; earlier durable
  journal records survive. Cleanup failure is not scientific completion.
- An interrupted attempt is never silently replayed. After a crash:

  ```sh
  dbp-pgl status s001
  dbp-pgl recover s001 --terminate
  dbp-pgl sync s001
  ```

  Only if an incomplete final journal fragment is reported, explicitly add
  `--repair-tail`. Earlier corruption or a complete altered record remains an error.
- Re-exposure requires a new reviewed study assignment. There is no automatic
  continue/replay policy for participant memory experiments.
- Failed synchronization never requires re-running PGL. Identical event and
  artifact retries are safe; conflicting replacements are rejected.
- The exclusive reservation does not silently expire while an offline experiment
  may still run. If a workstation is lost, an owner must confirm it has stopped
  before explicitly terminating its reservation.

## Integrity and limits

The server owns order and conditions; the wrapper never shuffles or assigns
subjects. Integration mapping: parent → `new-integration-parent`, repeat →
`old-integration-repeat`, foil → `new-integration-foil`. These are **not an approved
scientific schedule**. Day/block settings are not inferred scientific allocation.

Videos download into a SHA-256 cache with byte-range resume. Each trial gets a
unique basename, including repeated clips, because PGL consumes a basename
manifest. Media hashes are checked again before execution and each distinct video
is completely decoded with FFmpeg. Presentation uses only local files. Limits:
32 MiB/video, 2 GiB distinct media/subject package. Larger studies need appropriate
compressed renditions or reviewed blocks rather than bypassing these bounds.

Foils are rendered subclips of their assigned intervals, never full parents with
different labels. The server requires `DBP_RUNNER_FFMPEG_PATH` and sibling `ffprobe`.
Missing tools or failed interval validation stop preparation.

Journal appends are flushed, fsynced, sequence-numbered and hash-chained. PGL hook
times mark API boundaries, **not measured frame onset**. `response_saved` stores
the description in the native task and durable journal; native files are verified
separately at exit.

Artifacts exclude stimuli/credentials, reject symlinks/traversal, and are sealed
before upload in chunks ≤1 MiB. Bounds: 1,024 files, 256 MiB/file, 1 GiB/attempt.
Larger eye-tracker files need a reviewed extension. The server verifies file hashes,
exact journal bytes, milestones, native inventory and final manifest. Checksums
protect integrity, not against an authorized workstation fabricating an experiment.

## Credentials and storage

Use HTTPS for live origins, or HTTP only on literal loopback/localhost. The old
public HTTP-only site is not a safe credential endpoint; an authenticated SSH
tunnel to loopback is an alternative. Redirects and implicit environment proxies
are refused. Pairing codes/tokens never appear in command-line arguments.

`connect` uses hidden input and asks for private-file storage consent. Optional
flags: `--server`, `--device-name`, `--allow-file-token`. Keychain is not implemented;
secrets use owned mode-0600 files inside a mode-0700 directory. Native outputs are
private. Never commit credentials, participant responses, journals, media, or databases.

Global `--config-dir`, `--cache-root`, `--work-root` options precede the command.
Defaults: `~/.config/dbp-pgl`, `~/.local/share/dbp-pgl/cache`,
`~/.local/share/dbp-pgl/work`. Cache/work directories must be private, separate and
non-nested. Re-pairing retains previous tokens; revoke old devices and remove
unused tokens deliberately. Subjects: `s001`–`s100` / `subject-001`–`subject-100`.

## Verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests cover execution, interrupts, duplicate synchronization, altered artifacts,
unsafe paths, credentials, notebook syntax and native adapter cleanup with
headless test doubles. In the sibling website checkout, the real HTTP test covers
pairing → preparation → reservation → journal → native-shaped test outputs → upload
→ idempotent finalization → offline inspection:

```sh
PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_integration.WrapperIntegrationTests -v
```

This verifies software integration, not actual display/input/eye-tracker behavior.
Before participants, Justin/the study team must confirm conditions, foil/repeat
timing, interruptions and subject/day/block mapping, then run a non-participant
lab test and inspect native outputs.
