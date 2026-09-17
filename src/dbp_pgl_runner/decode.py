"""Decode prepared videos completely before opening experiment hardware."""

import shutil
import subprocess

from .models import ContractError
from .prepare import _inventory


def verify_decode(prepared, *, ffmpeg=None):
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise ContractError("FFmpeg is required for the full decode preflight; install it or pass its executable path")
    seen = set()
    for item in _inventory(prepared.package):
        if item["media_sha256"] in seen:
            continue
        command = [str(executable), "-nostdin", "-v", "error", "-xerror", "-threads", "1",
                   "-i", str(prepared.root / item["filename"]), "-map", "0:v:0", "-map", "0:a?",
                   "-threads", "1", "-f", "null", "-"]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ContractError("Video decode preflight could not finish; no experiment started") from None
        if result.returncode:
            raise ContractError("A prepared video failed full decoding; no experiment started")
        seen.add(item["media_sha256"])
    return {"schema_version": "dbp-pgl-decode-v1", "package_sha256": prepared.package.package_sha256,
            "unique_media": len(seen), "decoder": "ffmpeg", "full_decode": True}
