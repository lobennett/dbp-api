"""Durable, zero-based trial milestones; inspection never resumes presentation."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import fcntl
import json
import os
import stat

from .config import atomic_write, fsync_directory, private_directory, read_private
from .models import (ContractError, canonical_bytes, identity, seal_document,
                     strict_json, verify_document)


EVENT_SCHEMA = "dbp-pgl-event-v1"
JOURNAL_SCHEMA = "dbp-pgl-journal-v1"
MAX_EVENT_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 128 * 1024 * 1024
MILESTONES = ("trial_loaded", "stimulus_started", "stimulus_finished",
              "response_started", "response_saved", "trial_completed")
EVENT_FIELDS = {"schema_version", "attempt_id", "sequence", "previous_sha256",
                "kind", "trial_index", "payload", "event_sha256"}


def _signature(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _json_value(value):
    if type(value) is dict:
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    if type(value) is list:
        return all(_json_value(item) for item in value)
    return value is None or type(value) in (str, int, float, bool)


@contextmanager
def _private_file(path, flags):
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        raise ContractError("Journal file missing or unsafe") from None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid() or metadata.st_nlink != 1):
            raise ContractError("Journal files must be private, unlinked regular files")
        yield descriptor
    finally:
        os.close(descriptor)


@dataclass
class _State:
    state: str = "not_started"
    completed: int = 0
    phase: int = 0
    last_kind: str | None = None

    def advance(self, kind, trial_index, trial_count):
        if type(kind) is not str:
            raise ContractError("Invalid event kind")
        if kind in MILESTONES:
            if (self.state != "running" or type(trial_index) is not int
                    or trial_index != self.completed or self.completed >= trial_count
                    or kind != MILESTONES[self.phase]):
                raise ContractError("Illegal or unordered trial milestone")
            self.phase += 1
            if self.phase == len(MILESTONES):
                self.completed += 1
                self.phase = 0
        elif trial_index is not None:
            raise ContractError("Run events require a null trial index")
        elif kind == "run_started" and self.state == "not_started":
            self.state = "running"
        elif kind == "run_terminated" and self.state == "running":
            self.state = "terminated"
        elif (kind == "run_completed" and self.state == "running"
              and self.completed == trial_count and self.phase == 0):
            self.state = "completed"
        else:
            raise ContractError("Illegal run transition or unknown event kind")
        self.last_kind = kind


class Journal:
    """Store an attempt at root/attempt_id, validating on every read and write.

    Append returns the sealed event. Only an exact retry of the current tail is
    idempotent; changed or older milestones fail. Trial indices start at zero.
    Recovery discards only an explicitly requested unterminated, invalid-JSON
    final fragment, never a complete JSON event or corruption in the prefix.
    A needs_review status is advisory to callers: no method launches a trial.
    Validated state is cached under the file lock using device, inode, length,
    mtime and ctime; external changes invalidate the cache before any append.
    """

    def __init__(self, root, attempt_id, trial_count, *, recover_incomplete_tail=False):
        self.attempt_id = identity(attempt_id)
        if type(trial_count) is not int or not 1 <= trial_count <= 50_000:
            raise ContractError("Invalid journal trial count")
        if type(recover_incomplete_tail) is not bool:
            raise ContractError("Recovery must be an explicit boolean")
        self.trial_count = trial_count
        self._cache = None
        parent = private_directory(root)
        self.root = private_directory(parent / self.attempt_id)
        self.path = self.root / "events.jsonl"
        self._metadata_path = self.root / "journal.json"
        with self._locked():
            if not os.path.lexists(self._metadata_path):
                if os.path.lexists(self.path):
                    raise ContractError("Journal metadata is missing; refusing to recreate history")
                atomic_write(self._metadata_path, canonical_bytes(seal_document({
                    "schema_version": JOURNAL_SCHEMA, "attempt_id": self.attempt_id,
                    "trial_count": self.trial_count,
                }, "journal_sha256")))
                with _private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR) as descriptor:
                    os.fsync(descriptor)
                fsync_directory(self.root)
                fsync_directory(parent)
                fsync_directory(parent.parent)
            self._validate_metadata()
            with _private_file(self.path, os.O_RDWR) as descriptor:
                if recover_incomplete_tail:
                    self._recover(descriptor)
                self._scan(descriptor)

    @contextmanager
    def _locked(self):
        private_directory(self.root, create=False)
        with _private_file(self.root / ".journal.lock", os.O_CREAT | os.O_RDWR) as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_metadata(self):
        value = verify_document(strict_json(read_private(self._metadata_path, 4096)), "journal_sha256")
        expected = {"schema_version": JOURNAL_SCHEMA, "attempt_id": self.attempt_id,
                    "trial_count": self.trial_count}
        if value != expected or type(value.get("trial_count")) is not int:
            raise ContractError("Journal attempt or trial count mismatch")

    def _scan(self, descriptor, *, allow_partial=False):
        metadata = os.fstat(descriptor)
        if metadata.st_size > MAX_JOURNAL_BYTES:
            raise ContractError("Journal exceeds size limit")
        signature = _signature(metadata)
        if self._cache is not None and self._cache[0] == signature:
            _, events, state, offset = self._cache
            return events, replace(state), offset, b""
        self._cache = None
        os.lseek(descriptor, 0, os.SEEK_SET)
        events, state, offset = [], _State(), 0
        previous = "0" * 64
        with os.fdopen(os.dup(descriptor), "rb") as source:
            while True:
                line = source.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    if _signature(os.fstat(descriptor)) != signature:
                        raise ContractError("Journal changed during verification")
                    self._cache = signature, events, replace(state), offset
                    return events, state, offset, b""
                if len(line) > MAX_EVENT_BYTES:
                    raise ContractError("Journal event exceeds size limit")
                if not line.endswith(b"\n"):
                    if allow_partial:
                        return events, state, offset, line
                    raise ContractError("Incomplete journal tail requires explicit recovery")
                event = strict_json(line)
                if type(event) is not dict or set(event) != EVENT_FIELDS:
                    raise ContractError("Invalid journal event fields")
                verify_document(event, "event_sha256")
                if (event["schema_version"] != EVENT_SCHEMA
                        or event["attempt_id"] != self.attempt_id
                        or type(event["sequence"]) is not int
                        or event["sequence"] != len(events) + 1
                        or event["previous_sha256"] != previous
                        or type(event["payload"]) is not dict
                        or canonical_bytes(event) + b"\n" != line):
                    raise ContractError("Journal schema, sequence, canonical encoding, or chain mismatch")
                state.advance(event["kind"], event["trial_index"], self.trial_count)
                events.append(event)
                previous = event["event_sha256"]
                offset += len(line)

    def append(self, kind, trial_index=None, payload=None):
        """Durably append one milestone, or fsync and return an exact tail retry."""
        payload = {} if payload is None else payload
        try:
            valid_payload = type(payload) is dict and _json_value(payload)
        except RecursionError:
            valid_payload = False
        if not valid_payload:
            raise ContractError("Event payload must be a JSON object with string keys")
        payload = strict_json(canonical_bytes(payload))
        if trial_index is not None and type(trial_index) is not int:
            raise ContractError("Trial index must be an integer or null")
        with self._locked():
            self._validate_metadata()
            with _private_file(self.path, os.O_RDWR | os.O_APPEND) as descriptor:
                events, state, offset, _ = self._scan(descriptor)
                if events and (events[-1]["kind"] == kind
                               and events[-1]["trial_index"] == trial_index
                               and canonical_bytes(events[-1]["payload"]) == canonical_bytes(payload)):
                    os.fsync(descriptor)
                    return deepcopy(events[-1])
                state.advance(kind, trial_index, self.trial_count)
                event = seal_document({
                    "schema_version": EVENT_SCHEMA, "attempt_id": self.attempt_id,
                    "sequence": len(events) + 1,
                    "previous_sha256": events[-1]["event_sha256"] if events else "0" * 64,
                    "kind": kind, "trial_index": trial_index, "payload": payload,
                }, "event_sha256")
                line = canonical_bytes(event) + b"\n"
                if len(line) > MAX_EVENT_BYTES or offset + len(line) > MAX_JOURNAL_BYTES:
                    raise ContractError("Journal event or file exceeds size limit")
                self._cache = None
                remaining = memoryview(line)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("Journal write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
                events.append(event)
                self._cache = _signature(os.fstat(descriptor)), events, replace(state), offset + len(line)
                return deepcopy(event)

    def read_events(self):
        """Return independent, verified event dictionaries in sequence order."""
        with self._locked():
            self._validate_metadata()
            with _private_file(self.path, os.O_RDONLY) as descriptor:
                return deepcopy(self._scan(descriptor)[0])

    def status(self):
        """Return evidence-only state; exposed incomplete trials require review."""
        with self._locked():
            self._validate_metadata()
            with _private_file(self.path, os.O_RDONLY) as descriptor:
                events, state, _, _ = self._scan(descriptor)
                last_sequence = len(events)
                last_sha256 = events[-1]["event_sha256"] if events else "0" * 64
        return {
            "state": state.state, "completed_trials": state.completed,
            "last_completed_trial": state.completed - 1 if state.completed else None,
            "next_trial_index": state.completed if state.completed < self.trial_count else None,
            "last_kind": state.last_kind, "needs_review": 2 <= state.phase <= 5,
            "can_resume": state.state in ("not_started", "running") and state.phase < 2,
            "last_sequence": last_sequence, "last_sha256": last_sha256,
            "exposure_incomplete": 2 <= state.phase <= 5,
        }

    def _recover(self, descriptor):
        _, _, offset, partial = self._scan(descriptor, allow_partial=True)
        if not partial:
            return 0
        try:
            json.loads(partial)
        except (ValueError, UnicodeError):
            pass
        else:
            raise ContractError("Refusing to discard a complete JSON record without a newline")
        self._cache = None
        os.ftruncate(descriptor, offset)
        os.fsync(descriptor)
        return len(partial)

    def recover_incomplete_tail(self):
        """Explicitly discard only an incomplete final fragment; return bytes removed."""
        with self._locked():
            self._validate_metadata()
            with _private_file(self.path, os.O_RDWR) as descriptor:
                return self._recover(descriptor)

    def recover_tail(self):
        """Alias for explicit incomplete-final-fragment recovery."""
        return self.recover_incomplete_tail()
