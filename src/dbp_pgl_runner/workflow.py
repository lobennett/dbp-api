"""Prepare, execute locally, and synchronize without ever replaying to retry sync."""

from dataclasses import asdict
import hashlib
from pathlib import Path
import secrets
import time

from .config import atomic_write, private_directory, read_private
from .decode import verify_decode
from .journal import Journal
from .models import BlockPackage, ContractError, MAX_JSON_BYTES, canonical_bytes, identity, seal_document, strict_json, verify_document
from .pgl_adapter import PglAdapter, RunSettings
from .prepare import PreparedBlock, _lock, _subject_root, status_subject


def _attempts(prepared, *, create=False):
    return private_directory(prepared.root.parent / "attempts", create=create)


def _current(prepared, *, recover_tail=False):
    parent = _attempts(prepared)
    pointer = strict_json(read_private(parent / "current.json", 4096))
    if type(pointer) is not dict or set(pointer) != {"attempt_id", "package_sha256"}:
        raise ContractError("Invalid current attempt pointer")
    if pointer["package_sha256"] != prepared.package.package_sha256:
        raise ContractError("Current attempt belongs to a different immutable package")
    attempt_id = identity(pointer["attempt_id"])
    private_directory(parent / attempt_id, create=False)
    metadata = verify_document(strict_json(read_private(parent / attempt_id / "attempt.json", 65536)),
                               "attempt_sha256")
    if (metadata.get("attempt_id") != attempt_id or metadata.get("package_id") != prepared.package.package_id
            or metadata.get("package_sha256") != prepared.package.package_sha256
            or metadata.get("experiment_id") != prepared.package.experiment_id
            or metadata.get("subject_id") != prepared.package.subject_id
            or metadata.get("device_id") != prepared.root.parent.parent.name):
        raise ContractError("Attempt does not match the prepared package")
    read_private(parent / attempt_id / "journal.json", 4096)
    if not (parent / attempt_id / "events.jsonl").is_file():
        raise ContractError("Existing attempt journal is missing; it must not be recreated")
    journal = Journal(parent, attempt_id, len(prepared.package.trials), recover_incomplete_tail=recover_tail)
    return journal, metadata


def _saved_package(config, subject, work_root):
    subject_root = _subject_root(config, subject, work_root)
    parent = private_directory(subject_root / "attempts", create=False)
    pointer = strict_json(read_private(parent / "current.json", 4096))
    if type(pointer) is not dict or set(pointer) != {"attempt_id", "package_sha256"}:
        raise ContractError("Invalid current attempt pointer")
    root = private_directory(parent / identity(pointer["attempt_id"]), create=False)
    package = BlockPackage.from_dict(strict_json(read_private(root / "input-block.json", MAX_JSON_BYTES)))
    if (package.package_sha256 != pointer["package_sha256"]
            or package.experiment_id != config.experiment_id or package.subject_id != subject_root.name):
        raise ContractError("Saved attempt input does not match this experiment and subject")
    metadata = verify_document(strict_json(read_private(root / "attempt.json", 65536)), "attempt_sha256")
    if metadata.get("server_origin") != config.server_origin or metadata.get("device_id") != config.device_id:
        raise ContractError("Saved attempt belongs to another origin or workstation")
    return PreparedBlock(subject_root / package.package_id, package)


def _native_complete(root):
    groups = {}
    if not root.is_dir() or root.is_symlink():
        return False
    for index, path in enumerate(root.rglob("*")):
        if index >= 4096 or path.is_symlink():
            return False
        if path.is_file() and path.stat().st_size > 0:
            groups.setdefault(path.parent, set()).add(path.name)
    required = {"experimentSettings.json", "pgl.json", "settings.json", "state.json", "data.json"}
    task_required = {"settings.json", "state.json", "data.json"}
    return any(required <= names and any(task.parent == directory and task_required <= task_names
                                         for task, task_names in groups.items())
               for directory, names in groups.items())


