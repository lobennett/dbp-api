import csv
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from dbp_pgl_runner.models import BlockPackage, ContractError
from dbp_pgl_runner.prepare import PreparedBlock, _inventory
from dbp_pgl_runner.presentation import prepare_presentation
from tests.fixtures import block, seal


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and FFprobe are required")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "subject" / ("a" * 32)
        self.root.mkdir(mode=0o700, parents=True)
        source = self.root / "source.mp4"
        subprocess.run(
            [
                self.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=64x64:r=12:d=0.5",
                "-c:v",
                "libsvtav1",
                "-preset",
                "13",
                "-an",
                str(source),
            ],
            check=True,
            timeout=30,
        )
        content = source.read_bytes()
        document = block()
        for trial in document["trials"]:
            trial["media_bytes"] = len(content)
            trial["media_sha256"] = hashlib.sha256(content).hexdigest()
        self.package = BlockPackage.from_dict(seal(document))
        source.unlink()
        for item in _inventory(self.package):
            path = self.root / item["filename"]
            path.write_bytes(content)
            path.chmod(0o400)
        self.prepared = PreparedBlock(self.root, self.package)

    def test_av1_sources_become_verified_h264_presentation_files(self):
        presentation = prepare_presentation(self.prepared, ffmpeg=self.ffmpeg)
        rows = list(csv.DictReader(io.StringIO((presentation.root / "manifest.csv").read_text())))

        self.assertEqual(len(rows), len(self.package.trials))
        files = [presentation.root / row["filename"] for row in rows]
        self.assertTrue(all(path.is_file() for path in files))
        codec = subprocess.check_output(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=nw=1:nk=1",
                str(files[0]),
            ],
            text=True,
        ).strip()
        self.assertEqual(codec, "h264")
        self.assertEqual(os.stat(files[0]).st_ino, os.stat(files[1]).st_ino)
        self.assertEqual(presentation.receipt["source_package_sha256"], self.package.package_sha256)
        self.assertEqual(presentation.receipt["video_codec"], "h264")

    def test_verified_presentation_is_reused_and_tampering_is_rejected(self):
        first = prepare_presentation(self.prepared, ffmpeg=self.ffmpeg)
        before = {
            path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
            for path in first.root.iterdir()
        }
        second = prepare_presentation(self.prepared, ffmpeg=self.ffmpeg)
        self.assertEqual(first, second)
        self.assertEqual(
            before,
            {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in second.root.iterdir()},
        )

        movie = next(first.root.glob("*.mp4"))
        content = movie.read_bytes()
        movie.chmod(0o600)
        movie.write_bytes(b"X" * len(content))
        with self.assertRaises(ContractError):
            prepare_presentation(self.prepared, ffmpeg=self.ffmpeg)


if __name__ == "__main__":
    unittest.main()
