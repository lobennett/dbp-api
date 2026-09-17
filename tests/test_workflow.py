from pathlib import Path
from types import SimpleNamespace
import tempfile
import shutil
import unittest
from unittest.mock import patch

from dbp_pgl_runner.models import ContractError
from dbp_pgl_runner.prepare import prepare_subject
from dbp_pgl_runner.workflow import run_subject, sync_subject, recover_subject
from tests.test_prepare import MediaApi
from tests.test_api import config


class ExecutionApi(MediaApi):
    def __init__(self):
        super().__init__()
        self.claims = []
        self.events = []
        self.chunks = []
        self.fail_sync = False

    def claim_attempt(self, package, attempt_id):
        self.claims.append(attempt_id)
        return {"attempt_id": attempt_id, "package_id": package.package_id,
                "status": "claimed", "exclusive": True, "last_sequence": 0}

    def append_events(self, attempt_id, events):
        if self.fail_sync:
            raise OSError("offline")
        self.events.extend(events)
        return {}

    def upload_chunk(self, attempt_id, artifact_id, offset, content):
        self.chunks.append((artifact_id, offset, content))
        return {}

    def finalize_attempt(self, attempt_id, manifest):
        return {"attempt_id": attempt_id, "sync_status": "synced", "manifest_sha256": manifest["manifest_sha256"]}


