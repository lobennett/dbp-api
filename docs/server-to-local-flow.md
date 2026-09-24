# Server-to-local video flow

The databases store metadata and assignments; videos are separate files.

```mermaid
flowchart TD
    subgraph Server["DBP server"]
        Catalog[("Catalog database<br/>Video IDs, captions, transcripts")]
        Metrics[("Metrics database<br/>Duration, cuts, text labels, etc.")]
        AccountDB[("Account database<br/>Collections, assignments,<br/>published manifests, trial events")]
        Media[("Video files<br/>Server-accessible storage")]
        API["Authenticated DBP API"]
        Prepare["Prepare assigned media<br/>Full video OR rendered half"]

        Catalog --> API
        Metrics --> API
        API <--> AccountDB
        Media --> Prepare
        API -->|"Look up assigned trial"| Prepare
    end

    subgraph Local["Your computer"]
        Notebook["Notebook + Python client"]
        Assign["Create and publish assignments<br/>or reopen an experiment ID"]
        Subject["Choose subject"]
        Manifest["Fetch and validate manifest<br/>Fixed trial order + segment boundaries"]
        Download["Download each trial<br/>Verify file size and SHA-256"]
        Files[("Local download folder<br/>MP4 files + manifest.json")]
        Task["Your presentation program<br/>PGL, PsychoPy, etc."]
        Journal[("Local progress.sqlite3<br/>Started/completed events")]

        Notebook --> Assign --> Subject --> Manifest --> Download
        Download --> Files --> Task
        Task -->|"Explicit progress calls"| Journal
    end

    Assign <-->|"Save or retrieve assignments"| API
    Subject -->|"Request subject manifest"| API
    API -->|"Published manifest"| Manifest
    Download -->|"Request media by trial ID"| API
    Prepare -->|"MP4 bytes + checksum"| Download
    Journal -->|"Synchronize progress"| API
```

- Reopening an experiment ID does not resample.
- Halves are rendered server-side; downloaded files are ready to present.
- Downloads do not record presentations. The task must report progress explicitly.
- The notebook uses the authenticated HTTP API, not a direct database connection.
