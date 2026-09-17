"""Immutable PGL-compatible derivatives of sealed source media."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

from .config import atomic_write, fsync_directory, private_directory, read_private
from .models import (BlockPackage, ContractError, MAX_JSON_BYTES, canonical_bytes,
                     is_finite_number, seal_document, strict_json, verify_document)
from .prepare import PreparedBlock, _inventory, _lock, _manifest, _verify_media


PROFILE = "h264-yuv420p-v1"


@dataclass(frozen=True)
class PresentationBlock:
    root: Path
    package: BlockPackage
    receipt: dict


def _measure_media(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size <= 0):
                raise ContractError("Presentation media is not a private regular file")
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(source.fileno())
        current = path.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ContractError("Presentation media changed during hashing")
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
            raise ContractError("Presentation media changed during hashing")
        return current.st_size, digest.hexdigest()
    except OSError:
        raise ContractError("Presentation media is missing, linked, or unreadable") from None


def _expected_slots(package):
    return [
        {
            "trial_index": trial.trial_index,
            "filename": item["filename"],
            "source_media_sha256": trial.media_sha256,
        }
        for trial, item in zip(package.trials, _inventory(package))
    ]


def _verify_presentation(prepared, root):
    root = private_directory(root, create=False)
    manifest = read_private(root / "manifest.csv", MAX_JSON_BYTES)
    if manifest != _manifest(prepared.package):
        raise ContractError("Presentation manifest does not match the sealed package")
    receipt = strict_json(read_private(root / "presentation.json", MAX_JSON_BYTES))
    content = verify_document(receipt, "receipt_sha256")
    created_at = content.pop("created_at", None)
    files = content.pop("files", None)
    expected = {
        "schema_version": "dbp-pgl-presentation-v1",
        "source_package_sha256": prepared.package.package_sha256,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "profile": PROFILE,
        "video_codec": "h264",
        "pixel_format": "yuv420p",
        "transcoder": "ffmpeg-libx264",
    }
    if (not is_finite_number(created_at) or created_at <= 0 or content != expected
            or type(files) is not list or len(files) != len(prepared.package.trials)):
        raise ContractError("Invalid presentation receipt")
    expected_slots = _expected_slots(prepared.package)
    for slot, item in zip(expected_slots, files):
        if (type(item) is not dict or set(item) != {
                "trial_index", "filename", "source_media_sha256", "media_bytes", "media_sha256"}):
            raise ContractError("Invalid presentation media inventory")
        if any(item[key] != slot[key] for key in slot):
            raise ContractError("Presentation media inventory does not match the sealed package")
        if (type(item["media_bytes"]) is not int or item["media_bytes"] <= 0
                or type(item["media_sha256"]) is not str or len(item["media_sha256"]) != 64):
            raise ContractError("Invalid presentation media inventory")
        _verify_media(root / item["filename"], item["media_bytes"], item["media_sha256"])
    expected_names = {item["filename"] for item in files} | {"manifest.csv", "presentation.json"}
    if {path.name for path in root.iterdir()} != expected_names:
        raise ContractError("Presentation directory has missing or unexpected files")
    return PresentationBlock(root, prepared.package, receipt)


def _transcode(executable, source, destination):
    command = [
        executable,
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-tag:v",
        "avc1",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ContractError("PGL-compatible video conversion could not finish") from None
    if result.returncode:
        raise ContractError("A source video could not be converted for PGL presentation")


def prepare_presentation(prepared: PreparedBlock, *, ffmpeg=None):
    executable = shutil.which(ffmpeg or "ffmpeg")
    if not executable:
        raise ContractError("FFmpeg with libx264 is required to prepare PGL-compatible videos")
    parent = private_directory(prepared.root.parent / "presentations")
    final = parent / f"{prepared.package.package_id}-{PROFILE}"
    with _lock(parent):
        if final.exists() or final.is_symlink():
            return _verify_presentation(prepared, final)
        staging = Path(tempfile.mkdtemp(prefix=".presentation-", dir=parent))
        try:
            by_source = {}
            files = []
            for slot, source_item in zip(_expected_slots(prepared.package), _inventory(prepared.package)):
                source = prepared.root / source_item["filename"]
                _verify_media(source, source_item["media_bytes"], source_item["media_sha256"])
                destination = staging / slot["filename"]
                prior = by_source.get(slot["source_media_sha256"])
                if prior is None:
                    _transcode(executable, source, destination)
                    destination.chmod(0o400)
                    media_bytes, media_sha256 = _measure_media(destination)
                    by_source[slot["source_media_sha256"]] = (
                        destination,
                        media_bytes,
                        media_sha256,
                    )
                else:
                    prior_path, media_bytes, media_sha256 = prior
                    os.link(prior_path, destination, follow_symlinks=False)
                files.append({
                    **slot,
                    "media_bytes": media_bytes,
                    "media_sha256": media_sha256,
                })
            manifest = _manifest(prepared.package)
            receipt = seal_document({
                "schema_version": "dbp-pgl-presentation-v1",
                "source_package_sha256": prepared.package.package_sha256,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "profile": PROFILE,
                "video_codec": "h264",
                "pixel_format": "yuv420p",
                "transcoder": "ffmpeg-libx264",
                "created_at": time.time(),
                "files": files,
            }, "receipt_sha256")
            atomic_write(staging / "manifest.csv", manifest, 0o400)
            atomic_write(staging / "presentation.json", canonical_bytes(receipt), 0o400)
            fsync_directory(staging)
            os.rename(staging, final)
            fsync_directory(parent)
            return _verify_presentation(prepared, final)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
