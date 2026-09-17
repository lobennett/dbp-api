import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from dbp_pgl_runner.api import ApiError, RangeNotSupported
from dbp_pgl_runner.models import BlockPackage, ContractError
from dbp_pgl_runner.prepare import prepare_subject, status_subject
from tests.fixtures import MEDIA, block, seal
from tests.test_api import config


class MediaApi:
    def __init__(self):
        self.document = block()
        self.payload = MEDIA
        self.downloads = []
        self.interrupt = False
        self.ignore_range = False

    def next_block(self, alias):
        return BlockPackage.from_dict(self.document)

    def iter_media(self, package, trial, *, offset=0):
        self.downloads.append(offset)
        if offset and self.ignore_range:
            raise RangeNotSupported("restart required")
        if self.interrupt:
            yield self.payload[offset:offset + 4]
            raise ApiError("interrupted")
        yield self.payload[offset:]


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cache = self.base / "cache"
        self.work = self.base / "work"
        self.config = config("https://example.org")
        self.api = MediaApi()

    def prepare(self):
        return prepare_subject(self.api, self.config, "s001", self.cache, self.work)

    def test_status_does_not_create_missing_work_or_subject_directories(self):
        for depth in range(4):
            current = self.work
            for part in [self.config.experiment_id, self.config.device_id, "subject-001"][:depth]:
                current.mkdir(mode=0o700, parents=True, exist_ok=True)
                current = current / part
            before = set(self.base.rglob("*"))
            with self.subTest(depth=depth), self.assertRaises(ContractError):
                status_subject(self.config, "s001", self.work)
            self.assertEqual(set(self.base.rglob("*")), before)

    def test_status_dangling_pointer_does_not_poison_later_preparation(self):
        prepared = self.prepare()
        shutil.rmtree(prepared.root)
        before = set(self.work.rglob("*"))
        with self.assertRaises(ContractError):
            status_subject(self.config, "s001", self.work)
        self.assertEqual(set(self.work.rglob("*")), before)
        self.assertEqual(self.prepare(), prepared)

    def test_receipt_huge_integer_timestamp_is_controlled_contract_failure(self):
        prepared = self.prepare()
        receipt = prepared.root / "readiness.json"
        document = json.loads(receipt.read_text())
        document["prepared_at"] = 10 ** 400
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(seal(document, "receipt_sha256")))
        with self.assertRaises(ContractError):
            status_subject(self.config, "s001", self.work)

    def test_preparation_from_previous_release_remains_verifiable(self):
        prepared = self.prepare()
        receipt = prepared.root / "readiness.json"
        document = json.loads(receipt.read_text())
        document["wrapper_version"] = "0.1.0"
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(seal(document, "receipt_sha256")))
        self.assertEqual(status_subject(self.config, "s001", self.work).package, prepared.package)

    def test_manifest_uses_unique_verified_basenames_in_fixed_server_order(self):
        prepared = self.prepare()
        rows = list(csv.DictReader(io.StringIO((prepared.root / "manifest.csv").read_text())))
        self.assertEqual([row["trial_index"] for row in rows], ["0", "1"])
        self.assertEqual([row["condition"] for row in rows],
                         ["new-integration-parent", "old-integration-repeat"])
        self.assertEqual(len({row["filename"] for row in rows}), 2)
        for row in rows:
            self.assertEqual(Path(row["filename"]).name, row["filename"])
            self.assertEqual((prepared.root / row["filename"]).read_bytes(), MEDIA)
            self.assertEqual((prepared.root / row["filename"]).stat().st_mode & 0o777, 0o400)
        self.assertEqual(json.loads((prepared.root / "block.json").read_text()), block())
        self.assertEqual(status_subject(self.config, "subject-001", self.work).root, prepared.root)
        self.assertFalse(prepared.package.pgl_ready)
        self.assertEqual(self.api.downloads, [0])

    def test_repeated_prepare_is_immutable_and_uses_verified_cache(self):
        first = self.prepare()
        files = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in first.root.iterdir()}
        second = self.prepare()
        self.assertEqual(first, second)
        self.assertEqual(files, {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                                 for path in second.root.iterdir()})
        self.assertEqual(self.api.downloads, [0])
        self.api.document["package_id"] = "f" * 32
        self.api.document = seal(self.api.document)
        self.prepare()
        self.assertEqual(self.api.downloads, [0])

    def test_interruption_never_publishes_and_resumes_verified_partial(self):
        self.api.interrupt = True
        with self.assertRaises(ApiError):
            self.prepare()
        self.assertEqual(list(self.work.rglob("readiness.json")), [])
        self.assertEqual(list(self.work.rglob("current.json")), [])
        self.assertEqual(list(self.cache.glob("*.mp4")), [])
        self.assertEqual(len(list(self.cache.glob("*.partial"))), 1)
        self.api.interrupt = False
        self.prepare()
        self.assertEqual(self.api.downloads, [0, 4])
        self.assertEqual(list(self.cache.glob("*.partial")), [])

    def test_range_ignored_restarts_from_zero_without_duplicate_prefix(self):
        self.api.interrupt = True
        with self.assertRaises(ApiError):
            self.prepare()
        self.api.interrupt = False
        self.api.ignore_range = True
        prepared = self.prepare()
        self.assertEqual(self.api.downloads, [0, 4, 0])
        self.assertEqual(next(prepared.root.glob("*.mp4")).read_bytes(), MEDIA)

    def test_checksum_length_failures_never_publish_ready(self):
        for payload in [b"X" * len(MEDIA), MEDIA[:-1], MEDIA + b"extra"]:
            self.api.payload = payload
            with self.subTest(payload=payload), self.assertRaises((ApiError, ContractError)):
                self.prepare()
            self.assertEqual(list(self.work.rglob("readiness.json")), [])
            self.assertEqual(list(self.cache.glob("*.mp4")), [])

    def test_corrupt_cache_fails_without_deleting_or_replacing_existing_file(self):
        self.prepare()
        cached = next(self.cache.glob("*.mp4"))
        cached.chmod(0o600)
        cached.write_bytes(b"X" * len(MEDIA))
        with self.assertRaises(ContractError):
            self.prepare()
        self.assertEqual(cached.read_bytes(), b"X" * len(MEDIA))

    def test_status_rehashes_same_size_media_manifest_package_and_receipt(self):
        prepared = self.prepare()
        paths = [next(prepared.root.glob("*.mp4")), prepared.root / "manifest.csv",
                 prepared.root / "block.json", prepared.root / "readiness.json"]
        for path in paths:
            original = path.read_bytes()
            mode = path.stat().st_mode & 0o777
            path.chmod(0o600)
            path.write_bytes(b"X" * len(original))
            with self.subTest(path=path.name), self.assertRaises(ContractError):
                status_subject(self.config, "s001", self.work)
            path.write_bytes(original)
            path.chmod(mode)

    def test_status_rejects_self_resealed_wrong_manifest_mapping(self):
        prepared = self.prepare()
        manifest = prepared.root / "manifest.csv"
        manifest.chmod(0o600)
        manifest.write_bytes(b"filename,trial_index,condition\nother.mp4,0,new\n")
        receipt = prepared.root / "readiness.json"
        content = json.loads(receipt.read_text())
        content["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(seal(content, "receipt_sha256")))
        with self.assertRaises(ContractError):
            status_subject(self.config, "s001", self.work)

    def test_insufficient_disk_space_blocks_before_any_media_request(self):
        with patch("dbp_pgl_runner.prepare.shutil.disk_usage", return_value=(1, 1, 0)):
            with self.assertRaises(ContractError):
                self.prepare()
        self.assertEqual(self.api.downloads, [])

    def test_same_package_id_cannot_replace_immutable_block(self):
        prepared = self.prepare()
        before = (prepared.root / "block.json").read_bytes()
        self.api.document["block_id"] = "f" * 32
        self.api.document = seal(self.api.document)
        with self.assertRaises(ContractError):
            self.prepare()
        self.assertEqual((prepared.root / "block.json").read_bytes(), before)

    def test_prepare_rechecks_subject_and_experiment_even_for_custom_api(self):
        for field, value in [("subject_id", "subject-002"), ("experiment_id", "f" * 32)]:
            self.api.document = block()
            self.api.document[field] = value
            self.api.document = seal(self.api.document)
            with self.assertRaises(ContractError):
                self.prepare()
        self.assertEqual(self.api.downloads, [])

    def test_prepared_media_symlink_is_not_ready(self):
        prepared = self.prepare()
        media = next(prepared.root.glob("*.mp4"))
        media.unlink()
        media.symlink_to(next(self.cache.glob("*.mp4")))
        with self.assertRaises(ContractError):
            status_subject(self.config, "s001", self.work)

    def test_publication_failure_retains_verified_cache_but_not_partial_block(self):
        with patch("dbp_pgl_runner.prepare.os.rename", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(len(list(self.cache.glob("*.mp4"))), 1)
        self.assertEqual(list(self.work.rglob("readiness.json")), [])
        self.assertEqual(list(self.work.rglob("current.json")), [])
        self.prepare()
        self.assertEqual(self.api.downloads, [0])
