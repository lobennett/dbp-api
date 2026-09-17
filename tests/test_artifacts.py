import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dbp_pgl_runner.artifacts as artifacts_module
from dbp_pgl_runner.artifacts import iter_artifact_chunks, seal_artifacts
from dbp_pgl_runner.config import atomic_write
from dbp_pgl_runner.journal import Journal
from dbp_pgl_runner.models import ContractError, verify_document


ATTEMPT = "a" * 32


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.journal = Journal(Path(self.temporary.name) / "attempts", ATTEMPT, 1)
        self.journal.append("run_started")
        self.journal.append("run_terminated", payload={"reason": "test"})
        self.root = self.journal.root
        (self.root / "native").mkdir(mode=0o700)
        self.native = self.root / "native" / "events.json"
        atomic_write(self.native, b"native-output")

    def test_deterministic_inventory_contains_journal_and_native_outputs(self):
        manifest = seal_artifacts(self.root, ATTEMPT)
        self.assertEqual(seal_artifacts(self.root, ATTEMPT), manifest)
        self.assertEqual(manifest["schema_version"], "dbp-pgl-artifacts-v1")
        self.assertEqual(manifest["attempt_id"], ATTEMPT)
        verify_document(manifest, "manifest_sha256")
        files = manifest["files"]
        paths = [item["path"] for item in files]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("events.jsonl", paths)
        self.assertIn("journal.json", paths)
        self.assertNotIn(".journal.lock", paths)
        self.assertNotIn("artifact-manifest.json", paths)
        native = next(item for item in files if item["path"] == "native/events.json")
        self.assertEqual(native, {"path": "native/events.json", "bytes": 13,
                                  "sha256": hashlib.sha256(b"native-output").hexdigest()})
        self.assertEqual((self.root / "artifact-manifest.json").stat().st_mode & 0o777, 0o600)

    def test_stream_chunks_are_bounded_and_repeatable(self):
        manifest = seal_artifacts(self.root, ATTEMPT)
        item = next(item for item in manifest["files"] if item["path"] == "native/events.json")
        expected = [b"nati", b"ve-o", b"utpu", b"t"]
        self.assertEqual(list(iter_artifact_chunks(self.root, item, chunk_size=4)), expected)
        self.assertEqual(list(iter_artifact_chunks(self.root, item, chunk_size=4)), expected)
        for size in (0, -1, True, 1024 * 1024 + 1):
            with self.subTest(size=size), self.assertRaises(ContractError):
                list(iter_artifact_chunks(self.root, item, chunk_size=size))

    def test_empty_native_file_has_hash_and_no_chunks(self):
        atomic_write(self.native, b"")
        manifest = seal_artifacts(self.root, ATTEMPT)
        item = next(item for item in manifest["files"] if item["path"] == "native/events.json")
        self.assertEqual(item["bytes"], 0)
        self.assertEqual(item["sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(list(iter_artifact_chunks(self.root, item)), [])

    def test_changed_files_or_added_files_cannot_be_resealed(self):
        manifest = seal_artifacts(self.root, ATTEMPT)
        item = next(item for item in manifest["files"] if item["path"] == "native/events.json")
        atomic_write(self.native, b"edited-output")
        with self.assertRaises(ContractError):
            seal_artifacts(self.root, ATTEMPT)
        with self.assertRaises(ContractError):
            next(iter_artifact_chunks(self.root, item))
        atomic_write(self.native, b"native-output")
        atomic_write(self.root / "native" / "extra.json", b"new")
        with self.assertRaises(ContractError):
            seal_artifacts(self.root, ATTEMPT)

    def test_inventory_limits_enforced_without_publishing_manifest(self):
        for limits in ({"max_files": 2}, {"max_file_bytes": 5}, {"max_total_bytes": 5},
                       {"max_files": True}, {"max_total_bytes": 0}):
            with self.subTest(limits=limits), self.assertRaises(ContractError):
                seal_artifacts(self.root, ATTEMPT, **limits)
            self.assertFalse((self.root / "artifact-manifest.json").exists())

    def test_symlinks_hardlinks_special_files_and_media_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        atomic_write(outside, b"external")
        unsafe = self.root / "native" / "unsafe"
        makers = [lambda: unsafe.symlink_to(outside),
                  lambda: unsafe.symlink_to(outside.parent, target_is_directory=True),
                  lambda: os.link(outside, unsafe), lambda: os.mkfifo(unsafe, 0o600)]
        for make in makers:
            make()
            with self.assertRaises(ContractError):
                seal_artifacts(self.root, ATTEMPT)
            unsafe.unlink()
        atomic_write(self.root / "native" / "stimulus.mp4", b"video")
        with self.assertRaises(ContractError):
            seal_artifacts(self.root, ATTEMPT)

    def test_chunk_paths_cannot_escape_or_follow_directory_symlinks(self):
        for path in ("../secret", "/tmp/secret", "native/../events.jsonl", "native//events.json",
                     "native/./events.json", "native\\events.json", "C:secret", "."):
            item = {"path": path, "bytes": 13, "sha256": hashlib.sha256(b"native-output").hexdigest()}
            with self.subTest(path=path), self.assertRaises(ContractError):
                list(iter_artifact_chunks(self.root, item))
        (self.root / "alias").symlink_to(self.root / "native", target_is_directory=True)
        item = {"path": "alias/events.json", "bytes": 13,
                "sha256": hashlib.sha256(b"native-output").hexdigest()}
        with self.assertRaises(ContractError):
            list(iter_artifact_chunks(self.root, item))

    def test_cannot_seal_running_or_corrupt_journal_or_wrong_attempt(self):
        active = Journal(self.root.parent, "b" * 32, 1)
        active.append("run_started")
        with self.assertRaises(ContractError):
            seal_artifacts(active.root, "b" * 32)
        with self.assertRaises(ContractError):
            seal_artifacts(self.root, "b" * 32)
        with self.journal.path.open("ab") as target:
            target.write(b"partial")
        with self.assertRaises(ContractError):
            seal_artifacts(self.root, ATTEMPT)

    def test_changes_during_chunk_iteration_are_reported(self):
        manifest = seal_artifacts(self.root, ATTEMPT)
        item = next(item for item in manifest["files"] if item["path"] == "native/events.json")
        chunks = iter_artifact_chunks(self.root, item, chunk_size=4)
        self.assertEqual(next(chunks), b"nati")
        atomic_write(self.native, b"native-output")
        with self.assertRaises(ContractError):
            list(chunks)

    def test_failed_manifest_publication_is_retryable(self):
        with patch("dbp_pgl_runner.artifacts.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises((OSError, ContractError)):
                seal_artifacts(self.root, ATTEMPT)
        self.assertFalse((self.root / "artifact-manifest.json").exists())
        self.assertEqual(seal_artifacts(self.root, ATTEMPT), seal_artifacts(self.root, ATTEMPT))

    def test_verification_detects_tampered_manifest_and_outputs_without_resealing(self):
        manifest = seal_artifacts(self.root, ATTEMPT)
        self.assertEqual(artifacts_module.verify_artifacts(self.root, manifest), manifest)
        original = (self.root / "artifact-manifest.json").read_bytes()
        changed = dict(manifest, attempt_id="b" * 32)
        with self.assertRaises(ContractError):
            artifacts_module.verify_artifacts(self.root, changed)
        atomic_write(self.native, b"edited-output")
        with self.assertRaises(ContractError):
            artifacts_module.verify_artifacts(self.root, manifest)
        self.assertEqual((self.root / "artifact-manifest.json").read_bytes(), original)

    def test_late_sync_receipt_and_lockfiles_do_not_change_inventory(self):
        atomic_write(self.root / ".run.lock", b"")
        manifest = seal_artifacts(self.root, ATTEMPT)
        self.assertNotIn(".run.lock", [entry["path"] for entry in manifest["files"]])
        atomic_write(self.root / "sync-receipt.json", b'{"status":"synced"}')
        atomic_write(self.root / ".run.lock", b"updated")
        self.assertEqual(seal_artifacts(self.root, ATTEMPT), manifest)
        self.assertEqual(artifacts_module.verify_artifacts(self.root, manifest), manifest)

    def test_mutation_during_hashing_refuses_manifest(self):
        original_hash = artifacts_module._hash_source

        def mutate(source, size):
            digest = original_hash(source, size)
            if size == len(b"native-output"):
                atomic_write(self.native, b"native-output")
            return digest

        with patch("dbp_pgl_runner.artifacts._hash_source", side_effect=mutate):
            with self.assertRaises(ContractError):
                seal_artifacts(self.root, ATTEMPT)
        self.assertFalse((self.root / "artifact-manifest.json").exists())

    def test_existing_manifest_retry_repeats_directory_durability_barrier(self):
        manifest = seal_artifacts(self.root, ATTEMPT)
        with patch("dbp_pgl_runner.artifacts.fsync_directory", create=True,
                   side_effect=OSError("directory sync failure")):
            with self.assertRaises(OSError):
                seal_artifacts(self.root, ATTEMPT)
        self.assertEqual(seal_artifacts(self.root, ATTEMPT), manifest)
