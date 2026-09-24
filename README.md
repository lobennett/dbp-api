# DBP API

A platform-independent Python SDK for querying videos, creating experiments, and
downloading assigned trials. Python 3.11+; no runtime dependencies, PGL, or launcher.

## Install

```sh
python -m pip install "git+https://github.com/lobennett/dbp-api.git"
```

## Query, create, publish, download

```python
from getpass import getpass
from dbp_api import Client, ExperimentSpec, MetricFilter, media_from_row

with Client("https://your-dbp-host.example", timeout=30) as client:
    client.login(input("Username: "), getpass("Password: "))
    inventory = client.metrics()
    # Choose metric IDs/operators from inventory["metrics"].
    filters = [MetricFilter("duration_seconds", "gte", 3)]
    result = client.query_media(filters=filters, limit=100)
    videos = [media_from_row(row) for row in result["rows"]]

    spec = ExperimentSpec(
        name="Video experiment",
        seed="pilot-1",
        subject_count=100,
        parents_per_subject=50,  # TOTAL: 10 parents/block × 5 blocks
        block_count=5,
        foils_per_block=4,       # 20 additional trials, 70 total per subject
    )
    experiment = client.create_experiment(
        spec, filters=filters, version=result["version"],
    )
    publication = client.publish(experiment["id"])
    subject_id = publication["subjects"][0]["subject_id"]
    manifest_path = client.download_subject(
        experiment["id"], subject_id, "./subject-download",
    )
    client.logout()
```

Creation requires an explicit query `version`; re-use the same filters and custom
metric definitions that produced that version. Query results retain the server's
rows, aggregates, version, and pagination fields unchanged. Use `cursor` on
`query_media()` to request subsequent pages. Dataset changes can return HTTP 409;
query again and deliberately create a new experiment rather than silently retrying.
For semantic/hybrid searches, carry the query's `search_version` into creation,
along with the same `relevance_min`, `relevance_max`, and `cpu_pool` options.

`parents_per_subject` always means the total across all blocks and must divide
evenly into `block_count`. Shared parents are included in that total; repeats and
foils are extra trials. `shared_per_subject`, `repeats_per_subject`, and
`foils_per_subject` default to zero. If supplied, `foils_per_block` derives the
total foil count; a conflicting nonzero `foils_per_subject` is rejected.
Current server limits: 100 subjects, 10,000 parents per subject, 50,000 total trials;
shared, repeat, and foil counts cannot exceed parents per subject.

## Other operations

- `client.session()`: inspect the account session and refresh its CSRF token.
- `client.experiments(limit=100, offset=0)`: return the `experiments` envelope;
  pass its non-null `next_offset` into the next request.
- `client.experiment(experiment_id)`: detail, settings, subjects, recipe, publication.
- `client.subject_manifest(experiment_id, subject_id)`: validated published manifest.
- `client.query_media(experiment_id=experiment_id, subject_id="subject-001")`:
  inspect one subject's assigned parents and metric distributions, using the saved
  dataset version. Add `filters` to narrow inspection without changing assignments.
  Omit `subject_id` for all assigned parents. Foils need segment measurements;
  these distributions describe parents.
- `client.preview_custom_metric("cats", version=result["version"])`: preview a
  custom text metric (`corpus="both"`, `method="keyword_bm25_v1"` by default).
  Pass its `definition` in `custom_metrics=[preview["definition"]]` to queries and creation.

Preview uses the existing `/api/metrics/custom/preview` endpoint. General operations
use `/api/v1/metrics`, `/api/v1/media/query`, and `/api/v1/experiments`.
Authentication uses the existing `/api/auth/login`, `session`, and `logout` routes.

## Media and downloads

`Media` is abstract; `Video`, `Image`, and `Stimulus` are typed descriptors.
Only video execution is supported. Passing `media_type=Image`, `Stimulus`, or
their string names to creation raises `UnsupportedMediaError` before any request.
Metric inventory exposes supported `media_types`; no presentation framework is imported.
`media_from_row(row)` converts query rows (`clip_id`) or manifest trials (`media_id`)
to typed media descriptors. Media identifiers allow normalized printable catalog IDs,
including leading hyphens; they are never used as paths or filenames.

Download into a **new directory whose parent already exists**. Existing directories
and symlinks are refused; any failed download removes only the newly created directory.
Trial IDs are validated and media paths are derived from authenticated API routes,
never from server-provided URLs or filenames. Foils are rendered by the server.
Files stream in bounded chunks with configurable `max_media_bytes` (4 GiB per
trial by default). Both `Content-Length` and `X-Content-SHA256` are required and
verified. Subject manifests are checked using the backend's compact, sorted-key,
UTF-8 JSON SHA-256, excluding only the top-level `manifest_sha256`.

The returned `manifest.json` contains `{"manifest": <unchanged server manifest>,
"files": {<trial_id>: <relative MP4 path>}}`. Its nested server hash remains valid:
local paths are never inserted into the sealed server document. The enclosing local
document is not covered by that server hash. No PGL or other experiment runner launches.

## Security and errors

HTTPS is required except for exact localhost/loopback origins. URL credentials,
paths, queries, fragments, and all redirects are refused. Passwords are sent only
to login and never stored by the client; cookies and CSRF tokens stay in memory.
Environment proxies are disabled. Timeout is a per-socket-operation timeout, not
a whole-download deadline. JSON responses are size-bounded (16 MiB by default).

`ApiError.status` exposes HTTP failures. Expected HTTP 400/409 errors outside auth
include up to 512 characters of the server's `detail` (for example, insufficient
eligible videos); treat this as server-supplied text. All other response bodies and
all authentication error details are withheld. No request payload is added to errors.
Invalid inputs raise `ValueError`. There are no automatic retries. A client is
synchronous and not thread-safe. `logout()` revokes the server session and clears
local credentials even if it fails; `close()` and context-manager exit only clear
local credentials. Call `logout()` explicitly when server revocation is desired.

## Legacy compatibility

`dbp_pgl_runner` remains in this checkout as **deprecated compatibility code**.
Existing results, journals, examples, and launcher files are not removed or migrated.
The `dbp-api` wheel contains only `dbp_api`, so it does not overwrite an
existing runner installation. It has no launcher entrypoint or PGL dependency.
For an existing legacy environment, the old commands can still run from this
checkout with `PYTHONPATH=src python -m dbp_pgl_runner`. New integrations use
`dbp_api` to obtain media, then pass local files to their presentation framework.

## Development

```sh
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src
```
