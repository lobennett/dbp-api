"""Experiment recipes, fixed assignments, and presentation-independent sessions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING, Iterator, Literal, TypedDict, cast
from uuid import uuid4

from .models import ExperimentSpec, JSONObject, JSONValue, MediaType, MetricFilter, identifier

if TYPE_CHECKING:
    from .client import Client


class _SegmentDocument(TypedDict):
    start_seconds: float
    end_seconds: float


class _TrialDocument(TypedDict):
    trial_id: str
    media_id: str
    media_type: MediaType
    role: Literal["parent", "repeat", "foil"]
    segment: _SegmentDocument | None


class _BlockDocument(TypedDict):
    block_index: int
    trials: list[_TrialDocument]


class _SubjectDocument(TypedDict):
    manifest_sha256: str
    blocks: list[_BlockDocument]


def subject_ids(publication: JSONObject) -> tuple[str, ...]:
    subjects = publication.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("Publication has no subject roster")
    result = []
    for subject in subjects:
        if not isinstance(subject, dict):
            raise ValueError("Invalid subject roster")
        result.append(identifier(subject.get("subject_id")))
    if len(set(result)) != len(result):
        raise ValueError("Duplicate subjects in publication")
    return tuple(result)


@dataclass(frozen=True)
class Segment:
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class Trial:
    trial_id: str
    media_id: str
    media_type: MediaType
    block_index: int
    role: Literal["parent", "repeat", "foil"]
    segment: Segment | None = None
    local_path: Path | None = None


@dataclass(frozen=True)
class Selection:
    filters: tuple[MetricFilter, ...]
    content_query: str
    corpus: str
    version: str
    search_version: str | None = None

    def to_dict(self) -> dict:
        return dict(filters=self.filters, content_query=self.content_query, corpus=self.corpus,
                    version=self.version, search_version=self.search_version)


@dataclass(frozen=True)
class TrialProgress:
    status: Literal["started", "completed"]
    attempt_id: str


@dataclass(frozen=True)
class Progress:
    manifest_sha256: str
    trials: dict[str, TrialProgress]


@dataclass(frozen=True)
class Experiment:
    """A local selection recipe; assign() saves and publishes its assignments."""

    client: Client
    name: str
    seed: str
    selection: Selection

    def assign(self, *, subjects: int, items_per_subject: int, blocks: int = 1,
               foils_per_block: int = 0, shared_per_subject: int = 0,
               repeats_per_subject: int = 0, balance_cuts: bool = False) -> Assignments:
        spec = ExperimentSpec(self.name, self.seed, subjects, items_per_subject,
                              shared_per_subject=shared_per_subject, repeats_per_subject=repeats_per_subject,
                              block_count=blocks, foils_per_block=foils_per_block, balance_cuts=balance_cuts)
        record = self.client._create_experiment(spec, **self.selection.to_dict())
        experiment_id = str(record["id"])
        try:
            publication = self.client.publish(experiment_id)
        except Exception as error:
            raise RuntimeError(f"Assignments saved as {experiment_id}, but publication failed. "
                               "Retry client.publish(id), not assign().") from error
        return Assignments(self.client, experiment_id, subject_ids(publication))


@dataclass(frozen=True)
class Assignments:
    """Published subject assignments stored on the website."""

    client: Client
    experiment_id: str
    subject_ids: tuple[str, ...]

    def subject(self, subject_id: str, *, workspace: str | Path = ".dbp") -> Session:
        if subject_id not in self.subject_ids:
            raise ValueError("Choose a subject_id from assignments.subject_ids")
        return Session(self.client, self.experiment_id, subject_id, workspace=Path(workspace))


class Session:
    """A subject's trials and durable local event journal; never plays media."""

    def __init__(self, client: Client, experiment_id: str, subject_id: str,
                 *, workspace: Path) -> None:
        self.client = client
        self.experiment_id = experiment_id
        self.subject_id = subject_id
        self.manifest = cast(_SubjectDocument, client.subject_manifest(experiment_id, subject_id))
        self._path = client._subject_path(experiment_id, subject_id)
        identity = json.dumps([client.origin, experiment_id, subject_id, self.manifest["manifest_sha256"]])
        self.workspace = Path(workspace).resolve() / hashlib.sha256(identity.encode()).hexdigest()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal_path = self.workspace / "progress.sqlite3"
        with self._database() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, trial_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, kind TEXT NOT NULL,
                    occurred_at REAL NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS files (
                    trial_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL
                );
            """)
        self.journal_path.chmod(0o600)

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.journal_path)
        database.row_factory = sqlite3.Row
        try:
            with database:
                yield database
        finally:
            database.close()

    @property
    def trials(self) -> tuple[Trial, ...]:
        with self._database() as database:
            files = {row["trial_id"]: Path(row["path"]) for row in database.execute("SELECT * FROM files")}
        return tuple(Trial(trial["trial_id"], trial["media_id"], trial["media_type"], block["block_index"],
                           trial["role"], Segment(**trial["segment"]) if trial["segment"] else None,
                           files.get(trial["trial_id"]))
                     for block in self.manifest["blocks"] for trial in block["trials"])

    @staticmethod
    def _digest(path: Path) -> str:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    def download(self, destination: str | Path) -> tuple[Trial, ...]:
        """Download into a new directory; retain paths for later session openings."""
        manifest_path = self.client.download_subject(self.experiment_id, self.subject_id, destination)
        document = json.loads(manifest_path.read_text())
        if document["manifest"] != self.manifest:
            raise ValueError("Downloaded assignments differ from this session")
        with self._database() as database:
            for trial_id, relative in document["files"].items():
                path = (manifest_path.parent / relative).resolve()
                database.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)",
                                 (trial_id, str(path), self._digest(path)))
        return self.trials

    def sync(self) -> Progress:
        """Upload journal events, then fetch authoritative server progress."""
        while True:
            with self._database() as database:
                rows = database.execute("SELECT event_id,trial_id,attempt_id,kind,occurred_at FROM events "
                                        "WHERE acknowledged=0 ORDER BY sequence LIMIT 100").fetchall()
            if not rows:
                break
            events = [dict(row) for row in rows]
            reply = self.client._json("POST", self._path + "/events", dict(
                manifest_sha256=self.manifest["manifest_sha256"], events=cast(list[JSONValue], events)))
            if (reply.get("manifest_sha256") != self.manifest["manifest_sha256"]
                    or reply.get("accepted") != [event["event_id"] for event in events]):
                raise ValueError("Server did not acknowledge this event batch")
            with self._database() as database:
                database.executemany("UPDATE events SET acknowledged=1 WHERE event_id=?",
                                     [(event["event_id"],) for event in events])
        state = self.client._json("GET", self._path + "/progress")
        if state.get("manifest_sha256") != self.manifest["manifest_sha256"]:
            raise ValueError("Server progress belongs to different assignments")
        states = state.get("trials")
        if not isinstance(states, dict):
            raise ValueError("Invalid server progress")
        allowed = {trial.trial_id for trial in self.trials}
        progress = {}
        for trial_id, value in states.items():
            if (trial_id not in allowed or not isinstance(value, dict)
                    or value.get("status") not in ("started", "completed")
                    or not isinstance(value.get("attempt_id"), str)):
                raise ValueError("Invalid trial progress")
            progress[trial_id] = TrialProgress(cast(Literal["started", "completed"], value["status"]),
                                               cast(str, value["attempt_id"]))
        return Progress(str(self.manifest["manifest_sha256"]), progress)

    @property
    def pending_trials(self) -> tuple[Trial, ...]:
        """Unstarted trials only. Started but unfinished trials require review."""
        states = self.sync().trials
        return tuple(trial for trial in self.trials if trial.trial_id not in states)

    @property
    def incomplete_trials(self) -> tuple[Trial, ...]:
        states = self.sync().trials
        return tuple(trial for trial in self.trials
                     if trial.trial_id in states and states[trial.trial_id].status == "started")

    def _trial(self, trial: Trial) -> Trial:
        current = next((item for item in self.trials if item.trial_id == trial.trial_id), None)
        if current != trial:
            raise ValueError("Use a Trial from this session's current trials")
        return current

    def _record(self, trial_id: str, kind: Literal["started", "completed"], attempt_id: str) -> None:
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            previous = database.execute("SELECT kind,attempt_id FROM events WHERE trial_id=? ORDER BY sequence DESC LIMIT 1",
                                        (trial_id,)).fetchone()
            if kind == "started" and previous is not None:
                raise ValueError("Trial already recorded locally; sync and review before retrying")
            if kind == "completed":
                if previous is None or previous["attempt_id"] != attempt_id:
                    raise ValueError("No matching started attempt in this journal")
                if previous["kind"] == "completed":
                    return
            database.execute("INSERT INTO events(event_id,trial_id,attempt_id,kind,occurred_at) VALUES (?,?,?,?,?)",
                             (uuid4().hex, trial_id, attempt_id, kind, time.time()))

    def started(self, trial: Trial) -> None:
        """Persist and sync a start. Abort presentation if this raises."""
        self._trial(trial)
        if trial.local_path is None:
            raise ValueError("Call download() before starting a trial")
        with self._database() as database:
            expected = database.execute("SELECT sha256 FROM files WHERE trial_id=?", (trial.trial_id,)).fetchone()
        if expected is None or self._digest(trial.local_path) != expected["sha256"]:
            raise ValueError("Downloaded media changed; do not present this file")
        if trial.trial_id in self.sync().trials:
            raise ValueError("Trial already started or completed; review its progress")
        self._record(trial.trial_id, "started", uuid4().hex)
        self.sync()

    def completed(self, trial: Trial) -> None:
        """Record successful playback, not download or scheduling. Retry sync safely."""
        self._trial(trial)
        with self._database() as database:
            started = database.execute("SELECT * FROM events WHERE trial_id=? ORDER BY sequence DESC LIMIT 1",
                                       (trial.trial_id,)).fetchone()
        if started is None:
            raise ValueError("This device has no started event for this trial")
        if started["kind"] != "completed":
            self._record(trial.trial_id, "completed", started["attempt_id"])
        self.sync()
