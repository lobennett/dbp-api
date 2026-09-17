from pathlib import Path
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY, Mock, patch
from types import SimpleNamespace

from dbp_pgl_runner.api import ApiError
from dbp_pgl_runner.models import ContractError
from dbp_pgl_runner.launcher import LauncherModel, run_in_child
from dbp_pgl_runner.config import RunnerConfig, save_pairing
from tests.fixtures import TOKEN, device


def study():
    return {"experiment_id": "b" * 32, "study_id": "a" * 32, "study_name": "Practice study",
            "subjects": [{"subject_id": "subject-001", "trial_count": 2}]}


class LauncherTests(unittest.TestCase):
    def model(self):
        runner = Mock()
        runner.study.return_value = study()
        runner.pairing_identity.return_value = {"server_origin": "http://localhost:8000",
                                              "device_id": "d" * 32, "experiment_id": "b" * 32}
        runner.status.return_value = {"preparation_ready": True, "trial_count": 2}
        model = LauncherModel(runner)
        model.load()
        return model, runner

    def test_no_subject_is_preselected_and_only_assigned_subjects_allowed(self):
        model, runner = self.model()
        self.assertIsNone(model.subject)
        with self.assertRaises(ContractError):
            model.prepare()
        with self.assertRaises(ContractError):
            model.select("s002")
        model.select("subject-001")
        model.prepare()
        runner.prepare.assert_called_once_with("subject-001")

    def test_refresh_and_failed_refresh_clear_selection_and_preparation(self):
        model, runner = self.model()
        model.select("subject-001")
        model.prepare()
        runner.study.side_effect = ContractError("Revoked")
        with self.assertRaises(ContractError):
            model.load()
        self.assertIsNone(model.subject)
        self.assertIsNone(model.context)
        self.assertFalse(model.ready)

    def test_run_requires_preparation_acknowledgement_and_no_existing_attempt(self):
        model, runner = self.model()
        model.select("subject-001")
        with self.assertRaises(ContractError):
            model.run(integration_test=True)
        model.prepare()
        with self.assertRaises(ContractError):
            model.run(integration_test=False)
        runner.status.return_value = {"preparation_ready": True, "attempt": {"status": "completed"}}
        with patch("dbp_pgl_runner.launcher.run_in_child") as child:
            with self.assertRaises(ContractError):
                model.run(integration_test=True)
            child.assert_not_called()

    def test_run_is_separate_process_and_upload_retry_never_runs(self):
        model, runner = self.model()
        model.select("subject-001")
        model.prepare()
        with patch("dbp_pgl_runner.launcher.run_in_child", return_value={"status": "completed"}) as child:
            model.run(integration_test=True, ffmpeg="/local/ffmpeg")
            child.assert_called_once_with(runner, "subject-001", ffmpeg="/local/ffmpeg", expected_pairing=model.pairing)
        model.sync()
        runner.sync.assert_called_once_with("subject-001")
        runner.run.assert_not_called()
        self.assertFalse(model.ready)

    def test_failed_prepare_cannot_leave_old_readiness(self):
        model, runner = self.model()
        model.select("subject-001")
        model.prepare()
        runner.prepare.side_effect = OSError("disk")
        with self.assertRaises(OSError):
            model.prepare()
        self.assertFalse(model.ready)

    def test_repaired_profile_cannot_redirect_selected_subject_actions(self):
        for action in ("prepare", "status", "sync"):
            model, runner = self.model()
            model.select("subject-001")
            runner.pairing_identity.return_value = {"server_origin": "http://localhost:8000",
                                                  "device_id": "d" * 32, "experiment_id": "f" * 32}
            with self.subTest(action=action), self.assertRaisesRegex(ContractError, "connection changed"):
                getattr(model, action)()
            getattr(runner, action).assert_not_called()

    def test_identity_change_during_study_discovery_is_rejected(self):
        model, runner = self.model()
        runner.pairing_identity.return_value = {"experiment_id": "f" * 32}
        with self.assertRaises(ContractError):
            model.load()
        self.assertIsNone(model.context)

    def test_child_passes_only_selected_identity_paths_and_no_secrets(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runner = Mock(config_dir=Path(temporary.name) / "config", cache_root=Path("/cache"), work_root=Path("/work"))
        save_pairing(runner.config_dir, "http://localhost:8000", device(), allow_file_token=True)
        snapshot_paths = []
        def execute(command, **kwargs):
            self.assertEqual(command[1:3], ["-m", "dbp_pgl_runner"])
            self.assertIn("--integration-test", command)
            self.assertIn("subject-001", command)
            snapshot = Path(command[command.index("--config-dir") + 1])
            snapshot_paths.append(snapshot)
            self.assertNotEqual(snapshot, runner.config_dir)
            self.assertEqual(RunnerConfig.load(snapshot).experiment_id, "b" * 32)
            self.assertNotIn(TOKEN, str(command))
            self.assertEqual(RunnerConfig.load(snapshot).read_token(snapshot), TOKEN)
            changed = device()
            changed["experiment_id"] = "f" * 32
            save_pairing(runner.config_dir, "http://localhost:8000", changed, allow_file_token=True)
            self.assertEqual(RunnerConfig.load(snapshot).experiment_id, "b" * 32)
            child = Mock(stdout=io.BytesIO(b'x' * 100000 + b'\n{"status":"completed","sync_status":"synced"}\n'))
            child.wait.return_value = 0
            context = Mock()
            context.__enter__ = Mock(return_value=child)
            context.__exit__ = Mock(return_value=False)
            return context
        with patch("dbp_pgl_runner.launcher.subprocess.Popen", side_effect=execute):
            self.assertEqual(run_in_child(runner, "subject-001")["status"], "completed")
        self.assertFalse(snapshot_paths[0].exists())

    def test_handoff_rejects_an_unexpected_connection_before_starting(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = Mock(config_dir=Path(temporary) / "config", cache_root=Path("/cache"), work_root=Path("/work"))
            save_pairing(runner.config_dir, "http://localhost:8000", device(), allow_file_token=True)
            with patch("dbp_pgl_runner.launcher.subprocess.Popen") as child:
                with self.assertRaisesRegex(ContractError, "connection changed"):
                    run_in_child(runner, "subject-001", expected_pairing={"experiment_id": "f" * 32})
                child.assert_not_called()

    def test_real_child_pipe_drains_large_output_without_launching_pgl(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = Mock(config_dir=Path(temporary) / "config", cache_root=Path("/cache"), work_root=Path("/work"))
            save_pairing(runner.config_dir, "http://localhost:8000", device(), allow_file_token=True)
            real_popen = subprocess.Popen
            script = 'import sys; sys.stdout.write("x" * 2_000_000 + "\\n"); print(\'{"status":"completed","sync_status":"synced"}\')'
            def synthetic_child(command, **kwargs):
                return real_popen([sys.executable, "-c", script], **kwargs)
            with patch("dbp_pgl_runner.launcher.subprocess.Popen", side_effect=synthetic_child):
                result = run_in_child(runner, "subject-001")
            self.assertEqual(result["sync_status"], "synced")

    def test_child_failure_does_not_leak_raw_output_or_invent_success(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runner = Mock(config_dir=Path(temporary.name) / "config", cache_root=Path("/cache"), work_root=Path("/work"))
        save_pairing(runner.config_dir, "http://localhost:8000", device(), allow_file_token=True)
        def execute(command, **kwargs):
            child = Mock(stdout=io.BytesIO(b"sensitive native response\n"))
            child.wait.return_value = 1
            context = Mock()
            context.__enter__ = Mock(return_value=child)
            context.__exit__ = Mock(return_value=False)
            return context
        with patch("dbp_pgl_runner.launcher.subprocess.Popen", side_effect=execute):
            with self.assertRaisesRegex(ContractError, "Inspect local status") as error:
                run_in_child(runner, "subject-001")
            self.assertNotIn("sensitive", str(error.exception))

    def test_cli_launch_does_not_import_or_execute_pgl(self):
        from dbp_pgl_runner.cli import main
        with patch("dbp_pgl_runner.launcher.launch", return_value=0) as launch:
            self.assertEqual(main(["launch"]), 0)
            launch.assert_called_once()

    def test_missing_pgl_is_explained_without_importing_native_code(self):
        from dbp_pgl_runner.launcher import runtime_problem
        with (patch("dbp_pgl_runner.launcher.sys.platform", "darwin"),
              patch("dbp_pgl_runner.launcher.sys.version_info", (3, 12)),
              patch("dbp_pgl_runner.launcher.importlib.util.find_spec", return_value=None)):
            self.assertIn("PGL is not installed", runtime_problem())


@unittest.skipUnless(os.environ.get("DBP_TEST_GUI") == "1", "Opt-in local desktop Tk test")
class LauncherWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from dbp_pgl_runner.launcher import LauncherWindow
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        runtime = patch("dbp_pgl_runner.launcher.runtime_problem", return_value=None)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.window = LauncherWindow(self.root, config_dir=Path(self.temporary.name) / "config",
                                     cache_root=Path(self.temporary.name) / "cache",
                                     work_root=Path(self.temporary.name) / "work")
        self.addCleanup(self.root.destroy)
        self.addCleanup(lambda: self.root.after_cancel(self.window.poll_id))

    def wait(self):
        deadline = time.monotonic() + 5
        while self.window.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.window.busy)

    def browser_pairing(self, result=None, *, start_error=None, wait_error=None):
        pairing = Mock()
        pending = SimpleNamespace(origin="http://127.0.0.1:8769", user_code="AAAA-BBBB")
        pairing.start.side_effect = start_error
        pairing.start.return_value = pending
        pairing.wait.side_effect = wait_error
        pairing.wait.return_value = result
        self.window.browser_pairing = pairing
        return pairing, pending

    def test_url_to_browser_selection_populates_study_without_typed_name(self):
        runner = Mock()
        runner.study.return_value = study()
        runner.pairing_identity.return_value = {"server_origin": "http://127.0.0.1:8769",
                                                "device_id": "d" * 32, "experiment_id": "b" * 32}
        pairing, pending = self.browser_pairing({"device_response": device(), "study_context": study()})
        self.window.profiles.publish = Mock(return_value="practice-study-bbbbbbbb")
        self.window.profiles.names = Mock(return_value=["practice-study-bbbbbbbb"])
        self.window.origin.set("http://127.0.0.1:8769")
        with patch("dbp_pgl_runner.launcher.StudyRunner", return_value=runner):
            self.window.choose_study_button.invoke()
            self.wait()
        self.assertEqual(self.window.study["values"], ("Practice study · bbbbbbbb",))
        self.assertEqual(self.window.subject.get(), "")
        self.assertIn("subject-001", self.window.subjects["values"])
        pairing.wait.assert_called_once()
        self.assertIs(pairing.wait.call_args.args[0], pending)
        self.assertIsInstance(pairing.wait.call_args.args[1], threading.Event)
        self.assertIs(pairing.wait.call_args.kwargs["reuse"].__self__, self.window.profiles)
        self.window.profiles.publish.assert_called_once_with("http://127.0.0.1:8769",
                                                             device_response=device(), study_context=study())
        self.assertNotIn("pgl", sys.modules)

    def test_entered_https_origin_uses_primary_browser_selection(self):
        origin = "https://coordinator.example"
        runner = Mock()
        runner.study.return_value = study()
        runner.pairing_identity.return_value = {"server_origin": origin,
                                                "device_id": "d" * 32, "experiment_id": "b" * 32}
        pairing, pending = self.browser_pairing({"device_response": device(), "study_context": study()})
        pending.origin = origin
        self.window.profiles.publish = Mock(return_value="practice-study-bbbbbbbb")
        self.window.profiles.names = Mock(return_value=["practice-study-bbbbbbbb"])
        self.window.origins.delete(0, "end")
        self.window.origins.insert(0, origin)
        self.assertEqual(self.window.origin.get(), origin)
        with patch("dbp_pgl_runner.launcher.StudyRunner", return_value=runner):
            self.window.choose_study_button.invoke()
            self.wait()
        pairing.start.assert_called_once_with(origin, ANY, cancel_event=ANY)
        self.window.profiles.publish.assert_called_once_with(origin,
                                                             device_response=device(), study_context=study())

    def test_start_names_exact_study_subject_and_trial_count(self):
        runner = Mock()
        runner.study.return_value = study()
        runner.pairing_identity.return_value = {"server_origin": "http://localhost:8000",
                                                "device_id": "d" * 32, "experiment_id": "b" * 32}
        runner.status.return_value = {"preparation_ready": True, "trial_count": 2}
        self.window.profiles.names = Mock(return_value=["practice-study-bbbbbbbb"])
        with patch("dbp_pgl_runner.launcher.StudyRunner", return_value=runner):
            self.window.refresh_profiles()
            self.window.select_study("practice-study-bbbbbbbb")
            self.wait()
        self.window.select_subject("subject-001")
        self.window.prepare_button.invoke()
        self.wait()
        self.window.ack.set(True)
        self.window.update_controls()
        with patch("tkinter.messagebox.askokcancel", return_value=False) as confirm:
            self.window.start_button.invoke()
        prompt = confirm.call_args.args[1]
        self.assertIn("Practice study", prompt)
        self.assertIn("subject-001", prompt)
        self.assertIn("2 assigned trials", prompt)
        self.assertIn("day 1/block 1", prompt)
        self.assertIn("not an approved participant schedule", prompt)

    def test_browser_pairing_failures_do_not_publish_or_replay(self):
        failures = (
            ("browser", ApiError("Could not open authorization in the browser"), None),
            ("denied", None, ApiError("Authorization denied")),
            ("expired", None, ApiError("Authorization expired")),
        )
        for name, start_error, wait_error in failures:
            with self.subTest(name=name):
                pairing, _ = self.browser_pairing(start_error=start_error, wait_error=wait_error)
                self.window.profiles.publish = Mock()
                self.window.choose_study_button.invoke()
                self.wait()
                self.assertIn(str(start_error or wait_error), self.window.status_text.get())
                self.window.profiles.publish.assert_not_called()
                self.assertEqual(pairing.start.call_count, 1)
                self.assertLessEqual(pairing.wait.call_count, 1)

    def test_cancelling_browser_pairing_does_not_publish_or_replay(self):
        pairing = Mock()
        pending = SimpleNamespace(origin="http://127.0.0.1:8769", user_code="AAAA-BBBB")
        pairing.start.return_value = pending
        entered = threading.Event()

        def wait_for_cancel(_, cancel_event, *, reuse):
            entered.set()
            cancel_event.wait(5)
            raise ApiError("Authorization cancelled")

        pairing.wait.side_effect = wait_for_cancel
        self.window.browser_pairing = pairing
        self.window.profiles.publish = Mock()
        self.window.choose_study_button.invoke()
        deadline = time.monotonic() + 5
        while not entered.is_set() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(entered.is_set())
        self.window.cancel_pairing_button.invoke()
        self.wait()
        self.assertIn("cancelled", self.window.status_text.get().lower())
        self.window.profiles.publish.assert_not_called()
        pairing.start.assert_called_once()

    def test_reused_saved_study_is_not_published_and_revocation_clears_it(self):
        pairing, _ = self.browser_pairing({"profile_name": "practice-study-bbbbbbbb"})
        self.window.profiles.publish = Mock()
        self.window.profiles.names = Mock(return_value=["practice-study-bbbbbbbb"])
        runner = Mock()
        runner.pairing_identity.return_value = {"server_origin": "http://127.0.0.1:8769",
                                                "device_id": "d" * 32, "experiment_id": "b" * 32}
        runner.study.side_effect = ContractError("Saved study has been revoked")
        with patch("dbp_pgl_runner.launcher.StudyRunner", return_value=runner):
            self.window.choose_study_button.invoke()
            self.wait()
        self.window.profiles.publish.assert_not_called()
        self.assertEqual(self.window.study.get(), "")
        self.assertIn("revoked", self.window.status_text.get())
        self.assertEqual(pairing.start.call_count, 1)

    def test_runtime_problem_keeps_start_disabled_after_preparation(self):
        self.window.runtime_problem = "PGL is not installed"
        runner = Mock()
        runner.study.return_value = study()
        runner.pairing_identity.return_value = {"server_origin": "http://localhost:8000",
                                                "device_id": "d" * 32, "experiment_id": "b" * 32}
        runner.status.return_value = {"preparation_ready": True, "trial_count": 2}
        self.window.profiles.names = Mock(return_value=["practice-study-bbbbbbbb"])
        with patch("dbp_pgl_runner.launcher.StudyRunner", return_value=runner):
            self.window.refresh_profiles()
            self.window.select_study("practice-study-bbbbbbbb")
            self.wait()
        self.window.select_subject("subject-001")
        self.window.prepare_button.invoke()
        self.wait()
        self.window.ack.set(True)
        self.window.update_controls()
        self.assertIn("disabled", self.window.start_button.state())

    def test_manual_pairing_stays_hidden_until_advanced_recovery_is_opened(self):
        self.assertEqual(self.window.manual_pairing_button.winfo_manager(), "")
        self.window.advanced_recovery.set(True)
        self.window.update_advanced_recovery()
        self.assertEqual(self.window.manual_pairing_button.winfo_manager(), "grid")

    def test_submit_joins_worker_before_tk_teardown(self):
        completed = threading.Event()
        self.window.submit("Checking", lambda: {}, lambda result: completed.set())
        self.wait()
        self.assertTrue(completed.is_set())
        self.assertEqual(self.window.workers, set())

    def test_real_widgets_select_prepare_confirm_and_retry_upload(self):
        runner = Mock()
        runner.study.return_value = study()
        runner.pairing_identity.return_value = {"server_origin": "http://localhost:8000",
                                              "device_id": "d" * 32, "experiment_id": "b" * 32}
        runner.status.return_value = {"preparation_ready": True, "trial_count": 2}
        runner.sync.return_value = {"status": "completed", "sync_status": "synced", "completed_trials": 2}
        self.assertIn("disabled", self.window.start_button.state())
        self.window.connection.set("practice")
        with patch("dbp_pgl_runner.launcher.StudyRunner", return_value=runner):
            self.window.load_connection()
            self.wait()
        self.assertEqual(self.window.subject.get(), "")
        self.assertIn("Practice study", self.window.context_text.get())
        self.assertEqual(tuple(self.window.subjects["values"]), ("subject-001",))
        self.window.subject.set("subject-001")
        self.window.choose_subject()
        self.window.prepare_button.invoke()
        self.wait()
        self.assertIn("disabled", self.window.start_button.state())
        self.window.ack.set(True)
        self.window.update_controls()
        self.assertNotIn("disabled", self.window.start_button.state())
        with (patch("tkinter.messagebox.askokcancel", return_value=True),
              patch("dbp_pgl_runner.launcher.run_in_child", return_value={"status": "completed",
                    "sync_status": "pending", "completed_trials": 2}) as child):
            self.window.start_button.invoke()
            self.wait()
            child.assert_called_once()
        self.assertIn("Upload pending", self.window.status_text.get())
        self.assertIn("disabled", self.window.start_button.state())
        self.window.sync_button.invoke()
        self.wait()
        runner.sync.assert_called_once_with("subject-001")
        self.assertIn("Uploaded", self.window.status_text.get())

    def test_unexpected_callback_error_does_not_stop_polling(self):
        self.window.busy = True
        callback = Mock(side_effect=OSError("sensitive path"))
        self.window.events.put((callback, None, None))
        self.wait()
        self.assertIn("failed", self.window.status_text.get().lower())
        self.assertNotIn("sensitive", self.window.status_text.get())
        next_callback = Mock()
        self.window.busy = True
        self.window.events.put((next_callback, {}, None))
        self.wait()
        next_callback.assert_called_once_with({})


if __name__ == "__main__":
    unittest.main()
