"""Atomic preparation and offline integrity checks, never scientific readiness."""

from contextlib import contextmanager
import csv
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

from . import __version__
from .api import RangeNotSupported
from .config import atomic_write, fsync_directory, private_directory, read_private
from .models import (BlockPackage, ContractError, MAX_JSON_BYTES, canonical_bytes,
                     canonical_subject, identity, is_finite_number, seal_document,
                     strict_json, verify_document)


@dataclass(frozen=True)
class PreparedBlock:
    root: Path
    package: BlockPackage


@contextmanager
def _lock(root):
    descriptor = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ContractError("Preparation lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError("Another preparation is active; retry later") from None
        yield
    finally:
        os.close(descriptor)


def _signature(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _verify_media(path, expected_bytes, expected_digest):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size != expected_bytes
                    or before.st_uid != os.getuid() or before.st_mode & 0o077):
                raise ContractError("Media is not a private regular file of the sealed length")
            digest = hashlib.sha256()
            remaining = expected_bytes
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ContractError("Media truncated during verification")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(source.fileno())
        current = path.stat(follow_symlinks=False)
        if (_signature(before) != _signature(after) or _signature(after) != _signature(current)
                or digest.hexdigest() != expected_digest):
            raise ContractError("Media checksum mismatch or file changed during verification")
    except OSError:
        raise ContractError("Media missing, linked, or unreadable") from None


def _inventory(package):
    return [{"trial_index": trial.trial_index,
             "filename": f"trial-{trial.trial_index:05d}-{trial.media_sha256}.mp4",
             "media_bytes": trial.media_bytes, "media_sha256": trial.media_sha256}
            for trial in package.trials]


def _manifest(package):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["filename", "trial_index", "condition"])
    for trial, item in zip(package.trials, _inventory(package)):
        writer.writerow([item["filename"], trial.trial_index, trial.condition])
    return output.getvalue().encode("utf-8")


def _subject_root(config, alias, work_root, *, create=False):
    root = private_directory(work_root, create=create)
    for part in (config.experiment_id, config.device_id, canonical_subject(alias)):
        root = private_directory(root / part, create=create)
    return root


def _verify_prepared(config, subject, root, expected_digest):
    root = private_directory(root, create=False)
    package = BlockPackage.from_dict(strict_json(read_private(root / "block.json", MAX_JSON_BYTES)))
    if (package.subject_id != subject or package.experiment_id != config.experiment_id
            or package.package_id != root.name or package.package_sha256 != expected_digest):
        raise ContractError("Prepared package identity mismatch")
    manifest = read_private(root / "manifest.csv", MAX_JSON_BYTES)
    if manifest != _manifest(package):
        raise ContractError("Manifest does not match exact ordered package")
    receipt = verify_document(strict_json(read_private(root / "readiness.json", MAX_JSON_BYTES)), "receipt_sha256")
    expected = {"schema_version": "dbp-pgl-preparation-v1", "package_sha256": package.package_sha256,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "files": _inventory(package),
                "server_origin": config.server_origin, "workstation_id": config.device_id,
                "experiment_id": config.experiment_id, "subject_id": subject, "pgl_ready": False,
                "wrapper_version": receipt.get("wrapper_version")}
    prepared_at = receipt.pop("prepared_at", None)
    if (not is_finite_number(prepared_at) or prepared_at <= 0
            or receipt != expected or receipt.get("pgl_ready") is not False
            or receipt.get("wrapper_version") not in ("0.1.0", __version__)):
        raise ContractError("Invalid preparation receipt or workstation binding")
    expected_files = {item["filename"] for item in expected["files"]} | {
        "block.json", "manifest.csv", "readiness.json"}
    if {path.name for path in root.iterdir()} != expected_files:
        raise ContractError("Prepared directory has missing or unexpected files")
    for item in expected["files"]:
        _verify_media(root / item["filename"], item["media_bytes"], item["media_sha256"])
    return PreparedBlock(root, package)


def status_subject(config, subject_alias, work_root):
    subject = canonical_subject(subject_alias)
    root = _subject_root(config, subject, work_root)
    pointer = strict_json(read_private(root / "current.json", 4096))
    if type(pointer) is not dict or set(pointer) != {"package_id", "package_sha256"}:
        raise ContractError("Invalid current package pointer")
    package_id = identity(pointer["package_id"])
    return _verify_prepared(config, subject, root / package_id, pointer["package_sha256"])


