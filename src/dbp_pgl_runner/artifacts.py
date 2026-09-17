"""Bounded artifact manifests and verified, restartable local byte streams."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import PurePosixPath
import stat

from .config import atomic_write, fsync_directory, private_directory, read_private
from .journal import Journal, _private_file
from .models import (ContractError, MAX_JSON_BYTES, SHA256, canonical_bytes, identity,
                     seal_document, strict_json, verify_document)


ARTIFACT_SCHEMA = "dbp-pgl-artifacts-v1"
MAX_ARTIFACT_FILES = 1024
MAX_ARTIFACT_FILE_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MANIFEST_NAME = "artifact-manifest.json"
_EXCLUDED = {MANIFEST_NAME, ".journal.lock", "sync-receipt.json"}
_MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}


def _relative_path(value):
    if (type(value) is not str or not 1 <= len(value) <= 512 or not value.isprintable()
            or value != value.strip() or "\\" in value or ":" in value):
        raise ContractError("Invalid artifact relative path")
    parsed = PurePosixPath(value)
    if (parsed.is_absolute() or value != parsed.as_posix() or value == "."
            or ".." in parsed.parts or len(parsed.parts) > 16):
        raise ContractError("Unsafe artifact relative path")
    return parsed


def _signature(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _regular(metadata):
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()):
        raise ContractError("Artifact must be an owned regular file without links")


@contextmanager
def _open_output(root, relative):
    parts = _relative_path(relative).parts
    descriptors, directories = [], []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            directories.append((parent, part, os.fstat(child)))
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            _regular(before)
            yield source, before
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (_signature(before) != _signature(os.fstat(source.fileno()))
                    or _signature(before) != _signature(current)):
                raise ContractError("Artifact changed during read")
            for directory, name, original in directories:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
                        original.st_dev, original.st_ino):
                    raise ContractError("Artifact directory changed during read")
    except OSError:
        raise ContractError("Artifact missing, linked, or unreadable") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_source(source, size):
    digest, remaining = hashlib.sha256(), size
    while remaining:
        chunk = source.read(min(remaining, MAX_CHUNK_BYTES))
        if not chunk:
            raise ContractError("Artifact truncated during read")
        digest.update(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise ContractError("Artifact grew during read")
    return digest.hexdigest()


def _inventory(root, max_files, max_file_bytes, max_total_bytes):
    files, signatures, total_bytes, entries = [], {}, 0, 0

    def visit(directory, prefix):
        nonlocal total_bytes, entries
        before = os.fstat(directory)
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > max_files * 4 + len(_EXCLUDED):
                    raise ContractError("Artifact directory inventory exceeds limit")
                relative = f"{prefix}/{child.name}" if prefix else child.name
                parsed = _relative_path(relative)
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ContractError("Artifact symlinks are forbidden")
                if stat.S_ISDIR(metadata.st_mode):
                    if child.name.lower() in {"media", "cache", "prepared"}:
                        raise ContractError("Prepared media must not be uploaded as artifacts")
                    descriptor = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                         dir_fd=directory)
                    try:
                        visit(descriptor, relative)
                    finally:
                        os.close(descriptor)
                else:
                    _regular(metadata)
                    if (not prefix and child.name in _EXCLUDED) or child.name.endswith(".lock"):
                        continue
                    if parsed.suffix.lower() in _MEDIA_SUFFIXES:
                        raise ContractError("Stimulus media must not be uploaded as artifacts")
                    if len(files) >= max_files or metadata.st_size > max_file_bytes:
                        raise ContractError("Artifact count or file size exceeds limit")
                    total_bytes += metadata.st_size
                    if total_bytes > max_total_bytes:
                        raise ContractError("Artifact total size exceeds limit")
                    with _open_output(root, relative) as (source, opened):
                        if _signature(opened) != _signature(metadata):
                            raise ContractError("Artifact changed during inventory")
                        digest = _hash_source(source, opened.st_size)
                    files.append({"path": relative, "bytes": opened.st_size, "sha256": digest})
                    signatures[relative] = _signature(opened)
        if _signature(before) != _signature(os.fstat(directory)):
            raise ContractError("Artifact directory changed during inventory")

    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            visit(descriptor, "")
        finally:
            os.close(descriptor)
        for relative, expected in signatures.items():
            with _open_output(root, relative) as (_, metadata):
                if _signature(metadata) != expected:
                    raise ContractError("Artifact changed before manifest sealing")
    except OSError:
        raise ContractError("Artifact inventory missing, unsafe, or unreadable") from None
    return sorted(files, key=lambda item: item["path"])


@contextmanager
def _terminal_journal(root, attempt_id):
    if root.name != attempt_id:
        raise ContractError("Artifact root is not bound to this attempt")
    metadata = verify_document(strict_json(read_private(root / "journal.json", 4096)), "journal_sha256")
    journal = Journal(root.parent, attempt_id, metadata.get("trial_count"))
    with journal._locked():
        journal._validate_metadata()
        with _private_file(journal.path, os.O_RDONLY) as descriptor:
            _, state, _, _ = journal._scan(descriptor)
        if state.state not in ("completed", "terminated"):
            raise ContractError("Only a terminal journal can be sealed as artifacts")
        yield


def seal_artifacts(attempt_root, attempt_id, *, max_files=MAX_ARTIFACT_FILES,
                   max_file_bytes=MAX_ARTIFACT_FILE_BYTES,
                   max_total_bytes=MAX_ARTIFACT_TOTAL_BYTES):
    """Persist and return an idempotent manifest of terminal attempt outputs.

    The dedicated attempt root contains events.jsonl, journal.json, native PGL
    outputs and optional wrapper metadata, never prepared stimuli or credentials.
    The manifest, *.lock files and later sync receipt are excluded from inventory.
    Native files may be empty; their presence does not certify PGL completeness.
    An existing manifest is verified, not replaced with a changed inventory.
    """
    identity(attempt_id)
    for value, maximum in ((max_files, MAX_ARTIFACT_FILES),
                           (max_file_bytes, MAX_ARTIFACT_FILE_BYTES),
                           (max_total_bytes, MAX_ARTIFACT_TOTAL_BYTES)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ContractError("Artifact limits must be positive bounded integers")
    root = private_directory(attempt_root, create=False)
    with _terminal_journal(root, attempt_id):
        manifest = seal_document({"schema_version": ARTIFACT_SCHEMA, "attempt_id": attempt_id,
                                  "files": _inventory(root, max_files, max_file_bytes, max_total_bytes)},
                                 "manifest_sha256")
        destination = root / MANIFEST_NAME
        if os.path.lexists(destination):
            existing = strict_json(read_private(destination, MAX_JSON_BYTES))
            verify_document(existing, "manifest_sha256")
            if canonical_bytes(existing) != canonical_bytes(manifest):
                raise ContractError("Sealed artifact inventory has changed")
            fsync_directory(root)
        else:
            atomic_write(destination, canonical_bytes(manifest))
        return manifest


def verify_artifacts(attempt_root, manifest):
    """Verify a sealed manifest against terminal attempt outputs without writing."""
    document = verify_document(manifest, "manifest_sha256")
    if set(document) != {"schema_version", "attempt_id", "files"}:
        raise ContractError("Invalid artifact manifest fields")
    if document["schema_version"] != ARTIFACT_SCHEMA:
        raise ContractError("Unsupported artifact manifest schema")
    attempt_id = identity(document["attempt_id"])
    root = private_directory(attempt_root, create=False)
    with _terminal_journal(root, attempt_id):
        files = _inventory(root, MAX_ARTIFACT_FILES, MAX_ARTIFACT_FILE_BYTES, MAX_ARTIFACT_TOTAL_BYTES)
        if canonical_bytes(files) != canonical_bytes(document["files"]):
            raise ContractError("Artifact manifest does not match local outputs")
    return seal_document(document, "manifest_sha256")


def iter_artifact_chunks(attempt_root, artifact, *, chunk_size=MAX_CHUNK_BYTES):
    """Yield bounded bytes from offset zero, verifying before and after streaming.

    Enumerate chunks from zero for transport. Retrying starts the same immutable
    file again; transport idempotency and server-side final hash checks belong to
    the caller. Exhaust the iterator before finalizing any upload, including empty
    files, because a mutation detected after yielding raises ContractError.
    """
    if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_CHUNK_BYTES:
        raise ContractError("Artifact chunks must be between 1 byte and 1 MiB")
    if type(artifact) is not dict or set(artifact) != {"path", "bytes", "sha256"}:
        raise ContractError("Invalid artifact entry fields")
    _relative_path(artifact["path"])
    size, digest = artifact["bytes"], artifact["sha256"]
    if (type(size) is not int or not 0 <= size <= MAX_ARTIFACT_FILE_BYTES
            or type(digest) is not str or not SHA256.fullmatch(digest)):
        raise ContractError("Invalid artifact size or SHA-256")
    root = private_directory(attempt_root, create=False)
    with _open_output(root, artifact["path"]) as (source, before):
        if before.st_size != size or _hash_source(source, size) != digest:
            raise ContractError("Artifact checksum or length mismatch")
        if _signature(before) != _signature(os.fstat(source.fileno())):
            raise ContractError("Artifact changed during verification")
        source.seek(0)
        streamed, remaining = hashlib.sha256(), size
        while remaining:
            chunk = source.read(min(chunk_size, remaining))
            if not chunk:
                raise ContractError("Artifact truncated during upload")
            remaining -= len(chunk)
            streamed.update(chunk)
            yield chunk
        if source.read(1) or streamed.hexdigest() != digest:
            raise ContractError("Artifact changed during upload")
