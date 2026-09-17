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

### Coordinator launcher (recommended)

Open `dbp-pgl launch`, or double-click `launch-pilot.command` in a source
checkout on a Mac. The latter finds a local Python 3.12+ with Tk; set
`DBP_PGL_PYTHON` to use a specific installed interpreter. It does not install
anything, start a web server, or start PGL automatically. PGL and FFmpeg must be
installed in that interpreter/environment before actual presentation.

1. Start an updated local website, enter its literal loopback URL (or an HTTPS
   URL) in the launcher, and choose **Choose study in browser**.
2. Sign in in the browser if needed. Compare the displayed code with the
   launcher's code, select one of your published integration assignments, and
   approve it. The launcher polls, exchanges once, verifies the device and study,
   then creates a private, study-derived saved connection; it never receives the
   browser password or session.
3. Select that verified connection. Check its study name, origin, immutable
   assignment ID, and roster, then choose `subject-001` from the assigned-subject
   dropdown.
4. Choose **Prepare videos**, inspect the package/manifest and runtime checks,
   acknowledge non-participant use, then choose **Start test**. The final
   confirmation names the exact assignment and subject. Preparation verifies
   media hashes; starting also fully decodes videos before presentation.
5. Inspect the saved-results path and upload state. **Retry upload** never
   replays the videos. An existing attempt prevents starting again; interruptions
   require the explicit recovery/review workflow below.
6. If browser authorization is unavailable, reveal **Manual pairing…** and use
   the website's one-time code with explicit private-file-storage consent. It is
   recovery-only, not the normal study-selection workflow. Revoke a lost or
   retired workstation from the website before discarding its local profile.

The pairing code, not the subject ID, identifies the study. Each device
credential is restricted to one immutable published assignment. The launcher
supports several saved connections without broadening any credential's access:
select a different connection to switch studies. Publishing a revised assignment
requires a new pairing; it does not retarget an existing one. Subjects are fetched
from the paired assignment rather than guessed or entered into Python code.

Existing CLI connections appear as **default**. New connections live at
`~/.config/dbp-pgl/profiles/<name>` and never overwrite existing configurations.
For a browser-selected study, use the selected profile for every CLI inspection,
recovery, or upload command; do not fall back to the default connection:

```sh
PROFILE=~/.config/dbp-pgl/profiles/<selected-profile>
dbp-pgl --config-dir "$PROFILE" status subject-001
dbp-pgl --config-dir "$PROFILE" sync subject-001
```

The launcher uses the existing runner, not a second task implementation. PGL
runs on the main thread of a separate Python process while the launcher remains
responsive. It uses the entire integration block, labelled day 1/block 1, with
the existing 12-second description and 50-degree width defaults. These are not
approved participant session settings. The workstation profile fields select
installed PGL lab settings; scientific scheduling remains a separate validation.
The launcher disables Start when its Python cannot find PGL or is not a supported
macOS/Python version. Preparation and result inspection remain available.
At launch, a private temporary credential/configuration snapshot binds the child
to the confirmed connection even if the original profile changes; it is removed
when the child exits. Native console output is drained into a bounded memory
buffer rather than an unbounded log on the results disk.

Use the updated local website until the live server includes
`GET /api/runner-device/study` and the other runner endpoints. Starting only the
launcher does not make an older website compatible. Tk is an optional local
desktop requirement; normal CLI commands do not import it.

### Command line / notebook

1. On the **updated** local browser or live website, save a study, publish its
   integration-test assignment, and choose **Pair workstation**.
2. Pair this computer: `dbp-pgl connect`.
3. Set the selected browser-created profile before run, status, or sync:

```sh
PROFILE=~/.config/dbp-pgl/profiles/<selected-profile>
dbp-pgl --config-dir "$PROFILE" prepare s001
dbp-pgl --config-dir "$PROFILE" run s001 --integration-test
dbp-pgl --config-dir "$PROFILE" status s001
dbp-pgl --config-dir "$PROFILE" sync s001
```

`run` automatically prepares media, fully checks decoding, reserves an exclusive
attempt, launches real PGL, saves locally, and tries to synchronize afterward.
The notebook offers the same sequence. Alternatively:

```sh
python examples/digital_brain_pilot.py run s001 --integration-test
```

`dbp-pgl --config-dir "$PROFILE" prepare s001` can be done ahead of time.
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
  PROFILE=~/.config/dbp-pgl/profiles/<selected-profile>
  dbp-pgl --config-dir "$PROFILE" status s001
  dbp-pgl --config-dir "$PROFILE" recover s001 --terminate
  dbp-pgl --config-dir "$PROFILE" sync s001
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
browser authorization → authenticated assignment approval → one-time
exchange/profile publication → subject selection → preparation → reservation →
journal → clearly synthetic native-shaped test outputs → upload → idempotent
finalization → offline inspection:

```sh
DBP_PGL_RUNNER_SOURCE=/absolute/path/to/dbp-pgl-runner/src \\
  PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_study_runner_integration.WrapperIntegrationTests -v
```

This verifies software integration, not actual display/input/eye-tracker behavior.
For an opt-in local test of the actual Tk controls with a mocked presentation
process (no task display or participant data):

```sh
DBP_TEST_GUI=1 PYTHONPATH=src python3.12 -m unittest tests.test_launcher -v
```

Before participants, Justin/the study team must confirm conditions, foil/repeat
timing, interruptions and subject/day/block mapping, then run a non-participant
lab test and inspect native outputs.