def _summary(journal):
    status = journal.status()
    synchronized = False
    receipt_path = journal.root / "sync-receipt.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = strict_json(read_private(receipt_path, 65536))
        from .artifacts import seal_artifacts
        manifest = seal_artifacts(journal.root, journal.attempt_id)
        if (type(receipt) is not dict or receipt.get("attempt_id") != journal.attempt_id
                or receipt.get("sync_status") != "synced"
                or receipt.get("local_manifest_sha256") != manifest["manifest_sha256"]):
            raise ContractError("Synchronization receipt does not match the local sealed attempt")
        synchronized = True
    return {"attempt_id": journal.attempt_id, "attempt_root": str(journal.root),
            "status": status["state"], "completed_trials": status["completed_trials"],
            "needs_review": status["needs_review"], "pgl_ready": False,
            "sync_status": "synced" if synchronized else "pending"}


def _seal(journal, prepared):
    from .artifacts import seal_artifacts
    return seal_artifacts(journal.root, journal.attempt_id)


def run_subject(api, config, subject, work_root, *, integration_test=False, settings=None,
                adapter=None, ffmpeg=None):
    if integration_test is not True:
        raise ContractError("Use integration_test=True / --integration-test for a non-participant pilot only")
    prepared = status_subject(config, subject, work_root)
    parent = _attempts(prepared, create=True)
    settings = settings or RunSettings()
    adapter = adapter or PglAdapter()
    with _lock(parent):
        prior = None
        reuse = None
        if (parent / "current.json").exists():
            prior, _ = _current(prepared)
            state = prior.status()["state"]
            if state == "not_started":
                reuse = prior
            elif state not in ("completed", "terminated"):
                raise ContractError("Previous attempt is unfinished; recover and terminate it before any new presentation")
            else:
                raise ContractError("Attempt already ran; use sync. Re-exposure requires a new reviewed study assignment")
        adapter.preflight()
        decode = verify_decode(prepared, ffmpeg=ffmpeg)
        verified = status_subject(config, subject, work_root)
        if verified.root != prepared.root or verified.package.package_sha256 != prepared.package.package_sha256:
            raise ContractError("Prepared block changed during decoding; no experiment started")
        prepared = verified
        journal = reuse or Journal(parent, secrets.token_hex(16), len(prepared.package.trials))
        if reuse is None:
            metadata = seal_document({"schema_version": "dbp-pgl-attempt-v1", "attempt_id": journal.attempt_id,
                                     "package_id": prepared.package.package_id,
                                     "package_sha256": prepared.package.package_sha256,
                                     "experiment_id": config.experiment_id, "device_id": config.device_id,
                                     "subject_id": prepared.package.subject_id, "server_origin": config.server_origin,
                                     "settings": asdict(settings), "created_at": time.time()}, "attempt_sha256")
            atomic_write(journal.root / "attempt.json", canonical_bytes(metadata))
            atomic_write(journal.root / "input-block.json", canonical_bytes(prepared.package.to_dict()))
            atomic_write(parent / "current.json", canonical_bytes({"attempt_id": journal.attempt_id,
                                                                      "package_sha256": prepared.package.package_sha256}))
        else:
            _, metadata = _current(prepared)
            if metadata["settings"] != asdict(settings):
                raise ContractError("Retry the reserved attempt with its original settings")
        claimed = api.claim_attempt(prepared.package, journal.attempt_id)
        if claimed.get("attempt_id") != journal.attempt_id or claimed.get("package_id") != prepared.package.package_id:
            raise ContractError("Server returned a different attempt reservation")
        if (claimed.get("status") != "claimed" or claimed.get("exclusive") is not True
                or claimed.get("last_sequence") != 0):
            raise ContractError("This reservation is not an unused exclusive attempt; no presentation started")
        atomic_write(journal.root / "reservation.json", canonical_bytes(claimed))
        atomic_write(journal.root / "decode.json", canonical_bytes(decode))
        journal.append("run_started", payload={"package_sha256": prepared.package.package_sha256,
                                              "settings": asdict(settings), "time": time.time()})

        def callback(kind, trial_index, payload):
            data = dict(payload)
            if "filename" in data:
                data["filename"] = Path(data["filename"]).name
            data["monotonic_seconds"] = time.monotonic()
            journal.append(kind, trial_index, data)

        try:
            result = adapter.run(prepared.root, journal.root / "native", prepared.package.subject_id,
                                 journal.attempt_id, callback, settings)
            native_saved = result.native_saved and _native_complete(journal.root / "native")
            complete = (native_saved and result.error is None
                        and journal.status()["completed_trials"] == len(prepared.package.trials))
            journal.append("run_completed" if complete else "run_terminated",
                           payload={"native_saved": native_saved,
                                    "reason": result.error or ("all_trials_completed" if complete else "incomplete_native_or_trial_evidence")})
        except (Exception, KeyboardInterrupt) as error:
            if journal.status()["state"] == "running":
                journal.append("run_terminated", payload={"native_saved": False, "reason": type(error).__name__})
            _seal(journal, prepared)
            raise
        _seal(journal, prepared)
        return _summary(journal)


