from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest

from dbp_pgl_runner.models import BlockPackage, ContractError, canonical_bytes, canonical_subject
from tests.fixtures import block, seal


class ModelTests(unittest.TestCase):
    def test_sealed_package_and_trials_are_independent_and_immutable(self):
        source = block()
        parsed = BlockPackage.from_dict(source)
        source["trials"][0]["clip_id"] = "changed"
        self.assertEqual(parsed.trials[0].clip_id, "clip one")
        with self.assertRaises(FrozenInstanceError):
            parsed.trials[0].clip_id = "changed"
        exported = parsed.to_dict()
        exported["trials"].clear()
        self.assertEqual(len(parsed.trials), 2)
        self.assertEqual(parsed.to_dict(), block())

    def test_rejects_unknown_fields_digest_changes_and_production_claims(self):
        for field, value in [("surprise", True), ("pgl_ready", True),
                             ("mode", "production"), ("package_id", "../unsafe"),
                             ("subject_id", " subject-001"), ("block_id", "a" * 32)]:
            with self.subTest(field=field):
                bad = block()
                bad[field] = value
                with self.assertRaises(ContractError):
                    BlockPackage.from_dict(seal(bad))
        bad = block()
        bad["subject_id"] = "subject-002"
        with self.assertRaises(ContractError):
            BlockPackage.from_dict(bad)
        bad = block()
        del bad["package_sha256"]
        with self.assertRaises(ContractError):
            BlockPackage.from_dict(bad)

    def test_rejects_trial_order_paths_roles_sizes_and_conflicting_identity(self):
        cases = [("trial_index", True), ("trial_index", 4), ("media_bytes", True),
                 ("media_bytes", 0), ("media_bytes", 32 * 1024 * 1024 + 1),
                 ("media_sha256", "D" * 64), ("media_path", "../clip.mp4"),
                 ("media_path", "a/./clip.mp4"), ("media_path", "/clip.mp4"),
                 ("media_path", "C:\\clip.mp4"), ("media_path", "https://evil/clip"),
                 ("role", []), ("condition", "new"), ("clip_id", "\n")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                bad = block()
                bad["trials"][0][field] = value
                with self.assertRaises(ContractError):
                    BlockPackage.from_dict(seal(bad))
        bad = block()
        bad["trials"][1]["media_sha256"] = "0" * 64
        bad["trials"][1]["media_path"] = "other.mp4"
        with self.assertRaises(ContractError):
            BlockPackage.from_dict(seal(bad))

    def test_alias_mapping_is_explicit_and_bounded(self):
        for alias, expected in [("s001", "subject-001"), ("subject-100", "subject-100")]:
            self.assertEqual(canonical_subject(alias), expected)
        for alias in ["s000", "s101", "s1", "../s001", "s001/", "s001 ", "subject-101"]:
            with self.assertRaises(ContractError):
                canonical_subject(alias)

    def test_canonical_utf8_digest_matches_server_fixture_when_available(self):
        server = Path(__file__).resolve().parents[2] / "dbp-dataset-browser"
        if not (server / "tests/test_study_runner_contract.py").exists():
            self.skipTest("optional sibling server checkout unavailable")
        sys.path.insert(0, str(server))
        old_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec = importlib.util.spec_from_file_location(
                "server_contract_fixture", server / "tests/test_study_runner_contract.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original = module.valid_block()
            original["subject_id"] = "café"
            sealed = module.seal_document(original, "package_sha256")
            self.assertEqual(BlockPackage.from_dict(sealed).to_dict(), sealed)
            self.assertEqual(hashlib.sha256(canonical_bytes(original)).hexdigest(),
                             sealed["package_sha256"])
        finally:
            sys.path.remove(str(server))
            sys.dont_write_bytecode = old_bytecode