def _cached_media(api, package, trial, cache):
    completed = cache / (trial.media_sha256 + ".mp4")
    partial = cache / (trial.media_sha256 + ".partial")
    if completed.exists() or completed.is_symlink():
        _verify_media(completed, trial.media_bytes, trial.media_sha256)
        return completed
    descriptor = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "r+b") as target:
        metadata = os.fstat(target.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > trial.media_bytes
                or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()):
            raise ContractError("Unsafe partial media file")
        offset = metadata.st_size
        target.seek(offset)

        def transfer(start):
            written = start
            for chunk in api.iter_media(package, trial, offset=start):
                if not isinstance(chunk, bytes) or len(chunk) > 1024 * 1024:
                    raise ContractError("Media stream chunk is invalid")
                written += len(chunk)
                if written > trial.media_bytes:
                    raise ContractError("Media exceeds sealed length")
                target.write(chunk)
            if written != trial.media_bytes:
                raise ContractError("Media shorter than sealed length")

        try:
            if offset < trial.media_bytes:
                try:
                    transfer(offset)
                except RangeNotSupported:
                    target.seek(0)
                    target.truncate()
                    transfer(0)
        except ContractError:
            partial.unlink()
            raise
        finally:
            target.flush()
            os.fsync(target.fileno())
    try:
        _verify_media(partial, trial.media_bytes, trial.media_sha256)
    except ContractError:
        partial.unlink()
        raise
    partial.chmod(0o400)
    os.replace(partial, completed)
    fsync_directory(cache)
    return completed


def prepare_subject(api, config, subject_alias, cache_root, work_root):
    subject = canonical_subject(subject_alias)
    package = BlockPackage.from_dict(api.next_block(subject_alias).to_dict())
    if package.experiment_id != config.experiment_id or package.subject_id != subject:
        raise ContractError("Package does not match requested experiment and subject")
    cache, work = private_directory(cache_root), private_directory(work_root)
    if cache == work or cache in work.parents or work in cache.parents:
        raise ContractError("Cache and work directories must be separate, non-nested roots")
    with _lock(cache), _lock(work):
        root = _subject_root(config, subject, work, create=True)
        final = root / package.package_id
        pointer = canonical_bytes({"package_id": package.package_id,
                                   "package_sha256": package.package_sha256})
        if final.exists() or final.is_symlink():
            prepared = _verify_prepared(config, subject, final, package.package_sha256)
            atomic_write(root / "current.json", pointer)
            return prepared
        distinct = {trial.media_sha256: trial.media_bytes for trial in package.trials}
        needed_cache = sum(size for digest, size in distinct.items()
                           if not (cache / (digest + ".mp4")).exists())
        needed_work = len(canonical_bytes(package.to_dict())) + len(_manifest(package)) * 3 + 1024 * 1024
        if cache.stat().st_dev != work.stat().st_dev:
            needed_work += sum(trial.media_bytes for trial in package.trials)
        if cache.stat().st_dev == work.stat().st_dev:
            if shutil.disk_usage(cache)[2] < needed_cache + needed_work:
                raise ContractError("Insufficient disk space for preparation")
        elif (shutil.disk_usage(cache)[2] < needed_cache
              or shutil.disk_usage(work)[2] < needed_work):
            raise ContractError("Insufficient disk space for preparation")
        staging = Path(tempfile.mkdtemp(prefix=".prepare-", dir=root))
        try:
            inventory = _inventory(package)
            for trial, item in zip(package.trials, inventory):
                cached = _cached_media(api, package, trial, cache)
                destination = staging / item["filename"]
                try:
                    os.link(cached, destination, follow_symlinks=False)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    with cached.open("rb") as source, destination.open("xb") as target:
                        os.fchmod(target.fileno(), 0o600)
                        shutil.copyfileobj(source, target, 1024 * 1024)
                        target.flush()
                        os.fchmod(target.fileno(), 0o400)
                        os.fsync(target.fileno())
                _verify_media(destination, trial.media_bytes, trial.media_sha256)
            manifest = _manifest(package)
            receipt = seal_document({
                "schema_version": "dbp-pgl-preparation-v1", "package_sha256": package.package_sha256,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "files": inventory,
                "prepared_at": time.time(), "wrapper_version": __version__,
                "workstation_id": config.device_id, "server_origin": config.server_origin,
                "experiment_id": config.experiment_id, "subject_id": subject, "pgl_ready": False,
            }, "receipt_sha256")
            atomic_write(staging / "block.json", canonical_bytes(package.to_dict()), 0o400)
            atomic_write(staging / "manifest.csv", manifest, 0o400)
            atomic_write(staging / "readiness.json", canonical_bytes(receipt), 0o400)
            fsync_directory(staging)
            os.rename(staging, final)
            fsync_directory(root)
            prepared = _verify_prepared(config, subject, final, package.package_sha256)
            atomic_write(root / "current.json", pointer)
            return prepared
        finally:
            if staging.exists():
                shutil.rmtree(staging)
