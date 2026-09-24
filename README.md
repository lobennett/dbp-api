# DBP API

Query the Digital Brain website, inspect metric coverage, and download filtered
videos for your own analysis or experiment. Python 3.11+; no runtime dependencies.

## Install

```sh
pip install "git+https://github.com/lobennett/dbp-api.git"
```

## Demo notebook

```sh
git clone https://github.com/lobennett/dbp-api.git
cd dbp-api
uv run --extra demo jupyter lab examples/dbp_api_demo.ipynb
```

The [demo](examples/dbp_api_demo.ipynb) connects to your website, lists every metric
and its measured/unknown video counts, filters videos, and optionally creates an
experiment and downloads one subject's assignments. Downloads are ordinary MP4s
plus a manifest, ready for any downstream package. Nothing launches automatically.

Use a website running the `/api/v1` endpoints. HTTPS is required except on localhost.
Credentials are entered interactively; clear notebook outputs before sharing.
Unknown measurements are not negative classifications. Counts describe the server's
loaded dataset, not necessarily every video in the underlying archive.

## Python

```python
from getpass import getpass
from dbp_api import Client, MetricFilter

with Client("http://127.0.0.1:8773") as client:
    client.login(input("Username: "), getpass("Password: "))
    result = client.query_media(filters=[MetricFilter("duration_seconds", "gte", 10)])
    print(result["matching"], "of", result["total"], "videos")
    client.logout()
```

Queries return one page of rows and whole-selection distributions. Use the returned
cursor for more rows. Experiment creation pins the query version and filters;
rerun the query if the dataset changes. Downloads verify file sizes and SHA-256
hashes and require a new destination directory.

## Tests

```sh
uv run python -m unittest discover -s tests
```
