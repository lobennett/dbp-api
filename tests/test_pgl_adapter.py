from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from dbp_pgl_runner.pgl_adapter import PglAdapter, RunSettings
from dbp_pgl_runner.models import ContractError


class FakeExperiment:
    fail = False
    fail_save = False
    incomplete = False
    instances = []

    def __init__(self, engine, **kwargs):
        self.pgl = engine
        self.settings = SimpleNamespace(dataPath=None, closeScreenOnEnd=False)
        self.experimentSettings = SimpleNamespace(experimentSaveName="pilot", **kwargs)
        self.state = SimpleNamespace(openScreen=False, phaseNums=[0, 1], phaseNum=1,
                                     experimentDone=True, runFinishedWithError=False)
        self.isInitialized = True
        self.eyeTracker = None
        self.tasks = []
        self.saves = 0
        self.closes = 0
        self.instances.append(self)

    def initScreen(self):
        self.state.openScreen = True

    def run(self):
        if self.fail:
            raise KeyboardInterrupt
        self.callback("trial_loaded", 0, {})
        if self.incomplete:
            self.state.phaseNum = 0
        self.save()
        self.endScreen()

    def save(self):
        self.saves += 1
        if self.fail_save:
            return
        root = (Path(self.settings.dataPath) / "pilot" / self.experimentSettings.subjectID
                / self.experimentSettings.sessionName / self.experimentSettings.runName)
        root.mkdir(parents=True)
        for name in ("experimentSettings.json", "pgl.json", "settings.json", "state.json", "data.json"):
            (root / name).write_text("{}")

    def endScreen(self):
        self.closes += 1
        self.state.openScreen = False


def module():
    def configure(experiment, currentRun, moviePath=None, event_callback=None):
        experiment.callback = event_callback
        experiment.movie_path = moviePath
        return experiment
    return SimpleNamespace(pgl=lambda: SimpleNamespace(close=lambda: None),
                           pglExperiment=FakeExperiment, pglDigitalBrainConfigure=configure)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        FakeExperiment.fail = False
        FakeExperiment.fail_save = False
        FakeExperiment.incomplete = False
        FakeExperiment.instances = []

    def run_adapter(self, root):
        adapter = PglAdapter(module=module())
        return adapter.run(Path(root) / "movies", Path(root) / "native", "subject-001",
                           "a" * 32, lambda *args: None, RunSettings())

    def test_native_run_saves_once_and_closes_once(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.run_adapter(root)
            self.assertTrue(result.native_saved)
            self.assertIsNone(result.error)
            experiment = FakeExperiment.instances[-1]
            self.assertEqual(experiment.saves, 1)
            self.assertEqual(experiment.closes, 1)
            self.assertEqual(experiment.movie_path, Path(root) / "movies")
            self.assertEqual(experiment.experimentSettings.subjectID, "s0001")
            self.assertTrue(result.native_path.is_relative_to((Path(root) / "native").resolve()))

    def test_interrupt_preserves_native_data_and_closes(self):
        FakeExperiment.fail = True
        with tempfile.TemporaryDirectory() as root:
            result = self.run_adapter(root)
            self.assertEqual(result.error, "KeyboardInterrupt")
            self.assertTrue(result.native_saved)
            self.assertEqual(FakeExperiment.instances[-1].saves, 1)
            self.assertEqual(FakeExperiment.instances[-1].closes, 1)

    def test_silent_native_save_failure_is_not_success(self):
        FakeExperiment.fail_save = True
        with tempfile.TemporaryDirectory() as root:
            result = self.run_adapter(root)
            self.assertFalse(result.native_saved)
            self.assertIsNotNone(result.error)
            self.assertEqual(FakeExperiment.instances[-1].saves, 1)

    def test_escape_before_final_phase_is_not_complete_even_if_saved(self):
        FakeExperiment.incomplete = True
        with tempfile.TemporaryDirectory() as root:
            result = self.run_adapter(root)
            self.assertTrue(result.native_saved)
            self.assertEqual(result.error, "IncompleteExperiment")

    def test_settings_reject_invalid_parameters(self):
        for kwargs in ({"description_seconds": -1}, {"display_width": 0}, {"day": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                RunSettings(**kwargs)

    def test_preflight_rejects_installation_without_native_renderer(self):
        with tempfile.TemporaryDirectory() as root:
            package_file = Path(root) / "site-packages" / "pgl" / "__init__.py"
            package_file.parent.mkdir(parents=True)
            package_file.write_text("")
            incomplete = module()
            incomplete.__file__ = str(package_file)
            with self.assertRaisesRegex(ContractError, "native renderer"):
                PglAdapter(module=incomplete).preflight()
