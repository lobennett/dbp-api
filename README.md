# DBP API

Select database items, assign them to subjects, download them, and report
presentation progress. Python 3.11+; no playback framework or runtime dependencies.

## Notebook

```sh
git clone https://github.com/lobennett/dbp-api.git
cd dbp-api
uv run --extra demo jupyter lab examples/dbp_api_demo.ipynb
```

The [single demo](examples/dbp_api_demo.ipynb) lists all metrics and measured/unknown
video counts, filters videos, and optionally saves assignments and downloads media.
It includes commented examples for presentation reporting and future image support.
Use a website with the matching `/api/v1` experiment **and progress** endpoints.
HTTPS is required except on localhost. Clear notebook outputs before sharing.

## Interface

```python
from getpass import getpass
from dbp_api import Client, MetricFilter

client = Client("http://127.0.0.1:8773")
client.login(input("Username: "), getpass("Password: "))
experiment = client.create_experiment(
    name="Demo", seed="demo-1",
    filters=[MetricFilter("duration_seconds", "gte", 10)],
)
assignments = experiment.assign(subjects=2, items_per_subject=10, blocks=2)
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

## Tests

```sh
uv run python -m unittest discover -s tests
```