def attempt_status(config, subject, work_root):
    prepared = _saved_package(config, subject, work_root)
    journal, _ = _current(prepared)
    return _summary(journal)


def recover_subject(config, subject, work_root, *, terminate=False, repair_tail=False):
    if terminate is not True:
        raise ContractError("Recovery requires explicit termination; trials are never silently resumed")
    prepared = _saved_package(config, subject, work_root)
    with _lock(_attempts(prepared)):
        journal, _ = _current(prepared, recover_tail=repair_tail)
        state = journal.status()["state"]
        if state == "not_started":
            journal.append("run_started", payload={"recovery_without_presentation": True})
        if state not in ("completed", "terminated"):
            journal.append("run_terminated", payload={"reason": "explicit_crash_recovery", "native_saved": False,
                                                      "tail_repair_requested": repair_tail})
        _seal(journal, prepared)
        return _summary(journal)


def sync_subject(api, config, subject, work_root):
    from .artifacts import iter_artifact_chunks
    prepared = _saved_package(config, subject, work_root)
    with _lock(_attempts(prepared)):
        journal, _ = _current(prepared)
        if journal.status()["state"] not in ("completed", "terminated"):
            raise ContractError("End or recover this attempt before synchronizing its final results")
        local_manifest = _seal(journal, prepared)
        api.claim_attempt(prepared.package, journal.attempt_id)
        events = journal.read_events()
        for offset in range(0, len(events), 16):
            api.append_events(journal.attempt_id, events[offset:offset + 16])
        artifacts = []
        for entry in local_manifest["files"]:
            artifact_id = hashlib.sha256(entry["path"].encode("utf-8")).hexdigest()[:32]
            artifact = {"artifact_id": artifact_id, **entry}
            artifacts.append(artifact)
            uploaded = False
            for chunk_index, content in enumerate(iter_artifact_chunks(journal.root, entry)):
                api.upload_chunk(journal.attempt_id, artifact, chunk_index, content)
                uploaded = True
            if not uploaded:
                api.upload_chunk(journal.attempt_id, artifact, 0, b"")
        outcome = journal.status()["state"]
        manifest = seal_document({"schema_version": "dbp-pgl-artifacts-v1", "attempt_id": journal.attempt_id,
                                  "package_sha256": prepared.package.package_sha256,
                                  "last_sequence": len(events), "journal_sha256": events[-1]["event_sha256"],
                                  "outcome": outcome, "reason": events[-1]["payload"].get("reason", "")
                                  if outcome == "terminated" else "", "artifacts": artifacts}, "manifest_sha256")
        receipt = api.finalize_attempt(journal.attempt_id, manifest)
        if (receipt.get("attempt_id") != journal.attempt_id or receipt.get("sync_status") != "synced"
                or receipt.get("manifest_sha256") != manifest["manifest_sha256"]):
            raise ContractError("Server did not confirm the exact sealed result inventory")
        atomic_write(journal.root / "sync-receipt.json", canonical_bytes(
            {**receipt, "local_manifest_sha256": local_manifest["manifest_sha256"]}))
        return {**_summary(journal), "sync_status": "synced"}