class Adapter:
    runs = 0
    interrupt = False

    def preflight(self):
        pass

    def run(self, prepared_root, output_root, subject, attempt_id, callback, settings):
        self.runs += 1
        output_root.mkdir(mode=0o700)
        for directory, names in ((output_root, ("experimentSettings.json", "pgl.json", "settings.json", "state.json", "data.json")),
                                 (output_root / "task", ("settings.json", "state.json", "data.json"))):
            directory.mkdir(mode=0o700, exist_ok=True)
            for name in names:
                (directory / name).write_text('{"synthetic": true}')
                (directory / name).chmod(0o600)
        for index in range(2):
            for kind in ("trial_loaded", "stimulus_started", "stimulus_finished", "response_started",
                         "response_saved", "trial_completed"):
                callback(kind, index, {})
                if self.interrupt and kind == "stimulus_started":
                    return SimpleNamespace(native_saved=True, error="KeyboardInterrupt")
        return SimpleNamespace(native_saved=True, error=None)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = config("https://example.org")
        self.api = ExecutionApi()
        self.work = self.root / "work"
        prepare_subject(self.api, self.config, "s001", self.root / "cache", self.work)
        self.adapter = Adapter()
        self.decoder = patch("dbp_pgl_runner.workflow.verify_decode", return_value={"full_decode": True})
        self.decoder.start()
        self.addCleanup(self.decoder.stop)

    def run_subject(self, **kwargs):
        return run_subject(self.api, self.config, "s001", self.work,
                           adapter=self.adapter, integration_test=True, **kwargs)

    def test_run_requires_explicit_integration_acknowledgement(self):
        with self.assertRaises(ContractError):
            run_subject(self.api, self.config, "s001", self.work, adapter=self.adapter)
        self.assertEqual(self.api.claims, [])
        self.assertEqual(self.adapter.runs, 0)

    def test_complete_seal_sync_then_never_replay_on_retry(self):
        result = self.run_subject()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_trials"], 2)
        self.assertEqual(self.api.events, [])
        synced = sync_subject(self.api, self.config, "s001", self.work)
        self.assertEqual(synced["sync_status"], "synced")
        self.assertTrue(self.api.chunks)
        self.assertEqual(self.api.events[-1]["kind"], "run_completed")
        with self.assertRaises(ContractError):
            self.run_subject()
        self.assertEqual(self.adapter.runs, 1)

    def test_failure_to_sync_keeps_local_results_and_does_not_replay(self):
        result = self.run_subject()
        self.api.fail_sync = True
        with self.assertRaises(OSError):
            sync_subject(self.api, self.config, "s001", self.work)
        self.assertTrue(Path(result["attempt_root"]).is_dir())
        self.api.fail_sync = False
        self.assertEqual(sync_subject(self.api, self.config, "s001", self.work)["sync_status"], "synced")
        self.assertEqual(self.adapter.runs, 1)

    def test_interrupt_after_exposure_is_terminated_not_completed(self):
        self.adapter.interrupt = True
        result = self.run_subject()
        self.assertEqual(result["status"], "terminated")
        self.assertEqual(result["completed_trials"], 0)
        with self.assertRaises(ContractError):
            self.run_subject()

    def test_missing_journal_is_not_recreated_to_replay_a_run(self):
        result = self.run_subject()
        attempt = Path(result["attempt_root"])
        (attempt / "journal.json").unlink()
        (attempt / "events.jsonl").unlink()
        with self.assertRaises(ContractError):
            self.run_subject()
        self.assertEqual(self.adapter.runs, 1)
        self.assertFalse((attempt / "events.jsonl").exists())

    def test_revoked_reservation_cannot_launch(self):
        self.api.claim_attempt = lambda package, attempt: {"attempt_id": attempt,
            "package_id": package.package_id, "status": "terminated", "exclusive": False, "last_sequence": 0}
        with self.assertRaises(ContractError):
            self.run_subject()
        self.assertEqual(self.adapter.runs, 0)

    def test_sync_does_not_require_media_or_current_preparation_pointer(self):
        result = self.run_subject()
        parent = Path(result["attempt_root"]).parent.parent
        shutil.rmtree(parent / self.api.document["package_id"])
        (parent / "current.json").unlink()
        self.assertEqual(sync_subject(self.api, self.config, "s001", self.work)["sync_status"], "synced")
        from dbp_pgl_runner.runner import StudyRunner
        with patch("dbp_pgl_runner.runner.RunnerConfig.load", return_value=self.config):
            report = StudyRunner(work_root=self.work).status("s001")
        self.assertFalse(report["preparation_ready"])
        self.assertEqual(report["attempt"]["status"], "completed")

    def test_preparation_switch_after_decode_cannot_launch_unverified_media(self):
        from dbp_pgl_runner.prepare import status_subject
        prepared = status_subject(self.config, "s001", self.work)
        switched = SimpleNamespace(root=prepared.root.parent / ("f" * 32), package=prepared.package)
        with patch("dbp_pgl_runner.workflow.status_subject", side_effect=[prepared, switched]), self.assertRaises(ContractError):
            self.run_subject()
        self.assertEqual(self.adapter.runs, 0)

    def test_crash_recovery_does_not_need_missing_media(self):
        def crash(prepared_root, native, subject, attempt, callback, settings):
            callback("trial_loaded", 0, {})
            callback("stimulus_started", 0, {})
            raise SystemExit(9)
        self.adapter.run = crash
        with self.assertRaises(SystemExit):
            self.run_subject()
        for block_root in self.work.rglob(self.api.document["package_id"]):
            shutil.rmtree(block_root)
        result = recover_subject(self.config, "s001", self.work, terminate=True)
        self.assertEqual(result["status"], "terminated")
        self.assertTrue(result["needs_review"])
        self.assertEqual(sync_subject(self.api, self.config, "s001", self.work)["sync_status"], "synced")

    def test_adapter_boolean_cannot_substitute_for_native_outputs(self):
        original = self.adapter.run
        def run(*args):
            result = original(*args)
            (args[1] / "data.json").unlink()
            return result
        self.adapter.run = run
        self.assertEqual(self.run_subject()["status"], "terminated")

    def test_corrupt_sync_receipt_does_not_claim_success(self):
        from dbp_pgl_runner.workflow import attempt_status
        result = self.run_subject()
        sync_subject(self.api, self.config, "s001", self.work)
        (Path(result["attempt_root"]) / "sync-receipt.json").write_text("{}")
        with self.assertRaises(ContractError):
            attempt_status(self.config, "s001", self.work)
