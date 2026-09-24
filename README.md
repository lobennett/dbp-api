# DBP API

Select database items, assign them to subjects, download them, and report
presentation progress. Python 3.11+; no playback framework or runtime dependencies.

## Notebook

```sh
git clone https://github.com/lobennett/dbp-api.git
cd dbp-api
uv run --extra demo jupyter lab examples/dbp_api_demo.ipynb
```

The [notebook](examples/dbp_api_demo.ipynb) walks through sign-in, metric coverage,
creation, verification, downloads, and a first-half/second-half preview.
Edit the URL and username; only the password is prompted. Enter `"new"` to create,
or a saved experiment ID to reopen. Downloads happen only when you run that step.

The demo needs the updated `/api/v1` server with `DBP_CUT_BALANCE_CANDIDATES` and
`DBP_CUT_BALANCE_SHA256` configured for measured complementary halves. Media and
candidate data are not included. Use HTTPS except on localhost.
Clear outputs before sharing; previews embed video data.

## Interface

```python
from getpass import getpass
from dbp_api import Client, MetricFilter

client = Client("http://127.0.0.1:8773")
client.login("your-username", getpass("Website password: "))
experiment = client.create_experiment(
    name="Demo", seed="demo-1",
    filters=[MetricFilter("duration_seconds", "gte", 10)],
)
assignments = experiment.assign(subjects=2, items_per_subject=10, blocks=2)
print(assignments.summary())
session = assignments.subject("subject-001")
session.download("./subject-videos")
# Your task reports session.started(trial) and session.completed(trial).
client.logout()
```

| Type | Purpose |
|---|---|
| `Experiment` | Local recipe with filters, dataset version, and string seed. |
| `Assignments` | Saved, published subject assignments. `assign()` creates these. |
| `Session` | One subject's downloads, journal, and synchronized progress. |
| `Trial` | Media ID, block, role, optional `Segment`, and local path. |
| `Progress` | Server-confirmed started/completed state for each trial. |

`items_per_subject` counts originals across all blocks. Foils and repeats are
extra. Set `foils_per_block`, `shared_per_subject`, or `repeats_per_subject` as needed.
`assignments.summary()` returns one dictionary per subject: trial and video counts,
full/segment/repeat/foil counts, and measured cut/no-cut/unknown counts. It reads
the actual published trials, not estimates from the settings.
To resume, use `client.assignments(saved_experiment_id)` instead of calling
`assign()` again. New assignment calls create new records. Downloads require a
new destination and verify file hashes; reopening the same workspace restores paths.

`started()` and `completed()` save locally before synchronization. Stop if starting
fails. Complete only after successful playback; these calls do not verify playback.
`pending_trials` excludes previously started trials. Inspect `incomplete_trials`
after interruption—nothing silently replays. Retry `sync()` after connection loss;
event IDs prevent duplicate uploads. Other devices see only synchronized events.
Keep `.dbp/` journals until upload is confirmed. Calls involve network I/O, so keep
them outside timing-critical rendering code. The server currently supports videos;
`Image` and `Stimulus` are descriptors for future support, not working modalities.

## Use with a presentation framework

Create and publish once in the notebook. Save the experiment ID, then reopen it
in any task notebook or program with `client.assignments(experiment_id)`.
Choose a subject, download its fixed trials, and pass each local file to your
playback code. Report started/completed events through the session; do not call
`assign()` again to launch an existing experiment.

PGL and PsychoPy can use this Python client. Playback, timing, responses, and
task-result storage remain the presentation program's responsibility. A jsPsych
task needs a JavaScript adapter to the authenticated HTTP API (or a backend bridge);
this repository does not yet provide that adapter. No framework is launched by
the API itself. See the notebook's progress example before connecting real playback.

## Tests

```sh
uv run python -m unittest discover -s tests
```
