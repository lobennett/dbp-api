from pathlib import Path
import hashlib
import os
import shutil
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dbp_pgl_runner.decode import verify_decode
from dbp_pgl_runner.models import BlockPackage, ContractError
from dbp_pgl_runner.prepare import PreparedBlock
from dbp_pgl_runner.prepare import _inventory
from tests.fixtures import block, seal


class DecodeTests(unittest.TestCase):
    def test_decodes_unique_media_not_each_repeat(self):
        prepared = PreparedBlock(Path("/private/test"), BlockPackage.from_dict(block()))
        with patch("dbp_pgl_runner.decode.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            receipt = verify_decode(prepared, ffmpeg="/usr/local/bin/ffmpeg")
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("-xerror", command)
        self.assertEqual(receipt["unique_media"], 1)
        self.assertEqual(receipt["package_sha256"], prepared.package.package_sha256)

    def test_decode_failure_or_timeout_blocks_execution(self):
        prepared = PreparedBlock(Path("/private/test"), BlockPackage.from_dict(block()))
        for effect in (subprocess.TimeoutExpired("ffmpeg", 600), OSError("missing")):
            with patch("dbp_pgl_runner.decode.subprocess.run", side_effect=effect), self.assertRaises(ContractError):
                verify_decode(prepared, ffmpeg="ffmpeg")
        with patch("dbp_pgl_runner.decode.subprocess.run", return_value=SimpleNamespace(returncode=1)), self.assertRaises(ContractError):
            verify_decode(prepared, ffmpeg="ffmpeg")


class RealDecodeTests(unittest.TestCase):
    def test_full_decode_of_generated_video_and_corrupted_bytes(self):
        executable = os.environ.get("DBP_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not executable:
            self.skipTest("Set DBP_TEST_FFMPEG to exercise real local decoding")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movie = root / "generated.mp4"
            subprocess.run([executable, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=blue:s=64x64:r=10:d=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            str(movie)], check=True, timeout=20)
            content = movie.read_bytes()
            document = block()
            for trial in document["trials"]:
                trial["media_bytes"] = len(content)
                trial["media_sha256"] = hashlib.sha256(content).hexdigest()
            package = BlockPackage.from_dict(seal(document))
            prepared = PreparedBlock(root, package)
            for item in _inventory(package):
                (root / item["filename"]).write_bytes(content)
            self.assertEqual(verify_decode(prepared, ffmpeg=executable)["unique_media"], 1)
            (root / _inventory(package)[0]["filename"]).write_bytes(b"invalid-video")
            with self.assertRaises(ContractError):
                verify_decode(prepared, ffmpeg=executable)
